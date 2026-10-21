"""Every shipped connector, by name — what a connection's ``connector:`` may say (ADR-0042)."""

from iotshield.connect.biology import BIOLOGY
from iotshield.connect.port import ConnectorSpec
from iotshield.connect.sensipro import SENSIPRO

CONNECTORS: dict[str, ConnectorSpec] = {spec.name: spec for spec in (SENSIPRO, BIOLOGY)}
