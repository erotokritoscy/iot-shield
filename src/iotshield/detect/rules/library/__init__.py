"""The packaged rule library, loaded by id-sorted order via
``iotshield.detect.rules.loader.load_ruleset`` (ADR-0032 D2).

Three rules are ported from Antonia Voutouri's ``iot-ot-alert-correlation`` with her field
addresses re-pointed at our vocabulary; each names its origin in a comment. Three are
authored here to give the contributor guide worked examples of the parts of the language the
ported rules do not exercise (OR conditions, multi-field selections, severity tiers).
"""
