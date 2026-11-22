"""Database-free tests for the avatar route's validation and storage ordering."""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core import profile_storage
from app.core.config import settings
from app.routers.profile import upload_avatar


PNG = b"\x89PNG\r\n\x1a\nvalid-test-payload"


class Request:
    def __init__(self, body=PNG, content_type="image/png", content_length=None):
        self.headers = {"content-type": content_type}
        if content_length is not None:
            self.headers["content-length"] = str(content_length)
        self.body = body

    async def stream(self):
        yield self.body


class DB:
    def __init__(self, fail=False, events=None):
        self.fail = fail
        self.events = events if events is not None else []

    def commit(self):
        self.events.append("commit")
        if self.fail:
            raise RuntimeError("database failure")

    def rollback(self):
        self.events.append("rollback")

    def refresh(self, user):
        self.events.append("refresh")


def invoke(request, db, user=None):
    return asyncio.run(upload_avatar(request, user or SimpleNamespace(user_id=7, profile_photo=None), db))


@pytest.fixture(autouse=True)
def storage_config(monkeypatch):
    monkeypatch.setattr(settings, "supabase_url", "https://project.supabase.co")
    monkeypatch.setattr(settings, "supabase_secret_key", "test-only-secret")
    monkeypatch.setattr(settings, "supabase_storage_bucket", "profile-images")
    monkeypatch.setattr(settings, "max_profile_image_bytes", 5 * 1024 * 1024)


def test_successful_upload_returns_public_url(monkeypatch):
    calls = []

    async def upload(name, content, mime):
        calls.append((name, content, mime))
        return f"https://project.supabase.co/storage/v1/object/public/profile-images/{name}"

    monkeypatch.setattr(profile_storage, "upload_profile_image", upload)
    user = SimpleNamespace(user_id=7, profile_photo=None)
    result = invoke(Request(), DB(), user)
    assert result is user
    assert user.profile_photo.startswith("https://project.supabase.co/")
    assert calls[0][1:] == (PNG, "image/png")


@pytest.mark.parametrize("mime,body,status", [
    ("image/gif", PNG, 415),
    ("image/png", b"not an image", 415),
    ("image/png", PNG, 413),
])
def test_rejects_invalid_type_signature_and_size(mime, body, status, monkeypatch):
    monkeypatch.setattr(settings, "max_profile_image_bytes", len(PNG) - 1 if status == 413 else 5 * 1024 * 1024)
    with pytest.raises(HTTPException) as error:
        invoke(Request(body, mime), DB())
    assert error.value.status_code == status


def test_storage_failure_returns_gateway_error(monkeypatch):
    async def upload(*args):
        raise profile_storage.ProfileStorageError("private provider detail")

    monkeypatch.setattr(profile_storage, "upload_profile_image", upload)
    with pytest.raises(HTTPException) as error:
        invoke(Request(), DB())
    assert error.value.status_code == 502
    assert "private provider detail" not in error.value.detail


def test_commit_failure_cleans_up_new_object(monkeypatch):
    removed = []

    async def upload(name, *_):
        return "https://project.supabase.co/storage/v1/object/public/profile-images/" + name

    async def delete(name):
        removed.append(name)

    monkeypatch.setattr(profile_storage, "upload_profile_image", upload)
    monkeypatch.setattr(profile_storage, "delete_profile_image", delete)
    with pytest.raises(HTTPException) as error:
        invoke(Request(), DB(fail=True))
    assert error.value.status_code == 500
    assert len(removed) == 1


def test_replace_deletes_old_object_only_after_commit(monkeypatch):
    events = []

    async def upload(name, *_):
        events.append("upload")
        return "https://project.supabase.co/storage/v1/object/public/profile-images/" + name

    async def delete(name):
        events.append("delete")

    monkeypatch.setattr(profile_storage, "upload_profile_image", upload)
    monkeypatch.setattr(profile_storage, "delete_profile_image", delete)
    old = "https://project.supabase.co/storage/v1/object/public/profile-images/7_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.webp"
    invoke(Request(), DB(events=events), SimpleNamespace(user_id=7, profile_photo=old))
    assert events.index("upload") < events.index("commit") < events.index("delete")


def test_external_old_url_is_never_deleted(monkeypatch):
    removed = []

    async def upload(name, *_):
        return "https://project.supabase.co/storage/v1/object/public/profile-images/" + name

    async def delete(name):
        removed.append(name)

    monkeypatch.setattr(profile_storage, "upload_profile_image", upload)
    monkeypatch.setattr(profile_storage, "delete_profile_image", delete)
    user = SimpleNamespace(user_id=7, profile_photo="https://images.example.com/avatar.png")
    invoke(Request(), DB(), user)
    assert removed == []


@pytest.mark.parametrize("url,bucket", [
    ("http://project.supabase.co", "profile-images"),
    ("https://example.com", "profile-images"),
    ("https://project.supabase.co/path", "profile-images"),
    ("https://project.supabase.co", "profile-images/other"),
])
def test_rejects_unsafe_storage_configuration(monkeypatch, url, bucket):
    monkeypatch.setattr(settings, "supabase_url", url)
    monkeypatch.setattr(settings, "supabase_storage_bucket", bucket)
    with pytest.raises(profile_storage.ProfileStorageError):
        profile_storage._credentials()


@pytest.mark.parametrize("name", [
    "8_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.png",
    "other-object.png",
    "7_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa%2Fother.png",
    "7_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.png?download=1",
])
def test_does_not_claim_unowned_or_non_generated_bucket_objects(name):
    url = "https://project.supabase.co/storage/v1/object/public/profile-images/" + name
    assert profile_storage.profile_object_from_public_url(url, user_id=7) is None
