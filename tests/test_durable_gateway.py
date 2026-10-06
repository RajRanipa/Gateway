import json
import importlib.util
import sqlite3
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

if importlib.util.find_spec("snap7") is None:
    snap7_stub = types.ModuleType("snap7")
    snap7_stub.client = types.SimpleNamespace(Client=lambda: None)
    sys.modules["snap7"] = snap7_stub

from pi_node.api_client import post_batch
from pi_node.sender import should_send
from plc_pi.client import PLCClient
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
    "eventSequence": 42,
    "productCode": 1,
    "temperature": 1260,
    "density": 64,
    "sizeCode": 1,
    "weight": 7.65,
    "status": 1,
    "plcStatus": 1,
}


class FakeSnap7Client:
    def __init__(self, dbs):
        self.dbs = {db_no: bytearray(value) for db_no, value in dbs.items()}
        self.reads = []
        self.writes = []

    def db_read(self, db_no, offset, size):
        self.reads.append((db_no, offset, size))
        return bytes(self.dbs[db_no][offset:offset + size])

    def db_write(self, db_no, offset, data):
        self.writes.append((db_no, offset, bytes(data)))
        self.dbs[db_no][offset:offset + len(data)] = data

    @staticmethod
    def get_connected():
        return True


def put_int16(block, offset, value):
    import struct

    struct.pack_into(">h", block, offset, value)


def put_int32(block, offset, value):
    import struct

    struct.pack_into(">i", block, offset, value)


def put_real32(block, offset, value):
    import struct

    struct.pack_into(">f", block, offset, value)


class PLCLayoutTests(unittest.TestCase):
    def test_scale_snapshot_uses_only_the_source_specific_db(self):
        block = bytearray(145)
        put_int16(block, 0, 1)
        put_real32(block, 118, 7.65)
        put_int32(block, 132, 42)
        put_int16(block, 136, 1)
        put_int16(block, 138, 2)
        put_int16(block, 140, 96)
        put_int16(block, 142, 1260)
        block[144] |= 1

        fake = FakeSnap7Client({1: block})
        plc = PLCClient()
        plc.client = fake

        snapshot = plc.read_ready_snapshot("scale-1")

        self.assertEqual(snapshot["eventSequence"], 42)
        self.assertEqual(snapshot["productCode"], 1)
        self.assertEqual(snapshot["sizeCode"], 2)
        self.assertEqual(snapshot["density"], 96)
        self.assertEqual(snapshot["temperature"], 1260)
        self.assertEqual(snapshot["weight"], 7.65)
        self.assertEqual(snapshot["status"], 1)
        self.assertEqual(fake.reads, [(1, 0, 145), (1, 0, 2)])

    def test_et_snapshot_is_fully_read_from_db12(self):
        block = bytearray(28)
        put_int16(block, 0, 1)
        put_int32(block, 10, 9)
        put_int16(block, 14, 5)
        put_int16(block, 16, 0)
        put_int16(block, 18, 0)
        put_int16(block, 20, 1260)
        put_real32(block, 24, 3.25)

        fake = FakeSnap7Client({12: block})
        plc = PLCClient()
        plc.client = fake

        snapshot = plc.read_ready_snapshot("et")

        self.assertEqual(snapshot["eventSequence"], 9)
        self.assertEqual(snapshot["productCode"], 5)
        self.assertEqual(snapshot["weight"], 3.25)
        self.assertEqual(snapshot["status"], 0)
        self.assertEqual(fake.reads, [(12, 0, 28), (12, 0, 2)])

    def test_lane_quality_mismatch_is_rejected(self):
        block = bytearray(145)
        put_int16(block, 0, 1)
        put_int32(block, 132, 1)
        put_int16(block, 136, 1)

        plc = PLCClient()
        plc.client = FakeSnap7Client({1: block})

        with self.assertRaisesRegex(RuntimeError, "quality mismatch"):
            plc.read_ready_snapshot("scale-1")

    def test_acknowledgement_is_written_and_verified_in_the_source_db(self):
        block = bytearray(2)
        put_int16(block, 0, 1)
        fake = FakeSnap7Client({7: block})
        plc = PLCClient()
        plc.client = fake

        plc.acknowledge_persisted("scale-2")

        self.assertEqual(fake.writes, [(7, 0, b"\x00\x02")])
        self.assertEqual(bytes(fake.dbs[7]), b"\x00\x02")


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
    def test_one_pending_record_is_immediately_eligible_for_delivery(self, _backup):
        save_plc_event(
            "scale-1",
            SNAPSHOT,
            db_path=self.db_path,
            gateway_id="test-gateway",
        )

        with (
            patch("pi_node.sender.NODE_URL", "https://erp.example.test/gateway"),
            patch("pi_node.sender.SEND_IF_PENDING_AT_LEAST", 1),
        ):
            self.assertTrue(should_send(self.db_path))

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

    def test_legacy_duplicate_record_ids_do_not_block_schema_migration(self):
        legacy_path = Path(self.tempdir.name) / "legacy-queue.db"
        con = sqlite3.connect(legacy_path)
        try:
            con.executescript(
                """
                CREATE TABLE records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    unique_key TEXT NOT NULL UNIQUE,
                    record_id INTEGER,
                    at TEXT,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    sent_at TEXT
                );
                INSERT INTO records (
                    unique_key, record_id, at, payload, status, created_at
                ) VALUES
                    ('legacy-a', 0, '', '{}', 'SENT', '2026-01-01T00:00:00+00:00'),
                    ('legacy-b', 0, '', '{}', 'SENT', '2026-01-01T00:00:01+00:00');
                """
            )
            con.commit()
        finally:
            con.close()

        init_db(legacy_path)
        migrated = connect(legacy_path)
        try:
            self.assertEqual(
                migrated.execute("SELECT COUNT(*) FROM records").fetchone()[0],
                2,
            )
            indexes = {
                row["name"]
                for row in migrated.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                ).fetchall()
            }
            self.assertIn("idx_records_source_record_id_unique", indexes)
            self.assertNotIn("idx_records_record_id_unique", indexes)
        finally:
            migrated.close()

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
                                "printStatus": "READY",
                                "printJob": {
                                    "jobId": "SERIAL_LABEL:123",
                                    "template": "BLANKET_ROLL_TRACE_V1",
                                },
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
        self.assertEqual(len(result.print_jobs), 1)
        self.assertEqual(result.print_jobs[0]["jobId"], "SERIAL_LABEL:123")

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
