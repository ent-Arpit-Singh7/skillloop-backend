"""Create an admin account from the terminal. Admins can never be created through the API.

Usage:
    python -m app.scripts.create_admin
Or non-interactively (values come from environment variables, never from source code):
    ADMIN_NAME="Your Name" ADMIN_EMAIL=you@example.com ADMIN_PASSWORD='a long password' python -m app.scripts.create_admin

The "admin" table must already exist (created by docs/skillloop_database.sql).
"""
import getpass
import os
import sys

from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy import func, select

from app.core.security import hash_password
from app.db.session import SessionLocal
from app.models import Admin


def main() -> None:
    name = (os.environ.get("ADMIN_NAME") or input("Admin full name: ")).strip()
    email = (os.environ.get("ADMIN_EMAIL") or input("Admin email: ")).strip().lower()
    password = os.environ.get("ADMIN_PASSWORD") or getpass.getpass("Admin password (min 10 chars): ")
    try:
        # Same check the login form uses, so an admin can never be created with an email that cannot log in.
        email = str(TypeAdapter(EmailStr).validate_python(email)).lower()
    except ValidationError:
        sys.exit("That email address is not valid (domains such as .test or .local are rejected). Use a real-looking domain.")
    if not name or len(password) < 10:
        sys.exit("Need a name and a password of at least 10 characters.")

    with SessionLocal() as db:
        if db.scalar(select(Admin).where(func.lower(Admin.email) == email)):
            sys.exit("An admin with that email already exists.")
        db.add(Admin(full_name=name, email=email, password_hash=hash_password(password)))
        db.commit()
    print(f"Admin {email} created.")


if __name__ == "__main__":
    main()
