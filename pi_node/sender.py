from datetime import datetime, timedelta, timezone

from config import (
    DELIVERY_LEASE_SECONDS,
    NODE_URL,
    SEND_BATCH_SIZE,
    SEND_IF_OLDEST_PENDING_SECONDS,
    SEND_IF_PENDING_AT_LEAST,
    SENDER_RETRY_DELAYS_SEC,
    SENDER_RETRY_MAX_SEC,
    SQLITE_PATH,
)
from pi_node.api_client import post_batch
from storage.database import (
    claim_eligible_pending,
    count_new_pending,
    has_due_retry,
    mark_rows_sent,
    oldest_new_pending_created_at,
    quarantine_rows,
    release_lease,
    schedule_rows_retry,
)


def retry_delay_seconds(attempt_number: int) -> int:
    index = max(0, int(attempt_number) - 1)
    if index < len(SENDER_RETRY_DELAYS_SEC):
        return int(SENDER_RETRY_DELAYS_SEC[index])
    return int(SENDER_RETRY_MAX_SEC)


def should_send(db_path=SQLITE_PATH, *, force: bool = False) -> bool:
    if not NODE_URL:
        return False
    if force or has_due_retry(db_path):
        return True
    if count_new_pending(db_path) >= SEND_IF_PENDING_AT_LEAST:
        return True
    oldest = oldest_new_pending_created_at(db_path)
    if not oldest:
        return False
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(oldest)
        return age.total_seconds() >= SEND_IF_OLDEST_PENDING_SECONDS
    except ValueError:
        return True


def _retry_updates(rows) -> list[tuple[int, int, str]]:
    now = datetime.now(timezone.utc)
    updates = []
    for row in rows:
        attempts = int(row["attempts"] or 0) + 1
        retry_at = now + timedelta(seconds=retry_delay_seconds(attempts))
        updates.append(
            (int(row["id"]), attempts, retry_at.isoformat(timespec="seconds"))
        )
    return updates


def send_eligible_once(db_path=SQLITE_PATH, *, force: bool = False) -> bool:
    if not should_send(db_path, force=force):
        return False

    lease_token, rows = claim_eligible_pending(
        db_path,
        limit=SEND_BATCH_SIZE,
        lease_seconds=DELIVERY_LEASE_SECONDS,
    )
    if not rows or not lease_token:
        return False

    by_record_id = {str(row["record_id"]): row for row in rows}
    try:
        result = post_batch(rows)
        accepted_rows = [
            int(by_record_id[record_id]["id"])
            for record_id in result.accepted_record_ids
            if record_id in by_record_id
        ]
        if accepted_rows:
            mark_rows_sent(
                db_path,
                accepted_rows,
                lease_token=lease_token,
            )

        quarantined_rows = [
            int(by_record_id[record_id]["id"])
            for record_id in result.rejected
            if record_id in by_record_id
        ]
        if quarantined_rows:
            quarantine_rows(
                db_path,
                quarantined_rows,
                error="; ".join(result.rejected.values()),
                lease_token=lease_token,
            )

        retry_rows = [
            by_record_id[record_id]
            for record_id in result.retryable_record_ids
            if record_id in by_record_id
        ]
        if retry_rows:
            schedule_rows_retry(
                db_path,
                _retry_updates(retry_rows),
                error=result.error or "backend requested retry",
                lease_token=lease_token,
            )

        release_lease(db_path, lease_token)
        print(
            "📌 delivery_result "
            f"accepted={len(accepted_rows)} "
            f"retry={len(retry_rows)} "
            f"quarantined={len(quarantined_rows)}"
        )
        return bool(accepted_rows)
    except Exception as exc:
        schedule_rows_retry(
            db_path,
            _retry_updates(rows),
            error=str(exc),
            lease_token=lease_token,
        )
        print(f"delivery_error rows={len(rows)} error={exc}")
        return False
