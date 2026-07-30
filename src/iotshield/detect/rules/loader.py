"""Loading a rule set: from the packaged library, or from the deployment's shelf (ADR-0032 D2).

Two homes, both supported on purpose (the decision that keeps the feature's promise true):

- **The packaged library** (``iotshield/detect/rules/library``) ships inside the product, so
  a fresh install has working detection with no configuration, and so tests never depend on
  a working directory. Loaded through ``importlib.resources``, the same way the trained
  model artifacts are (ADR-0012 D2). Named ``packaged`` in config.
- **A named rule set on the deployment's shelf** (``ruleset: site-rules``), so a deployment
  can maintain its own without touching the product — which is what makes "adding a detection
  is dropping in a file" true for someone who is not rebuilding an image.

The shelf is one directory the *deployment* designates (``IOTSHIELD_RULES_PATH``), holding
one subdirectory per rule set. Config names a rule set; it never names a path (ADR-0037).
That split is load-bearing rather than cosmetic: a ``ruleset`` value is deserialized from a
YAML file *or* from an HTTP request body — one ``PipelineSpec`` either way — so treating it
as a path made every run request an invitation to read an arbitrary directory on the server
(ADR-0018 §9-A, the rule that already keeps ``database_url`` out of the spec and addresses
corpora by name). ``resolve_ruleset`` is that boundary, and ``available_rulesets`` is what
lets a form offer the shelf as a dropdown instead of asking for a path nobody can guess.

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
import os
from collections.abc import Iterator
from functools import cache
from pathlib import Path
from typing import Any

import yaml

from iotshield._paths import REPO_ROOT
from iotshield.contracts import Lane
from iotshield.detect.rules.model import Rule, compile_rule

DEFAULT_LIBRARY = "iotshield.detect.rules.library"
"""The packaged rule set, used when config names no ``ruleset``."""

PACKAGED = "packaged"
"""What config calls the packaged library.

A real name rather than a blank, so the vocabulary a form offers has a member for every
reachable rule set and the default is one of them. The alternative — an optional param whose
"not given" means the library — would need the blank ``<option>`` neither ``Select`` nor
``st.selectbox`` renders, and would therefore *display* the first shelf entry while holding
nothing (the display-vs-value incoherence ADR-0036 D2 removed one field over).

Reserved: a shelf directory of this name is unreachable, and ``available_rulesets`` de-dupes
so it cannot appear twice in one dropdown.
"""

RULES_PATH_ENV = "IOTSHIELD_RULES_PATH"

DEFAULT_RULES_ROOT = REPO_ROOT / "config" / "rules"
"""The shelf ships in the repository beside ``config/bench``, so a clone can hold its own
rule sets with no setup. Overridable, because in an image the shelf is a mount point.
"""

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


def _rule_files(root: Path) -> Iterator[Path]:
    """Every rule file under a directory, unsorted and lazily.

    Lazy because its two callers want different things from the same definition of "holds
    rules": loading needs all of them in a stable order, and listing the shelf needs only to
    know whether there is one — and stops at the first, rather than walking a directory that
    is not a rule set at all.
    """
    return (path for path in root.rglob("*") if path.is_file() and path.suffix in _SUFFIXES)


def _directory_documents(root: Path) -> Iterator[tuple[str, Any]]:
    if not root.is_dir():
        raise ValueError(f"ruleset {str(root)!r} is not a directory")
    paths = sorted(_rule_files(root))
    if not paths:
        raise ValueError(f"ruleset {str(root)!r} contains no .yml/.yaml rule files")
    for path in paths:
        source = str(path)
        yield source, _parse(path.read_text(encoding="utf-8"), source=source)


def rules_root(root: Path | None = None) -> Path:
    """The shelf rule sets live on: explicit argument, else env, else the repo's own.

    The same three-step shape as ``corpus_root`` and ``bench_root``, because it is the same
    kind of fact.
    """
    if root is not None:
        return root
    configured = os.environ.get(RULES_PATH_ENV, "").strip()
    return Path(configured) if configured else DEFAULT_RULES_ROOT


def available_rulesets(root: Path | None = None) -> tuple[str, ...]:
    """Every rule set a config may name: the packaged library, then the shelf's, sorted.

    **Discovered, not registered** — the property corpora and artifacts already have (ADR-0021
    D5, ADR-0035 D1). A subdirectory holding at least one ``.yml``/``.yaml`` anywhere under it
    is a rule set; there is nothing to edit to add one, which is the whole promise.

    Deliberately **not cached**, unlike ``packaged_artifact_ids``. That cache is sound because
    package resources cannot change while the process runs; a shelf can and is meant to, since
    the Pipeline API is long-lived and an operator who drops in a rule set should see it on the
    next page load rather than after a restart. The cost is one directory listing per
    ``/modules`` call, bounded by a directory the deployment controls.

    A missing or unreadable shelf lists the packaged library alone rather than raising:
    ``/modules`` is a catalogue call that must answer for every registered adapter, and the
    default root does not exist in every install (the same decision ``packaged_artifact_ids``
    makes for a package shipped without its artifacts).
    """
    try:
        shelf = sorted(
            entry.name
            for entry in rules_root(root).iterdir()
            if entry.is_dir() and next(_rule_files(entry), None) is not None
        )
    except OSError:
        shelf = []
    return (PACKAGED, *(name for name in shelf if name != PACKAGED))


def resolve_ruleset(name: str | None = None, root: Path | None = None) -> str | None:
    """Turn a configured rule set *name* into a directory, or ``None`` for the library.

    The boundary between the config plane and the filesystem (ADR-0037), and the one function
    that has to be right: everything downstream of it already treats its argument as a
    directory it may read.

    A name is checked for **membership** of the shelf, never sanitized — the stance
    ``sink/file.py`` argues for on the same hazard, because reducing a bad value to a
    plausible one turns a wrong path into a *different* wrong path. So ``../secrets``, an
    absolute path and an honest typo all fail the same way, with the list of what exists and
    no hint about whether the thing they named is there.
    """
    if name is None or not name.strip() or name == PACKAGED:
        return None
    available = available_rulesets(root)
    if name not in available:
        raise ValueError(
            f"unknown rule set {name!r}; available: {', '.join(available)}. "
            f"A rule set is named, not pathed: it is a directory on the shelf the deployment "
            f"configures ({RULES_PATH_ENV})"
        )
    shelf = rules_root(root).resolve()
    resolved = (shelf / name).resolve()
    # The docstring's promise as a checked property rather than a claim about what
    # `available_rulesets` can return — cheap, and it cannot rot if that listing changes.
    if not resolved.is_relative_to(shelf):
        raise ValueError(f"rule set {name!r} resolves outside the shelf at {str(shelf)!r}")
    return str(resolved)


def load_ruleset(ruleset: str | None = None) -> tuple[Rule, ...]:
    """Compile every rule in a rule set *directory* (or the packaged library), sorted by id.

    Takes a path, not a shelf name: this is the library-level entry point, and a Python caller
    who can pass a path can already open the file. ``resolve_ruleset`` is what turns the
    *config plane's* name into one of these, and the registry factory is the only caller that
    matters for that (ADR-0037).

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
