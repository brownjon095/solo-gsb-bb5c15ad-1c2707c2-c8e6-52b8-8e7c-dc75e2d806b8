"""Unit tests for deterministic RFC 6902 patch generation."""

import copy
import unittest

from app.patch import canonical_json, generate_patch, sha256_hex

from tests.patch_apply import apply_patch


def assert_roundtrip(test, base, result):
    patch = generate_patch(base, result)
    test.assertEqual(apply_patch(base, patch), result)
    # Paths are emitted in lexicographic order.
    paths = [op["path"] for op in patch]
    test.assertEqual(paths, sorted(paths))
    return patch


class PatchGenerationTest(unittest.TestCase):
    def test_equal_documents_emit_empty_patch(self):
        base = {"a": 1, "b": {"c": [1, 2]}, "x": None}
        self.assertEqual(generate_patch(base, copy.deepcopy(base)), [])

    def test_nested_add_remove_replace_and_ordering(self):
        base = {
            "keep": 1,
            "gone": {"deep": 1},
            "changed": 1,
            "nested": {"same": 0, "val": 1, "drop": "x"},
            "list": [1, 2, 3],
        }
        result = {
            "keep": 1,
            "changed": 2,
            "nested": {"same": 0, "val": 2, "added": 9},
            "list": [1, 2, 3, 4],
            "brand": {"new": {"sub": True}},
        }
        patch = assert_roundtrip(self, base, result)
        self.assertEqual(
            patch,
            [
                {"op": "add", "path": "/brand", "value": {"new": {"sub": True}}},
                {"op": "replace", "path": "/changed", "value": 2},
                {"op": "remove", "path": "/gone"},
                {"op": "replace", "path": "/list", "value": [1, 2, 3, 4]},
                {"op": "add", "path": "/nested/added", "value": 9},
                {"op": "remove", "path": "/nested/drop"},
                {"op": "replace", "path": "/nested/val", "value": 2},
            ],
        )

    def test_arrays_are_wholesale_replaced(self):
        base = {"l": [1, 2], "m": [1]}
        result = {"l": [1, 2], "m": "scalar"}
        patch = assert_roundtrip(self, base, result)
        # Only one op: the array change is a single replace, no per-index ops.
        self.assertEqual(
            patch, [{"op": "replace", "path": "/m", "value": "scalar"}]
        )

    def test_pointer_escaping_for_special_keys(self):
        base = {"a/b": 1, "a~b": 2, "": 3}
        result = {"a/b": 10, "a~b": 20, "": 30}
        patch = assert_roundtrip(self, base, result)
        # Escape applied: "~" -> "~0" before "/" -> "~1".
        self.assertEqual(
            [op["path"] for op in patch], ["/", "/a~0b", "/a~1b"]
        )
        self.assertEqual([op["value"] for op in patch], [30, 20, 10])

    def test_root_type_change_is_root_replace(self):
        patch = assert_roundtrip(self, {"a": 1}, [1, 2, 3])
        self.assertEqual(
            patch, [{"op": "replace", "path": "", "value": [1, 2, 3]}]
        )
        patch2 = assert_roundtrip(self, [1, 2, 3], {"a": 1})
        self.assertEqual(
            patch2, [{"op": "replace", "path": "", "value": {"a": 1}}]
        )

    def test_root_scalar_replace(self):
        patch = assert_roundtrip(self, 1, 2)
        self.assertEqual(patch, [{"op": "replace", "path": "", "value": 2}])

    def test_bool_is_not_equal_to_int(self):
        patch = assert_roundtrip(self, {"a": 1}, {"a": True})
        self.assertEqual(
            patch, [{"op": "replace", "path": "/a", "value": True}]
        )
        patch2 = assert_roundtrip(self, [1], [True])
        self.assertEqual(
            patch2, [{"op": "replace", "path": "", "value": [True]}]
        )

    def test_null_changes_are_replaced_or_added(self):
        assert_roundtrip(self, {"a": None}, {"a": 0})
        assert_roundtrip(self, {"a": 0}, {"a": None})
        assert_roundtrip(self, {}, {"a": None})

    def test_added_object_is_single_add_with_whole_subtree(self):
        base = {"x": 1}
        result = {"x": 1, "o": {"k1": 1, "k2": {"k3": 2}}}
        patch = assert_roundtrip(self, base, result)
        self.assertEqual(
            patch,
            [{"op": "add", "path": "/o", "value": {"k1": 1, "k2": {"k3": 2}}}],
        )

    def test_sha256_is_canonical_and_order_independent(self):
        h1 = sha256_hex({"a": 1, "b": [1, 2]})
        h2 = sha256_hex({"b": [1, 2], "a": 1})
        self.assertEqual(h1, h2)
        self.assertNotEqual(h1, sha256_hex({"a": 1, "b": [1, 3]}))
        self.assertEqual(canonical_json({"b": 2, "a": 1}), b'{"a":1,"b":2}')
        self.assertEqual(len(h1), 64)


if __name__ == "__main__":
    unittest.main()
