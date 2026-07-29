"""Loading a rule set: from the packaged library, or from a directory (ADR-0032 D2).

Two homes, both supported on purpose (the decision that keeps the feature's promise true):

- **The packaged library** (``iotshield/detect/rules/library``) ships inside the product, so
  a fresh install has working detection with no configuration, and so tests never depend on
  a working directory. Loaded through ``importlib.resources``, the same way the trained
  model artifacts are (ADR-0012 D2).
- **A directory named by config** (``ruleset: path/to/rules``), so a deployment can maintain
  its own set without touching the product — which is what makes "adding a detection is
  dropping in a file" true for someone who is not rebuilding an image.

Three loading behaviours worth stating, each replacing a silent failure with a loud one:

- **Every file is compiled, even ones for other lanes.** A broken log rule fails a
  flow-lane run too. That is deliberate: a malformed file in a shipped rule set is a defect,
  and the alternative hides it until somebody happens to configure that lane.
- **Duplicate rule ids are refused.** ``origin`` on an alert says ``rule:<id>``, so two
  files claiming one id would make the alert's attribution ambiguous.
- **A lane with no rules is refused at construction.** Building a rule detector for a lane
  the rule set cannot feed is a misconfiguration whose only symptom would otherwise be a
  detector that finds nothing — indistinguishable from a quiet night, which is the failure
  mode this whole adapter is written to avoid.
"""

from __future__ import annotations

import importlib.resources
from collections.abc import Iterator
from functools import cache
from pathlib import Path
from typing import Any

import yaml

from iotshield.contracts import Lane
from iotshield.detect.rules.model import Rule, compile_rule

DEFAULT_LIBRARY = "iotshield.detect.rules.library"
"""The packaged rule set, used when config names no ``ruleset``."""

_SUFFIXES = (".yml", ".yaml")


def _parse(text: str, *, source: str) -> Any:
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid rule in {source}: not readable as YAML: {exc}") from exc


def _packaged_documents() -> Iterator[tuple[str, Any]]:
    resources = importlib.resources.files(DEFAULT_LIBRARY)
    # Sorted by name so that when several files are broken, the SAME one is reported first
    # every run. (The rules themselves are sorted by id at the end, so this ordering is not
    # otherwise observable.)
    for entry in sorted(resources.iterdir(), key=lambda item: item.name):
        if entry.name.endswith(_SUFFIXES) and entry.is_file():
            yield entry.name, _parse(entry.read_text(encoding="utf-8"), source=entry.name)


def _directory_documents(root: Path) -> Iterator[tuple[str, Any]]:
    if not root.is_dir():
        raise ValueError(f"ruleset {str(root)!r} is not a directory")
    paths = sorted(path for path in root.rglob("*") if path.is_file() and path.suffix in _SUFFIXES)
    if not paths:
        raise ValueError(f"ruleset {str(root)!r} contains no .yml/.yaml rule files")
    for path in paths:
        source = str(path)
        yield source, _parse(path.read_text(encoding="utf-8"), source=source)


def load_ruleset(ruleset: str | None = None) -> tuple[Rule, ...]:
    """Compile every rule in the named set (or the packaged library), sorted by id.

    The packaged library is memoized: it cannot change while the process runs, and a run
    builds one detector per lane while a benchmark builds one per case — so an uncached load
    re-parsed the same shipped YAML once per lane per case (measured at 6.3 ms per load,
    32% of a five-case benchmark's wall time, and growing linearly with the library the
    feature exists to grow). A named ``ruleset`` directory is deliberately NOT cached: the
    Pipeline API is long-lived, and an operator who edits a rule file should not have to
    restart to see it.
    """
    if ruleset is None:
        return _packaged_ruleset()
    return _compile_all(_directory_documents(Path(ruleset)))


@cache
def _packaged_ruleset() -> tuple[Rule, ...]:
    return _compile_all(_packaged_documents())


def _compile_all(documents: Iterator[tuple[str, Any]]) -> tuple[Rule, ...]:
    """Validate and compile every document, refusing duplicate ids."""
    rules: list[Rule] = []
    seen: dict[str, str] = {}
    for source, document in documents:
        if document is None:
            raise ValueError(f"invalid rule in {source}: the file is empty")
        if not isinstance(document, dict):
            raise ValueError(
                f"invalid rule in {source}: a rule file holds one mapping, "
                f"got {type(document).__name__}"
            )
        rule = compile_rule(document, source=source)
        if rule.id in seen:
            raise ValueError(
                f"duplicate rule id {rule.id!r} in {source} (already defined in {seen[rule.id]}); "
                "ids must be unique because an alert attributes its threat class to one"
            )
        seen[rule.id] = source
        rules.append(rule)

    # A tuple, not a list: the packaged set is shared between every detector built in the
    # process, so nothing handed out may be mutable. Every Rule below it is already frozen.
    return tuple(sorted(rules, key=lambda rule: rule.id))


def load_rules(lane: Lane, ruleset: str | None = None) -> tuple[Rule, ...]:
    """The rules a detector on this lane will evaluate; refuses an empty result.

    One entry point rather than load-then-filter, so the ruleset name is not threaded
    through a second function purely to appear in this error message.
    """
    rules = load_ruleset(ruleset)
    selected = tuple(rule for rule in rules if rule.lane is lane)
    if not selected:
        where = ruleset or "the packaged rule library"
        available = sorted({rule.lane.value for rule in rules})
        raise ValueError(
            f"{where} holds no {lane.value!r} rules, so a rule detector on that lane would "
            f"judge nothing. Rules are available for: {', '.join(available) or '(none)'}"
        )
    return selected
