"""Tests for ``db.read_snapshot`` — one shared read snapshot per request.

All statements inside one ``read_snapshot`` block must see the same SQLite
snapshot even when another connection commits writes between them; a new
block opens a fresh snapshot. The snapshot connection is read-only and
rolls back its (never-started-write) transaction on exit.
"""

import os
import tempfile

os.environ["DATABASE_PATH"] = os.path.join(tempfile.mkdtemp(), "test_snapshot.db")

import sqlite3  # noqa: E402

from app import db  # noqa: E402


def _seed(merge_id="snapshot-probe"):
    db.init_schema()
    with db.transaction() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO merges (id, idempotency_key, status, "
            "revision, base_doc, left_doc, right_doc, document, conflicts, "
            "source_conflicts, initial_document, result, result_schema, "
            "created_at, updated_at) "
            "VALUES (?, NULL, 'pending', 1, '{}', '{}', '{}', '{}', '[]', "
            "'[]', '{}', NULL, NULL, 't', 't')",
            (merge_id,),
        )


def test_concurrent_commit_between_reads_is_not_visible_within_one_snapshot():
    _seed()

    with db.read_snapshot() as conn:
        before = conn.execute("SELECT COUNT(*) FROM merges").fetchone()[0]

        # Another connection (the writer path, holding the global lock)
        # commits a brand-new merge while this snapshot transaction is open.
        with db.transaction() as writer:
            writer.execute(
                "INSERT INTO merges (id, idempotency_key, status, revision, "
                "base_doc, left_doc, right_doc, document, conflicts, "
                "source_conflicts, initial_document, result, result_schema, "
                "created_at, updated_at) "
                "VALUES (?, NULL, 'pending', 1, '{}', '{}', '{}', '{}', "
                "'[]', '[]', '{}', NULL, NULL, 't', 't')",
                ("committed-mid-snapshot",),
            )

        # A second read in the same block must see the same snapshot.
        during = conn.execute("SELECT COUNT(*) FROM merges").fetchone()[0]
        assert during == before

    # A fresh block opens a fresh snapshot and observes the commit.
    with db.read_snapshot() as conn:
        after = conn.execute("SELECT COUNT(*) FROM merges").fetchone()[0]
    assert after == before + 1

    # The committed row is visible to the normal writer/reader connection.
    row = db.query_one(
        "SELECT id FROM merges WHERE id = ?", ("committed-mid-snapshot",)
    )
    assert row is not None


def test_read_snapshot_is_read_only():
    _seed()
    with db.read_snapshot() as conn:
        for statement in (
            "INSERT INTO merges (id, status, revision, base_doc, left_doc, "
            "right_doc, document, conflicts, created_at, updated_at) "
            "VALUES ('x', 'pending', 1, '{}', '{}', '{}', '{}', '[]', 't', 't')",
            "UPDATE merges SET revision = 9 WHERE id = 'snapshot-probe'",
            "DELETE FROM merges",
            "CREATE TABLE should_not_exist (x)",
        ):
            try:
                conn.execute(statement)
            except sqlite3.Error:
                pass
            else:  # pragma: no cover - query_only must reject every write
                raise AssertionError(f"snapshot allowed a write: {statement}")

    row = db.query_one(
        "SELECT revision FROM merges WHERE id = ?", ("snapshot-probe",)
    )
    assert row["revision"] == 1
    assert db.query_one(
        "SELECT name FROM sqlite_master WHERE name = 'should_not_exist'"
    ) is None
