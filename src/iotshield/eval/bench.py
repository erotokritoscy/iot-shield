"""The shipped benchmark specs, and which corpora each one can actually run on.

The missing half of the harness's vocabulary. A corpus declares its input shape and a spec
declares its sources, and until now *nothing recorded which pairs are legal* — six specs,
six corpora, roughly seven working cells, and the map lived only in YAML comments. So the
CLI's default benchmarked a pairing the documentation says cannot work, the dashboard could
not offer a committed spec at all, and both surfaces were left to guess.

The pairing is **derived, not declared**. A spec's ``sources`` and a corpus's
``source_formats`` must match positionally and by format — that is ``runner._check_alignment``
— so the legal set is computable from what both sides already publish. A hand-maintained
``corpus:`` field on each spec would say the same thing while being free to drift from the
thing it describes; this cannot.

The root follows ``corpus.corpus_root``'s pattern exactly — explicit argument, else
``IOTSHIELD_BENCH_PATH``, else the repository's own — because it is the same kind of fact:
*where files live* is a deployment property. Deriving it from ``__file__`` alone happens to
work for an editable install and a repo-shaped image, and fails silently everywhere else: a
non-editable wheel resolves to the site-packages parent, every listing comes back empty, and
the dashboard blames the corpus for a missing directory.
"""

import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from iotshield._paths import REPO_ROOT
from iotshield.eval.corpus import CorpusInfo
from iotshield.pipeline import PipelineSpec, load_spec

BENCH_PATH_ENV = "IOTSHIELD_BENCH_PATH"

DEFAULT_BENCH_ROOT = REPO_ROOT / "config" / "bench"
"""Bench specs ship inside the repository, beside the corpora they are written against."""


@dataclass(frozen=True)
class BenchSpec:
    """One committed spec, ready to benchmark, with the shape it demands of a corpus.

    ``spec`` is ``None`` exactly when ``error`` is set: the file is present but unreadable.
    Mirrors ``CorpusInfo``'s treatment of a broken bundle, so both catalogues answer "what is
    here, and what is wrong with it" rather than quietly shortening the list.
    """

    name: str
    spec: PipelineSpec | None
    error: str = ""

    @property
    def source_formats(self) -> tuple[str, ...]:
        if self.spec is None:
            return ()
        return tuple(source.source_format for source in self.spec.sources)

    @property
    def detectors(self) -> tuple[str, ...]:
        if self.spec is None:
            return ()
        return tuple(entry.name for entry in self.spec.detectors)

    def fits(self, source_formats: Sequence[str]) -> bool:
        """Whether a corpus of this shape can be benchmarked with this spec.

        A broken spec fits nothing — it is listed so its breakage is visible, not so a run
        can be attempted against a spec that does not parse.
        """
        if self.spec is None:
            return False
        return self.source_formats == tuple(source_formats)


def bench_root(root: Path | None = None) -> Path:
    """The directory bench specs live in: explicit argument, else env, else the repo's own."""
    if root is not None:
        return root
    configured = os.environ.get(BENCH_PATH_ENV, "").strip()
    return Path(configured) if configured else DEFAULT_BENCH_ROOT


def list_bench_specs(root: Path | None = None) -> list[BenchSpec]:
    """Every loadable spec under the bench root, in name order.

    A spec that does not parse is LISTED WITH ITS REASON rather than dropped: one broken file
    must not make the other five unlistable, but omitting it silently is worse than either.
    ``CorpusSummary.error`` already made this decision for bundles — "published rather than
    filtered out so a broken bundle is visibly broken instead of silently absent from the
    picker" — and specs got the opposite treatment.

    The old excuse was that a bad spec "will still fail loudly the moment someone asks for it
    by name". On the API/dashboard path nobody ever asks by name: the endpoint publishes the
    *parsed* spec and the page forwards the object, so a spec an operator broke while the
    server was running simply vanished from the picker — and if it was the only fitting one,
    the page blamed the corpus (ADR-0033).
    """
    directory = bench_root(root)
    if not directory.is_dir():
        return []
    specs: list[BenchSpec] = []
    for path in sorted(directory.glob("*.yaml")):
        try:
            specs.append(BenchSpec(name=path.stem, spec=load_spec(path)))
        except (ValueError, OSError) as exc:
            specs.append(BenchSpec(name=path.stem, spec=None, error=str(exc)))
    return specs


# There is deliberately no `load_bench_spec(name)`. A by-name loader needs a containment check
# and an availability message, and both already exist (`bundle.contained`, `load_corpus`) — but
# nothing addresses a spec by name: the CLI takes `--spec <path>` through `pipeline.load_spec`,
# and the API publishes the parsed `PipelineSpec` so a client forwards the object. A complete
# addressing scheme with no caller is a second, differently-worded security rule that nothing
# exercises. It arrives with its caller, if one ever appears.


def benchmarkable(infos: Sequence[CorpusInfo]) -> list[tuple[str, Sequence[str]]]:
    """The corpora a spec could actually run on, as ``(name, source formats)``.

    The filter is the point: a bundle that will not load has no input shape to match against,
    so including it would offer a pairing that cannot run. Two surfaces need this (the CLI's
    pairing table and ``/eval/specs``), and "remember to drop the broken ones" is not a rule to
    keep in two heads.
    """
    return [(info.name, info.source_formats) for info in infos if not info.error]


def render_pairings(infos: Sequence[CorpusInfo], root: Path | None = None) -> str:
    """The pairing table as text, for a CLI that has been asked to guess and will not."""
    specs = list_bench_specs(root)
    lines = ["Corpora and the shipped specs that fit them:"]
    for name, formats in benchmarkable(infos):
        fits = ", ".join(f"config/bench/{spec.name}.yaml" for spec in specs if spec.fits(formats))
        lines.append(f"  --corpus {name:<16} --spec {fits or '(none)'}")
    return "\n".join(lines)
