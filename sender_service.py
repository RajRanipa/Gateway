import time

from config import (
    NODE_URL,
    SEND_BATCH_SIZE,
    SEND_IF_PENDING_AT_LEAST,
    SENT_RETENTION_DAYS,
    SENDER_POLL_SEC,
    SQLITE_PATH,
)
from pi_node.sender import send_eligible_once
from storage.database import init_db, prune_sent


def run() -> None:
    init_db(SQLITE_PATH)
    deleted = prune_sent(SQLITE_PATH, SENT_RETENTION_DAYS)
    print(
        "gateway_sender_started "
        f"sqlitePath={SQLITE_PATH} "
        f"nodeUrl={NODE_URL or 'NOT_CONFIGURED'} "
        f"sendThreshold={SEND_IF_PENDING_AT_LEAST} "
        f"batchSize={SEND_BATCH_SIZE} "
        f"pollSeconds={SENDER_POLL_SEC} "
        f"prunedSent={deleted}",
        flush=True,
    )
    send_eligible_once(SQLITE_PATH, force=True)

    while True:
        try:
            send_eligible_once(SQLITE_PATH)
            time.sleep(SENDER_POLL_SEC)
        except KeyboardInterrupt:
            print("gateway_sender_stopped", flush=True)
            return
        except Exception as exc:
            print(f"gateway_sender_error error={exc}", flush=True)
            time.sleep(SENDER_POLL_SEC)


if __name__ == "__main__":
    run()
