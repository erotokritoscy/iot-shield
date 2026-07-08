"""Streamlit dashboard. Consumes the API only — never touches storage directly.

Run: uv run streamlit run src/iotshield/dashboard/app.py
"""

from iotshield.dashboard.client import ApiClient

__all__ = ["ApiClient"]
