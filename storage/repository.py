import hashlib
import json
import os
import uuid
from pathlib import Path

from config import DATA_DIR, GATEWAY_ID, JSONL_NAME_FMT, SQLITE_PATH
from storage.database import persist_capture
from utility.time_utils import utc_now_iso


def _canonical_json(value: dict) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def snapshot_fingerprint(source: str, snapshot: dict) -> str:
    immutable = {
        "source": source,
        "eventSequence": snapshot["eventSequence"],
        "scaleNo": snapshot["scaleNo"],
        "productCode": snapshot["productCode"],
        "temperature": snapshot["temperature"],
        "density": snapshot["density"],
        "sizeCode": snapshot["sizeCode"],
        "weight": snapshot["weight"],
        "status": snapshot["status"],
    }
    return hashlib.sha256(_canonical_json(immutable).encode("utf-8")).hexdigest()


def _append_jsonl(record: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    from datetime import datetime

    path = DATA_DIR / datetime.now().strftime(JSONL_NAME_FMT)
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with path.open("a", encoding="utf-8") as stream:
        stream.write(line)
        stream.flush()
        os.fsync(stream.fileno())


def save_plc_event(
    source: str,
    snapshot: dict,
    *,
    db_path: Path = SQLITE_PATH,
    gateway_id: str = GATEWAY_ID,
) -> tuple[bool, dict, str]:
    """
    Assign identity once and commit it with the queue row.

    A reread of the same PLC READY state returns the original identity, allowing
    the gateway to safely retry the PLC acknowledgement after a crash.
    """
    fingerprint = snapshot_fingerprint(source, snapshot)
    record_id = str(uuid.uuid4())
    captured_at = utc_now_iso()
    unique_key = f"{gateway_id}:{source}:{record_id}"
    record = {
        "recordId": record_id,
        **snapshot,
        "at": captured_at,
    }
    inserted, row = persist_capture(
        db_path,
        source=source,
        fingerprint=fingerprint,
        record_id=record_id,
        unique_key=unique_key,
        captured_at=captured_at,
        payload_json=_canonical_json(record),
    )
    persisted_record = json.loads(row["payload"])
    if inserted:
        try:
            _append_jsonl(persisted_record)
        except Exception as exc:
            # SQLite remains authoritative and was fsynced before PLC ACK.
            print(f"WARNING jsonl_backup_failed source={source} error={exc}")
    return inserted, persisted_record, str(row["unique_key"])
