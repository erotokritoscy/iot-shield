"""CLI entry point: python -m iotshield.pipeline --input <file> --config <yaml>."""

import argparse
import sys
from pathlib import Path

from pydantic import ValidationError

from iotshield.pipeline.config import load_config
from iotshield.pipeline.runner import run_pipeline


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m iotshield.pipeline",
        description="Run the iotshield detection pipeline on an input file.",
    )
    parser.add_argument("--input", required=True, type=Path, help="Input data file")
    parser.add_argument("--config", required=True, type=Path, help="Pipeline YAML config")
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
        report = run_pipeline(config, args.input)
    except (ValidationError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(report.render())
    return 0


if __name__ == "__main__":
    sys.exit(main())
