"""Unit tests for path-pattern rule resolution (pure, no dependencies)."""

import unittest

from app.rules import (
    InvalidPattern,
    InvalidRuleSet,
    RuleApplicationFailed,
    apply_plan,
    compile_pattern,
    compile_rules,
    group_matches,
    pattern_matches,
    plan_resolutions,
)
from app.merge import placeholder


def compile_one(**kwargs):
    return compile_rules([kwargs])[0]


def hits(pattern_source, pointer):
    pattern = compile_pattern(pattern_source)
    from app.merge import parse_pointer

    return pattern_matches(pattern, tuple(parse_pointer(pointer)))


class CompilePatternTest(unittest.TestCase):
    def test_literal_pattern(self):
        pattern = compile_pattern("/db/host")
        self.assertEqual(pattern.segments, ("db", "host"))
        self.assertEqual(pattern.literal_count, 2)
        self.assertEqual(pattern.star_count, 0)

    def test_root_pattern(self):
        self.assertEqual(compile_pattern("").segments, ())

    def test_must_start_with_slash(self):
        with self.assertRaises(InvalidPattern):
            compile_pattern("db/host")

    def test_rfc6901_escaping_is_decoded(self):
        pattern = compile_pattern("/a~1b/c~0d")
        self.assertEqual(pattern.segments, ("a/b", "c~d"))

    def test_single_star_segment(self):
        pattern = compile_pattern("/svc/*/port")
        self.assertEqual(pattern.segments, ("svc", "*", "port"))
        self.assertEqual(pattern.literal_count, 2)
        self.assertEqual(pattern.star_count, 1)

    def test_trailing_double_star(self):
        pattern = compile_pattern("/svc/**")
        self.assertEqual(pattern.segments, ("svc", "**"))
        self.assertEqual(pattern.star_count, 2)

    def test_double_star_only_at_end(self):
        with self.assertRaises(InvalidPattern):
            compile_pattern("/svc/**/port")

    def test_wildcard_must_be_whole_segment(self):
        with self.assertRaises(InvalidPattern):
            compile_pattern("/svc/p*/port")
        with self.assertRaises(InvalidPattern):
            compile_pattern("/svc/*x")
        with self.assertRaises(InvalidPattern):
            compile_pattern("/svc/x**")


class PatternMatchTest(unittest.TestCase):
    def test_literal_match_and_miss(self):
        self.assertTrue(hits("/db/host", "/db/host"))
        self.assertFalse(hits("/db/host", "/db/port"))
        self.assertFalse(hits("/db/host", "/db/host/extra"))

    def test_root_pattern_matches_only_root(self):
        self.assertTrue(hits("", ""))
        self.assertFalse(hits("", "/a"))

    def test_single_star_matches_exactly_one_segment(self):
        self.assertTrue(hits("/svc/*/port", "/svc/db/port"))
        self.assertFalse(hits("/svc/*/port", "/svc/db/extra/port"))
        self.assertFalse(hits("/svc/*/port", "/svc/port"))

    def test_double_star_matches_zero_or_more_segments(self):
        self.assertTrue(hits("/svc/**", "/svc/db/host"))
        self.assertTrue(hits("/svc/**", "/svc/x"))
        self.assertTrue(hits("/svc/**", "/svc"))
        self.assertFalse(hits("/svc/**", "/other"))

    def test_escaped_pointer_tokens_match_literals(self):
        self.assertTrue(hits("/a~1b", "/a~1b"))


class PlanTest(unittest.TestCase):
    def _conflicts(self, pointers, missing=None):
        missing = missing or {}
        result = []
        for pointer in pointers:
            result.append(
                {
                    "pointer": pointer,
                    "base": 0,
                    "left": f"L:{pointer}",
                    "right": f"R:{pointer}",
                    "missing": missing.get(pointer, []),
                }
            )
        return result

    def test_unique_rule_per_conflict(self):
        rules = compile_rules([{"left": "/svc/*/host"}, {"right": "/**"}])
        winners = plan_resolutions(self._conflicts(["/svc/db/host"]), rules)
        self.assertEqual(len(winners), 1)
        self.assertEqual(winners[0].side, "left")
        self.assertEqual(winners[0].rule_index, 0)

    def test_fewer_asterisk_characters_wins(self):
        # At /a: '/**' and '/*' both use zero literals; '/*' carries one
        # '*' character versus two in '**', so the later '/*' rule wins.
        rules = compile_rules([{"right": "/**"}, {"left": "/*"}])
        winners = plan_resolutions(self._conflicts(["/a"]), rules)
        self.assertEqual(winners[0].side, "left")
        self.assertEqual(winners[0].rule_index, 1)

    def test_equal_star_count_falls_back_to_declaration_order(self):
        # '/db/host': '/**' and '/*/*' each carry two '*' characters, so
        # the earlier declared rule wins.
        rules = compile_rules([{"right": "/**"}, {"left": "/*/*"}])
        winners = plan_resolutions(self._conflicts(["/db/host"]), rules)
        self.assertEqual(winners[0].side, "right")
        self.assertEqual(winners[0].rule_index, 0)

    def test_fewer_wildcards_wins(self):
        rules = compile_rules([{"left": "/*/*"}, {"right": "/db/*"}])
        winners = plan_resolutions(self._conflicts(["/db/host"]), rules)
        self.assertEqual(winners[0].side, "right")

    def test_declaration_order_breaks_ties(self):
        rules = compile_rules([{"left": "/db/*"}, {"right": "/db/*"}])
        winners = plan_resolutions(self._conflicts(["/db/host"]), rules)
        self.assertEqual(winners[0].side, "left")
        self.assertEqual(winners[0].rule_index, 0)

    def test_left_before_right_within_one_rule(self):
        rules = compile_rules([{"left": "/db/*", "right": "/db/*"}])
        winners = plan_resolutions(self._conflicts(["/db/host"]), rules)
        self.assertEqual(winners[0].side, "left")

    def test_unmatched_conflicts_are_left_out(self):
        rules = compile_rules([{"left": "/db/*"}])
        winners = plan_resolutions(
            self._conflicts(["/db/host", "/svc/port"]), rules
        )
        self.assertEqual([w.pointer for w in winners], ["/db/host"])

    def test_group_matches_groups_by_rule_and_side(self):
        rules = compile_rules(
            [
                {"left": "/svc/*/host", "right": "/svc/*/port"},
                {"right": "/other/**"},
            ]
        )
        conflicts = self._conflicts(
            ["/svc/db/host", "/svc/db/port", "/other/x/y", "/lonely"]
        )
        groups = group_matches(plan_resolutions(conflicts, rules))
        self.assertEqual(
            groups,
            [
                {"rule_index": 0, "side": "left", "pointers": ["/svc/db/host"]},
                {"rule_index": 0, "side": "right", "pointers": ["/svc/db/port"]},
                {"rule_index": 1, "side": "right", "pointers": ["/other/x/y"]},
            ],
        )

    def test_empty_rules_rejected(self):
        with self.assertRaises(InvalidRuleSet):
            compile_rules([])

    def test_rule_without_any_pattern_rejected(self):
        with self.assertRaises(InvalidRuleSet):
            compile_rules([{}])


class ApplyPlanTest(unittest.TestCase):
    def test_apply_sides_and_remaining(self):
        conflicts = [
            {"pointer": "/a", "base": 1, "left": 2, "right": 3, "missing": []},
            {"pointer": "/b", "base": 1, "left": 2, "right": 3, "missing": []},
        ]
        document = {"a": placeholder("/a"), "b": placeholder("/b")}
        rules = compile_rules([{"left": "/a"}])
        winners = plan_resolutions(conflicts, rules)
        document, remaining = apply_plan(document, conflicts, winners)
        self.assertEqual(document, {"a": 2, "b": placeholder("/b")})
        self.assertEqual([c["pointer"] for c in remaining], ["/b"])

    def test_missing_chosen_side_deletes_key(self):
        conflicts = [
            {"pointer": "/a", "base": 1, "left": None, "right": 3, "missing": ["left"]}
        ]
        document = {"a": placeholder("/a"), "keep": 1}
        winners = plan_resolutions(conflicts, compile_rules([{"left": "/a"}]))
        document, remaining = apply_plan(document, conflicts, winners)
        self.assertEqual(document, {"keep": 1})
        self.assertEqual(remaining, [])

    def test_missing_side_at_root_fails_without_partial_change(self):
        conflicts = [
            {"pointer": "", "base": 1, "left": None, "right": 3, "missing": ["left"]}
        ]
        document = placeholder("")
        winners = plan_resolutions(conflicts, compile_rules([{"left": ""}]))
        with self.assertRaises(RuleApplicationFailed):
            apply_plan(document, conflicts, winners)
        # The placeholder document handed in is untouched.
        self.assertEqual(document, placeholder(""))


if __name__ == "__main__":
    unittest.main()
