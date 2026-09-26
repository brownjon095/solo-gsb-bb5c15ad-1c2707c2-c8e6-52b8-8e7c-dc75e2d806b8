"""Unit tests for the pre-merge impact analysis (pure, no dependencies)."""

import unittest

from app.analysis import (
    CONFLICT,
    LEFT_ONLY,
    RIGHT_ONLY,
    SAME_CHANGE,
    analyze_merge,
)
from app.merge import merge3


def pointers_of(result, kind=None):
    return [
        change["pointer"]
        for change in result["changes"]
        if kind is None or change["type"] == kind
    ]


def find(result, pointer):
    return next(change for change in result["changes"] if change["pointer"] == pointer)


class MergeAnalysisTest(unittest.TestCase):
    def test_four_way_classification(self):
        result = analyze_merge(
            base={"same": 0, "l": 1, "r": 1, "sc": 1, "cf": 1},
            left={"same": 0, "l": 2, "r": 1, "sc": 9, "cf": 2},
            right={"same": 0, "l": 1, "r": 3, "sc": 9, "cf": 3},
        )
        self.assertNotIn("/same", pointers_of(result))
        self.assertEqual(pointers_of(result, LEFT_ONLY), ["/l"])
        self.assertEqual(pointers_of(result, RIGHT_ONLY), ["/r"])
        self.assertEqual(pointers_of(result, SAME_CHANGE), ["/sc"])
        self.assertEqual(pointers_of(result, CONFLICT), ["/cf"])
        self.assertEqual(
            result["counts"],
            {"left_only": 1, "right_only": 1, "same_change": 1, "conflict": 1, "total": 4},
        )
        self.assertTrue(result["would_conflict"])

    def test_records_are_sorted_by_pointer(self):
        result = analyze_merge(
            base={"b": 1, "a": {"x": 1}, "c": 1},
            left={"b": 2, "a": {"x": 2}, "c": 1},
            right={"b": 1, "a": {"x": 1}, "c": 3},
        )
        pointers = pointers_of(result)
        self.assertEqual(pointers, sorted(pointers))

    def test_deleted_subtree_is_single_minimal_record(self):
        result = analyze_merge(
            base={"a": {"x": 1, "y": {"z": 2}}},
            left={},
            right={"a": {"x": 1, "y": {"z": 2}}},
        )
        self.assertEqual(pointers_of(result), ["/a"])
        record = find(result, "/a")
        self.assertEqual(record["type"], LEFT_ONLY)
        self.assertIsNone(record["left"])
        self.assertEqual(record["missing"], ["left"])

    def test_delete_vs_modify_conflicts_at_subtree_root_only(self):
        result = analyze_merge(
            base={"a": {"x": 1, "y": 2}},
            left={},
            right={"a": {"x": 9, "y": 2}},
        )
        self.assertEqual(pointers_of(result), ["/a"])
        self.assertEqual(find(result, "/a")["type"], CONFLICT)
        self.assertTrue(result["would_conflict"])

    def test_added_subtree_is_single_minimal_record(self):
        subtree = {"x": 1, "y": {"z": [1, 2]}}
        result = analyze_merge(
            base={}, left={"a": subtree}, right={"a": subtree}
        )
        self.assertEqual(pointers_of(result), ["/a"])
        record = find(result, "/a")
        self.assertEqual(record["type"], SAME_CHANGE)
        self.assertEqual(record["missing"], ["base"])
        self.assertEqual(record["left"], subtree)

    def test_type_replacement_is_one_record_without_descendants(self):
        result = analyze_merge(
            base={"o": {"x": 1, "y": 2}},
            left={"o": [1, 2]},
            right={"o": {"x": 1, "y": 2}},
        )
        self.assertEqual(pointers_of(result), ["/o"])
        self.assertEqual(find(result, "/o")["type"], LEFT_ONLY)

    def test_type_replacement_on_both_sides_conflicts_at_root(self):
        result = analyze_merge(
            base={"o": {"x": 1}}, left={"o": [1]}, right={"o": "scalar"}
        )
        self.assertEqual(pointers_of(result), ["/o"])
        self.assertEqual(find(result, "/o")["type"], CONFLICT)

    def test_identical_object_edit_recurses_to_leaves(self):
        result = analyze_merge(
            base={"o": {"x": 1, "y": 1}},
            left={"o": {"x": 2, "y": 1}},
            right={"o": {"x": 2, "y": 1}},
        )
        self.assertEqual(pointers_of(result), ["/o/x"])
        self.assertEqual(find(result, "/o/x")["type"], SAME_CHANGE)

    def test_divergent_new_subtrees_recurse_by_key(self):
        result = analyze_merge(
            base={},
            left={"o": {"a": 1}},
            right={"o": {"b": 2}},
        )
        self.assertEqual(
            pointers_of(result), ["/o/a", "/o/b"]
        )
        self.assertEqual(find(result, "/o/a")["missing"], ["base", "right"])
        self.assertEqual(find(result, "/o/b")["missing"], ["base", "left"])

    def test_missing_key_distinct_from_explicit_null(self):
        # Left sets the key to null; right deletes the key: different changes.
        result = analyze_merge(
            base={"a": 1}, left={"a": None}, right={}
        )
        record = find(result, "/a")
        self.assertEqual(record["type"], CONFLICT)
        self.assertIsNone(record["left"])
        self.assertEqual(record["missing"], ["right"])

        # Both sides explicitly set null: a same_change with no missing side.
        result = analyze_merge(base={"a": 1}, left={"a": None}, right={"a": None})
        record = find(result, "/a")
        self.assertEqual(record["type"], SAME_CHANGE)
        self.assertEqual(record["missing"], [])

        # One side adds an explicit null, the other leaves the key absent.
        result = analyze_merge(base={}, left={"a": None}, right={})
        record = find(result, "/a")
        self.assertEqual(record["type"], LEFT_ONLY)
        self.assertIsNone(record["left"])
        self.assertEqual(record["missing"], ["base", "right"])

    def test_root_level_records(self):
        divergent = analyze_merge(1, 2, 3)
        self.assertEqual(pointers_of(divergent), [""])
        self.assertEqual(find(divergent, "")["type"], CONFLICT)
        self.assertTrue(divergent["would_conflict"])

        same = analyze_merge(1, 2, 2)
        self.assertEqual(find(same, "")["type"], SAME_CHANGE)
        self.assertFalse(same["would_conflict"])

    def test_no_changes(self):
        doc = {"a": [1, 2], "b": {"c": None}}
        result = analyze_merge(doc, doc, doc)
        self.assertEqual(result["changes"], [])
        self.assertEqual(
            result["counts"],
            {"left_only": 0, "right_only": 0, "same_change": 0, "conflict": 0, "total": 0},
        )
        self.assertFalse(result["would_conflict"])

    def test_pointer_escaping(self):
        result = analyze_merge(
            base={"a/b": 1, "c~d": 1},
            left={"a/b": 2, "c~d": 2},
            right={"a/b": 2, "c~d": 2},
        )
        self.assertEqual(pointers_of(result), ["/a~1b", "/c~0d"])

    def test_conflict_decisions_match_merge_engine(self):
        # would_conflict and conflict pointers must equal merge3's output.
        cases = [
            ({"a": 1}, {"a": 2}, {"a": 3}),
            ({"a": {"x": 1}}, {}, {"a": {"x": 2}}),
            ({"l": [1]}, {"l": [1, 2]}, {"l": [1, 3]}),
            (1, 2, 3),
            ({}, {"x": 1}, {"x": 2}),
            ({"a": 1}, {"a": 1, "b": 2}, {"a": 1, "b": 2}),
            (None, {"x": 1}, {"y": 2}),
        ]
        for base, left, right in cases:
            with self.subTest(base=base, left=left, right=right):
                _, conflicts = merge3(base, left, right)
                result = analyze_merge(base, left, right)
                engine_pointers = sorted(c["pointer"] for c in conflicts)
                analysis_pointers = pointers_of(result, CONFLICT)
                self.assertEqual(analysis_pointers, engine_pointers)
                self.assertEqual(result["would_conflict"], bool(conflicts))

    def test_repeated_requests_are_identical(self):
        args = (
            {"replicas": 1, "db": {"host": "a", "port": 5432}},
            {"replicas": 3, "db": {"host": "b", "port": 5432}},
            {"replicas": 1, "db": {"host": "c", "port": 5433}},
        )
        self.assertEqual(analyze_merge(*args), analyze_merge(*args))


if __name__ == "__main__":
    unittest.main()
