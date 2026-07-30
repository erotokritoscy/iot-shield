"""Batch pipeline runner: wires ports together per a YAML config, atomically.

CLI: python -m iotshield.pipeline --config <spec.yaml> [--input <file>]

The dashboard's Compose page is the other caller, and the usual one: it posts the same
PipelineSpec with the input as an uploaded payload rather than a path.
"""

from iotshield.pipeline.config import (
    PipelineConfig,
    PipelineSpec,
    SinkConfig,
    SourceSpec,
    config_from_spec,
    load_config,
    load_spec,
)
from iotshield.pipeline.runner import (
    NormalizedInputs,
    RunReport,
    build_detectors,
    compute_result,
    compute_run,
    execute_run,
    normalize_sources,
    run_pipeline,
)

__all__ = [
    "NormalizedInputs",
    "PipelineConfig",
    "PipelineSpec",
    "RunReport",
    "SinkConfig",
    "SourceSpec",
    "build_detectors",
    "compute_result",
    "compute_run",
    "config_from_spec",
    "execute_run",
    "load_config",
    "load_spec",
    "normalize_sources",
    "run_pipeline",
]
