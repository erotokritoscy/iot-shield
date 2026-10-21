"""The synthetic Biology attack (scripts/biology_fake_attack.py) tells the story it claims.

Its flows are rendered exactly as the ``biology`` connector renders a ``packets`` row, read by
the flow normalizer, and judged by the detectors the presentation uses — so the attack is known
to light up each chart before anyone inserts it into a real database.
"""

import importlib.util
from collections import Counter
from pathlib import Path
from types import ModuleType

from iotshield.connect.biology import BIOLOGY
from iotshield.connect.port import row
from iotshield.pipeline.registry import build_detector, build_normalizer

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "biology_fake_attack.py"


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("biology_fake_attack", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fake = _script()


def _as_pulled() -> str:
    """The fake flows as a Biology pull would deliver them (the connector's header and row)."""
    lines = [
        row(
            (
                fake.ATTACKER,
                dst,
                sport,
                dport,
                proto,
                when,
                duration,
                fwd,
                bwd,
                fwd_bytes,
                bwd_bytes,
            )
        )
        for dst, sport, dport, proto, when, duration, fwd, bwd, fwd_bytes, bwd_bytes in fake.flows()
    ]
    return BIOLOGY.header + "\n" + "\n".join(lines) + "\n"


def test_every_fake_flow_is_read_and_attributed_to_the_attacker() -> None:
    result = build_normalizer("flow").normalize(
        _as_pulled(), source_format="cicflowmeter-csv", origin="fake"
    )
    assert result.malformed == []
    assert len(result.events) == len(fake.flows()) == 60
    assert {event.asset_id for event in result.events} == {fake.ATTACKER}
    assert {event.timestamp.strftime("%Y-%m-%d") for event in result.events} == {"2023-07-22"}


def test_each_step_of_the_story_trips_its_detector() -> None:
    events = (
        build_normalizer("flow")
        .normalize(_as_pulled(), source_format="cicflowmeter-csv", origin="fake")
        .events
    )
    found = [
        *build_detector("rules", {"lane": "flow"}).detect(events),
        *(
            detection
            for preset in ("port_scan", "fan_out", "exfiltration")
            for detection in build_detector("flow_burst", {"preset": preset}).detect(events)
        ),
    ]
    by_method_hour = Counter((d.method, d.window_start.hour) for d in found)
    assert by_method_hour[("flow_burst:port_scan", 13)] == 1
    assert by_method_hour[("flow_burst:fan_out", 14)] == 1
    assert by_method_hour[("flow_burst:exfiltration", 15)] == 1
    origins = {d.evidence["enrichment"]["origin"] for d in found if d.method == "rules:flow"}
    assert {"rule:telnet-outbound-propagation", "rule:high-upload-volume"} <= origins


def test_remove_targets_only_the_marked_rows() -> None:
    sql = fake.remove_sql()
    assert f"src_ip = '{fake.ATTACKER}' AND created_at = '{fake.MARKER}'" in sql
    # insert clears an earlier copy first, so running it twice cannot double the attack.
    assert fake.insert_sql().startswith("DELETE FROM packets WHERE")
