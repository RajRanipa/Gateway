import math
import time

from config import PLC_CONNECT_RETRY_SEC, PLC_POLL_MS, SQLITE_PATH
from plc_pi.client import PLCClient, READY
from storage.database import (
    clear_capture_state,
    init_db,
    mark_capture_acknowledged,
)
from storage.repository import save_plc_event


def validate_snapshot(source: str, record: dict) -> None:
    if int(record["eventSequence"]) <= 0:
        raise ValueError(
            f"{source}: invalid eventSequence={record['eventSequence']}"
        )
    if record["scaleNo"] not in (1, 2, 3):
        raise ValueError(f"{source}: invalid scaleNo={record['scaleNo']}")
    if record["productCode"] not in (1, 2, 3, 4, 5):
        raise ValueError(
            f"{source}: unsupported productCode={record['productCode']}"
        )
    weight = float(record["weight"])
    if not math.isfinite(weight) or weight <= 0:
        raise ValueError(f"{source}: invalid captured weight={weight}")
    if record["productCode"] != 5:
        for field in ("temperature", "sizeCode"):
            if int(record[field]) <= 0:
                raise ValueError(f"{source}: invalid {field}={record[field]}")


def run() -> None:
    init_db(SQLITE_PATH)
    plc = PLCClient()
    print("gateway_capture_started")

    while True:
        try:
            plc.ensure_connected()
            states = plc.read_states()

            for source, state in states.items():
                if state == 0:
                    # A full 1 -> 2 -> 0 PLC cycle separates identical products.
                    clear_capture_state(SQLITE_PATH, source)
                    continue
                if state != READY:
                    continue

                snapshot = plc.read_ready_snapshot(source)
                validate_snapshot(source, snapshot)
                inserted, record, key = save_plc_event(source, snapshot)

                # Both new and replayed rows are durably present. ACKing a replay
                # is required to recover from a process crash after SQLite commit.
                plc.acknowledge_persisted(source)
                mark_capture_acknowledged(SQLITE_PATH, source)
                action = "captured" if inserted else "recovered_ack"
                print(
                    f"{action} source={source} recordId={record['recordId']} "
                    f"weightKg={record['weight']} key={key}"
                )

            time.sleep(PLC_POLL_MS / 1000.0)
        except KeyboardInterrupt:
            plc.disconnect()
            print("gateway_capture_stopped")
            return
        except Exception as exc:
            print(f"gateway_capture_error error={exc}")
            plc.disconnect()
            time.sleep(PLC_CONNECT_RETRY_SEC)


if __name__ == "__main__":
    run()
