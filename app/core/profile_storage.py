"""Small Supabase Storage REST client for profile images."""
import re
from urllib.parse import urlsplit

import httpx

from app.core.config import settings


class ProfileStorageError(Exception):
    """Storage operation failed; callers must not expose provider details."""


def _credentials() -> tuple[str, str, str]:
    if not settings.supabase_url or not settings.supabase_secret_key:
        raise ProfileStorageError("Profile image storage is not configured")
    try:
        parsed = urlsplit(settings.supabase_url)
        host = parsed.hostname or ""
        if (
            parsed.scheme != "https"
            or not host.endswith(".supabase.co")
            or not host[:-len(".supabase.co")]
            or parsed.username
            or parsed.password
            or parsed.port
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError
        bucket = settings.supabase_storage_bucket
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", bucket):
            raise ValueError
    except (AttributeError, TypeError, ValueError):
        raise ProfileStorageError("Profile image storage configuration is invalid") from None
    return settings.supabase_url.rstrip("/"), settings.supabase_secret_key, bucket


def _headers(key: str, content_type: str | None = None) -> dict[str, str]:
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    if content_type:
        headers["Content-Type"] = content_type
    return headers


async def upload_profile_image(object_name: str, content: bytes, content_type: str) -> str:
    base, key, bucket = _credentials()
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{base}/storage/v1/object/{bucket}/{object_name}",
                content=content,
                headers=_headers(key, content_type),
            )
            response.raise_for_status()
    except (httpx.HTTPError, ValueError) as exc:
        raise ProfileStorageError("Profile image upload failed") from exc
    return f"{base}/storage/v1/object/public/{bucket}/{object_name}"


def profile_object_from_public_url(photo_url: str | None, user_id: int) -> str | None:
    """Return only this user's generated avatar key from this configured public bucket."""
    if not photo_url or not settings.supabase_url:
        return None
    try:
        expected = urlsplit(settings.supabase_url)
        actual = urlsplit(photo_url)
        prefix = f"/storage/v1/object/public/{settings.supabase_storage_bucket}/"
        if (
            (actual.scheme, actual.netloc) != (expected.scheme, expected.netloc)
            or actual.query
            or actual.fragment
            or not actual.path.startswith(prefix)
        ):
            return None
        name = actual.path[len(prefix):]
        if not re.fullmatch(rf"{user_id}_[0-9a-f]{{32}}\.(?:jpg|png|webp)", name):
            return None
        return name
    except (TypeError, ValueError):
        return None


async def delete_profile_image(object_name: str) -> None:
    base, key, bucket = _credentials()
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.delete(
                f"{base}/storage/v1/object/{bucket}/{object_name}",
                headers=_headers(key),
            )
            response.raise_for_status()
    except (httpx.HTTPError, ValueError) as exc:
        raise ProfileStorageError("Profile image deletion failed") from exc
