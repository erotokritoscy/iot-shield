"""Dump both FastAPI apps' OpenAPI documents to disk, for `npm run gen:types`.

Why a script and not `curl http://localhost:8000/openapi.json`: type generation must work
on a clean checkout with nothing running, and both apps are constructible in-process around
a throwaway in-memory engine (that is the whole point of ``create_app(engine)``).

``IOTSHIELD_EVAL_API`` is set *before* the Pipeline API is built, because the ``/eval``
router is registered at construction rather than consulted per request (ADR-0021 D1) — so
without it the generated types would simply be missing the Evaluate mode's whole surface.
Both apps are built with ``auth=True`` for the same reason: the ``/auth`` router exists only
when login is on (ADR-0039), and the dashboard must be typed against the deployment that has it.

Read-only with respect to ``src/``: it imports the apps, it never modifies them.
"""

import json
import os
import sys
from pathlib import Path

os.environ.setdefault("IOTSHIELD_EVAL_API", "1")

from sqlalchemy import create_engine

from iotshield.api import create_app
from iotshield.api.control import create_control_app


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("web/src/lib/api/openapi")
    out.mkdir(parents=True, exist_ok=True)
    for name, app in (
        ("read", create_app(create_engine("sqlite://"), auth=True)),
        ("pipeline", create_control_app(create_engine("sqlite://"), auth=True)),
    ):
        target = out / f"{name}.json"
        # newline="\n": write_text otherwise translates to os.linesep, so regenerating on
        # Windows rewrote every line of a committed snapshot as CRLF and buried the real
        # schema change in a whole-file diff (the same lesson as bundle.NEWLINE, ADR-0028).
        target.write_text(
            json.dumps(app.openapi(), indent=2) + "\n", encoding="utf-8", newline="\n"
        )
        print(f"wrote {target}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
