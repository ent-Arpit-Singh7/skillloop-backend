"""Test setup.

The tests run against a REAL PostgreSQL database built from the team's SQL file (docs/skillloop_database.sql),
so the database triggers and constraints are exercised too.

Set TEST_DATABASE_URL to an EMPTY throwaway database whose URL contains the word "test". Example:
    createdb skillloop_test
    TEST_DATABASE_URL=postgresql+psycopg://user:password@localhost:5432/skillloop_test pytest

WARNING: the tests DROP everything in that database's public schema. Never point this at the team database.
"""
import os
from pathlib import Path

import pytest

from sqlalchemy.engine import make_url  # noqa: E402

TEST_URL = os.environ.get("TEST_DATABASE_URL", "")
try:
    _db_name = (make_url(TEST_URL).database or "").lower()
except Exception:
    _db_name = ""
if not TEST_URL.startswith("postgresql") or "test" not in _db_name:
    pytest.exit(
        "Set TEST_DATABASE_URL to a throwaway PostgreSQL database whose NAME contains 'test' "
        "(for example postgresql+psycopg://user:pw@localhost:5432/skillloop_test). "
        "The tests wipe that database.",
        returncode=2,
    )

os.environ["DATABASE_URL"] = TEST_URL
os.environ["SECRET_KEY"] = "test-secret-key-for-pytest-only-0123456789"

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.core.security import hash_password  # noqa: E402
from app.db.session import SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Admin  # noqa: E402
from app.core.rate_limit import report_target_limiter  # noqa: E402

SCHEMA_FILE = Path(__file__).resolve().parent.parent / "docs" / "skillloop_database.sql"

TABLES = (
    '"User", Admin, SkillCategory, TeachingSkill, LearningSkill, Language, UserLanguage, Availability, '
    "Connection, ConnectionRequest, NetworkConnectionRequest, Conversation, Message, Session, Review, Notification, Report, UserBlock"
)


@pytest.fixture(scope="session", autouse=True)
def database_schema():
    """Build the team's schema once per test run."""
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP SCHEMA public CASCADE")
        conn.exec_driver_sql("CREATE SCHEMA public")
    raw = engine.raw_connection()
    try:
        cur = raw.cursor()
        # Start from the exact database SQL supplied by the team, then apply
        # the same additive migrations documented for existing databases.
        cur.execute(SCHEMA_FILE.read_text(encoding="utf-8"))
        migrations = Path(__file__).resolve().parent.parent / "docs" / "migrations"
        for migration_name in (
            "001_user_block.sql",
            "002_soft_delete_and_remove_chat.sql",
            "003_independent_connections.sql",
        ):
            cur.execute((migrations / migration_name).read_text(encoding="utf-8"))
        raw.commit()
    finally:
        raw.close()
    yield


@pytest.fixture()
def client(database_schema):
    """Empty tables (identity counters reset) + a few languages, for every test."""
    report_target_limiter.clear()
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
        conn.execute(text("INSERT INTO Language (language_name) VALUES ('English'), ('Hindi'), ('Spanish')"))
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def admin_headers(client):
    with SessionLocal() as db:
        db.add(Admin(full_name="Root Admin", email="admin@example.com", password_hash=hash_password("adminpassword1")))
        db.commit()
    r = client.post("/api/auth/admin/login", json={"email": "admin@example.com", "password": "adminpassword1"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture()
def category_id(client, admin_headers):
    r = client.post("/api/admin/categories", headers=admin_headers, json={"category_name": "Technology"})
    assert r.status_code == 201, r.text
    return r.json()["category_id"]


@pytest.fixture()
def make_user(client):
    """Registers + logs in a user and returns (auth_headers, user_json)."""

    def _make(email: str, name: str = "Test User"):
        r = client.post("/api/auth/register", json={"email": email, "password": "password123", "full_name": name})
        assert r.status_code == 201, r.text
        t = client.post("/api/auth/login", json={"email": email, "password": "password123"})
        assert t.status_code == 200, t.text
        return {"Authorization": f"Bearer {t.json()['access_token']}"}, r.json()

    return _make
