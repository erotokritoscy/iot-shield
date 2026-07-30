"""The shipped corpora, as data.

Adding a corpus is adding a ``Scenario`` here — no machinery, no registry function, no loader.
And because corpora are *discovered* on disk once written (ADR-0021 D5), the new bundle then
appears in ``eval list``, in ``/eval/corpora``, and in the dashboard's picker with no change to
any of them.
"""

from iotshield.detect.meter.params import DEFAULT_MIN_TRAIN_HOURS
from iotshield.eval.fit import TRAIN_FRACTION
from iotshield.eval.generate.flow import SOURCE_FORMAT as FLOW_CSV
from iotshield.eval.generate.meter import fitting_hours, hours
from iotshield.eval.generate.world import Clock, Device, FaultSpec, Recording, Scenario
from iotshield.normalize.meter import SENSIPRO_HOURLY_CSV
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
    # Multiples of `stub_threshold`'s boundary of 80 (ADR-0028 D6), chosen to reproduce the
    # magnitudes this corpus was calibrated around when they were hard-coded in the handlers:
    # the spike peaked at 95 (1.19x) and the drift ended at 84 (1.05x). `stuck` has no magnitude
    # — it freezes the channel wherever it happens to be — so its intensity is never read.
    over = {"spike": 1.2, "drift": 1.05, "stuck": 1.0}
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
                            intensity=over[kind],
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

    - ``GATEWAY`` sends twenty flows per minute to ONE destination (300 per recording).
      Volume is not fan-out. The 'thirty' this once claimed belongs to data/demo/demo_flow.log.
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

    def sweep(kind: str, asset_id: str, start: int, over: float) -> FaultSpec:
        # `over` is multiples of the shape's own detector boundary (ADR-0028 D6), so 1.75 is
        # "75% past fan-out's 20 peers" = 35 peers. Written this way so the two scanners below
        # differ in a comparable quantity rather than in raw peer counts that mean nothing
        # without knowing the threshold they are being read against. The span is wide enough to
        # hold the burst at one flow per second and still fall inside fan-out's one-minute
        # window, which is the property that makes it read as fan-out at all.
        return FaultSpec(
            kind=kind, asset_id=asset_id, start_step=start, length_steps=40, intensity=over
        )

    return (
        # One scanner among a realistic fleet. The measurement is the fleet, not the scanner.
        Recording(
            name="scan-one",
            clock=clock,
            devices=_fleet(camera),
            faults=(sweep("scan", CAMERA, 300, 1.75),),
        ),
        # Two scanners at different times: the first queue where the ranking metrics measure
        # something real, because true and false alerts come from *different devices*.
        Recording(
            name="scan-two",
            clock=clock,
            devices=_fleet(camera, second),
            faults=(sweep("scan", CAMERA, 200, 2.0), sweep("scan", "10.0.0.25", 600, 1.4)),
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
                    length_steps=40,
                    # 2.7x the port-scan boundary of 15 ports = 41 ports, on ONE peer, so the
                    # peer count stays at 1 and fan-out cannot see it.
                    intensity=2.7,
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
                    # 1.44x the 50 MB / 30 min boundary, which the behaviour turns into however
                    # many fixed-size chunks that takes.
                    intensity=1.44,
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
        # telemetry half a thermal rise. **Each is deliberately marginal** — 1.3x each lane's own
        # alarm line — which is what makes the corroboration A/B measure corroboration rather
        # than a spike anyone would catch.
        #
        # That claim used to be false and nothing could see it. `intensity` was dimensioned, so
        # the 30 this needed for 30 flow peers also meant "multiply the thermal rise by 30" and
        # drove the camera to 316.7 degrees against a threshold of 80; the telemetry-only arm
        # caught the fault outright, and the experiment compared corroboration against an
        # already-solved problem. A dimensionless intensity (ADR-0028 D6) plus the meta-test in
        # tests/eval/generate/test_scenarios.py is what keeps the three halves comparable.
        return FaultSpec(
            kind="compromise", asset_id=CAMERA, start_step=start, length_steps=40, intensity=1.3
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
                    # 1.6x log_burst's min_count of 5 = 8 lines: comfortably a real burst, so the
                    # decoy competes for the top of the queue on its own merits.
                    intensity=1.6,
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

WATER_METER = "meter-water-04"
POWER_METER = "meter-power-11"


def _meter_recordings() -> tuple[Recording, ...]:
    """One recording per fault shape plus a clean one, on a water meter and a power meter.

    ## Why these recordings are about 31 weeks long

    Every other corpus here is minutes of data. ``meter_iforest`` cannot be fitted from minutes:
    it needs ``DEFAULT_MIN_TRAIN_HOURS`` of clean history — itself derived from ``fp_budget``,
    715 rows for the quantile plus a week to warm the rolling window — and ``--fit-protocol
    prefix`` only reserves the first ``TRAIN_FRACTION`` of the clock. The length is therefore
    *computed* from those two numbers rather than typed, so it stays correct if either moves.
    Sizing it by hand is how a corpus ends up one row short of fittable and reports a table of
    skips that reads like a broken detector.

    That length is also why there is one recording per shape rather than ``synthetic``'s three.
    Per-case variance is worth having and this trades it for a clock long enough to fit at the
    product's real operating point, which was the more important of the two: a corpus that
    cannot be fitted measures nothing at all.

    Each fault lands on one meter while the other is **asserted clean** in the same recording,
    which is what makes a false alarm on the quiet meter countable (ADR-0021's roster). They
    alternate, so each meter is both subject and control across the corpus, and the two
    utilities exercise the one-model-per-meter law that ADR-0030 D7 built the adapter around.
    """
    water = Device(asset_id=WATER_METER, kind="water_meter", role="district water meter")
    power = Device(asset_id=POWER_METER, kind="power_meter", role="building supply meter")
    meters = (water, power)
    clock = hours(fitting_hours(TRAIN_FRACTION, DEFAULT_MIN_TRAIN_HOURS))
    # Multiples of "3x what this hour of the week normally draws" (meter.CALIBRATION). The spike
    # sits above the line and the drift ends on it, mirroring `synthetic`; `stuck` freezes the
    # counter and has no magnitude, so its intensity is never read.
    over = {"spike": 1.3, "drift": 1.0, "stuck": 1.0}
    # Comfortably past the reserved prefix: `prefix` caps training at the asset's FIRST fault,
    # so a fault inside the reserved fraction would shorten the very history it needs.
    start = int(clock.steps * 0.65)
    return (
        *(
            Recording(
                name=f"{kind}-{subject.asset_id.rsplit('-', 1)[-1]}",
                clock=clock,
                devices=meters,
                faults=(
                    FaultSpec(
                        kind=kind,
                        asset_id=subject.asset_id,
                        start_step=start + offset,
                        # Long enough to be an episode rather than one odd hour: a leak or a
                        # stalled register lasts days, and `min_episode_hours` means a
                        # single-hour excursion is reportable but not what this measures.
                        length_steps=72,
                        intensity=over[kind],
                    ),
                ),
            )
            for offset, (kind, subject) in enumerate(
                (("spike", water), ("drift", power), ("stuck", water))
            )
        ),
        Recording(name="clean", clock=clock, devices=meters),
    )


METER_FLEET = Scenario(
    name="meter_fleet",
    description=(
        "Hourly utility metering on a water and an electricity meter: a consumption surge, a "
        "developing leak and a frozen register against an hour-of-week profile"
    ),
    formats=(SENSIPRO_HOURLY_CSV.name,),
    recordings=_meter_recordings(),
    seed=7000,
)

SCENARIOS: dict[str, Scenario] = {
    scenario.name: scenario for scenario in (SYNTHETIC, FLEET_SCAN, TRIMODAL, METER_FLEET)
}

# There is deliberately no `DEFAULT_SCENARIO`. It described itself as "what `--corpus` defaults
# to", which stopped being true when both benchmark flags became required (`eval.__main__`):
# the old defaults benchmarked `synthetic` with team3, the one pairing that cannot work. It
# survived only as `write()`'s default argument, which nothing passed — so it was a constant
# whose only remaining job was to be stale.
