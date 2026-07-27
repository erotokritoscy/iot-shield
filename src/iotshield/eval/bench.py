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

Specs are addressed by **name** (the file stem), never by path. A caller that names a path is
a caller that can read any file on the host (ADR-0018 §9-A), and this is reached from the API.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from iotshield.pipeline import PipelineSpec, load_spec

_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_BENCH_ROOT = _REPO_ROOT / "config" / "bench"
"""Bench specs ship inside the repository, beside the corpora they are written against."""


@dataclass(frozen=True)
class BenchSpec:
    """One committed spec, ready to benchmark, with the shape it demands of a corpus."""

    name: str
    spec: PipelineSpec

    @property
    def source_formats(self) -> tuple[str, ...]:
        return tuple(source.source_format for source in self.spec.sources)

    @property
    def detectors(self) -> tuple[str, ...]:
        return tuple(entry.name for entry in self.spec.detectors)

    def fits(self, source_formats: Sequence[str]) -> bool:
        """Whether a corpus of this shape can be benchmarked with this spec."""
        return self.source_formats == tuple(source_formats)


def bench_root(root: Path | None = None) -> Path:
    return root if root is not None else DEFAULT_BENCH_ROOT


def list_bench_specs(root: Path | None = None) -> list[BenchSpec]:
    """Every loadable spec under the bench root, in name order.

    A spec that does not parse is skipped rather than raising: this feeds a catalogue and a
    help message, and one broken file must not make the other five unlistable. It will still
    fail loudly, with its own message, the moment someone asks for it by name.
    """
    directory = bench_root(root)
    if not directory.is_dir():
        return []
    specs: list[BenchSpec] = []
    for path in sorted(directory.glob("*.yaml")):
        try:
            specs.append(BenchSpec(name=path.stem, spec=load_spec(path)))
        except (ValueError, OSError):
            continue
    return specs


def load_bench_spec(name: str, root: Path | None = None) -> PipelineSpec:
    """One shipped spec by name, or a message naming the ones that exist."""
    directory = bench_root(root)
    path = directory / f"{name}.yaml"
    # Resolved and contained for the reason `bundle.contained` gives: this name arrives from a
    # CLI flag and an API request, and `../../etc/passwd` is a name too.
    if path.resolve().parent != directory.resolve() or not path.is_file():
        available = ", ".join(spec.name for spec in list_bench_specs(root)) or "none"
        raise ValueError(f"unknown bench spec {name!r}; available: {available}")
    return load_spec(path)


def pairings(
    corpora: Sequence[tuple[str, Sequence[str]]], root: Path | None = None
) -> list[tuple[str, list[str]]]:
    """For each ``(corpus name, source formats)``, the shipped specs that can benchmark it.

    Corpora arrive as pairs rather than being read here, so this module never depends on the
    corpus root and the caller keeps whatever listing it already paid for.
    """
    specs = list_bench_specs(root)
    return [
        (name, [spec.name for spec in specs if spec.fits(formats)]) for name, formats in corpora
    ]


def render_pairings(corpora: Sequence[tuple[str, Sequence[str]]], root: Path | None = None) -> str:
    """The pairing table as text, for a CLI that has been asked to guess and will not."""
    lines = ["Corpora and the shipped specs that fit them:"]
    for name, spec_names in pairings(corpora, root):
        fits = ", ".join(f"config/bench/{spec}.yaml" for spec in spec_names) or "(none)"
        lines.append(f"  --corpus {name:<16} --spec {fits}")
    return "\n".join(lines)
