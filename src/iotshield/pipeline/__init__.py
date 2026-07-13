"""Batch pipeline runner: wires ports together per a YAML config, atomically.

CLI: python -m iotshield.pipeline --input data/demo/demo.csv --config config/demo.yaml
"""

from iotshield.pipeline.config import (
    PipelineConfig,
    PipelineSpec,
    config_from_spec,
    load_config,
)
from iotshield.pipeline.runner import RunReport, execute_run, run_pipeline

__all__ = [
    "PipelineConfig",
    "PipelineSpec",
    "RunReport",
    "config_from_spec",
    "execute_run",
    "load_config",
    "run_pipeline",
]
