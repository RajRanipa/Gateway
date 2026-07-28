import time

from config import (
    SENT_RETENTION_DAYS,
    SENDER_POLL_SEC,
    SQLITE_PATH,
)
from pi_node.sender import send_eligible_once
from storage.database import init_db, prune_sent


def run() -> None:
    init_db(SQLITE_PATH)
    deleted = prune_sent(SQLITE_PATH, SENT_RETENTION_DAYS)
    print(f"gateway_sender_started prunedSent={deleted}")
    send_eligible_once(SQLITE_PATH, force=True)

    while True:
        try:
            send_eligible_once(SQLITE_PATH)
            time.sleep(SENDER_POLL_SEC)
        except KeyboardInterrupt:
            print("gateway_sender_stopped")
            return
        except Exception as exc:
            print(f"gateway_sender_error error={exc}")
            time.sleep(SENDER_POLL_SEC)


if __name__ == "__main__":
    run()

