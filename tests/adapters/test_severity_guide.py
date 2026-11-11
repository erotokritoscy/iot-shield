"""The Triage page's severity table states the server's bands, not a copy that can drift.

The server decides an alert's severity (``alert/_correlate.py: _SEVERITY_FLOOR``); the
dashboard only explains it, in ``web/src/pages/triage/help.ts: SEVERITY_GUIDE``. Two languages
cannot share the constant, so this reads the floors out of the TypeScript and compares.
"""

import re
from pathlib import Path

from iotshield.alert._correlate import (  # pyright: ignore[reportPrivateUsage]
    _SEVERITY_FLOOR,
    severity_from_priority,
)
from iotshield.contracts import AlertSeverity

HELP = Path(__file__).resolve().parents[2] / "web" / "src" / "pages" / "triage" / "help.ts"


def _guide() -> list[tuple[str, float]]:
    text = HELP.read_text(encoding="utf-8")
    body = text[text.index("export const SEVERITY_GUIDE") :]
    return [
        (severity, float(floor))
        for severity, floor in re.findall(r'severity: "(\w+)",\s*floor: ([\d.]+)', body)
    ]


def test_the_table_names_every_severity_once_highest_first() -> None:
    names = [severity for severity, _ in _guide()]
    assert sorted(names) == sorted(s.value for s in AlertSeverity)
    floors = [floor for _, floor in _guide()]
    assert floors == sorted(floors, reverse=True)


def test_each_floor_is_the_servers() -> None:
    server = {severity.value: floor for floor, severity in _SEVERITY_FLOOR} | {"low": 0.0}
    assert dict(_guide()) == server
    for severity, floor in _guide():
        assert severity_from_priority(floor) == AlertSeverity(severity)
