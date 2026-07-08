"""FastAPI read-side service: /health, /assets, /events, /detections, /alerts,
/alerts/{id}(/detections), /malformed, /summary.

Serve locally: IOTSHIELD_DATABASE_URL=sqlite:///demo.db python -m iotshield.api
"""

from iotshield.api.app import Summary, create_app

__all__ = ["Summary", "create_app"]
