"""Which cached fits an adapter's package actually ships (ADR-0035 D1).

The listing half of ADR-0012 D2's artifact scheme. ``load_artifact(id)`` resolves one id;
nothing could ever *enumerate* them, so every surface that needed the set had to be told
out of band — and the only surface that needed it, the Evaluate page, was told by a
hardcoded table in TypeScript that listed one of the two shipped artifacts and whose own
comment named this module as the fix.

Why that mattered more than a missing convenience: an artifact id is the difference between
a detector that judges and a detector a run refuses (ADR-0022), and it was the single
free-text field in a page where every other closed vocabulary is a control. An operator who
does not already know the ids cannot guess them, and the failure arrives as a config error
after the run is submitted rather than as an unavailable option.

The ids are a **closed vocabulary the registry knows and the factory signature cannot
express** — ``artifact: str | None`` is the only annotation that can be written, because the
members are files discovered at import time rather than literals. So they are published as
``ParamInfo.choices`` through the same door ADR-0023 D9 opened for presets and lanes, and
every form that already renders a closed vocabulary as a ``<select>`` renders this one too.

Scope: package resources only, which is exactly what ``load_artifact`` resolves. The day an
adapter can load a fit from a path (ADR-0030 anticipates deployment-fitted meters, which are
data we may not commit), the namespace stops being closed and this becomes the *shipped*
half of a wider answer.
"""

from __future__ import annotations

import importlib.resources
from functools import cache

_SUFFIX = ".json"


@cache
def packaged_artifact_ids(package: str) -> tuple[str, ...]:
    """The artifact ids ``load_artifact`` can resolve from ``package``, sorted.

    Cached for the same reason ``load_artifact`` is, and with the same consequence: package
    resources are read-only at runtime, so a process sees one answer for its lifetime. The
    two caches therefore agree — an id this returns is one that will load.

    A package with no artifact directory yields ``()`` rather than raising. That is not
    defensive padding: ``/modules`` is a catalogue call that must answer for every registered
    adapter, and one adapter packaged without its artifacts must not take the catalogue down
    with it.
    """
    try:
        entries = list(importlib.resources.files(package).iterdir())
    except (ModuleNotFoundError, FileNotFoundError, NotADirectoryError):
        return ()
    return tuple(
        sorted(
            entry.name.removesuffix(_SUFFIX)
            for entry in entries
            if entry.is_file() and entry.name.endswith(_SUFFIX)
        )
    )
