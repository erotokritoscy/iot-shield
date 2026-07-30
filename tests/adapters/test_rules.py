"""The rule language: vocabulary, compilation, and the shipped library (ADR-0032 D1/D2).

The theme running through these is that a detection rule which loads clean and matches
nothing is the worst outcome available — it is indistinguishable from a quiet night. Almost
every test here pins a case where that would otherwise happen and asserts a loud failure
instead.
"""

import dataclasses
import os
import re
from datetime import timedelta
from pathlib import Path

import pytest
import yaml

from iotshield.contracts import Lane, Severity
from iotshield.detect._severity import SEVERITY_RANK
from iotshield.detect.rules.fields import (
    EventView,
    FieldKind,
    coerce_value,
    describe,
    fields_for,
    read_comparable,
    resolve,
)
from iotshield.detect.rules.loader import (
    PACKAGED,
    available_rulesets,
    load_rules,
    load_ruleset,
    resolve_ruleset,
)
from iotshield.detect.rules.model import (
    THRESHOLD,
    ConfidenceTier,
    Operator,
    SeverityHint,
    compile_rule,
)
from iotshield.enrichment import EnrichmentSource
from iotshield.flow import METADATA_KEY, FlowRecord
from tests.rule_fixtures import (
    T0,
    flow_event,
    log_event,
    one_selection,
    oriented_event,
    rule_document,
    telemetry_event,
    write_ruleset,
)


def compile_document(**overrides: object):  # type: ignore[no-untyped-def]
    return compile_rule(rule_document(**overrides), source="test.yml")


def compile_selection(field: str, op: str, value: object):  # type: ignore[no-untyped-def]
    """The one-selection shape twelve of these tests need, in one line."""
    return compile_rule(one_selection(field, op, value), source="test.yml")


# --- The field vocabulary ------------------------------------------------------------


def test_the_vocabulary_is_closed_and_names_alternatives() -> None:
    """A typo is the failure this whole design exists to catch, so the error does the work."""
    with pytest.raises(ValueError, match=re.escape("Did you mean 'flow.bytes_out'")):
        resolve(Lane.FLOW, "flow.byte_out")


def test_an_unknown_field_lists_the_whole_vocabulary() -> None:
    with pytest.raises(ValueError, match="Known fields:"):
        resolve(Lane.FLOW, "metadata.flow.bytes_out")


def test_a_lane_cannot_address_another_lanes_fields() -> None:
    """Lanes have separate tables because lanes are separate — nothing converges."""
    with pytest.raises(
        ValueError, match=re.escape("unknown field 'flow.bytes_out' for lane 'log'")
    ):
        resolve(Lane.LOG, "flow.bytes_out")


def test_flow_fields_read_through_the_typed_record() -> None:
    view = EventView(flow_event(service="smb", bytes_out=99))
    assert read_comparable(resolve(Lane.FLOW, "flow.service"), view) == "smb"
    assert read_comparable(resolve(Lane.FLOW, "flow.bytes_out"), view) == 99.0


def test_a_flow_field_on_an_event_with_no_flow_record_reads_as_absent() -> None:
    """The FLOW lane can carry events from a foreign normalizer we cannot decode; a field
    that is not there must read as "cannot say", never as a value."""
    event = flow_event().model_copy(update={"metadata": {}})
    assert read_comparable(resolve(Lane.FLOW, "flow.service"), EventView(event)) is None


def test_every_published_field_reads_in_the_form_its_value_was_coerced_to() -> None:
    """Coercion happens at load, reading happens per event, and the two must land in the
    same comparable form or every comparison on that field is quietly meaningless.

    Note the two sides take *different inputs*: a rule author writes ``critical`` and an
    event carries ``Severity.CRITICAL``. What has to agree is the type they both produce,
    which is exactly what a comparison operator sees.
    """
    authored_literal = {FieldKind.NUMBER: 1, FieldKind.TEXT: "x", FieldKind.TIER: "info"}
    samples = {
        Lane.FLOW: flow_event(service="smb"),
        Lane.LOG: log_event(message="hello", severity=Severity.ERROR),
        Lane.TELEMETRY: telemetry_event(),
    }
    for lane, event in samples.items():
        view = EventView(event)
        for spec in fields_for(lane).values():
            read = read_comparable(spec, view)
            if read is None:
                continue
            assert type(read) is type(coerce_value(spec, authored_literal[spec.kind])), spec.name


def test_the_vocabulary_is_publishable_as_plain_data() -> None:
    """What makes the authoring guide impossible to get out of step with the code."""
    published = describe()
    assert set(published) == {"telemetry", "log", "flow"}
    service = next(spec for spec in published["flow"] if spec["name"] == "flow.service")
    assert service["kind"] == "text"
    assert service["description"]


# --- Operators and comparison --------------------------------------------------------


@pytest.mark.parametrize(
    ("operator", "value", "bytes_out", "expected"),
    [
        (">", 1000, 1200, True),
        (">", 1000, 1000, False),
        (">=", 1000, 1000, True),
        ("<", 1000, 999, True),
        ("<=", 1000, 1000, True),
        ("==", 1200, 1200, True),
        ("!=", 1200, 1200, False),
    ],
)
def test_numeric_operators(operator: str, value: int, bytes_out: int, expected: bool) -> None:
    rule = compile_document(
        detection={
            "selection": {"flow.bytes_out": {"operator": operator, "value": value}},
            "condition": "selection",
        }
    )
    assert rule.matches(EventView(flow_event(bytes_out=bytes_out))) is expected


def test_contains_is_case_insensitive_but_equality_is_not() -> None:
    """`contains` is a search, and casing differs between exporters far more often than
    meaning does. `==` stays exact, because equality that quietly ignores case is a
    surprise — the authoring guide says to prefer `contains` where casing varies."""
    contains = compile_selection("flow.service", "contains", "SMB")
    equals = compile_selection("flow.service", "==", "SMB")
    event = EventView(flow_event(service="smb"))
    assert contains.matches(event) is True
    assert equals.matches(event) is False


def test_a_missing_value_never_matches_even_a_negation() -> None:
    """The upstream engine returns True for `!= x` on an absent field, so a rule reading
    "service is not smb" fires on every event that has no service at all — an absence read
    as a positive claim. Every operator here reads absence as "cannot say"."""
    rule = compile_selection("flow.service", "!=", "smb")
    assert rule.matches(EventView(flow_event(service=None))) is False
    assert rule.matches(EventView(flow_event(service="ssl"))) is True


def test_a_severity_tier_compares_by_rank_not_by_text() -> None:
    """`Severity` is a StrEnum, so a bare `>=` compares text and "critical" < "debug"."""
    rule = compile_rule(one_selection("severity", ">=", "error", lane="log"), source="test.yml")
    assert rule.matches(EventView(log_event(message="x", severity=Severity.CRITICAL))) is True
    assert rule.matches(EventView(log_event(message="x", severity=Severity.ERROR))) is True
    assert rule.matches(EventView(log_event(message="x", severity=Severity.DEBUG))) is False


def test_a_quoted_number_still_compares_as_a_number() -> None:
    """YAML habit puts quotes on values; comparing "10000" as text would never fire."""
    rule = compile_selection("flow.bytes_out", ">", "1000")
    assert rule.matches(EventView(flow_event(bytes_out=1200))) is True


# --- Load-time refusals --------------------------------------------------------------


def test_an_operator_that_does_not_suit_the_field_is_refused_at_load() -> None:
    """Upstream this raises an uncaught TypeError on the first event, taking the run down.
    Here the field's kind is known, so the rule never loads."""
    with pytest.raises(ValueError, match="holds a text, so operator '>' does not apply"):
        compile_selection("flow.service", ">", 10)


def test_a_non_numeric_value_on_a_numeric_field_is_refused_at_load() -> None:
    with pytest.raises(ValueError, match="must be numeric"):
        compile_selection("flow.bytes_out", ">", "lots")


def test_an_unknown_severity_tier_is_refused_with_the_known_ones() -> None:
    with pytest.raises(ValueError, match="not one of: debug, info, warning, error, critical"):
        compile_rule(one_selection("severity", ">=", "catastrophic", lane="log"), source="test.yml")


def test_a_condition_naming_a_missing_selection_is_refused_at_load() -> None:
    """Upstream this raises KeyError on the first event, i.e. in production."""
    with pytest.raises(ValueError, match="names selection\\(s\\) 'typo'"):
        compile_document(
            detection={
                "selection": {"flow.bytes_out": {"operator": ">", "value": 1}},
                "condition": "typo",
            }
        )


def test_a_selection_nobody_references_is_refused() -> None:
    """Dead code in a detection rule means the author believes something is being checked
    that is not — "I forgot the OR" is far likelier than "I meant to leave that there".

    It is the mistyped-field failure arrived at from the other direction: the rule loads,
    runs, and silently checks less than it says. Upstream evaluates the condition and
    ignores the rest.
    """
    with pytest.raises(ValueError, match="never named by condition"):
        compile_document(
            detection={
                "selection_a": {"flow.bytes_out": {"operator": ">", "value": 1}},
                "selection_b": {"flow.service": {"operator": "==", "value": "smb"}},
                "condition": "selection_a",
            }
        )


def test_mixing_and_with_or_is_refused_rather_than_silently_parsed() -> None:
    with pytest.raises(ValueError, match="mixes AND and OR"):
        compile_document(
            detection={
                "a": {"flow.bytes_out": {"operator": ">", "value": 1}},
                "b": {"flow.bytes_out": {"operator": ">", "value": 2}},
                "c": {"flow.bytes_out": {"operator": ">", "value": 3}},
                "condition": "a AND b OR c",
            }
        )


def test_an_unknown_key_in_a_rule_file_is_refused() -> None:
    """extra="forbid" like every contract here: for a detection rule, a silently ignored
    key means silently not detecting."""
    with pytest.raises(ValueError, match="sevrity_hint"):
        compile_document(sevrity_hint="high")


def test_an_unknown_operator_lists_the_known_ones() -> None:
    with pytest.raises(ValueError, match="known operators:"):
        compile_selection("flow.bytes_out", "=~", 1)


def test_a_label_based_rule_cannot_be_loaded_into_the_production_set() -> None:
    """Defence in depth: a demo rule matching a dataset's answer key can never fire on live
    data, so dropping one into the library is refused rather than silently useless."""
    with pytest.raises(ValueError, match="only 'observable' rules may be loaded"):
        compile_document(signal_basis="label_based")


def test_a_correlation_rule_is_refused_with_the_reason() -> None:
    with pytest.raises(ValueError, match="single-event signatures only"):
        compile_document(rule_type="correlation")


def test_upstream_provenance_fields_are_accepted_and_inert() -> None:
    """A rule should move between the two repos with only its field addresses changed, so
    the upstream metadata vocabulary is accepted rather than forbidden."""
    rule = compile_document(
        signal_basis="observable",
        rule_type="single_event",
        integration_status="production",
        severity="medium",
        correlation={"group_by": "entities.source.id"},
        one_line="Outbound bytes exceed the limit.",
    )
    assert rule.explanation == "Outbound bytes exceed the limit."


def test_the_explanation_falls_back_to_the_description() -> None:
    assert compile_document().explanation == "A rule for tests."


# --- Scoring ---------------------------------------------------------------------------


def test_severity_and_confidence_together_set_the_margin() -> None:
    high_confidence = compile_document(severity_hint="high", confidence="high")
    low_confidence = compile_document(severity_hint="high", confidence="low")
    assert high_confidence.score == pytest.approx(3.0)
    assert low_confidence.score == pytest.approx(2.1)
    assert high_confidence.threshold == THRESHOLD


def test_every_severity_confidence_pair_clears_the_threshold_and_fits_the_scale() -> None:
    """Two invariants the table is calibrated to, pinned so a future tier cannot break them
    silently: the port requires a strict `score > threshold`, and the weighted engine clamps
    its severity factor to 4.0, so a product above that would be silently discarded."""
    for severity in SeverityHint:
        for confidence in ConfidenceTier:
            rule = compile_document(severity_hint=severity.value, confidence=confidence.value)
            assert rule.score > THRESHOLD, (severity, confidence)
            assert rule.score <= 4.0, (severity, confidence)


def test_the_enrichment_is_built_from_the_rules_own_mitre() -> None:
    rule = compile_document(
        mitre=[
            {
                "tactic": "Exfiltration",
                "technique": "Exfiltration Over C2 Channel",
                "technique_id": "T1041",
            }
        ],
        suggested_actions=["Check the destination."],
    )
    assert rule.enrichment.threat_class == "Exfiltration Over C2 Channel (T1041)"
    assert rule.enrichment.source is EnrichmentSource.RULE
    assert rule.enrichment.origin == "rule:test-rule"
    assert rule.enrichment.suggested_actions == ("Check the destination.",)


def test_a_rule_without_a_mitre_mapping_uses_its_title_as_the_threat_class() -> None:
    """Not every worthwhile signature is an attack — a kernel OOM kill has no technique —
    and a title is a far more honest classification than "unknown"."""
    assert compile_document().enrichment.threat_class == "Test Rule"
    assert compile_document().enrichment.mitre == ()


# --- Loading ---------------------------------------------------------------------------


def test_the_packaged_library_loads_sorted_by_id() -> None:
    rules = load_ruleset()
    assert [rule.id for rule in rules] == sorted(rule.id for rule in rules)
    assert len(rules) >= 6


def test_a_directory_ruleset_loads_recursively(tmp_path: Path) -> None:
    nested = tmp_path / "flow"
    nested.mkdir()
    (nested / "one.yml").write_text(yaml.safe_dump(rule_document()), encoding="utf-8")
    (tmp_path / "two.yaml").write_text(
        yaml.safe_dump(rule_document(id="other-rule")), encoding="utf-8"
    )
    assert [rule.id for rule in load_ruleset(str(tmp_path))] == ["other-rule", "test-rule"]


def test_duplicate_rule_ids_are_refused(tmp_path: Path) -> None:
    """An alert attributes its threat class to one rule id, so two files claiming one id
    would make that attribution ambiguous."""
    (tmp_path / "a.yml").write_text(yaml.safe_dump(rule_document()), encoding="utf-8")
    (tmp_path / "b.yml").write_text(yaml.safe_dump(rule_document()), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate rule id 'test-rule'"):
        load_ruleset(str(tmp_path))


def test_a_broken_file_names_itself(tmp_path: Path) -> None:
    (tmp_path / "broken.yml").write_text("id: [unclosed", encoding="utf-8")
    with pytest.raises(ValueError, match=re.escape("broken.yml")):
        load_ruleset(str(tmp_path))


def test_an_empty_rule_file_is_refused(tmp_path: Path) -> None:
    (tmp_path / "empty.yml").write_text("\n", encoding="utf-8")
    with pytest.raises(ValueError, match="the file is empty"):
        load_ruleset(str(tmp_path))


def test_a_ruleset_with_no_rule_files_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=re.escape("contains no .yml/.yaml rule files")):
        load_ruleset(str(tmp_path))


def test_a_missing_ruleset_directory_is_refused() -> None:
    with pytest.raises(ValueError, match="is not a directory"):
        load_ruleset("no/such/place")


def test_a_lane_the_ruleset_cannot_feed_is_refused_with_what_it_holds() -> None:
    """The silent-emptiness failure in its purest form: a rule detector on a lane with no
    rules would find nothing, forever, and look exactly like a quiet night."""
    with pytest.raises(ValueError, match="no 'telemetry' rules"):
        load_rules(Lane.TELEMETRY)


# --- The shelf: config names a rule set, never a path (ADR-0037) ------------------------


def put_ruleset(root: Path, name: str) -> Path:
    """Put one loadable rule set on a shelf, and return its directory."""
    directory = root / name
    directory.mkdir(parents=True)
    write_ruleset(directory, rule_document())
    return directory


def test_the_shelf_lists_the_library_first_then_its_own(tmp_path: Path) -> None:
    put_ruleset(tmp_path, "site-rules")
    put_ruleset(tmp_path, "pilot")
    assert available_rulesets(tmp_path) == (PACKAGED, "pilot", "site-rules")


def test_a_directory_with_no_rule_files_is_not_a_rule_set(tmp_path: Path) -> None:
    """Offering it would be offering a name that fails at construction — the dropdown's whole
    value is that its members are ones the adapter can actually load."""
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "README.md").write_text("not a rule\n", encoding="utf-8")
    (tmp_path / "stray.yml").write_text("id: loose\n", encoding="utf-8")  # a file, not a set
    assert available_rulesets(tmp_path) == (PACKAGED,)


def test_a_missing_shelf_lists_the_library_rather_than_raising(tmp_path: Path) -> None:
    """``/modules`` must answer for every registered adapter, so a shelf that is simply not
    there cannot take the catalogue down with it (the decision ``packaged_artifact_ids``
    already made for a package shipped without its artifacts)."""
    assert available_rulesets(tmp_path / "no-such-shelf") == (PACKAGED,)


# Root bypasses the permission bits entirely, so the case cannot be staged as root; Windows
# expresses this through ACLs, which deny differently and swallow it inside `rglob`.
_PERMISSIONS_ARE_TESTABLE = os.name != "nt" and getattr(os, "geteuid", lambda: 0)() != 0


@pytest.mark.skipif(not _PERMISSIONS_ARE_TESTABLE, reason="POSIX permission bits, and not root")
def test_a_shelf_that_can_be_listed_but_not_entered_lists_the_library(tmp_path: Path) -> None:
    """The failure the missing-shelf case *looks* like it already covers, and did not.

    POSIX needs ``r`` on a directory to list it and ``x`` to look inside, so mode 644 is a real
    and reachable state: one ``chmod -R`` over a bind mount, or a non-root container reading a
    root-owned volume — which is exactly how ``docker-compose.yml`` mounts the shelf. Then
    ``iterdir`` succeeds and every step after it raises, and only the first was inside the
    ``try``. The blast radius is what makes it worth a test with a ``skipif`` on it: this
    resolves inside ``/modules``, so a permissions slip on the rules mount answered a request
    about **every** adapter with a 500 and blanked the whole Compose page.
    """
    shelf = tmp_path / "shelf"
    put_ruleset(shelf, "site-rules")
    shelf.chmod(0o644)  # readable, NOT searchable
    try:
        assert available_rulesets(shelf) == (PACKAGED,)
    finally:
        shelf.chmod(0o755)


def test_the_shelf_is_read_per_call_not_snapshotted(tmp_path: Path) -> None:
    """The reason this vocabulary is a callable and the artifact one was a tuple (ADR-0037):
    an operator dropping a rule set onto a running server's shelf must see it."""
    assert available_rulesets(tmp_path) == (PACKAGED,)
    put_ruleset(tmp_path, "later")
    assert available_rulesets(tmp_path) == (PACKAGED, "later")


def test_the_packaged_name_is_reserved_and_cannot_be_shadowed(tmp_path: Path) -> None:
    """A shelf directory called ``packaged`` would otherwise appear twice in one dropdown, with
    the two options meaning different things."""
    put_ruleset(tmp_path, PACKAGED)
    assert available_rulesets(tmp_path) == (PACKAGED,)
    assert resolve_ruleset(PACKAGED, tmp_path) is None


@pytest.mark.parametrize("name", [None, "", "   ", PACKAGED])
def test_the_library_resolves_to_no_directory(name: str | None, tmp_path: Path) -> None:
    """Blank and absent mean the default, as they did before rule sets had names — so a config
    written against the old free-text field keeps working instead of failing at construction."""
    assert resolve_ruleset(name, tmp_path) is None


def test_a_shelved_name_resolves_to_its_directory(tmp_path: Path) -> None:
    directory = put_ruleset(tmp_path, "site-rules")
    resolved = resolve_ruleset("site-rules", tmp_path)
    assert resolved is not None
    assert Path(resolved) == directory.resolve()
    assert [rule.id for rule in load_ruleset(resolved)] == ["test-rule"]


def test_every_offered_name_resolves_to_the_rule_set_of_that_name(tmp_path: Path) -> None:
    """The invariant the one-mapping design exists to give, stated over the whole offer.

    Listing and resolving read ``_shelved`` together so they cannot disagree about *which names
    exist* — but that is only half of it, and the half that was true. Resolution also has to land
    on the entry the operator picked, and a ``strip()`` ahead of the lookup quietly broke it: two
    genuinely distinct rule sets whose names differ only in whitespace were both offered, and
    either choice ran the first. A dropdown whose entries are not the things it runs is the exact
    failure the symlink fix was written for, arriving through a different door — so this asserts
    the property rather than that one door.
    """
    put_ruleset(tmp_path, "site-rules")
    put_ruleset(tmp_path, " site-rules")  # legal on both platforms, and a different rule set
    offered = available_rulesets(tmp_path)
    assert offered == (PACKAGED, " site-rules", "site-rules")
    for name in offered:
        resolved = resolve_ruleset(name, tmp_path)
        if name == PACKAGED:
            assert resolved is None
        else:
            assert resolved is not None and Path(resolved).name == name


def test_a_name_that_only_differs_by_whitespace_is_not_that_rule_set(tmp_path: Path) -> None:
    """Refused like any other name that is not on the shelf. Trimming it to one that *is* would
    be the sanitizing this boundary refuses everywhere else — the wrong value quietly becoming a
    plausible one — and here it would silently judge a run with rules nobody selected."""
    put_ruleset(tmp_path, "site-rules")
    with pytest.raises(ValueError, match="unknown rule set"):
        resolve_ruleset(" site-rules", tmp_path)


def test_a_rule_set_is_found_whatever_the_case_of_its_file_extension(tmp_path: Path) -> None:
    """``rule.YML`` is an ordinary thing to save, and made its whole rule set vanish.

    A directory holding only uppercase-suffixed files was not on the shelf at all, so naming it
    got "unknown rule set" — the *typo* message, which tells an operator the thing does not exist
    when it does. Before the shelf, pathing that same directory said "contains no .yml/.yaml rule
    files", which is the answer. This is the deployment's own extension point and nothing
    publishes where it is, so the silent version leaves nowhere to look.
    """
    directory = tmp_path / "site-rules"
    directory.mkdir()
    write_ruleset(directory, rule_document())
    for path in list(directory.glob("*.yml")):
        path.rename(path.with_suffix(".YML"))

    assert available_rulesets(tmp_path) == (PACKAGED, "site-rules")
    resolved = resolve_ruleset("site-rules", tmp_path)
    assert resolved is not None
    assert [rule.id for rule in load_ruleset(resolved)] == ["test-rule"]


@pytest.mark.parametrize("name", ["../secrets", "/etc", "typo"])
def test_a_name_that_is_not_on_the_shelf_is_refused_with_what_is(name: str, tmp_path: Path) -> None:
    """The security half of ADR-0037. Membership, never sanitizing.

    Three *intents*, not three branches — a traversal, an absolute path, an honest typo. A shelf
    name can only ever be one of the single path components ``iterdir`` yielded, so everything
    else fails identically by construction. What is asserted is that the failure reveals nothing:
    not whether the thing named exists, and not where the shelf is.
    """
    put_ruleset(tmp_path, "site-rules")
    with pytest.raises(ValueError) as caught:
        resolve_ruleset(name, tmp_path)
    message = str(caught.value)
    assert "unknown rule set" in message
    assert "packaged, site-rules" in message
    assert str(tmp_path) not in message


def test_a_refused_name_is_never_read_from_disk(tmp_path: Path) -> None:
    """A directory that exists but is not on the shelf is refused *as a name*, so nothing under
    it is opened. Without this the message could confirm a path's contents (a real one echoed
    two lines of the offending file back to the HTTP caller before ADR-0037)."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.yaml").write_text("token: s3cr3t\n", encoding="utf-8")
    shelf = tmp_path / "shelf"
    put_ruleset(shelf, "site-rules")
    with pytest.raises(ValueError) as caught:
        resolve_ruleset(str(outside), shelf)
    assert "s3cr3t" not in str(caught.value)
    assert "private.yaml" not in str(caught.value)


# --- The shipped library (the port of the upstream contract checker) --------------------


def test_every_shipped_rule_is_complete_and_actionable() -> None:
    """The upstream repo's ``check_rule_integration_contract.py`` as a test rather than a
    script someone must remember to run. Every shipped rule must carry the enrichment that
    justifies its existence: a rule with no explanation and no next step fills a threat
    class with a label and leaves the analyst exactly where they started."""
    for rule in load_ruleset():
        assert rule.description.strip(), rule.id
        assert rule.explanation.strip(), rule.id
        assert rule.enrichment.suggested_actions, rule.id
        assert rule.enrichment.threat_class.strip(), rule.id
        assert rule.selections, rule.id


def test_shipped_rule_ids_are_readable_slugs() -> None:
    """The id reaches the analyst through the alert rationale (``per rule:<id>``), so it is
    analyst-facing text rather than a bookkeeping key."""
    for rule in load_ruleset():
        assert rule.id == rule.id.lower()
        assert " " not in rule.id


def test_shipped_rules_only_use_operators_the_language_defines() -> None:
    known = set(Operator)
    for rule in load_ruleset():
        for selection in rule.selections:
            for comparison in selection.comparisons:
                assert comparison.operator in known


def test_the_shipped_library_covers_more_than_one_lane() -> None:
    """Both configured instances in the demo need rules, and the two-instances-per-lane
    shape is only exercised if the library actually spans lanes."""
    assert {rule.lane for rule in load_ruleset()} >= {Lane.FLOW, Lane.LOG}


def test_the_ported_rules_kept_their_upstream_threat_mappings() -> None:
    """Provenance is the point of the collaboration: the knowledge stays hers."""
    by_id = {rule.id: rule for rule in load_ruleset()}
    assert by_id["high-upload-volume"].enrichment.mitre[0].technique_id == "T1041"
    assert by_id["smb-service-activity"].enrichment.mitre[0].technique_id == "T1021"
    assert by_id["ssh-possible-break-in"].enrichment.mitre[0].technique_id == "T1046"


def test_the_break_in_rule_matches_the_line_sshd_actually_writes() -> None:
    """Re-expressed from a semantic field the upstream normalizer produces and ours does
    not, so the thing worth pinning is that it still fires on the real message."""
    rule = {rule.id: rule for rule in load_ruleset()}["ssh-possible-break-in"]
    line = (
        "[sshd] reverse mapping checking getaddrinfo for ns.example.com [173.234.31.186] "
        "failed - POSSIBLE BREAK-IN ATTEMPT!"
    )
    assert rule.matches(EventView(log_event(message=line))) is True
    assert (
        rule.matches(EventView(log_event(message="[sshd] Accepted publickey for admin"))) is False
    )


def test_the_upload_rule_separates_exfiltration_from_ordinary_traffic() -> None:
    """The threshold is dataset-flavoured, so what matters is the separation it produces on
    the corpus the benchmark uses: 1.2 MB chunks against a 2.2 kB busiest innocent device."""
    rule = {rule.id: rule for rule in load_ruleset()}["high-upload-volume"]
    assert rule.matches(EventView(flow_event(bytes_out=1_200_000))) is True
    assert rule.matches(EventView(flow_event(bytes_out=2_210))) is False


def test_the_inbound_admin_rule_fires_on_a_real_oriented_inbound_session() -> None:
    """Pinned against ``orient`` rather than a hand-built record, because the first draft of
    this rule addressed ``peer_port`` and could never have fired: every flow field is
    asset-relative, so on a connection the device RECEIVED the service port is the port the
    device EXPOSED. A hand-built fixture would have happily agreed with the wrong rule."""
    rule = {rule.id: rule for rule in load_ruleset()}["inbound-remote-admin"]
    from_outside = oriented_event(src_ip="203.0.113.9", dst_ip="10.0.0.11", dst_port=22)
    assert from_outside.metadata[METADATA_KEY]["direction"] == "inbound"
    assert rule.matches(EventView(from_outside)) is True


def test_the_inbound_admin_rule_ignores_traffic_from_inside_the_fleet() -> None:
    """`inbound` means the peer is outside home_networks; an admin connecting from inside
    orients as `internal`, which is the distinction that keeps this rule tolerable."""
    rule = {rule.id: rule for rule in load_ruleset()}["inbound-remote-admin"]
    from_inside = oriented_event(src_ip="10.0.0.9", dst_ip="10.0.0.11", dst_port=22)
    assert from_inside.metadata[METADATA_KEY]["direction"] == "internal"
    assert rule.matches(EventView(from_inside)) is False


def test_the_telnet_rule_fires_on_a_real_oriented_outbound_connection() -> None:
    """The mirror case: outbound, so the service port lands on ``peer_port``."""
    rule = {rule.id: rule for rule in load_ruleset()}["telnet-outbound-propagation"]
    outbound = oriented_event(src_ip="10.0.0.24", dst_ip="198.51.100.4", dst_port=23)
    assert rule.matches(EventView(outbound)) is True


def test_time_ordering_of_the_library_is_irrelevant_to_matching() -> None:
    """Sanity check that matching is per event: a rule is a predicate, not a window."""
    rule = {rule.id: rule for rule in load_ruleset()}["telnet-outbound-propagation"]
    later = flow_event(peer_port=23).model_copy(update={"timestamp": T0 + timedelta(hours=5)})
    assert rule.matches(EventView(later)) is True


# --- The published field list cannot go stale -------------------------------------------


def test_every_published_field_documents_itself() -> None:
    """The vocabulary IS its own documentation, so there is no second copy to go stale.

    This used to pin a hand-written field list in a separate authoring guide. That is a
    promise to keep two things in sync forever, which nobody keeps — and the cost of it
    drifting falls on the one audience with no type checker and no error message: someone
    writing YAML. Since ``FieldSpec.description`` is what ``python -m iotshield.detect.rules``
    prints, the copy was removable rather than merely checkable, and drift stops being
    *detected* and becomes impossible (the move ADR-0035 D2 made for ``PAYLOAD_BY_LANE``).

    What is left to enforce is that the descriptions are usable *as* the reference: present,
    long enough to say something, and not a restatement of the name they sit beside — the
    three ways a field silently becomes undocumented while still appearing in the listing.
    """
    for lane, lane_fields in describe().items():
        for spec in lane_fields:
            name, text = spec["name"], spec["description"]
            # Lane-qualified in the message, not by concatenation: a flow field is already
            # named `flow.peer_ip`, while `severity` is published on all three lanes.
            where = f"{name!r} on lane {lane}"
            assert text.strip(), f"{where} is published with no description"
            assert len(text) >= 20, f"{where} has a description too short to document it: {text!r}"
            assert not text.lower().startswith(name.lower()), (
                f"{where} has a description that restates its name rather than explaining it: "
                f"{text!r}"
            )


def test_the_flow_vocabulary_tracks_flowrecord() -> None:
    """The table reads THROUGH ``FlowRecord``, so a renamed field stops type-checking — but
    an *added* one would simply never be addressable, silently.

    ``asset_ip`` is deliberately unpublished: a rule already runs per asset, so matching on
    the asset's own address would be a worse spelling of something the pipeline decided at
    ingest (ADR-0023 D2).
    """
    unpublished = {"asset_ip"}
    published = {
        spec.name.removeprefix("flow.")
        for spec in fields_for(Lane.FLOW).values()
        if spec.name.startswith("flow.")
    }
    record_fields = {field.name for field in dataclasses.fields(FlowRecord)}
    assert published | unpublished == record_fields, (
        "FlowRecord and the rule vocabulary disagree; publish the new field or add it to "
        "the deliberately-unpublished set"
    )


def test_every_severity_has_a_rank() -> None:
    """``read_comparable`` indexes the rank table directly, so a Severity member missing
    from it is a KeyError mid-run rather than a refusal at load."""
    assert set(SEVERITY_RANK) == set(Severity)
    assert sorted(SEVERITY_RANK, key=lambda level: SEVERITY_RANK[level]) == [
        Severity.DEBUG,
        Severity.INFO,
        Severity.WARNING,
        Severity.ERROR,
        Severity.CRITICAL,
    ]
