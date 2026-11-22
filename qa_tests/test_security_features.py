import os
import unittest

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://unused:unused@127.0.0.1:5432/unused")
os.environ.setdefault("SECRET_KEY", "qa-only-secret-key-0123456789-abcdefghijklmnopqrstuvwxyz")

from app.core.security import (
    DUMMY_PASSWORD_HASH, create_access_token, create_refresh_token,
    decode_access_token, decode_refresh_token, verify_password,
)
from app.core.rate_limit import AccountLoginRateLimiter, ReportTargetRateLimiter


class TokenTests(unittest.TestCase):
    def test_access_token_is_not_refresh_token(self):
        access = create_access_token(42, "user")
        self.assertEqual(decode_access_token(access)["sub"], "42")
        self.assertIsNone(decode_refresh_token(access))

    def test_refresh_token_is_not_access_token(self):
        refresh = create_refresh_token(42, "user")
        self.assertEqual(decode_refresh_token(refresh)["sub"], "42")
        self.assertIsNone(decode_access_token(refresh))

    def test_untyped_jwt_is_not_accepted_as_access_token(self):
        import jwt
        from app.core.security import ALGORITHM
        from app.core.config import settings
        token = jwt.encode({"sub": "42", "role": "user"}, settings.secret_key, algorithm=ALGORITHM)
        self.assertIsNone(decode_access_token(token))

    def test_admin_and_user_roles_are_preserved(self):
        refresh = create_refresh_token(7, "admin")
        self.assertEqual(decode_refresh_token(refresh)["role"], "admin")


class LoginHardeningTests(unittest.TestCase):
    def test_dummy_hash_is_valid_argon2_hash_but_never_matches_login_password(self):
        self.assertFalse(verify_password("any-user-password", DUMMY_PASSWORD_HASH))

    def test_dummy_hash_is_argon2id_and_runs_real_verification(self):
        self.assertTrue(DUMMY_PASSWORD_HASH.startswith("$argon2id$"))
        # A mismatch is expected; the important property is that verify_password
        # accepts the stored hash format and completes the Argon2 verification path.
        self.assertFalse(verify_password("not-the-dummy-password", DUMMY_PASSWORD_HASH))

    def test_account_limiter_counts_failures_and_expires_them(self):
        limiter = AccountLoginRateLimiter(max_attempts=2, window_seconds=60)
        self.assertEqual(limiter.is_limited("user", "Person@Example.com"), (False, 0))
        limiter.record_failure("user", "person@example.com")
        limiter.record_failure("user", "PERSON@example.com")
        limited, retry = limiter.is_limited("user", "person@example.com")
        self.assertTrue(limited)
        self.assertGreater(retry, 0)
        limiter.clear("user", "person@example.com")
        self.assertEqual(limiter.is_limited("user", "person@example.com"), (False, 0))

    def test_account_limiter_separates_user_and_admin_roles(self):
        limiter = AccountLoginRateLimiter(max_attempts=1, window_seconds=60)
        limiter.record_failure("user", "admin@example.com")
        self.assertTrue(limiter.is_limited("user", "admin@example.com")[0])
        self.assertFalse(limiter.is_limited("admin", "admin@example.com")[0])


class ReportTargetRateLimitTests(unittest.TestCase):
    def test_duplicate_target_is_limited_but_other_content_is_allowed(self):
        limiter = ReportTargetRateLimiter(window_seconds=60)
        self.assertEqual(limiter.claim(5, "Message", 11), 0)
        self.assertGreater(limiter.claim(5, "Message", 11), 0)
        self.assertEqual(limiter.claim(5, "Message", 12), 0)
        self.assertEqual(limiter.claim(6, "Message", 11), 0)
        limiter.release(5, "Message", 11)
        self.assertEqual(limiter.claim(5, "Message", 11), 0)


class ReportAuthorizationTests(unittest.TestCase):
    @staticmethod
    def fake_db(rows):
        class FakeDatabase:
            def get(self, model, object_id):
                return rows.get((model, object_id))
        return FakeDatabase()

    def test_user_target_rejects_self_missing_and_deleted(self):
        from types import SimpleNamespace
        from fastapi import HTTPException
        from app.models import User
        from app.routers.notifications import _authorize_report_target
        from app.schemas import ReportIn

        reporter = SimpleNamespace(user_id=5)
        with self.assertRaises(HTTPException) as self_report:
            _authorize_report_target(ReportIn(reported_user_id=5, reason="Spam"), reporter, self.fake_db({}))
        self.assertEqual(self_report.exception.status_code, 400)
        with self.assertRaises(HTTPException) as missing:
            _authorize_report_target(ReportIn(reported_user_id=99, reason="Spam"), reporter, self.fake_db({}))
        self.assertEqual(missing.exception.status_code, 404)
        deleted = SimpleNamespace(user_id=99, account_status="Deleted")
        with self.assertRaises(HTTPException) as unavailable:
            _authorize_report_target(ReportIn(reported_user_id=99, reason="Spam"), reporter, self.fake_db({(User, 99): deleted}))
        self.assertEqual(unavailable.exception.status_code, 404)

    def test_message_requires_existing_conversation_access(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from fastapi import HTTPException
        from app.models import Message
        from app.routers import notifications
        from app.schemas import ReportIn

        reporter = SimpleNamespace(user_id=5)
        message = SimpleNamespace(message_id=11, conversation_id=20)
        db = self.fake_db({(Message, 11): message})
        payload = ReportIn(content_type="Message", content_id=11, reason="Concern")
        with patch.object(notifications, "_conversation_context", side_effect=HTTPException(status_code=404)):
            with self.assertRaises(HTTPException) as denied:
                notifications._authorize_report_target(payload, reporter, db)
        self.assertEqual(denied.exception.status_code, 404)
        with patch.object(notifications, "_conversation_context", return_value=(None, None, None)) as access:
            self.assertEqual(notifications._authorize_report_target(payload, reporter, db), ("Message", 11))
        access.assert_called_once_with(db, 20, reporter)

    def test_review_requires_active_public_teacher(self):
        from types import SimpleNamespace
        from fastapi import HTTPException
        from app.models import Review, User
        from app.routers.notifications import _authorize_report_target
        from app.schemas import ReportIn

        reporter = SimpleNamespace(user_id=5)
        review = SimpleNamespace(review_id=11, teacher_id=7)
        payload = ReportIn(content_type="Review", content_id=11, reason="Concern")
        db = self.fake_db({(Review, 11): review, (User, 7): SimpleNamespace(user_id=7, account_status="Active")})
        self.assertEqual(_authorize_report_target(payload, reporter, db), ("Review", 11))
        db = self.fake_db({(Review, 11): review, (User, 7): SimpleNamespace(user_id=7, account_status="Deleted")})
        with self.assertRaises(HTTPException) as denied:
            _authorize_report_target(payload, reporter, db)
        self.assertEqual(denied.exception.status_code, 404)

    def test_skills_follow_public_or_owner_visibility(self):
        from types import SimpleNamespace
        from fastapi import HTTPException
        from app.models import LearningSkill, TeachingSkill, User
        from app.routers.notifications import _authorize_report_target
        from app.schemas import ReportIn

        reporter = SimpleNamespace(user_id=5)
        public_skill = SimpleNamespace(teaching_skill_id=11, user_id=7, is_active=True)
        active_owner = SimpleNamespace(user_id=7, account_status="Active")
        db = self.fake_db({(TeachingSkill, 11): public_skill, (User, 7): active_owner})
        self.assertEqual(_authorize_report_target(ReportIn(content_type="TeachingSkill", content_id=11, reason="Concern"), reporter, db), ("TeachingSkill", 11))

        private_skill = SimpleNamespace(learning_skill_id=12, user_id=7, is_active=True)
        db = self.fake_db({(LearningSkill, 12): private_skill})
        payload = ReportIn(content_type="LearningSkill", content_id=12, reason="Concern")
        with self.assertRaises(HTTPException) as denied:
            _authorize_report_target(payload, reporter, db)
        self.assertEqual(denied.exception.status_code, 404)
        owner = SimpleNamespace(user_id=7)
        self.assertEqual(_authorize_report_target(payload, owner, db), ("LearningSkill", 12))


class UnknownEmailTimingTests(unittest.TestCase):
    def test_unknown_email_runs_dummy_hash_verification(self):
        import sys
        import types
        from unittest.mock import patch
        from fastapi import HTTPException
        # Stub only the engine/session module so this route-level unit test does
        # not need a live PostgreSQL driver or server.
        db_session_stub = types.ModuleType("app.db.session")
        from sqlalchemy.orm import declarative_base
        db_session_stub.Base = declarative_base()
        db_session_stub.get_db = lambda: None
        with patch.dict(sys.modules, {"app.db.session": db_session_stub}):
            import importlib
            auth_module = importlib.import_module("app.routers.auth")
            login = auth_module.login
        from app.schemas import LoginIn

        class EmptyDatabase:
            def scalar(self, _query):
                return None

        seen = {}
        def fake_verify(password, password_hash):
            seen["password"] = password
            seen["hash"] = password_hash
            return False

        with patch.object(auth_module, "verify_password", side_effect=fake_verify):
            with self.assertRaises(HTTPException) as raised:
                login(LoginIn(email="unknown@example.com", password="wrong-password"), EmptyDatabase())
        self.assertEqual(raised.exception.status_code, 401)
        self.assertEqual(seen["password"], "wrong-password")
        self.assertEqual(seen["hash"], DUMMY_PASSWORD_HASH)


if __name__ == "__main__":
    unittest.main()
