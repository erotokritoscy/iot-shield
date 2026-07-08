"""Streamlit entry script — the `streamlit run` (and AppTest) target.

Kept to an import + call so all logic lives in the importable module
(iotshield.dashboard.app), where tests can monkeypatch the client factory.
"""

from iotshield.dashboard.app import main

main()
