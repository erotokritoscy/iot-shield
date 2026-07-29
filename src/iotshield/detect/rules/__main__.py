"""Print the rule field vocabulary and the shipped rule library.

The published field list, generated from the table that implements it — so a rule author
can always ask the system what it can see, rather than trusting a document to have been
kept in sync (ADR-0032 D1).

    uv run python -m iotshield.detect.rules
"""

from iotshield.detect.rules.fields import describe
from iotshield.detect.rules.loader import load_ruleset


def main() -> None:
    print("FIELD VOCABULARY — what a rule may name on the left of a comparison\n")
    for lane, specs in describe().items():
        print(f"  {lane}:")
        width = max(len(spec["name"]) for spec in specs)
        for spec in specs:
            print(f"    {spec['name']:<{width}}  {spec['kind']:<6}  {spec['description']}")
        print()

    rules = load_ruleset()
    print(f"PACKAGED RULE LIBRARY — {len(rules)} rules\n")
    for rule in rules:
        print(f"  [{rule.lane.value}] {rule.id}")
        print(f"      {rule.title} ({rule.severity_hint.value}/{rule.confidence.value})")
        print(f"      {rule.explanation}")
        print(f"      -> {rule.enrichment.threat_class}")
    print()


if __name__ == "__main__":
    main()
