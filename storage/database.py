import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from utility.time_utils import utc_now_iso


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path, timeout=10.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=FULL")
    con.execute("PRAGMA busy_timeout=10000")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def _column_exists(con: sqlite3.Connection, table: str, column: str) -> bool:
    return any(
        row["name"] == column
        for row in con.execute(f"PRAGMA table_info({table})").fetchall()
    )


def init_db(db_path: Path) -> None:
    con = connect(db_path)
    try:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                unique_key TEXT NOT NULL UNIQUE,
                record_id TEXT NOT NULL UNIQUE,
                source TEXT,
                at TEXT NOT NULL,
                payload TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING'
                    CHECK(status IN ('PENDING', 'SENT', 'QUARANTINED')),
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                last_attempt_at TEXT,
                next_retry_at TEXT,
                lease_token TEXT,
                lease_expires_at TEXT,
                created_at TEXT NOT NULL,
                sent_at TEXT
            );

            CREATE TABLE IF NOT EXISTS capture_state (
                source TEXT PRIMARY KEY,
                fingerprint TEXT NOT NULL,
                record_id TEXT NOT NULL,
                unique_key TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                acknowledged_at TEXT,
                FOREIGN KEY(record_id) REFERENCES records(record_id)
            );

            CREATE TABLE IF NOT EXISTS kv (
                k TEXT PRIMARY KEY,
                v TEXT
            );
            """
        )

        migrations = {
            "source": "ALTER TABLE records ADD COLUMN source TEXT",
            "last_attempt_at": "ALTER TABLE records ADD COLUMN last_attempt_at TEXT",
            "next_retry_at": "ALTER TABLE records ADD COLUMN next_retry_at TEXT",
            "lease_token": "ALTER TABLE records ADD COLUMN lease_token TEXT",
            "lease_expires_at": "ALTER TABLE records ADD COLUMN lease_expires_at TEXT",
        }
        for column, statement in migrations.items():
            if not _column_exists(con, "records", column):
                con.execute(statement)

        # Older versions used FAILED for ordinary network failures.
        con.execute(
            """
            UPDATE records
            SET status='PENDING', next_retry_at=NULL
            WHERE status='FAILED'
            """
        )
        con.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_records_delivery
            ON records(status, next_retry_at, lease_expires_at, created_at);

            CREATE INDEX IF NOT EXISTS idx_records_created
            ON records(created_at);

            CREATE UNIQUE INDEX IF NOT EXISTS idx_records_record_id_unique
            ON records(record_id);
            """
        )
        con.commit()
    finally:
        con.close()


def persist_capture(
    db_path: Path,
    *,
    source: str,
    fingerprint: str,
    record_id: str,
    unique_key: str,
    captured_at: str,
    payload_json: str,
) -> tuple[bool, sqlite3.Row]:
    """
    Atomically persist one PLC event and its active handshake identity.

    When the same ready flag is reread after an ACK/network/process failure,
    return the already committed row instead of creating another product.
    """
    con = connect(db_path)
    try:
        con.execute("BEGIN IMMEDIATE")
        active = con.execute(
            "SELECT * FROM capture_state WHERE source=?",
            (source,),
        ).fetchone()
        if active:
            if active["fingerprint"] != fingerprint:
                raise RuntimeError(
                    f"PLC snapshot changed while {source} remained READY; "
                    "refusing to acknowledge an ambiguous product"
                )
            row = con.execute(
                "SELECT * FROM records WHERE record_id=?",
                (active["record_id"],),
            ).fetchone()
            if not row:
                raise RuntimeError(
                    f"Capture state for {source} references a missing queue row"
                )
            con.commit()
            return False, row

        con.execute(
            """
            INSERT INTO records (
                unique_key, record_id, source, at, payload, status,
                attempts, created_at
            ) VALUES (?, ?, ?, ?, ?, 'PENDING', 0, ?)
            """,
            (
                unique_key,
                record_id,
                source,
                captured_at,
                payload_json,
                utc_now_iso(),
            ),
        )
        con.execute(
            """
            INSERT INTO capture_state (
                source, fingerprint, record_id, unique_key, captured_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (source, fingerprint, record_id, unique_key, captured_at),
        )
        row = con.execute(
            "SELECT * FROM records WHERE record_id=?",
            (record_id,),
        ).fetchone()
        con.commit()
        return True, row
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def mark_capture_acknowledged(db_path: Path, source: str) -> None:
    con = connect(db_path)
    try:
        con.execute(
            "UPDATE capture_state SET acknowledged_at=? WHERE source=?",
            (utc_now_iso(), source),
        )
        con.commit()
    finally:
        con.close()


def clear_capture_state(db_path: Path, source: str) -> None:
    con = connect(db_path)
    try:
        con.execute("DELETE FROM capture_state WHERE source=?", (source,))
        con.commit()
    finally:
        con.close()


def count_new_pending(db_path: Path) -> int:
    con = connect(db_path)
    try:
        row = con.execute(
            """
            SELECT COUNT(*) AS c
            FROM records
            WHERE status='PENDING'
              AND next_retry_at IS NULL
              AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
            """,
            (utc_now_iso(),),
        ).fetchone()
        return int(row["c"])
    finally:
        con.close()


def oldest_new_pending_created_at(db_path: Path) -> str | None:
    con = connect(db_path)
    try:
        row = con.execute(
            """
            SELECT created_at
            FROM records
            WHERE status='PENDING'
              AND next_retry_at IS NULL
            ORDER BY created_at
            LIMIT 1
            """
        ).fetchone()
        return row["created_at"] if row else None
    finally:
        con.close()


def has_due_retry(db_path: Path) -> bool:
    now = utc_now_iso()
    con = connect(db_path)
    try:
        return con.execute(
            """
            SELECT 1
            FROM records
            WHERE status='PENDING'
              AND next_retry_at IS NOT NULL
              AND next_retry_at <= ?
              AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
            LIMIT 1
            """,
            (now, now),
        ).fetchone() is not None
    finally:
        con.close()


def claim_eligible_pending(
    db_path: Path,
    *,
    limit: int,
    lease_seconds: int,
) -> tuple[str | None, list[sqlite3.Row]]:
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat(timespec="seconds")
    lease_token = str(uuid.uuid4())
    lease_expires = (now + timedelta(seconds=lease_seconds)).isoformat(
        timespec="seconds"
    )
    con = connect(db_path)
    try:
        con.execute("BEGIN IMMEDIATE")
        rows = con.execute(
            """
            SELECT *
            FROM records
            WHERE status='PENDING'
              AND (next_retry_at IS NULL OR next_retry_at <= ?)
              AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
            ORDER BY created_at, id
            LIMIT ?
            """,
            (now_iso, now_iso, int(limit)),
        ).fetchall()
        if not rows:
            con.commit()
            return None, []
        ids = [int(row["id"]) for row in rows]
        placeholders = ",".join("?" for _ in ids)
        con.execute(
            f"""
            UPDATE records
            SET lease_token=?, lease_expires_at=?
            WHERE id IN ({placeholders})
            """,
            (lease_token, lease_expires, *ids),
        )
        claimed = con.execute(
            f"SELECT * FROM records WHERE id IN ({placeholders}) ORDER BY id",
            ids,
        ).fetchall()
        con.commit()
        return lease_token, claimed
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def mark_rows_sent(
    db_path: Path,
    row_ids: Iterable[int],
    *,
    lease_token: str,
) -> int:
    ids = [int(value) for value in row_ids]
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    now = utc_now_iso()
    con = connect(db_path)
    try:
        cur = con.execute(
            f"""
            UPDATE records
            SET status='SENT', sent_at=?, last_attempt_at=?,
                last_error=NULL, next_retry_at=NULL,
                lease_token=NULL, lease_expires_at=NULL
            WHERE id IN ({placeholders}) AND lease_token=?
            """,
            (now, now, *ids, lease_token),
        )
        con.commit()
        return int(cur.rowcount or 0)
    finally:
        con.close()


def schedule_rows_retry(
    db_path: Path,
    updates: Iterable[tuple[int, int, str]],
    *,
    error: str,
    lease_token: str,
) -> int:
    values = list(updates)
    if not values:
        return 0
    now = utc_now_iso()
    safe_error = str(error or "delivery failed")[:1000]
    con = connect(db_path)
    try:
        con.executemany(
            """
            UPDATE records
            SET attempts=?, last_error=?, last_attempt_at=?,
                next_retry_at=?, sent_at=NULL,
                lease_token=NULL, lease_expires_at=NULL
            WHERE id=? AND status='PENDING' AND lease_token=?
            """,
            [
                (attempts, safe_error, now, retry_at, row_id, lease_token)
                for row_id, attempts, retry_at in values
            ],
        )
        con.commit()
        return int(con.total_changes)
    finally:
        con.close()


def quarantine_rows(
    db_path: Path,
    row_ids: Iterable[int],
    *,
    error: str,
    lease_token: str,
) -> int:
    ids = [int(value) for value in row_ids]
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    con = connect(db_path)
    try:
        cur = con.execute(
            f"""
            UPDATE records
            SET status='QUARANTINED', last_error=?, last_attempt_at=?,
                lease_token=NULL, lease_expires_at=NULL
            WHERE id IN ({placeholders}) AND lease_token=?
            """,
            (str(error)[:1000], utc_now_iso(), *ids, lease_token),
        )
        con.commit()
        return int(cur.rowcount or 0)
    finally:
        con.close()


def release_lease(db_path: Path, lease_token: str) -> None:
    con = connect(db_path)
    try:
        con.execute(
            """
            UPDATE records
            SET lease_token=NULL, lease_expires_at=NULL
            WHERE lease_token=? AND status='PENDING'
            """,
            (lease_token,),
        )
        con.commit()
    finally:
        con.close()


def kv_get(db_path: Path, key: str) -> str | None:
    con = connect(db_path)
    try:
        row = con.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
        return row["v"] if row else None
    finally:
        con.close()


def kv_set(db_path: Path, key: str, value: str) -> None:
    con = connect(db_path)
    try:
        con.execute(
            """
            INSERT INTO kv(k, v) VALUES(?, ?)
            ON CONFLICT(k) DO UPDATE SET v=excluded.v
            """,
            (key, value),
        )
        con.commit()
    finally:
        con.close()


def prune_sent(db_path: Path, retention_days: int) -> int:
    if retention_days <= 0:
        return 0
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=retention_days)
    ).isoformat(timespec="seconds")
    con = connect(db_path)
    try:
        cur = con.execute(
            "DELETE FROM records WHERE status='SENT' AND sent_at < ?",
            (cutoff,),
        )
        con.commit()
        return int(cur.rowcount or 0)
    finally:
        con.close()
