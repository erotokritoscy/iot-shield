"""Create the database schema, then exit — the compose schema-init step.

    python -m iotshield.storage.init   (reads IOTSHIELD_DATABASE_URL)

A dedicated one-shot owner of schema creation so the long-lived, read-only API
service never has to create tables on a shared Postgres. In compose the api,
dashboard, and runner all depend on this completing first. On the host the
pipeline runner still creates the schema itself, so a standalone run stays a
single command; this step is what a compose stack uses instead.
"""

import sys

from iotshield.storage import create_schema, database_url_from_env, describe_location, make_engine


def main() -> int:
    url = database_url_from_env()
    if url is None:
        return 2
    engine = make_engine(url)
    try:
        create_schema(engine)
    finally:
        engine.dispose()
    print(f"iotshield schema ready in database: {describe_location(url)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
