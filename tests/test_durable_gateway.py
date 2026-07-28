import json
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

if importlib.util.find_spec("requests") is None:
    requests_stub = types.ModuleType("requests")
    requests_stub.RequestException = Exception
    requests_stub.Session = lambda: type(
        "Session",
        (),
        {"post": lambda *_args, **_kwargs: None},
    )()
    sys.modules["requests"] = requests_stub

from pi_node.api_client import post_batch
from storage.database import (
    claim_eligible_pending,
    clear_capture_state,
    connect,
    init_db,
    mark_rows_sent,
)
from storage.repository import save_plc_event


SNAPSHOT = {
    "scaleNo": 1,
    "productCode": 1,
    "temperature": 1260,
    "density": 64,
    "sizeCode": 1,
    "weight": 7.65,
    "status": 1,
    "plcStatus": 1,
}


class DurableCaptureTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "queue.db"
        init_db(self.db_path)

    def tearDown(self):
        self.tempdir.cleanup()

    @patch("storage.repository._append_jsonl")
    def test_reread_of_same_ready_flag_reuses_identity(self, _backup):
        inserted_1, record_1, _ = save_plc_event(
            "scale-1",
            SNAPSHOT,
            db_path=self.db_path,
            gateway_id="test-gateway",
        )
        inserted_2, record_2, _ = save_plc_event(
            "scale-1",
            SNAPSHOT,
            db_path=self.db_path,
            gateway_id="test-gateway",
        )

        self.assertTrue(inserted_1)
        self.assertFalse(inserted_2)
        self.assertEqual(record_1["recordId"], record_2["recordId"])
        con = connect(self.db_path)
        try:
            self.assertEqual(
                con.execute("SELECT COUNT(*) FROM records").fetchone()[0],
                1,
            )
        finally:
            con.close()

    @patch("storage.repository._append_jsonl")
    def test_idle_transition_allows_identical_next_product(self, _backup):
        _, first, _ = save_plc_event(
            "scale-1",
            SNAPSHOT,
            db_path=self.db_path,
            gateway_id="test-gateway",
        )
        clear_capture_state(self.db_path, "scale-1")
        _, second, _ = save_plc_event(
            "scale-1",
            SNAPSHOT,
            db_path=self.db_path,
            gateway_id="test-gateway",
        )
        self.assertNotEqual(first["recordId"], second["recordId"])

    @patch("storage.repository._append_jsonl")
    def test_delivery_lease_prevents_two_senders_claiming_same_row(self, _backup):
        save_plc_event(
            "scale-1",
            SNAPSHOT,
            db_path=self.db_path,
            gateway_id="test-gateway",
        )
        token_1, rows_1 = claim_eligible_pending(
            self.db_path,
            limit=5,
            lease_seconds=120,
        )
        token_2, rows_2 = claim_eligible_pending(
            self.db_path,
            limit=5,
            lease_seconds=120,
        )
        self.assertIsNotNone(token_1)
        self.assertEqual(len(rows_1), 1)
        self.assertIsNone(token_2)
        self.assertEqual(rows_2, [])

        updated = mark_rows_sent(
            self.db_path,
            [rows_1[0]["id"]],
            lease_token=token_1,
        )
        self.assertEqual(updated, 1)

    @patch("storage.repository._append_jsonl")
    def test_snapshot_mutation_while_ready_is_rejected(self, _backup):
        save_plc_event(
            "scale-1",
            SNAPSHOT,
            db_path=self.db_path,
            gateway_id="test-gateway",
        )
        changed = json.loads(json.dumps(SNAPSHOT))
        changed["weight"] = 8.0
        with self.assertRaisesRegex(RuntimeError, "changed while"):
            save_plc_event(
                "scale-1",
                changed,
                db_path=self.db_path,
                gateway_id="test-gateway",
            )

    @patch("pi_node.api_client._session.post")
    def test_sender_requires_ack_for_each_record(self, post):
        class Response:
            status_code = 200
            text = ""

            @staticmethod
            def json():
                return {
                    "data": {
                        "recordResults": [
                            {
                                "recordId": "accepted-id",
                                "accepted": True,
                                "retryable": False,
                            }
                        ]
                    }
                }

        post.return_value = Response()
        rows = [
            {
                "payload": json.dumps(
                    {**SNAPSHOT, "recordId": "accepted-id", "at": "2026-01-01T00:00:00+00:00"}
                )
            },
            {
                "payload": json.dumps(
                    {**SNAPSHOT, "recordId": "missing-id", "at": "2026-01-01T00:00:01+00:00"}
                )
            },
        ]
        result = post_batch(rows)
        self.assertEqual(result.accepted_record_ids, {"accepted-id"})
        self.assertEqual(result.retryable_record_ids, {"missing-id"})

    @patch("pi_node.api_client._session.post")
    def test_unrecognized_2xx_response_never_deletes_local_rows(self, post):
        class Response:
            status_code = 200
            text = ""

            @staticmethod
            def json():
                return {"data": {"summary": {"inserted": 1}}}

        post.return_value = Response()
        rows = [
            {
                "payload": json.dumps(
                    {**SNAPSHOT, "recordId": "record-1", "at": "2026-01-01T00:00:00+00:00"}
                )
            }
        ]
        result = post_batch(rows)
        self.assertFalse(result.accepted_record_ids)
        self.assertEqual(result.retryable_record_ids, {"record-1"})


if __name__ == "__main__":
    unittest.main()
