"""CLI entry point: python -m iotshield.pipeline --input <file> --config <yaml>."""

import argparse
import sys
from pathlib import Path

from pydantic import ValidationError

from iotshield.pipeline.config import load_config
from iotshield.pipeline.runner import run_pipeline
from iotshield.storage import DEFAULT_ORG_ID
from iotshield.storage.accounts import slugify


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m iotshield.pipeline",
        description="Run the iotshield detection pipeline on an input file.",
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=None,
        help="Input file for a single-source config; multi-source configs name a path per source",
    )
    parser.add_argument("--config", required=True, type=Path, help="Pipeline YAML config")
    parser.add_argument(
        "--org",
        default=DEFAULT_ORG_ID,
        help=(
            "Organization id the results are stored under (ADR-0039): the slug shown on the "
            f"dashboard's Users page. Created if absent. Default: {DEFAULT_ORG_ID!r}"
        ),
    )
    parser.add_argument(
        "--name",
        default=None,
        help=(
            "What the saved run is called on the dashboard's Runs page (ADR-0046). "
            "Default: the config file's name"
        ),
    )
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
        org_id = slugify(args.org)
        name = args.name or f"{args.config.name} (CLI)"
        report = run_pipeline(config, args.input, org_id=org_id, name=name)
    except (ValidationError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(report.render())
    return 0


if __name__ == "__main__":
    sys.exit(main())
