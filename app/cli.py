"""Administrative commands without seeded accounts or plaintext passwords."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import math
import os
import re

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlmodel import col, select

from .auth import hash_password
from .db import Database
from .jobs import JobStore, PruneResult
from .models import User
from .patches import remove_artifact
from .settings import Settings, get_settings


async def create_user(
    settings: Settings,
    username: str,
    password: str,
    *,
    admin: bool = False,
    reset_password: bool = False,
) -> None:
    """Create an account, or explicitly reset and reactivate an existing account."""
    if re.fullmatch(r"[A-Za-z0-9_.@+-]{1,150}", username) is None:
        raise ValueError("Username must contain 1–150 letters, digits, or _.@+-")
    hashed_password = await hash_password(password)
    database = Database(settings)
    try:
        await database.initialize()
        async with database.session() as session:
            user = (await session.exec(select(User).where(User.username == username))).first()
            if user is not None:
                if not reset_password:
                    raise ValueError("User already exists; use --reset-password explicitly")
                await session.exec(
                    update(User)
                    .where(col(User.id) == user.id)
                    .values(
                        hashed_password=hashed_password,
                        is_active=True,
                        is_admin=True if admin else col(User.is_admin),
                        auth_revision=col(User.auth_revision) + 1,
                    )
                )
            else:
                user = User(username=username, hashed_password=hashed_password, is_admin=admin)
                session.add(user)
            await session.commit()
    except IntegrityError:
        raise ValueError("Username already exists; use --reset-password explicitly") from None
    finally:
        await database.close()


async def _initialize(settings: Settings) -> None:
    database = Database(settings)
    try:
        await database.initialize()
    finally:
        await database.close()


async def prune_history(
    settings: Settings,
    *,
    days: float = 30,
    limit: int = 100,
    include_reports: bool = False,
) -> PruneResult:
    """Explicitly remove one old terminal-job batch; preserve reports by default."""
    if not math.isfinite(days) or not 0 <= days <= 36500:
        raise ValueError("Retention days must be finite and between 0 and 36500")
    if not 1 <= limit <= 1000:
        raise ValueError("Retention batch limit must be between 1 and 1000")
    database = Database(settings)
    try:
        await database.initialize()
        result = await JobStore(database).prune(
            older_than_seconds=days * 86400,
            limit=limit,
            include_reports=include_reports,
        )
        for artifact_id in result.artifact_ids:
            try:
                await asyncio.to_thread(
                    remove_artifact, settings.data_dir / "artifacts", artifact_id
                )
            except FileNotFoundError:
                # Retention is idempotent if a private artifact was already removed.
                continue
        return result
    finally:
        await database.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init-db", help="Apply additive database migrations")
    create = commands.add_parser("create-user", help="Create or explicitly reset an API account")
    create.add_argument("username")
    create.add_argument("--admin", action="store_true")
    create.add_argument("--reset-password", action="store_true")
    create.add_argument(
        "--password-env", metavar="VARIABLE", help="Read password from an injected variable"
    )
    commands.add_parser("worker", help="Run the persistent review worker")
    prune = commands.add_parser(
        "prune",
        help="Explicitly delete one batch of old terminal jobs and artifacts",
        description=(
            "Delete old completed, failed and cancelled jobs, their source snapshots, "
            "results and artifacts only when this command is run. Active jobs survive. "
            "Report history is preserved unless --include-reports is supplied. "
            "One bounded batch is processed; repeat deliberately for further batches."
        ),
    )
    prune.add_argument(
        "--days",
        "--older-than-days",
        type=float,
        default=30,
        help="Minimum age in days (default: 30)",
    )
    prune.add_argument(
        "--limit", type=int, default=100, help="Maximum jobs to delete (1–1000; default: 100)"
    )
    prune.add_argument(
        "--include-reports",
        action="store_true",
        help="Also permanently delete reports associated with selected jobs",
    )
    arguments = parser.parse_args()
    settings = get_settings()
    try:
        if arguments.command == "init-db":
            asyncio.run(_initialize(settings))
            print("Database migrations applied; no users were created.")
        elif arguments.command == "create-user":
            if arguments.password_env:
                password = os.environ.get(arguments.password_env, "")
                if not password:
                    parser.error("The requested password environment variable is missing or empty")
            else:
                password = getpass.getpass("Password (at least 12 characters): ")
                if password != getpass.getpass("Confirm password: "):
                    parser.error("Passwords do not match")
            asyncio.run(
                create_user(
                    settings,
                    arguments.username,
                    password,
                    admin=arguments.admin,
                    reset_password=arguments.reset_password,
                )
            )
            print("Account saved. Password has not been displayed.")
        elif arguments.command == "prune":
            result = asyncio.run(
                prune_history(
                    settings,
                    days=arguments.days,
                    limit=arguments.limit,
                    include_reports=arguments.include_reports,
                )
            )
            report_action = (
                "Associated report history deleted."
                if arguments.include_reports
                else "Report history preserved."
            )
            print(
                f"Deleted {result.removed_jobs} old terminal jobs; processed {len(result.artifact_ids)} artifact paths. {report_action}"
            )
        else:
            from .worker import run_worker

            asyncio.run(run_worker(settings))
    except (ValueError, OSError) as error:
        parser.exit(1, f"Configuration or account error: {error}\n")


if __name__ == "__main__":
    main()
