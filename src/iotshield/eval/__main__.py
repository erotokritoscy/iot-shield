"""The evaluation CLI — the researcher's other command.

    uv run pytest                         # does my adapter FIT?    (the port suite)
    uv run python -m iotshield.eval ...   # is my adapter GOOD?     (this)

That symmetry is the point: the port suite already tells someone their adapter is legal, and
nothing told them whether it was any good. Both halves of the extensibility contract are now
one command each (ADR-0021).

    uv run python -m iotshield.eval --corpus demo_micro --spec config/bench/team3.yaml
    uv run python -m iotshield.eval --corpus skab_valve1 --fit-protocol prefix \
        --spec config/bench/team3.yaml --spec config/bench/stub.yaml --out reports/x.json
"""

import argparse
import sys
from pathlib import Path

from iotshield.eval.corpus import CORPORA, CorpusUnavailable, load_corpus
from iotshield.eval.fit import FitProtocol
from iotshield.eval.report import render
from iotshield.eval.runner import evaluate, spec_label
from iotshield.pipeline import load_spec

_DEFAULT_SPEC = Path("config/bench/team3.yaml")


def _list_corpora() -> str:
    lines = ["Registered corpora:"]
    for name, spec in sorted(CORPORA.items()):
        external = "  (needs --corpus-path)" if spec.needs_external_data else ""
        lines.append(f"  {name:<14} {spec.description}{external}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m iotshield.eval",
        description="Benchmark pipeline specs against a labelled corpus.",
    )
    parser.add_argument("--corpus", default="demo_micro", help="corpus name (default: demo_micro)")
    parser.add_argument(
        "--corpus-path", type=Path, default=None, help="root of an external dataset"
    )
    parser.add_argument(
        "--spec",
        type=Path,
        action="append",
        dest="specs",
        help="pipeline spec YAML to benchmark; repeat to compare (default: bench/team3.yaml)",
    )
    parser.add_argument(
        "--fit-protocol",
        type=FitProtocol,
        choices=list(FitProtocol),
        default=FitProtocol.NONE,
        help="how detectors learn 'normal' before judging (default: none)",
    )
    parser.add_argument("--out", type=Path, default=None, help="write the JSON report here")
    parser.add_argument(
        "--list-corpora", action="store_true", help="list registered corpora and exit"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.list_corpora:
        print(_list_corpora())
        return 0

    try:
        corpus = load_corpus(args.corpus, args.corpus_path)
    except CorpusUnavailable as exc:
        # Not an error in the user's usage — the dataset simply is not on this machine.
        # Exit 0 so a CI job that runs the harness over optional corpora stays green.
        print(f"skipped: {exc}", file=sys.stderr)
        return 0
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    paths = args.specs or [_DEFAULT_SPEC]
    try:
        specs = [(spec_label(path), load_spec(path)) for path in paths]
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    report = evaluate(corpus, specs, protocol=args.fit_protocol)
    print(render(report))

    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report.model_dump_json(indent=2), encoding="utf-8")
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
