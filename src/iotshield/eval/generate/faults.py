"""What can go wrong: a **name** and a description. Which lanes show it is *derived*.

A fault deliberately owns no rendering. The tempting design is for it to know how to damage
each modality — "a compromise means *these* flow records, *these* log lines, *this*
temperature curve" — and the cost of that is fatal to extensibility: adding a fourth lane
would mean going back and teaching *every existing fault* how to express itself in it.

So the direction is inverted. A fault is a name, and each **behaviour** decides what that name
looks like in its own world, by putting it in its handler table (``port.Behaviour.expresses``
is that table's keys). Adding a lane is then one new behaviour plus one new renderer, and no
fault changes.

That claim only holds because the lane set is *not stored here*. It was, briefly, and it made
one fact true in three places — this table, each behaviour's ``expresses``, and each
behaviour's dispatch — so a new lane expressing an existing fault had to edit a fault after
all, with a meta-test to catch it if you forgot. ``lanes_of`` asks the behaviours instead.

The three telemetry shapes are the ones ADR-0021 D5b chose, and the reason they are three is
that they *discriminate*: a fixed threshold catches two and is structurally blind to the
third. A corpus where every detector scores the same teaches nothing.
"""

from dataclasses import dataclass

from iotshield.contracts import Lane


@dataclass(frozen=True)
class Fault:
    """What a fault means. Which lanes show it is derived, not stored — see ``lanes_of``."""

    describe: str


def lanes_of(kind: str) -> frozenset[Lane]:
    """Which lanes can express a fault, asked of the behaviours that implement it.

    Imported lazily because ``port`` holds the registry while the behaviours registered in it
    import this module; the dependency genuinely runs that way, and only a caller asking the
    question pays for it.
    """
    from iotshield.eval.generate.port import BEHAVIOURS

    # A *lane*, deliberately, though the registry is keyed on (lane, payload): a label is
    # indexed by (asset, lane, instant) and carries no payload axis (ADR-0028 D1), so two
    # shapes on one lane answer this question with the same lane. That is also why the set
    # collapses rather than reporting metering separately — a consumption anomaly and a
    # temperature spike on one device are two projections that correlate.
    return frozenset(
        lane for (lane, _payload), behaviour in BEHAVIOURS.items() if kind in behaviour.expresses
    )


FAULTS: dict[str, Fault] = {
    # --- telemetry: the shapes that separate "compares to normal" from "compares to a
    # constant" (ADR-0021 D5b) ---
    "spike": Fault("short violent excursion, far past any threshold"),
    "drift": Fault("slow ramp ending past the threshold but never locally sudden"),
    "stuck": Fault(
        "sensor frozen at a plausible value: in range throughout, so a fixed threshold is blind"
    ),
    # --- flow: cardinality, not volume. `scan` is the Mirai propagation shape ADR-0024's
    # fan_out preset exists for; `port_sweep` is its discriminating sibling, invisible to
    # fan_out because the peer count stays low. ---
    "scan": Fault("sweep of many distinct peers on one port, mostly unanswered"),
    "port_sweep": Fault("many distinct ports on few peers: low fan-out, high port cardinality"),
    "exfiltration": Fault("sustained bulk transfer to a single external peer"),
    # --- logs: the two shapes ADR-0020's presets split on. `auth_storm` is INFO-severity on
    # purpose, so a severity floor cannot see it. ---
    "error_storm": Fault("burst of error-and-above log lines"),
    "auth_storm": Fault(
        "burst of authentication failures logged at INFO, invisible to a severity floor"
    ),
    # --- the multimodal one: the whole reason a trimodal corpus can exist. Three behaviours
    # express it, so its incident's `lanes` comes out as all three without anyone saying so. ---
    "compromise": Fault("a device scanning, failing its own integrity checks, and running hot"),
}
