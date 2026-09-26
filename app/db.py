"""SQLite persistence. Schema is created on application startup."""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager

from .audit import METHODS

DATABASE_PATH = os.environ.get("DATABASE_PATH", "./merges.db")

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

# Used in the audit table's CHECK constraint: "... method IN ('resolutions',
# 'rule-resolutions', ...)". Rebuilt whenever a new method (resolution-undo)
# is introduced, so existing database files keep accepting events.
_AUDIT_METHODS_SQL = ", ".join(f"'{method}'" for method in METHODS)

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS merges (
    id              TEXT PRIMARY KEY,
    idempotency_key TEXT UNIQUE,
    status          TEXT NOT NULL CHECK (status IN ('pending', 'resolved')),
    revision        INTEGER NOT NULL,
    base_doc        TEXT NOT NULL,
    left_doc        TEXT NOT NULL,
    right_doc       TEXT NOT NULL,
    document        TEXT NOT NULL,
    conflicts       TEXT NOT NULL,
    -- Conflict snapshot as first recorded, preserved across resolutions
    -- so later merges can reuse this order's historical adjudications.
    source_conflicts TEXT,
    -- Immutable initial working document (the merge3 output with every
    -- conflict placeholder). Resolution undo rebuilds the document and the
    -- open conflicts by replaying audit events from this snapshot, never by
    -- reverse-editing the current document.
    initial_document TEXT,
    result          TEXT,
    result_schema   TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS merge_drafts (
    id              TEXT PRIMARY KEY,
    merge_id        TEXT NOT NULL REFERENCES merges (id),
    status          TEXT NOT NULL CHECK (status IN ('open', 'committed')),
    base_revision   INTEGER NOT NULL,
    document        TEXT NOT NULL,
    conflicts       TEXT NOT NULL,
    resolutions     TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- Adjudication audit trail: one row per successful state-changing
-- resolution request, chained per merge order via prev_hash/hash.
CREATE TABLE IF NOT EXISTS merge_audit_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    merge_id        TEXT NOT NULL REFERENCES merges (id),
    seq             INTEGER NOT NULL,
    method          TEXT NOT NULL CHECK (method IN ({_AUDIT_METHODS_SQL})),
    revision_before INTEGER NOT NULL,
    revision_after  INTEGER NOT NULL,
    pointers        TEXT NOT NULL,
    detail          TEXT NOT NULL,
    prev_hash       TEXT NOT NULL,
    hash            TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    UNIQUE (merge_id, seq)
);
"""


def _ensure_database_directory() -> None:
    directory = os.path.dirname(os.path.abspath(DATABASE_PATH))
    os.makedirs(directory, exist_ok=True)


def connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _ensure_database_directory()
        _conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA foreign_keys=ON")
    return _conn


@contextmanager
def read_snapshot():
    """Run a multi-query read-only request on one shared SQLite snapshot.

    A separate short-lived connection (not the writer connection, and not
    taking the global write lock) opens one explicit deferred transaction.
    In WAL mode every read inside that transaction sees the same database
    snapshot — the one established by the first read — so adjudication
    commits landing between the statements of one request cannot mix old
    and new states. The connection is ``query_only`` and the transaction is
    rolled back on exit, so a snapshot read provably writes nothing.
    A subsequent request opens a new snapshot and therefore sees the
    committed new state.
    """
    _ensure_database_directory()
    # isolation_level=None puts the driver in autocommit mode, making our
    # BEGIN/ROLLBACK below the only transaction control (no implicit
    # transactions started around individual statements).
    conn = sqlite3.connect(DATABASE_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only = ON")
        conn.execute("BEGIN")
        yield conn
    finally:
        conn.rollback()
        conn.close()


# Objects created after the initial release; applied to database files that
# predate them so an existing Docker volume keeps working. The merge_drafts
# table is part of SCHEMA above (CREATE TABLE IF NOT EXISTS runs on every
# startup); only column-level changes need an ALTER here. Each entry pairs
# the statement with the column it adds, for an idempotent existence check.
_MIGRATIONS = (
    ("ALTER TABLE merges ADD COLUMN result_schema TEXT", "result_schema"),
    ("ALTER TABLE merges ADD COLUMN source_conflicts TEXT", "source_conflicts"),
    ("ALTER TABLE merges ADD COLUMN initial_document TEXT", "initial_document"),
)


def _audit_table_accepts_all_methods(conn: sqlite3.Connection) -> bool:
    """Whether the stored audit table's CHECK already lists every method.

    A database file created before ``resolution-undo`` existed keeps the
    old CHECK constraint (a CREATE TABLE constraint cannot be ALTERed), so
    inserting the new method would raise. The table then needs a one-time
    rebuild with the widened constraint; the rows are copied verbatim, so
    stored hashes and sequence numbers are untouched.
    """
    create_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' "
        "AND name = 'merge_audit_events'"
    ).fetchone()
    if create_sql is None:
        # SCHEMA creates the table immediately afterwards; nothing to migrate.
        return True
    # The stored CREATE TABLE lists each allowed method as a quoted literal;
    # membership on that exact token is independent of the constraint's
    # spacing/line breaks.
    constraint = create_sql["sql"]
    return all(f"'{method}'" in constraint for method in METHODS)


def init_schema() -> None:
    with _lock:
        conn = connect()
        conn.executescript(SCHEMA)
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(merges)")}
        for statement, column in _MIGRATIONS:
            if column not in existing:
                conn.execute(statement)
        if not _audit_table_accepts_all_methods(conn):
            # SQLite cannot ALTER a CHECK constraint: rebuild the table,
            # copying every stored event verbatim (hashes preserved).
            # Bound parameters are prohibited inside CHECK constraints, so
            # the method list (a fixed code constant, never user input) is
            # interpolated like in SCHEMA above.
            conn.execute("ALTER TABLE merge_audit_events RENAME TO merge_audit_events_old")
            conn.execute(
                f"""
                CREATE TABLE merge_audit_events (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    merge_id        TEXT NOT NULL REFERENCES merges (id),
                    seq             INTEGER NOT NULL,
                    method          TEXT NOT NULL CHECK (method IN ({_AUDIT_METHODS_SQL})),
                    revision_before INTEGER NOT NULL,
                    revision_after  INTEGER NOT NULL,
                    pointers        TEXT NOT NULL,
                    detail          TEXT NOT NULL,
                    prev_hash       TEXT NOT NULL,
                    hash            TEXT NOT NULL,
                    created_at      TEXT NOT NULL,
                    UNIQUE (merge_id, seq)
                )
                """
            )
            conn.execute(
                """
                INSERT INTO merge_audit_events (
                    id, merge_id, seq, method, revision_before, revision_after,
                    pointers, detail, prev_hash, hash, created_at
                )
                SELECT id, merge_id, seq, method, revision_before, revision_after,
                       pointers, detail, prev_hash, hash, created_at
                FROM merge_audit_events_old
                """
            )
            conn.execute("DROP TABLE merge_audit_events_old")
        conn.commit()


def execute(query: str, params: tuple = ()) -> sqlite3.Cursor:
    with _lock:
        cur = connect().execute(query, params)
        connect().commit()
        return cur


@contextmanager
def transaction():
    """Run a read/check/write sequence as one atomic transaction.

    The global lock is held for the whole block, so rows read inside cannot
    change underneath the caller; any exception rolls everything back and
    nothing is partially written.
    """
    with _lock:
        conn = connect()
        try:
            yield conn
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()


def query_one(query: str, params: tuple = ()) -> sqlite3.Row | None:
    with _lock:
        return connect().execute(query, params).fetchone()


def query_all(query: str, params: tuple = ()) -> list[sqlite3.Row]:
    with _lock:
        return connect().execute(query, params).fetchall()
