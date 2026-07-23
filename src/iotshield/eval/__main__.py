"""The evaluation CLI — the researcher's other command.

    uv run pytest                         # does my adapter FIT?    (the port suite)
    uv run python -m iotshield.eval ...   # is my adapter GOOD?     (this)

That symmetry is the point: the port suite already tells someone their adapter is legal, and
nothing told them whether it was any good. Both halves of the extensibility contract are now
one command each (ADR-0021).

    uv run python -m iotshield.eval --corpus synthetic --spec config/bench/team3.yaml
    uv run python -m iotshield.eval --fit-protocol prefix --sweep window_size=20,60,120
    uv run python -m iotshield.eval list
    uv run python -m iotshield.eval fetch skab
    uv run python -m iotshield.eval generate

Benchmarking is the default verb: bare flags with no subcommand run one, so the command a
researcher types most often is the shortest.
"""

import argparse
import sys
from pathlib import Path
from typing import Any

from iotshield.eval.corpus import (
    CorpusUnavailable,
    corpus_root,
    fetchable,
    list_corpora,
    load_corpus,
)
from iotshield.eval.fit import FitProtocol
from iotshield.eval.generate import CORPUS_NAME
from iotshield.eval.report import render
from iotshield.eval.runner import evaluate
from iotshield.eval.sweep import expand, values
from iotshield.pipeline import load_spec

_DEFAULT_SPEC = Path("config/bench/team3.yaml")


def _common_parser() -> argparse.ArgumentParser:
    """Flags every verb accepts, declared once and inherited via ``parents=``.

    Declaring --corpus-path per subparser instead let the root-level flag be silently
    overwritten by the subparser's own default, so `--corpus-path X list` ignored X.
    """
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--corpus-path", type=Path, default=None, help="corpus root override")
    return common


def _add_run_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--corpus", default=CORPUS_NAME, help="corpus name (default: synthetic)")
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
        "--sweep",
        action="append",
        dest="sweeps",
        metavar="NAME=V1,V2",
        help=(
            "vary a detector hyperparameter across values, expanding each spec into one "
            "run per combination; repeat for a cross-product (ADR-0022)"
        ),
    )
    parser.add_argument(
        "--with-timeline",
        action="store_true",
        help="record per-case detection/alert spans in the report (larger; for charts)",
    )
    parser.add_argument("--out", type=Path, default=None, help="write the JSON report here")


def build_parser() -> argparse.ArgumentParser:
    common = _common_parser()
    parser = argparse.ArgumentParser(
        prog="python -m iotshield.eval",
        description="Benchmark pipeline specs against labelled corpora.",
        parents=[common],
    )
    subparsers = parser.add_subparsers(dest="command")
    _add_run_arguments(parser)

    subparsers.add_parser("list", parents=[common], help="list available corpora")

    fetching = subparsers.add_parser(
        "fetch", parents=[common], help="convert a third-party dataset into bundles"
    )
    fetching.add_argument("dataset", help="dataset name, e.g. skab")
    fetching.add_argument("--source", type=Path, default=None, help="local checkout to convert")

    subparsers.add_parser("generate", parents=[common], help="(re)build the synthetic bundle")
    return parser


def _list(args: argparse.Namespace) -> int:
    root = corpus_root(args.corpus_path)
    print(f"Corpora in {root}:")
    infos = list_corpora(args.corpus_path)
    for info in infos:
        print(f"  {info.name:<16} {info.cases:>3} case(s)  {info.description}")
    if not infos:
        print("  (none — run: python -m iotshield.eval generate)")
    # fetchable() is keyed by corpus so load_corpus can match exactly; one dataset yields
    # several, so print each distinct instruction once rather than once per corpus.
    print("\nAvailable to convert locally (not redistributed):")
    for hint in sorted(set(fetchable().values())):
        print(f"  {hint}")
    return 0


def _fetch(args: argparse.Namespace) -> int:
    from iotshield.eval.fetch import fetch

    root = corpus_root(args.corpus_path)
    try:
        written = fetch(args.dataset, root, args.source)
    except (CorpusUnavailable, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for name, cases in written:
        print(f"wrote {root / name}  ({cases} case(s))")
    return 0


def _generate(args: argparse.Namespace) -> int:
    from iotshield.eval.generate import write

    directory = corpus_root(args.corpus_path) / CORPUS_NAME
    corpus = write(directory)
    print(f"wrote {directory}  ({len(corpus.cases)} cases, {corpus.incidents} incidents)")
    return 0


def _parse_sweeps(raw: list[str] | None) -> dict[str, list[Any]]:
    """``["window_size=20,60"]`` -> ``{"window_size": [20, 60]}``."""
    parsed: dict[str, list[Any]] = {}
    for entry in raw or []:
        name, separator, text = entry.partition("=")
        if not separator or not name.strip():
            raise ValueError(f"--sweep expects NAME=V1,V2 but got {entry!r}")
        parsed[name.strip()] = values(text)
    return parsed


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
        specs = expand([(path.stem, load_spec(path)) for path in paths], _parse_sweeps(args.sweeps))
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        report = evaluate(
            corpus, specs, protocol=args.fit_protocol, include_timeline=args.with_timeline
        )
    except ValueError as exc:
        # Reached by a spec/corpus mismatch and by a construction refusal — notably
        # sweeping a hyperparameter of a spec that names an artifact (ADR-0022: an artifact
        # is atomic). Both are usage errors with actionable messages, not tracebacks.
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(render(report))

    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report.model_dump_json(indent=2), encoding="utf-8")
        print(f"\nwrote {args.out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    # `command` is None when no verb was given, which is the benchmark case — so the command
    # a researcher types most often stays the shortest, with no mandatory `run` verb.
    handlers = {"list": _list, "fetch": _fetch, "generate": _generate}
    return handlers.get(args.command, _run)(args)


if __name__ == "__main__":
    raise SystemExit(main())
