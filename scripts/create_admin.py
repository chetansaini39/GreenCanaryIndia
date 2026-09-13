#!/usr/bin/env python3
"""
Create a dedicated admin account (email + password) in MongoDB.

Admin/staff accounts are never self-registered — use this script for the
first admin, or to add another admin when needed.

Usage:
    python scripts/create_admin.py --email admin@example.com --name "Admin User"
    python scripts/create_admin.py --email admin@example.com --name "Admin User" --password 'your-secure-password'
    
    
    # Auto-generate a password
python scripts/create_admin.py --email admin@example.com --name "Admin User"
# Set your own password
python scripts/create_admin.py --email admin@example.com --name "Admin User" --password 'your-secure-password'
# Promote an existing account to admin
python scripts/create_admin.py --email user@example.com --name "User Name" --promote

If --password is omitted, a random password is generated and printed once.
"""
from __future__ import annotations

import argparse
import secrets
import string
import sys
from pathlib import Path

from dotenv import load_dotenv
from pymongo import MongoClient
from werkzeug.security import generate_password_hash

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env")

from app.config import BaseConfig  # noqa: E402
from app.models import users as users_model  # noqa: E402
from app.utils.time import now_ct  # noqa: E402


def _generate_password(length: int = 20) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a dedicated admin account.")
    parser.add_argument("--email", required=True, help="Admin login email")
    parser.add_argument("--name", required=True, help="Display name")
    parser.add_argument(
        "--password",
        help="Password (min 8 chars). If omitted, one is generated or prompted.",
    )
    parser.add_argument(
        "--promote",
        action="store_true",
        help="Promote an existing account to admin instead of creating a new one.",
    )
    args = parser.parse_args()

    email = args.email.strip().lower()
    name = args.name.strip()
    password = args.password

    if not email or not name:
        print("Email and name are required.", file=sys.stderr)
        return 1

    if password is None:
        password = _generate_password()
        generated = True
    else:
        generated = False
        if len(password) < 8:
            print("Password must be at least 8 characters.", file=sys.stderr)
            return 1

    client = MongoClient(BaseConfig.MONGO_URI)
    db_name = BaseConfig.MONGO_URI.rsplit("/", 1)[-1].split("?")[0] or "greencanaryindia"
    db = client[db_name]

    existing = users_model.find_by_email(db, email)
    if existing:
        if not args.promote:
            print(
                f"An account with email {email} already exists. "
                "Use --promote to upgrade it to admin.",
                file=sys.stderr,
            )
            return 1
        users_model.update_by_id(
            db,
            existing["_id"],
            {
                "role": "admin",
                "name": name,
                "is_active": True,
            },
        )
        print(f"Promoted existing account to admin: {email}")
        if generated:
            print(f"Generated password (not applied — account already had credentials): {password}")
        return 0

    now = now_ct()
    user_id = users_model.insert_one(
        db,
        {
            "email": email,
            "name": name,
            "password_hash": generate_password_hash(password),
            "google_id": None,
            "role": "admin",
            "subscription_status": "none",
            "created_at": now,
            "last_login_at": None,
            "is_active": True,
        },
    )

    print(f"Admin account created: {email} (id={user_id})")
    if generated:
        print(f"Generated password: {password}")
        print("Save this password now — it will not be shown again.")
    else:
        print("Log in at /login with the email and password you provided.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
