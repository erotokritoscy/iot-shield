"""team3 telemetry anomaly detector (ADR-0012).

Robust z-score over engineered rolling/lag/FFT features. The model + feature math
are vendored from ``reference/team3-telemetry-ad`` with provenance; the pivot,
windowing, artifact loading, thresholding, and episode merging are rewritten
against our Detector port. Model parameters ship as a pure-JSON artifact.
"""

from iotshield.detect.team3.detector import Team3ZScoreDetector

__all__ = ["Team3ZScoreDetector"]
