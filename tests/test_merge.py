"""Unit tests for the pure three-way merge engine (no dependencies)."""

import unittest

from app.merge import PLACEHOLDER_KEY, merge3, parse_pointer


class MergeEngineTest(unittest.TestCase):
    def test_single_side_change_is_adopted(self):
        base = {"a": 1, "b": 2}
        left = {"a": 10, "b": 2}
        right = {"a": 1, "b": 20}
        doc, conflicts = merge3(base, left, right)
        self.assertEqual(doc, {"a": 10, "b": 20})
        self.assertEqual(conflicts, [])

    def test_both_sides_same_change_merges(self):
        doc, conflicts = merge3({"a": 1}, {"a": 5}, {"a": 5})
        self.assertEqual(doc, {"a": 5})
        self.assertEqual(conflicts, [])

    def test_scalar_conflict_keeps_placeholder_and_candidates(self):
        doc, conflicts = merge3({"a": 1}, {"a": 2}, {"a": 3})
        self.assertEqual(doc, {"a": {PLACEHOLDER_KEY: "/a"}})
        self.assertEqual(len(conflicts), 1)
        c = conflicts[0]
        self.assertEqual(c["pointer"], "/a")
        self.assertEqual((c["base"], c["left"], c["right"]), (1, 2, 3))
        self.assertEqual(c["missing"], [])

    def test_nested_recursion_by_json_pointer(self):
        base = {"svc": {"replicas": 1, "image": "v1"}}
        left = {"svc": {"replicas": 3, "image": "v1"}}
        right = {"svc": {"replicas": 1, "image": "v2"}}
        doc, conflicts = merge3(base, left, right)
        self.assertEqual(doc, {"svc": {"replicas": 3, "image": "v2"}})
        self.assertEqual(conflicts, [])

    def test_nested_conflict_pointer(self):
        base = {"svc": {"db": {"host": "a"}}}
        left = {"svc": {"db": {"host": "b"}}}
        right = {"svc": {"db": {"host": "c"}}}
        doc, conflicts = merge3(base, left, right)
        self.assertEqual(conflicts[0]["pointer"], "/svc/db/host")
        self.assertEqual(doc["svc"]["db"]["host"], {PLACEHOLDER_KEY: "/svc/db/host"})

    def test_delete_vs_unchanged_deletes(self):
        doc, conflicts = merge3({"a": 1, "b": 2}, {"b": 2}, {"a": 1, "b": 2})
        self.assertEqual(doc, {"b": 2})
        self.assertEqual(conflicts, [])

    def test_delete_vs_modify_conflicts(self):
        doc, conflicts = merge3({"a": 1}, {}, {"a": 9})
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["missing"], ["left"])
        self.assertEqual(conflicts[0]["right"], 9)

    def test_both_added_same_key_different_value_conflicts(self):
        doc, conflicts = merge3({}, {"x": 1}, {"x": 2})
        self.assertEqual(conflicts[0]["missing"], ["base"])

    def test_both_added_objects_merge_recursively(self):
        doc, conflicts = merge3({}, {"o": {"a": 1}}, {"o": {"b": 2}})
        self.assertEqual(doc, {"o": {"a": 1, "b": 2}})
        self.assertEqual(conflicts, [])

    def test_arrays_are_scalar_conflicts(self):
        doc, conflicts = merge3({"l": [1]}, {"l": [1, 2]}, {"l": [1, 3]})
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["left"], [1, 2])
        self.assertEqual(conflicts[0]["right"], [1, 3])

    def test_pointer_escaping(self):
        self.assertEqual(parse_pointer("/a~1b/c~0d"), ["a/b", "c~d"])
        self.assertEqual(parse_pointer(""), [])


if __name__ == "__main__":
    unittest.main()
