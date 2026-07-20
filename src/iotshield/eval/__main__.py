"""The evaluation CLI — the researcher's other command.

    uv run pytest                         # does my adapter FIT?    (the port suite)
    uv run python -m iotshield.eval ...   # is my adapter GOOD?     (this)

That symmetry is the point: the port suite already tells someone their adapter is legal, and
nothing told them whether it was any good. Both halves of the extensibility contract are now
one command each (ADR-0021).

    uv run python -m iotshield.eval --corpus synthetic --spec config/bench/team3.yaml
    uv run python -m iotshield.eval list
    uv run python -m iotshield.eval fetch skab
    uv run python -m iotshield.eval generate

Benchmarking is the default verb: bare flags with no subcommand run one, so the command a
researcher types most often is the shortest.
"""

import argparse
import sys
from pathlib import Path

from iotshield.eval.corpus import (
    FETCHABLE,
    CorpusUnavailable,
    corpus_root,
    list_corpora,
    load_corpus,
)
from iotshield.eval.fit import FitProtocol
from iotshield.eval.report import render
from iotshield.eval.runner import evaluate
from iotshield.pipeline import load_spec

_DEFAULT_SPEC = Path("config/bench/team3.yaml")
_SUBCOMMANDS = ("list", "fetch", "generate")


def _add_run_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--corpus", default="synthetic", help="corpus name (default: synthetic)")
    parser.add_argument("--corpus-path", type=Path, default=None, help="corpus root override")
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
    parser.add_argument(
        "--with-timeline",
        action="store_true",
        help="record per-case detection/alert spans in the report (larger; for charts)",
    )
    parser.add_argument("--out", type=Path, default=None, help="write the JSON report here")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m iotshield.eval",
        description="Benchmark pipeline specs against labelled corpora.",
    )
    subparsers = parser.add_subparsers(dest="command")
    _add_run_arguments(parser)

    listing = subparsers.add_parser("list", help="list available corpora")
    listing.add_argument("--corpus-path", type=Path, default=None)

    fetching = subparsers.add_parser("fetch", help="convert a third-party dataset into bundles")
    fetching.add_argument("dataset", help="dataset name, e.g. skab")
    fetching.add_argument("--source", type=Path, default=None, help="local checkout to convert")
    fetching.add_argument("--corpus-path", type=Path, default=None)

    generating = subparsers.add_parser("generate", help="(re)build the synthetic corpus bundle")
    generating.add_argument("--corpus-path", type=Path, default=None)
    return parser


def _list(args: argparse.Namespace) -> int:
    root = corpus_root(args.corpus_path)
    print(f"Corpora in {root}:")
    for info in list_corpora(args.corpus_path):
        print(f"  {info.name:<16} {info.cases:>3} case(s)  {info.description}")
    if not list_corpora(args.corpus_path):
        print("  (none — run: python -m iotshield.eval generate)")
    print("\nAvailable to convert locally (not redistributed):")
    for name, hint in sorted(FETCHABLE.items()):
        print(f"  {name:<16} {hint}")
    return 0


def _fetch(args: argparse.Namespace) -> int:
    from iotshield.eval.fetch import fetch

    root = corpus_root(args.corpus_path)
    try:
        written = fetch(args.dataset, root, args.source)
    except CorpusUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for name, cases in written:
        print(f"wrote {root / name}  ({cases} case(s))")
    return 0


def _generate(args: argparse.Namespace) -> int:
    from iotshield.eval.generate import write

    root = corpus_root(args.corpus_path)
    corpus = write(root / "synthetic")
    print(f"wrote {root / 'synthetic'}  ({len(corpus.cases)} cases, {corpus.incidents} incidents)")
    return 0


def _run(args: argparse.Namespace) -> int:
    try:
        corpus = load_corpus(args.corpus, args.corpus_path)
    except CorpusUnavailable as exc:
        # Not a usage error — the dataset simply is not on this machine. Exit 0 so a CI job
        # that benchmarks over optional corpora stays green on a fresh clone.
        print(f"skipped: {exc}", file=sys.stderr)
        return 0

    paths = args.specs or [_DEFAULT_SPEC]
    try:
        specs = [(path.stem, load_spec(path)) for path in paths]
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    report = evaluate(
        corpus, specs, protocol=args.fit_protocol, include_timeline=args.with_timeline
    )
    print(render(report))

    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report.model_dump_json(indent=2), encoding="utf-8")
        print(f"\nwrote {args.out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    # argparse cannot express "subcommand optional, else these flags"; deciding here keeps
    # the common case (`--corpus X --spec Y`) free of a mandatory `run` verb.
    if raw and raw[0] in _SUBCOMMANDS:
        args = parser.parse_args(raw)
        return {"list": _list, "fetch": _fetch, "generate": _generate}[args.command](args)
    args = parser.parse_args(raw)
    return _run(args)


if __name__ == "__main__":
    raise SystemExit(main())
