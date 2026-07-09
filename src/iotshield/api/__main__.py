"""Serve the API: python -m iotshield.api

Services configure via environment (12-factor), unlike the per-run pipeline CLI:
  IOTSHIELD_DATABASE_URL   required, e.g. postgresql+psycopg://... or sqlite:///demo.db
  IOTSHIELD_API_HOST       default 127.0.0.1 (compose overrides to 0.0.0.0)
  IOTSHIELD_API_PORT       default 8000
"""

import os
import sys

import uvicorn

from iotshield.api.app import create_app
from iotshield.storage import database_url_from_env, describe_location, make_engine


def main() -> int:
    url = database_url_from_env()
    if url is None:
        return 2
    host = os.environ.get("IOTSHIELD_API_HOST", "127.0.0.1")
    port = int(os.environ.get("IOTSHIELD_API_PORT", "8000"))
    # Print the resolved (absolute, for SQLite) database location so a cwd-relative
    # URL pointing at an unexpected file is visible instead of silently empty.
    print(
        f"iotshield API on {host}:{port} using database: {describe_location(url)}", file=sys.stderr
    )
    uvicorn.run(create_app(make_engine(url)), host=host, port=port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
