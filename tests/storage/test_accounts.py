"""Account persistence (ADR-0039): organizations, users, sessions, and tenant scoping."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from iotshield.contracts import Lane, NormalizedEvent, Severity
from iotshield.storage import DEFAULT_ORG_ID, accounts, repository
from iotshield.storage.accounts import AccountConflict, ProtectedAccount
from iotshield.storage.orm import (
    AlertDetectionRow,
    AlertRow,
    AssetRow,
    DetectionRow,
    EventRow,
    MalformedRow,
    SessionRow,
    UserRow,
)

HASH = "scrypt$1$1$1$AA==$AA=="  # storage never inspects it


def register(session: Session, org: str = "Acme Water", user: str = "alice") -> accounts.Account:
    return accounts.register_organization(session, name=org, username=user, password_hash=HASH)


class TestSlugify:
    @pytest.mark.parametrize(
        ("name", "slug"),
        [
            ("Acme Water", "acme-water"),
            ("  ACME   water!! ", "acme-water"),
            ("Ünïcode Örg 2", "n-code-rg-2"),
            ("a" * 80, "a" * 64),
        ],
    )
    def test_a_name_becomes_a_path_safe_id(self, name: str, slug: str) -> None:
        assert accounts.slugify(name) == slug

    @pytest.mark.parametrize("name", ["", "   ", "!!!", "../.."])
    def test_a_name_with_nothing_usable_is_refused(self, name: str) -> None:
        with pytest.raises(ValueError, match="letter or digit"):
            accounts.slugify(name)


class TestRegistration:
    def test_registering_creates_the_org_and_its_admin(self, session: Session) -> None:
        account = register(session)
        assert account.organization == accounts.Organization("acme-water", "Acme Water")
        assert account.user.role == "admin" and account.is_admin
        assert account.user.username == "alice"
        assert accounts.list_users(session, org_id="acme-water") == [account.user]

    def test_an_org_name_that_maps_to_a_taken_id_conflicts(self, session: Session) -> None:
        register(session, "Acme Water", "alice")
        with pytest.raises(AccountConflict, match="already exists"):
            register(session, "acme  WATER", "bob")

    def test_usernames_are_unique_across_orgs_and_case(self, session: Session) -> None:
        register(session, "Acme", "alice")
        with pytest.raises(AccountConflict, match="taken"):
            register(session, "Globex", "ALICE")


class TestUsers:
    def test_add_list_and_delete_within_an_org(self, session: Session) -> None:
        admin = register(session)
        bob = accounts.add_user(
            session, org_id=admin.org_id, username="Bob", password_hash=HASH, role="member"
        )
        assert bob.username == "bob"
        assert [u.username for u in accounts.list_users(session, org_id=admin.org_id)] == [
            "alice",
            "bob",
        ]
        assert accounts.count_admins(session, org_id=admin.org_id) == 1
        assert accounts.delete_user(session, bob.user_id, org_id=admin.org_id)
        assert accounts.get_user(session, bob.user_id, org_id=admin.org_id) is None

    def test_another_orgs_user_is_not_found_and_not_deleted(self, session: Session) -> None:
        acme = register(session, "Acme", "alice")
        globex = register(session, "Globex", "gina")
        assert accounts.get_user(session, globex.user.user_id, org_id=acme.org_id) is None
        assert not accounts.delete_user(session, globex.user.user_id, org_id=acme.org_id)
        assert accounts.get_user(session, globex.user.user_id, org_id=globex.org_id) is not None

    def test_credentials_are_found_case_insensitively(self, session: Session) -> None:
        register(session)
        found = accounts.credentials_for(session, "  ALICE ")
        assert found is not None and found[1] == HASH
        assert accounts.credentials_for(session, "nobody") is None


class TestSessions:
    def test_a_token_resolves_to_its_account_until_it_ends(self, session: Session) -> None:
        account = register(session)
        token = accounts.create_session(session, account.user.user_id)
        assert accounts.resolve_session(session, token) == account
        accounts.end_session(session, token)
        assert accounts.resolve_session(session, token) is None

    def test_only_the_tokens_hash_is_stored(self, session: Session) -> None:
        account = register(session)
        token = accounts.create_session(session, account.user.user_id)
        stored = session.scalars(select(SessionRow.token_hash)).all()
        assert token not in stored and len(stored) == 1 and len(stored[0]) == 64

    def test_an_expired_session_does_not_resolve_and_is_purged(self, session: Session) -> None:
        account = register(session)
        token = accounts.create_session(session, account.user.user_id, ttl=timedelta(hours=1))
        later = datetime.now(UTC) + timedelta(hours=2)
        assert accounts.resolve_session(session, token, now=later) is None
        accounts.purge_expired_sessions(session, now=later)
        assert session.scalars(select(SessionRow)).all() == []

    def test_an_unknown_token_resolves_to_nobody(self, session: Session) -> None:
        register(session)
        assert accounts.resolve_session(session, "not-a-token") is None

    def test_deleting_a_user_ends_their_sessions(self, session: Session) -> None:
        admin = register(session)
        bob = accounts.add_user(
            session, org_id=admin.org_id, username="bob", password_hash=HASH, role="member"
        )
        token = accounts.create_session(session, bob.user_id)
        accounts.delete_user(session, bob.user_id, org_id=admin.org_id)
        assert accounts.resolve_session(session, token) is None

    def test_ending_other_sessions_keeps_the_current_one(self, session: Session) -> None:
        account = register(session)
        keep = accounts.create_session(session, account.user.user_id)
        other = accounts.create_session(session, account.user.user_id)
        accounts.end_other_sessions(session, account.user.user_id, keep_token=keep)
        assert accounts.resolve_session(session, keep) is not None
        assert accounts.resolve_session(session, other) is None


def _event(asset_id: str, value: float) -> NormalizedEvent:
    return NormalizedEvent(
        timestamp=datetime(2026, 7, 7, 12, 0, tzinfo=UTC),
        asset_id=asset_id,
        source_type=Lane.TELEMETRY,
        source_format="stub-csv",
        metric="Pressure",
        value=value,
        severity=Severity.INFO,
        raw=f"{asset_id},{value}",
    )


class TestTenantScoping:
    """The repository half of isolation: one asset id, two organizations, no crossover."""

    def test_two_orgs_may_own_the_same_asset_id(self, session: Session) -> None:
        repository.ensure_organization(session, "globex")
        repository.insert_events(session, [_event("pump-02", 1.0)], org_id=DEFAULT_ORG_ID)
        repository.insert_events(session, [_event("pump-02", 2.0)], org_id="globex")

        mine = repository.get_events(session, org_id=DEFAULT_ORG_ID)
        theirs = repository.get_events(session, org_id="globex")
        assert [e.value for e in mine] == [1.0]
        assert [e.value for e in theirs] == [2.0]
        assert [a.asset_id for a in repository.list_assets(session, org_id="globex")] == ["pump-02"]
        assert repository.count_summary(session, org_id="globex")["events"] == 1

    def test_an_org_that_owns_nothing_sees_nothing(self, session: Session) -> None:
        repository.insert_events(session, [_event("pump-02", 1.0)], org_id=DEFAULT_ORG_ID)
        repository.ensure_organization(session, "empty")
        assert list(repository.get_events(session, org_id="empty")) == []
        assert set(repository.count_summary(session, org_id="empty").values()) == {0}

    def test_ensure_organization_never_renames_an_existing_one(self, session: Session) -> None:
        register(session, "Acme Water")
        repository.ensure_organization(session, "acme-water", "something else")
        assert accounts.get_organization(session, "acme-water") == accounts.Organization(
            "acme-water", "Acme Water"
        )


class TestSuperAdmin:
    def test_a_super_admin_belongs_to_no_org_and_resolves(self, session: Session) -> None:
        root = accounts.create_superadmin(session, username="Root", password_hash=HASH)
        assert (root.username, root.role, root.org_id) == ("root", "superadmin", None)
        account = accounts.resolve_session(session, accounts.create_session(session, root.user_id))
        assert account is not None and account.is_superadmin and account.organization is None
        with pytest.raises(ValueError, match="no organization"):
            _ = account.org_id
        assert accounts.list_superadmins(session) == [root]

    def test_a_super_admin_may_have_a_home_org_and_then_resolves_into_it(
        self, session: Session
    ) -> None:
        ucy = register(session, "UCY", "ucy-admin")
        root = accounts.create_superadmin(
            session, username="admin@admin.com", password_hash=HASH, org_id=ucy.org_id
        )
        token = accounts.create_session(session, root.user_id)
        account = accounts.resolve_session(session, token)
        assert account is not None and account.is_superadmin and account.is_admin
        assert account.org_id == "ucy"
        # It is listed among UCY's users, but is not one of UCY's admins.
        assert "admin@admin.com" in [u.username for u in accounts.list_users(session, org_id="ucy")]
        assert accounts.count_admins(session, org_id="ucy") == 1

    def test_the_table_refuses_an_org_user_without_an_org(self, session: Session) -> None:
        from uuid import uuid4

        session.add(
            UserRow(
                user_id=uuid4(),
                org_id=None,
                username="orphan",
                password_hash=HASH,
                role="admin",
                created_at=datetime.now(UTC),
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()


class TestOrganizationManagement:
    def test_summaries_count_users_admins_and_data(self, session: Session) -> None:
        acme = register(session, "Acme", "alice")
        accounts.add_user(
            session, org_id=acme.org_id, username="bob", password_hash=HASH, role="member"
        )
        repository.insert_events(session, [_event("pump-02", 1.0)], org_id=acme.org_id)
        summary = accounts.organization_summary(session, acme.org_id)
        assert summary is not None
        assert (summary.users, summary.admins, summary.events, summary.alerts) == (2, 1, 1, 0)
        assert accounts.organization_summary(session, "nope") is None

    def test_suspending_ends_sessions_and_hides_them(self, session: Session) -> None:
        acme = register(session)
        token = accounts.create_session(session, acme.user.user_id)
        assert accounts.set_suspended(session, acme.org_id, suspended=True) is not None
        assert accounts.resolve_session(session, token) is None
        assert session.scalars(select(SessionRow)).all() == []
        # Even a session created afterwards (which login refuses to do) would not resolve.
        late = accounts.create_session(session, acme.user.user_id)
        assert accounts.resolve_session(session, late) is None
        accounts.set_suspended(session, acme.org_id, suspended=False)
        assert accounts.resolve_session(session, late) is not None

    def test_delete_removes_every_row_of_that_org_and_only_that_org(self, session: Session) -> None:
        from iotshield.contracts import AlertSeverity, DetectionResult, PrioritizedAlert

        def seed(org_id: str) -> None:
            event = _event("pump-02", 1.0)
            detection = DetectionResult(
                asset_id="pump-02",
                window_start=event.timestamp,
                window_end=event.timestamp,
                lane=Lane.TELEMETRY,
                method="test",
                score=2.0,
                threshold=1.0,
                evidence={"explanation": "x"},
            )
            alert = PrioritizedAlert(
                asset_id="pump-02",
                window_start=event.timestamp,
                window_end=event.timestamp,
                severity=AlertSeverity.HIGH,
                priority_score=50.0,
                threat_class="unknown",
                rationale="r",
                detection_ids=[detection.detection_id],
            )
            repository.insert_events(session, [event], org_id=org_id)
            repository.insert_detections(session, [detection], org_id=org_id)
            repository.insert_alerts(session, [alert], org_id=org_id)

        acme = register(session, "Acme", "alice")
        globex = register(session, "Globex", "gina")
        seed(acme.org_id)
        seed(globex.org_id)
        accounts.create_session(session, acme.user.user_id)

        assert accounts.delete_organization(session, acme.org_id)
        assert accounts.get_organization(session, acme.org_id) is None
        for row_type in (EventRow, DetectionRow, AlertRow, AssetRow, MalformedRow):
            orgs = set(session.scalars(select(row_type.org_id)))
            assert acme.org_id not in orgs, row_type.__tablename__
        assert len(session.scalars(select(AlertDetectionRow)).all()) == 1  # globex's link
        assert [u.username for u in accounts.list_users(session, org_id=globex.org_id)] == ["gina"]
        assert accounts.credentials_for(session, "alice") is None
        assert not accounts.delete_organization(session, acme.org_id)

    def test_set_role_refuses_to_demote_the_last_admin(self, session: Session) -> None:
        acme = register(session)
        with pytest.raises(AccountConflict, match="only admin"):
            accounts.set_role(session, acme.user.user_id, "member", org_id=acme.org_id)
        bob = accounts.add_user(
            session, org_id=acme.org_id, username="bob", password_hash=HASH, role="member"
        )
        assert accounts.set_role(session, bob.user_id, "admin", org_id=acme.org_id) is not None
        demoted = accounts.set_role(session, acme.user.user_id, "member", org_id=acme.org_id)
        assert demoted is not None and demoted.role == "member"
        assert accounts.set_role(session, bob.user_id, "member", org_id="other") is None


class TestASuperAdminsHomeIsProtected:
    @pytest.fixture()
    def home(self, session: Session) -> accounts.User:
        register(session, "UCY", "ucy-admin")
        return accounts.create_superadmin(
            session, username="admin@admin.com", password_hash=HASH, org_id="ucy"
        )

    def test_the_home_org_cannot_be_suspended_or_deleted(
        self, session: Session, home: accounts.User
    ) -> None:
        with pytest.raises(ProtectedAccount, match="home organization of super admin"):
            accounts.set_suspended(session, "ucy", suspended=True)
        with pytest.raises(ProtectedAccount, match="home organization of super admin"):
            accounts.delete_organization(session, "ucy")
        assert accounts.get_organization(session, "ucy") == accounts.Organization("ucy", "UCY")
        # Reactivating is never refused (it cannot lock anybody out).
        assert accounts.set_suspended(session, "ucy", suspended=False) is not None

    def test_the_super_admin_cannot_be_removed_or_demoted_by_org_routes(
        self, session: Session, home: accounts.User
    ) -> None:
        with pytest.raises(ProtectedAccount, match="managed from the server"):
            accounts.delete_user(session, home.user_id, org_id="ucy")
        with pytest.raises(ProtectedAccount, match="managed from the server"):
            accounts.set_role(session, home.user_id, "member", org_id="ucy")
        assert accounts.credentials_for(session, "admin@admin.com") is not None

    def test_other_orgs_are_unaffected(self, session: Session, home: accounts.User) -> None:
        other = register(session, "Acme", "alice")
        assert accounts.set_suspended(session, other.org_id, suspended=True) is not None
        assert accounts.delete_organization(session, other.org_id)
