"""Batch pipeline runner: wires ports together per a YAML config, atomically.

CLI: python -m iotshield.pipeline --input data/demo/demo.csv --config config/demo.yaml
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
