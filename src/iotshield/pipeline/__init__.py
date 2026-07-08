"""Batch pipeline runner: wires ports together per a YAML config, atomically.

CLI: python -m iotshield.pipeline --input data/demo/demo.csv --config config/demo.yaml
"""

from iotshield.pipeline.config import PipelineConfig, load_config
from iotshield.pipeline.runner import RunReport, run_pipeline

__all__ = ["PipelineConfig", "RunReport", "load_config", "run_pipeline"]
