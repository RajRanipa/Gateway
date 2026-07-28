import struct

import snap7

from config import (
    DB_ET,
    DB_SCALE1,
    DB_SCALE2,
    O_CAPTURED_WEIGHT,
    O_ET_GW_CAPTURED_WEIGHT,
    O_ET_GW_DENSITY,
    O_ET_GW_EVENT_SEQUENCE,
    O_ET_GW_PRODUCT_CODE,
    O_ET_GW_QUALITY_BIT,
    O_ET_GW_QUALITY_BYTE,
    O_ET_GW_SIZE_CODE,
    O_ET_GW_TEMPERATURE,
    O_GW_DENSITY,
    O_GW_EVENT_SEQUENCE,
    O_GW_PRODUCT_CODE,
    O_GW_QUALITY_BIT,
    O_GW_QUALITY_BYTE,
    O_GW_SIZE_CODE,
    O_GW_TEMPERATURE,
    O_NODEJS_READ,
    O_RECORD_STATUS_BIT,
    O_RECORD_STATUS_BYTE,
    PLC_IP,
    PLC_RACK,
    PLC_SLOT,
)


READY = 1
PERSISTED_ACK = 2


def _int16(buffer: bytes, offset: int) -> int:
    return struct.unpack_from(">h", buffer, offset)[0]


def _int32(buffer: bytes, offset: int) -> int:
    return struct.unpack_from(">i", buffer, offset)[0]


def _real32(buffer: bytes, offset: int) -> float:
    return struct.unpack_from(">f", buffer, offset)[0]


def _bit(buffer: bytes, byte_offset: int, bit: int) -> bool:
    return ((buffer[byte_offset] >> bit) & 1) == 1


class PLCClient:
    """
    Snap7 client for the 0=IDLE, 1=READY, 2=PERSISTED handshake.

    Each source exposes one immutable, source-specific snapshot. The PLC writes
    READY only after its sequence, weight, parameters, and quality are frozen.
    The Pi therefore reads one contiguous PDU from one DB and verifies READY
    again before accepting it.
    """

    SOURCES = {
        "scale-1": {
            "db": DB_SCALE1,
            "scale_no": 1,
            "expected_quality_ok": True,
            "read_size": O_GW_QUALITY_BYTE + 1,
            "event_sequence": O_GW_EVENT_SEQUENCE,
            "product_code": O_GW_PRODUCT_CODE,
            "size_code": O_GW_SIZE_CODE,
            "density": O_GW_DENSITY,
            "temperature": O_GW_TEMPERATURE,
            "quality_byte": O_GW_QUALITY_BYTE,
            "quality_bit": O_GW_QUALITY_BIT,
            "weight": O_CAPTURED_WEIGHT,
            "legacy_status_byte": O_RECORD_STATUS_BYTE,
            "legacy_status_bit": O_RECORD_STATUS_BIT,
        },
        "scale-2": {
            "db": DB_SCALE2,
            "scale_no": 2,
            "expected_quality_ok": False,
            "read_size": O_GW_QUALITY_BYTE + 1,
            "event_sequence": O_GW_EVENT_SEQUENCE,
            "product_code": O_GW_PRODUCT_CODE,
            "size_code": O_GW_SIZE_CODE,
            "density": O_GW_DENSITY,
            "temperature": O_GW_TEMPERATURE,
            "quality_byte": O_GW_QUALITY_BYTE,
            "quality_bit": O_GW_QUALITY_BIT,
            "weight": O_CAPTURED_WEIGHT,
            "legacy_status_byte": O_RECORD_STATUS_BYTE,
            "legacy_status_bit": O_RECORD_STATUS_BIT,
        },
        "et": {
            "db": DB_ET,
            "scale_no": 3,
            "expected_quality_ok": False,
            "read_size": O_ET_GW_CAPTURED_WEIGHT + 4,
            "event_sequence": O_ET_GW_EVENT_SEQUENCE,
            "product_code": O_ET_GW_PRODUCT_CODE,
            "size_code": O_ET_GW_SIZE_CODE,
            "density": O_ET_GW_DENSITY,
            "temperature": O_ET_GW_TEMPERATURE,
            "quality_byte": O_ET_GW_QUALITY_BYTE,
            "quality_bit": O_ET_GW_QUALITY_BIT,
            "weight": O_ET_GW_CAPTURED_WEIGHT,
            "legacy_status_byte": None,
            "legacy_status_bit": None,
        },
    }

    def __init__(self):
        self.client = None

    def connect(self) -> None:
        client = snap7.client.Client()
        client.set_connection_type(0x03)
        client.connect(PLC_IP, PLC_RACK, PLC_SLOT)
        if not client.get_connected():
            raise ConnectionError(f"Unable to connect to PLC at {PLC_IP}")
        self.client = client

    def disconnect(self) -> None:
        try:
            if self.client:
                self.client.disconnect()
        finally:
            self.client = None

    def ensure_connected(self) -> None:
        if self.client is None or not self.client.get_connected():
            self.disconnect()
            self.connect()

    def _read_int(self, db_no: int, offset: int) -> int:
        return _int16(self.client.db_read(db_no, offset, 2), 0)

    def _write_int(self, db_no: int, offset: int, value: int) -> None:
        self.client.db_write(db_no, offset, struct.pack(">h", int(value)))

    def read_states(self) -> dict[str, int]:
        return {
            source: self._read_int(config["db"], O_NODEJS_READ)
            for source, config in self.SOURCES.items()
        }

    def _snapshot(self, source: str) -> dict:
        config = self.SOURCES.get(source)
        if not config:
            raise ValueError(f"Unsupported PLC source: {source}")

        block = self.client.db_read(config["db"], 0, config["read_size"])
        if _int16(block, O_NODEJS_READ) != READY:
            raise RuntimeError(f"{source} changed state during snapshot")

        event_sequence = _int32(block, config["event_sequence"])
        if event_sequence <= 0:
            raise RuntimeError(
                f"{source} has invalid event sequence {event_sequence}"
            )

        quality_ok = _bit(
            block,
            config["quality_byte"],
            config["quality_bit"],
        )
        if quality_ok != config["expected_quality_ok"]:
            raise RuntimeError(
                f"{source} quality mismatch: expected "
                f"{config['expected_quality_ok']}, got {quality_ok}"
            )

        legacy_status_byte = config["legacy_status_byte"]
        snapshot = {
            "scaleNo": config["scale_no"],
            "eventSequence": event_sequence,
            "productCode": _int16(block, config["product_code"]),
            "temperature": _int16(block, config["temperature"]),
            "density": _int16(block, config["density"]),
            "sizeCode": _int16(block, config["size_code"]),
            "weight": round(_real32(block, config["weight"]), 3),
            "status": int(quality_ok),
            "plcStatus": (
                int(
                    _bit(
                        block,
                        legacy_status_byte,
                        config["legacy_status_bit"],
                    )
                )
                if legacy_status_byte is not None
                else int(quality_ok)
            ),
        }
        if self._read_int(config["db"], O_NODEJS_READ) != READY:
            raise RuntimeError(f"{source} changed state before verification")
        return snapshot

    def read_ready_snapshot(self, source: str) -> dict:
        return self._snapshot(source)

    def acknowledge_persisted(self, source: str) -> None:
        config = self.SOURCES.get(source)
        if not config:
            raise ValueError(f"Unsupported PLC source: {source}")
        current = self._read_int(config["db"], O_NODEJS_READ)
        if current == PERSISTED_ACK:
            return
        if current != READY:
            raise RuntimeError(
                f"Cannot acknowledge {source}: expected READY=1, got {current}"
            )
        self._write_int(config["db"], O_NODEJS_READ, PERSISTED_ACK)
        verified = self._read_int(config["db"], O_NODEJS_READ)
        if verified != PERSISTED_ACK:
            raise RuntimeError(
                f"PLC did not retain acknowledgement for {source}; got {verified}"
            )
