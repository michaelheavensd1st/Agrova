"""Command-line utilities for the AgOS API.

Run with ``python -m app.cli <command>`` from ``apps/api/``.

Available commands:

    seed
        Idempotently seed the canonical permissions + system roles.

    create_admin [--email EMAIL] [--password PASSWORD]
        Create (or promote) a **platform administrator**. Never seeded
        with default credentials. When ``--email`` / ``--password`` are
        omitted the CLI prompts interactively and password input is
        hidden. The user is created ``is_active=True``,
        ``is_verified=True``, ``is_superuser=True``.

Example::

    $ python -m app.cli create_admin
    Email: alice@agrovix.com
    Password: ********

    $ python -m app.cli create_admin --email bob@agrovix.com --password ${ADMIN_PW}
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import re
import sys

from app.core.config import get_settings
from app.core.security import hash_password, verify_password
from app.repositories.role_repo import RoleAssignmentRepository, RoleRepository
from app.repositories.user_repo import UserRepository
from app.seed import seed_permissions_and_roles

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _prompt_credentials(email: str | None, password: str | None) -> tuple[str, str]:
    if email is None:
        email = input("Email: ").strip()
    if not _EMAIL_RE.match(email):
        print(f"Invalid email: {email!r}", file=sys.stderr)
        sys.exit(2)
    if password is None:
        settings = get_settings()
        while True:
            password = getpass.getpass("Password: ")
            if len(password) < settings.password_min_length:
                print(
                    f"Password must be at least {settings.password_min_length} characters.",
                    file=sys.stderr,
                )
                continue
            confirm = getpass.getpass("Confirm:  ")
            if confirm != password:
                print("Passwords do not match.", file=sys.stderr)
                continue
            break
    return email.lower(), password


async def _create_admin(email: str, password: str) -> None:
    from app.db import session as _db

    async with _db.AsyncSessionLocal() as session:
        user_repo = UserRepository(session)
        role_repo = RoleRepository(session)
        role_assign_repo = RoleAssignmentRepository(session)

        user = await user_repo.get_by_email_for_update(email)
        existing_user = user is not None
        security_state_changed = False
        if user is None:
            user = await user_repo.create(
                email=email,
                hashed_password=hash_password(password),
                full_name="Platform Administrator",
            )
        else:
            password_changed = user.hashed_password is None or not verify_password(
                password, user.hashed_password
            )
            security_state_changed = password_changed or any(
                (not user.is_active, not user.is_verified, not user.is_superuser)
            )
            if password_changed:
                await user_repo.set_password_hash(user, hash_password(password))

        user.is_active = True
        user.is_verified = True
        user.is_superuser = True
        session.add(user)
        await session.flush()

        role = await role_repo.get_by_name("platform_admin")
        if role is None:
            print(
                "platform_admin role not found — run `python -m app.seed` first.",
                file=sys.stderr,
            )
            sys.exit(1)

        # Idempotent: only insert if not already present.
        existing = [
            a
            for a in await role_assign_repo.list_for_user(user.id)
            if a.role_id == role.id and a.organization_id is None and a.farm_id is None
        ]
        if not existing:
            await role_assign_repo.create(
                user_id=user.id,
                role_id=role.id,
                organization_id=None,
                farm_id=None,
                granted_by_id=user.id,
            )
            security_state_changed = existing_user
        if existing_user and security_state_changed:
            await user_repo.increment_session_version(user)
        await session.commit()
        print(f"Platform administrator ready: {email}")


def _cmd_seed(_: argparse.Namespace) -> None:
    asyncio.run(seed_permissions_and_roles())
    print("Seed complete.")


def _cmd_create_admin(args: argparse.Namespace) -> None:
    email, password = _prompt_credentials(args.email, args.password)
    asyncio.run(_create_admin(email, password))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="app.cli", description="Agrovix AgOS admin CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    seed = sub.add_parser("seed", help="Seed permissions + system roles (idempotent).")
    seed.set_defaults(func=_cmd_seed)

    admin = sub.add_parser("create_admin", help="Create or promote a platform administrator.")
    admin.add_argument("--email", type=str, default=None)
    admin.add_argument("--password", type=str, default=None)
    admin.set_defaults(func=_cmd_create_admin)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
