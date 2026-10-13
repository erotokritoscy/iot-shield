"""Create or manage the platform's super admin accounts (ADR-0040).

    python -m iotshield.auth.superadmin create --username root      # prompts for the password
    python -m iotshield.auth.superadmin set-password --username root
    python -m iotshield.auth.superadmin list

In compose (the runner image carries the database URL):

    docker compose run --rm runner python -m iotshield.auth.superadmin create --username root

A command rather than a route on purpose: whoever can run it already has the database, so it
grants nothing new — while a web route that could mint super admins would be the most valuable
endpoint in the product to attack. The password is read from the terminal (never an argument,
which would land in shell history and ``ps``), or from stdin with ``--password-stdin`` for
scripted setups.
"""

import argparse
import getpass
import sys

from iotshield.auth.passwords import hash_password
from iotshield.storage import (
    accounts,
    create_schema,
    database_url_from_env,
    make_engine,
    make_session_factory,
)
from iotshield.storage.accounts import AccountConflict

MIN_PASSWORD = 8  # the same floor the API enforces (api/auth.py)


def _read_password(from_stdin: bool) -> str:
    if from_stdin:
        password = sys.stdin.readline().rstrip("\n")
    else:
        password = getpass.getpass("Password: ")
        if getpass.getpass("Repeat password: ") != password:
            raise ValueError("the passwords do not match")
    if len(password) < MIN_PASSWORD:
        raise ValueError(f"the password must be at least {MIN_PASSWORD} characters")
    return password


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m iotshield.auth.superadmin")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("create", "create a super admin"),
        ("set-password", "replace a super admin's password (signs them out)"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--username", required=True)
        command.add_argument(
            "--password-stdin", action="store_true", help="read the password from stdin"
        )
    commands.add_parser("list", help="list the super admins")
    args = parser.parse_args(argv)

    url = database_url_from_env()
    if url is None:
        return 2
    engine = make_engine(url)
    try:
        create_schema(engine)  # idempotent; lets a fresh host database be bootstrapped
        with make_session_factory(engine)() as session:
            if args.command == "list":
                for user in accounts.list_superadmins(session):
                    print(user.username)
                return 0
            username = accounts.validate_username(args.username)
            password = _read_password(args.password_stdin)
            if args.command == "create":
                user = accounts.create_superadmin(
                    session, username=username, password_hash=hash_password(password)
                )
                session.commit()
                print(f"super admin {user.username!r} created", file=sys.stderr)
                return 0
            found = accounts.credentials_for(session, username)
            if found is None or found[0].role != "superadmin":
                print(f"error: no super admin named {args.username!r}", file=sys.stderr)
                return 2
            accounts.set_password(session, found[0].user_id, hash_password(password))
            accounts.end_other_sessions(session, found[0].user_id, keep_token=None)
            session.commit()
            print(f"password replaced for {found[0].username!r}", file=sys.stderr)
            return 0
    except (AccountConflict, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
