"""Asset discovery (ADR-0005): the inventory is populated by the data itself.

Precedence rule under test: discovery only fills absence; registration inserts
or updates and always beats discovery.
"""

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from iotshield.contracts import Asset, Criticality, Lane, NormalizedEvent
from iotshield.storage import DEFAULT_ORG_ID, repository

T0 = datetime(2026, 7, 7, 12, 0, 0, tzinfo=UTC)


def event_for(asset_id: str) -> NormalizedEvent:
    return NormalizedEvent(
        timestamp=T0,
        asset_id=asset_id,
        source_type=Lane.TELEMETRY,
        source_format="stub-csv",
        raw="r",
    )


def registration() -> Asset:
    return Asset(
        asset_id="gw-01",
        name="Plant gateway 1",
        asset_type="gateway",
        criticality=Criticality.HIGH,
        metadata={"owner": "ops"},
    )


def test_unknown_asset_gets_discovered_stub(session: Session) -> None:
    repository.insert_events(session, [event_for("gw-01")], org_id=DEFAULT_ORG_ID)
    (stub,) = repository.list_assets(session, org_id=DEFAULT_ORG_ID)
    assert stub.asset_id == "gw-01"
    assert stub.name == "gw-01"  # the id is the best name we have
    assert stub.asset_type == "unknown"
    assert stub.criticality == Criticality.MEDIUM  # neutral default, not LOW
    assert stub.metadata == {"auto_discovered": True}


def test_registration_enriches_discovered_stub(session: Session) -> None:
    repository.insert_events(session, [event_for("gw-01")], org_id=DEFAULT_ORG_ID)
    repository.upsert_assets(session, [registration()], org_id=DEFAULT_ORG_ID)
    (asset,) = repository.list_assets(session, org_id=DEFAULT_ORG_ID)
    assert asset == registration()  # stub fully replaced, flag gone


def test_discovery_never_overwrites_registration(session: Session) -> None:
    repository.upsert_assets(session, [registration()], org_id=DEFAULT_ORG_ID)
    repository.insert_events(session, [event_for("gw-01")], org_id=DEFAULT_ORG_ID)
    (asset,) = repository.list_assets(session, org_id=DEFAULT_ORG_ID)
    assert asset == registration()


def test_discovery_deduplicates_within_batch(session: Session) -> None:
    repository.insert_events(
        session, [event_for("gw-01"), event_for("gw-01")], org_id=DEFAULT_ORG_ID
    )
    assert len(repository.list_assets(session, org_id=DEFAULT_ORG_ID)) == 1
