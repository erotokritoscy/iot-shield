"""Shared line-oriented normalize loop: the port's laws, implemented once.

A line-wise normalizer maps each non-blank input line to exactly one event or one
MalformedRecord. This helper owns the parts every such adapter otherwise repeats,
which are precisely the port's enforced laws (see ``port.py``): the
``source_format`` guard, the blank-line skip, verbatim-``raw`` reject capture, and
the ``ValidationError -> MalformedRecord`` translation. An adapter supplies only
its per-line ``parse_line``; adapters that fan one line into many events, or that
parse a whole-file format, don't use this.
"""

import re
from collections.abc import Callable
from typing import Any

from pydantic import ValidationError

from iotshield.contracts import MalformedRecord, NormalizedEvent
from iotshield.normalize.port import NormalizeResult

_LINE_BREAK = re.compile(r"\r\n|\r|\n")


def split_lines(text: str) -> list[str]:
    """Split input into lines on REAL newlines only — the record boundary, nothing else.

    ``str.splitlines`` also breaks on ``\\x0b \\x0c \\x1c \\x1d \\x1e \\x85 \\u2028 \\u2029``,
    every one of which can occur legally *inside* a record: U+2028 is an unescaped-legal
    character in a JSON string (the well-known "JSON is not a JavaScript subset" hazard) and
    form feed / NEL turn up in real syslog payloads. Splitting there breaks two of the port's
    laws at once — the record becomes neither one event nor *one* MalformedRecord, and ``raw``
    stops being the verbatim input — and the reject reason ("not valid JSON: Unterminated
    string") blames the operator's data for our parsing (ADR-0033).

    Shared by every whole-file reader, not just this loop, so the three normalizers that
    scan text cannot disagree about where a record ends.
    """
    return _LINE_BREAK.split(text)


class LineParseError(Exception):
    """Raised by a per-line parser to reject a line; the message is the reason."""


def normalize_linewise(
    text: str,
    *,
    source_format: str,
    expected_format: str,
    origin: str,
    parse_line: Callable[[str], dict[str, Any]],
) -> NormalizeResult:
    """Run ``parse_line`` over each non-blank line, capturing failures as rejects.

    ``parse_line`` returns the NormalizedEvent field mapping for a good line (all
    fields except ``raw``, which only this loop knows verbatim) or raises
    ``LineParseError`` to reject it; contract-validation failures are caught and
    rejected here too, so contract rules are never re-implemented in an adapter.
    An unsupported ``source_format`` raises — a config error, not a data error.
    """
    if source_format != expected_format:
        raise ValueError(f"unsupported source_format: {source_format!r}")

    result = NormalizeResult()

    def reject(line: str, error: str) -> None:
        result.malformed.append(
            MalformedRecord(raw=line, error=error, source_format=source_format, origin=origin)
        )

    for line in split_lines(text):
        if not line.strip():
            continue
        try:
            event = NormalizedEvent.model_validate({**parse_line(line), "raw": line})
        except LineParseError as exc:
            reject(line, str(exc))
        except ValidationError as exc:
            reject(line, "; ".join(error["msg"] for error in exc.errors()))
        else:
            result.events.append(event)
    return result
