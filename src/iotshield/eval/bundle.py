"""Corpus bundles: a labelled dataset as *data on disk*, not code.

The unit that makes the harness usable by someone who clones only this repository. A bundle
is a self-describing directory under ``data/eval/``:

    data/eval/<corpus>/
      manifest.json              name, description, provenance, licence, the case list
      cases/<case>.<ext>.gz      readings, in a format one of our normalizers parses
      cases/<case>.labels.json   the answer key: per asset, its row clock + fault intervals

Three decisions are worth the space (ADR-0021 D5):

**Discovered, not registered.** Corpora used to be Python loader functions in a registry, so
adding a dataset meant writing code. Scanning a directory means adding a corpus is *adding
data* — the same drop-in gesture as adding a config, and one a researcher can do without
reading our source. It also means the CLI, the API and the dashboard all see a new corpus
with no change to any of them.

**Readings stay in a real ingest format, gzipped.** A bundle holds the text a run would
actually ingest, so a benchmark exercises normalization too and measures the *pipeline*
rather than a detector in isolation. Gzip because the corpora are repetitive numeric text
(measured 4.4x on SKAB) and a repository is a bad place for megabytes that compress away.

**Labels are intervals plus the instant clock, both keyed by (asset, lane).** See
``AssetTruth.from_incidents``. Storing the clock alongside the intervals costs a little
duplication and buys a labels file that is a complete answer key on its own — the reader
never has to parse readings to learn the truth, so it needs no knowledge of their format. Per
*asset* because a recording's devices do not share a clock, per *lane* because one device's
modalities do not either, and each *interval* names its lane too, so nothing has to be
inferred from an empty set (ADR-0028 D2). The set of assets listed is the corpus's roster: one
with an empty ``incidents`` list is *asserted clean*, and one absent entirely is out of scope
(ADR-0026; see ``truth`` for why those must not be conflated).

**Instants, not rows.** A clock entry is one moment this asset was observed at on this lane,
not one line of the readings: our contract fans a multi-channel reading into one event per
channel (ADR-0003 D1), so a 1,148-instant SKAB recording is 9,184 lines here. Row counts are
a property of the encoding; the answer key is indexed by what a detector can claim
(ADR-0028 D1).

Provenance and licence are *required* manifest fields, because the first thing this format
had to solve was which datasets we may redistribute at all: SKAB is GPLv3, so it ships as a
fetch step rather than committed bytes, and a bundle that cannot say where it came from is a
bundle nobody can safely reuse.
"""

import gzip
import json
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from iotshield.normalize.flow import FLOW_CSV as _FLOW_CSV
from iotshield.normalize.flow import ZEEK_CONN as _ZEEK_CONN
from iotshield.normalize.stub import SOURCE_FORMAT as STUB_CSV
from iotshield.normalize.syslog import SOURCE_FORMAT as SYSLOG

MANIFEST_NAME = "manifest.json"
CASES_DIR = "cases"
LABELS_SUFFIX = ".labels.json.gz"
"""Gzipped, like the readings.

The row clock is a long run of near-identical ISO-8601 strings, which is the best case for
gzip and the worst case for storing raw: measured on the synthetic corpus, uncompressed
labels were *larger* than the compressed readings they annotate (264 KB vs 220 KB). Read
one by hand with ``gzip -dc``.
"""

LABELS_VERSION = 4
"""On-disk answer-key schema.

The version history is one coordinate system being completed one axis at a time, which is
worth reading as a whole rather than as four repairs (ADR-0028 D1):

===  ==================================================================================
v1   one global clock, bare ``(start, end)`` pairs. "Something was wrong at 00:05."
v2   the **asset** axis (ADR-0026). "The camera was wrong at 00:05."
v3   the **lane** axis, on the clock (ADR-0027 D2). "The camera's network was wrong."
v4   the lane axis on the **incident** (ADR-0028 D2), so no field is read two ways.
===  ==================================================================================

v3 gave the clock a lane but left an incident carrying a *set* of them, and that set had to
mean "every lane" to point scoring and "no claim" to per-lane recall. v4 makes a lane an axis
of the incident: one incident per (fault, lane), sharing a ``fault_id``.

An older file is *rejected* with the command to regenerate rather than upgraded, for the same
reason a v1 team3 artifact is: an upgrade would have to invent the missing dimension, and the
only place to find it is the readings — which is precisely what this format exists to avoid
needing. Here it is sharper than usual: splitting a v3 incident's lane set into v4 incidents
would have to guess each projection's *extent*, and the union it stored is the one thing that
cannot be un-unioned. A guessed lane is a number nobody measured.
"""

# Extension per source format, so a bundle's readings are recognisable on sight rather than
# uniformly ".txt". Unknown formats fall back to .txt — the manifest is authoritative either
# way, this only affects what a human sees in a file listing.
#
# Keyed only by formats a normalizer actually *consumes*, which is all a bundle input can
# declare. The syslog dialect names (`syslog-rfc5424`, `syslog-rfc3164`) are what that
# normalizer stamps on the events it emits; no bundle can ever be tagged with one, so entries
# for them were unreachable.
_EXTENSIONS = {
    STUB_CSV: "csv",
    SYSLOG: "log",
    _FLOW_CSV.name: "csv",
    _ZEEK_CONN.name: "log",
}


class BundleError(ValueError):
    """A bundle directory is missing, malformed, or internally inconsistent.

    A ``ValueError`` so that every caller already handles it. As a bare ``RuntimeError`` it was
    caught by *nothing*: the CLI and the API both map ``ValueError``, the listing functions
    swallowed it into ``continue``, and so a bundle with a stale answer key was listed
    normally and then raised uncaught when selected — CLI traceback, API 500 — while every
    carefully worded message in this module, including the ones naming the exact regenerate
    command, was unreachable.
    """


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InputManifest(_Model):
    """One readings file: what format it is in, and where it sits inside the bundle."""

    source_format: str = Field(min_length=1)
    path: str = Field(min_length=1)


class CaseManifest(_Model):
    name: str = Field(min_length=1)
    inputs: list[InputManifest] = Field(min_length=1)
    labels: str = Field(min_length=1)


class CorpusManifest(_Model):
    """A bundle's self-description — everything needed to load and to cite it."""

    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    provenance: str = Field(
        min_length=1, description="Where the data came from; a URL when there is one"
    )
    licence: str = Field(min_length=1, description="Terms the data is distributed under")
    cases: list[CaseManifest] = Field(min_length=1)


class IncidentLabel(_Model):
    """One fault interval **as one lane saw it** (ADR-0028 D2).

    ``lane`` is required, because it is an axis rather than a description: it decides which
    clock this interval flags, and a missing one is not a corpus that declined to say — it is
    a corpus nothing can score. Its v3 predecessor ``lanes: list[str]`` was optional and empty
    meant two contradictory things at once.

    ``fault_id`` groups the projections of one cause, so it is what the alert level counts.
    Still defaulted, because a hand-written bundle may legitimately have one interval per
    fault and nothing to group; ``kind`` stays purely descriptive — SKAB's ``anomaly`` column
    records that a fault happened, not what shape it had.
    """

    start: datetime
    end: datetime
    lane: str = Field(min_length=1)
    kind: str = ""
    fault_id: str = ""


class AssetLabel(_Model):
    """One asset's answer key: its row clock **per lane**, plus its faults.

    An empty ``incidents`` list is not missing data — it is the corpus **asserting this
    asset was clean throughout**, which is what makes a detection on it an unambiguous false
    alarm. An asset that is simply out of scope is omitted from the file entirely.

    ``clocks`` is keyed by lane value because a device's modalities do not share a clock and
    point metrics are scored per lane (``truth.AssetTruth``). A single-lane corpus has one
    entry, which is the v2 shape with its lane written down.
    """

    asset_id: str = Field(min_length=1)
    clocks: dict[str, list[datetime]] = Field(min_length=1)
    incidents: list[IncidentLabel] = Field(default_factory=list)


class Labels(_Model):
    """A case's answer key: one entry per asset the corpus has an opinion about.

    ``schema_version`` is pinned rather than merely present, so a future v5 fails at
    validation with a legible message instead of silently dropping fields it does not
    recognise (``extra="forbid"`` would otherwise reject them one at a time).
    """

    schema_version: Literal[4] = LABELS_VERSION
    assets: list[AssetLabel] = Field(min_length=1)


def contained(directory: Path, relative: str) -> Path:
    """Resolve a manifest path inside its bundle, refusing anything that escapes it.

    A bundle is **untrusted data**. That is the whole premise of "discovered, not registered":
    corpora are a drop-in a third party supplies, ``data/eval`` is a bind mount in
    docker-compose, and the eval API will load whatever is under the corpus root. Joining a
    manifest's ``path`` to the bundle directory without this check made ``"../secret.txt"`` —
    or a plain absolute path — a way to read any file the server can, and return its contents
    inside a benchmark report.

    Enforced here rather than in the callers so every entry point inherits it and a future one
    cannot forget. ``resolve`` first, because the traversal may be spelled with symlinks or
    ``..`` segments that only normalize away on a real path.
    """
    base = directory.resolve()
    target = (base / relative).resolve()
    if target != base and base not in target.parents:
        raise BundleError(
            f"manifest path {relative!r} resolves outside its bundle ({base}); a bundle may "
            "only reference files it contains"
        )
    return target


def read_payload(path: Path) -> str:
    """Read a bundle payload, transparently gunzipping when it is compressed."""
    if path.suffix == ".gz":
        return gzip.decompress(path.read_bytes()).decode("utf-8")
    return path.read_text(encoding="utf-8")


def read_manifest(directory: Path) -> CorpusManifest:
    """Parse a bundle's manifest, or say precisely what is wrong with it."""
    manifest_path = directory / MANIFEST_NAME
    if not manifest_path.is_file():
        raise BundleError(f"no {MANIFEST_NAME} in {directory}")
    try:
        return CorpusManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise BundleError(f"invalid {manifest_path}: {exc}") from exc


REGENERATE_HINT = (
    "regenerate it: `python -m iotshield.eval generate` for a synthetic corpus, "
    "`python -m iotshield.eval fetch <dataset>` for a converted one"
)


def read_labels(path: Path) -> Labels:
    """Parse a case's answer key, naming the fix when its schema is not the current one.

    The version is read *structurally*, before shape validation, exactly as the team3 artifact
    loader does — and for the same reason. ``extra="forbid"`` would otherwise reject a v1
    payload with a pydantic complaint about an unexpected ``timestamps`` key, which tells the
    reader nothing about what to do; and a future v3 would fail the same opaque way rather
    than saying which version it is. A stale answer key is one command away.

    Absent means v1: that version had no ``schema_version`` field at all.
    """
    try:
        payload = read_payload(path)
        found = json.loads(payload)
    except (OSError, ValueError) as exc:
        raise BundleError(f"invalid labels {path}: {exc}") from exc
    version = found.get("schema_version", 1) if isinstance(found, dict) else None
    if version != LABELS_VERSION:
        raise BundleError(
            f"{path} is a v{version} answer key; this build reads v{LABELS_VERSION}, in which "
            "each incident names exactly one lane rather than carrying a set of them. "
            f"{REGENERATE_HINT}"
        )
    try:
        return Labels.model_validate(found)
    except ValueError as exc:
        raise BundleError(f"invalid labels {path}: {exc}") from exc


def _stem(case_name: str) -> str:
    """A case name as a filename. One rule, because readings and labels must agree.

    They are paired by the manifest, but a human reading ``cases/`` expects
    ``foo.csv.gz`` beside ``foo.labels.json.gz``; two copies of this could desynchronise
    that silently.
    """
    return case_name.replace("/", "-").replace("\\", "-").replace(" ", "_")


def payload_name(case_name: str, source_format: str, index: int) -> str:
    """The on-disk filename for one case input — slashes flattened, format-hinted."""
    suffix = f"-{index}" if index else ""
    return f"{CASES_DIR}/{_stem(case_name)}{suffix}.{_EXTENSIONS.get(source_format, 'txt')}.gz"


def labels_name(case_name: str) -> str:
    return f"{CASES_DIR}/{_stem(case_name)}{LABELS_SUFFIX}"


NEWLINE = "\n"
"""Line ending for every text file a bundle writes. **Never the platform's.**

A committed corpus must be byte-identical to a fresh build, which is what
``test_a_fresh_build_reproduces_the_committed_bytes`` checks. ``write_text`` with the default
``newline=None`` translates ``\\n`` to ``os.linesep``, so a bundle generated on Windows carried
CRLF while ``.gitattributes`` (``* text=auto eol=lf``) checks the file out with LF — the freshness
test then failed on a fresh Windows clone for a reason that has nothing to do with the corpus, and
passed only where a previous local build had already overwritten the file.

Gzipped payloads were never exposed to this (``write_bytes``), which is why it went unnoticed:
only the manifest and the labels of an uncompressed bundle are text.
"""


def write_payload(directory: Path, relative: str, text: str) -> None:
    """Write one bundle payload, gzipping when the name says so."""
    target = directory / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.suffix == ".gz":
        # mtime=0 keeps the bytes reproducible: a regenerated bundle with unchanged content
        # must not show up as a diff just because the clock moved.
        target.write_bytes(gzip.compress(text.encode("utf-8"), mtime=0))
    else:
        target.write_text(text, encoding="utf-8", newline=NEWLINE)


def discover(root: Path) -> Iterator[tuple[str, Path]]:
    """Yield ``(name, directory)`` for every bundle under ``root``, in name order.

    A directory without a manifest is silently skipped rather than raising — the corpus root
    is a place people put things, and one stray folder should not break ``--list-corpora``.
    A directory *with* a broken manifest is a different matter and surfaces when loaded.
    """
    if not root.is_dir():
        return
    for directory in sorted(p for p in root.iterdir() if p.is_dir()):
        if (directory / MANIFEST_NAME).is_file():
            yield directory.name, directory


def manifest_json(manifest: CorpusManifest) -> str:
    return json.dumps(manifest.model_dump(mode="json"), indent=2) + "\n"


def source_formats(manifest: CorpusManifest) -> tuple[str, ...]:
    """The corpus's input shape: one format per position, in the order a spec must declare.

    Refuses a corpus whose cases disagree. A benchmark checks each case's inputs against *one*
    spec positionally (``runner._check_alignment``), so a corpus mixing shapes is unbenchmarkable
    by construction — and finding that out case-by-case mid-run is far worse than one message at
    load time. It also lets a caller compose a matching spec without decompressing any readings,
    which is what stops a picker offering a corpus it cannot run.
    """
    shapes = {tuple(given.source_format for given in case.inputs) for case in manifest.cases}
    if len(shapes) > 1:
        listed = " / ".join(", ".join(shape) for shape in sorted(shapes))
        raise BundleError(
            f"corpus {manifest.name!r} mixes input shapes ({listed}); every case must supply "
            "the same formats in the same order, or no single spec can benchmark it"
        )
    return next(iter(shapes))
