"""The shipped corpora, as data.

Adding a corpus is adding a ``Scenario`` here — no machinery, no registry function, no loader.
And because corpora are *discovered* on disk once written (ADR-0021 D5), the new bundle then
appears in ``eval list``, in ``/eval/corpora``, and in the dashboard's picker with no change to
any of them.
"""

from iotshield.eval.generate.flow import SOURCE_FORMAT as FLOW_CSV
from iotshield.eval.generate.world import Clock, Device, FaultSpec, Recording, Scenario
from iotshield.normalize.stub import SOURCE_FORMAT as STUB_CSV
from iotshield.normalize.syslog import SOURCE_FORMAT as SYSLOG

RIG = "synthetic-rig"


def _telemetry_recordings() -> tuple[Recording, ...]:
    """Three fault shapes x three recordings, plus two clean ones.

    Deliberately more than one recording per shape: a single example per shape would let a
    detector look good or bad by luck, and per-case variance is one of the things the harness
    exists to show (a mean hides the 3-of-16 pattern SKAB revealed).

    The offsets and lengths vary per repeat so the three are not the same experiment three
    times — a later fault start is a shorter clean prefix, which is what the ``prefix`` fit
    protocol has to cope with.
    """
    rig = (Device(asset_id=RIG, kind="sensor", role="test rig"),)
    clock = Clock()
    recordings: list[Recording] = []
    for kind in ("spike", "drift", "stuck"):
        for repeat in range(3):
            recordings.append(
                Recording(
                    name=f"{kind}-{repeat}",
                    clock=clock,
                    devices=rig,
                    faults=(
                        FaultSpec(
                            kind=kind,
                            asset_id=RIG,
                            start_step=300 + repeat * 60,
                            length_steps=120 + repeat * 30,
                        ),
                    ),
                )
            )
    recordings.extend(
        Recording(name=f"clean-{repeat}", clock=clock, devices=rig) for repeat in range(2)
    )
    return tuple(recordings)


SYNTHETIC = Scenario(
    name="synthetic",
    description="Synthetic rig telemetry: spike / drift / stuck faults + clean baselines",
    formats=(STUB_CSV,),
    recordings=_telemetry_recordings(),
    seed=1000,
)

CAMERA = "10.0.0.24"
GATEWAY = "10.0.0.11"
SERVER = "10.0.0.9"
RESOLVER = "10.0.0.1"
MIRROR = "10.0.0.53"


def _fleet(*extra: Device) -> tuple[Device, ...]:
    """The standing fleet every flow recording shares, plus whatever the case adds.

    The four named devices are not scenery — each one is a *control* the flow detector's
    presets make a claim about, and none has ever been measured:

    - ``GATEWAY`` sends thirty flows to ONE destination. Volume is not fan-out.
    - ``SERVER`` is dialled *into* by many peers. Inbound is not fan-out.
    - ``RESOLVER`` is legitimately chatty, sitting just *under* the 20-peer boundary.
    - ``MIRROR`` is legitimately chatty *above* it, so the corpus measures what a fleet-wide
      constant costs instead of confirming it. ADR-0024's Consequences predicted this false
      alarm; here it becomes a number.
    """
    standing = (
        Device(asset_id=GATEWAY, kind="gateway", role="uplink, high volume to one peer"),
        Device(asset_id=SERVER, kind="server", role="inbound-heavy jump host"),
        Device(asset_id=RESOLVER, kind="resolver", role="chatty, just under fan_out"),
        Device(asset_id=MIRROR, kind="mirror", role="chatty, deliberately over fan_out"),
    )
    quiet = tuple(
        Device(asset_id=f"10.0.0.{host}", kind="sensor", role="quiet neighbour")
        for host in range(30, 40)
    )
    return (*standing, *quiet, *extra)


def _flow_recordings() -> tuple[Recording, ...]:
    clock = Clock()
    camera = Device(asset_id=CAMERA, kind="camera", role="IP camera")
    second = Device(asset_id="10.0.0.25", kind="camera", role="second IP camera")

    def sweep(kind: str, asset_id: str, start: int, peers: int) -> FaultSpec:
        return FaultSpec(
            kind=kind, asset_id=asset_id, start_step=start, length_steps=12, intensity=peers
        )

    return (
        # One scanner among a realistic fleet. The measurement is the fleet, not the scanner.
        Recording(
            name="scan-one",
            clock=clock,
            devices=_fleet(camera),
            faults=(sweep("scan", CAMERA, 300, 35),),
        ),
        # Two scanners at different times: the first queue where the ranking metrics measure
        # something real, because true and false alerts come from *different devices*.
        Recording(
            name="scan-two",
            clock=clock,
            devices=_fleet(camera, second),
            faults=(sweep("scan", CAMERA, 200, 40), sweep("scan", "10.0.0.25", 600, 28)),
        ),
        # The discriminating pair: fan_out is structurally blind to a port sweep, exactly as a
        # fixed threshold is blind to a stuck sensor.
        Recording(
            name="port-sweep",
            clock=clock,
            devices=_fleet(camera),
            faults=(
                FaultSpec(
                    kind="port_sweep",
                    asset_id=CAMERA,
                    start_step=300,
                    length_steps=20,
                    intensity=40,
                ),
            ),
        ),
        # Bulk out to one peer: neither cardinality shape can see it.
        Recording(
            name="exfiltration",
            clock=clock,
            devices=_fleet(camera),
            faults=(
                FaultSpec(
                    kind="exfiltration",
                    asset_id=CAMERA,
                    start_step=200,
                    length_steps=400,
                    intensity=60,
                ),
            ),
        ),
        # No faults at all: every alert here is a false alarm, at fleet scale.
        Recording(name="clean", clock=clock, devices=_fleet(camera)),
    )


FLEET_SCAN = Scenario(
    name="fleet_scan",
    description=(
        "Synthetic flow captures: a fleet with scanning, port-sweeping and exfiltrating "
        "devices plus volume/direction/cardinality controls and a clean recording"
    ),
    formats=(FLOW_CSV,),
    recordings=_flow_recordings(),
    seed=3000,
)


def _trimodal_recordings() -> tuple[Recording, ...]:
    """One device seen three ways, plus single-lane decoys so the queue can be re-ranked.

    The design point that makes this corpus non-vacuous. Corroboration multiplies an alert's
    priority — it never creates or removes one — so the only metrics that can move are the
    *ranking* ones. With one device and one incident the true alert is rank 1 in both arms and
    the corpus would pass its own test while measuring nothing.

    So the fleet carries devices that raise **single-lane** alerts of their own, at other times:
    the chatty mirror on flow, and a device with an error storm on logs. The question the A/B
    then asks is whether judging three modalities lifts the genuinely three-lane incident above
    that noise.
    """
    clock = Clock()
    camera = Device(asset_id=CAMERA, kind="camera", role="compromised IP camera")
    gateway = Device(asset_id=GATEWAY, kind="gateway", role="uplink")
    mirror = Device(asset_id=MIRROR, kind="mirror", role="chatty update mirror (flow decoy)")
    fleet = (camera, gateway, mirror)

    def compromise(start: int) -> FaultSpec:
        # One fault, three lanes: the flow half is a sweep, the log half an error storm, the
        # telemetry half a modest thermal rise. Each is arguable alone — which is the point.
        return FaultSpec(
            kind="compromise", asset_id=CAMERA, start_step=start, length_steps=40, intensity=30
        )

    return (
        Recording(name="compromise", clock=clock, devices=fleet, faults=(compromise(400),)),
        # A log-only decoy: a real burst on a device that is NOT compromised, so it is a
        # genuine single-lane alert competing for the top of the queue.
        Recording(
            name="compromise-with-log-decoy",
            clock=clock,
            devices=fleet,
            faults=(
                compromise(500),
                FaultSpec(
                    kind="error_storm",
                    asset_id=GATEWAY,
                    start_step=100,
                    length_steps=40,
                    intensity=8,
                ),
            ),
        ),
        Recording(name="clean", clock=clock, devices=fleet),
    )


TRIMODAL = Scenario(
    name="device_trimodal",
    description=(
        "One device observed on telemetry, syslog and flow at once: a three-lane compromise "
        "against single-lane decoys, for the corroboration A/B"
    ),
    # Order is the shape any bench spec must declare in its `sources` (positional alignment).
    formats=(STUB_CSV, SYSLOG, FLOW_CSV),
    recordings=_trimodal_recordings(),
    seed=5000,
)

SCENARIOS: dict[str, Scenario] = {
    scenario.name: scenario for scenario in (SYNTHETIC, FLEET_SCAN, TRIMODAL)
}

DEFAULT_SCENARIO = SYNTHETIC.name
"""What ``--corpus`` defaults to: the one corpus a fresh clone is guaranteed to have."""
