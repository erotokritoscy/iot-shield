"""Rule-based detection: named, authored signatures over single events (ADR-0032).

``python -m iotshield.detect.rules`` prints the field vocabulary a rule may address, per
lane — the published field list, generated from the code that implements it.
"""

from iotshield.detect.rules.detector import RuleDetector, capability
from iotshield.detect.rules.fields import describe as describe_fields
from iotshield.detect.rules.loader import DEFAULT_LIBRARY, load_rules, load_ruleset
from iotshield.detect.rules.model import Rule, compile_rule

__all__ = [
    "DEFAULT_LIBRARY",
    "Rule",
    "RuleDetector",
    "capability",
    "compile_rule",
    "describe_fields",
    "load_rules",
    "load_ruleset",
]
