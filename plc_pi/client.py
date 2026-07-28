import struct

import snap7

from config import (
    DB_ET,
    DB_PARAM,
    DB_SCALE1,
    DB_SCALE2,
    O_CAPTURED_WEIGHT,
    O_ET_PRODUCT_CODE,
    O_ET_TEMP,
    O_NODEJS_READ,
    O_REC_DENSITY,
    O_REC_PRODUCT,
    O_REC_SIZE,
    O_REC_TEMP,
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


def _real32(buffer: bytes, offset: int) -> float:
    return struct.unpack_from(">f", buffer, offset)[0]


def _bit(buffer: bytes, byte_offset: int, bit: int) -> bool:
    return ((buffer[byte_offset] >> bit) & 1) == 1


class PLCClient:
    """
    Snap7 client for the 0=IDLE, 1=READY, 2=PERSISTED handshake.

    Scale 1 is the physical OK lane. Scale 2 and ET are rejected lanes, so
    canonical quality status comes from the lane instead of a mutable HMI bit.
    The raw PLC status is retained in `plcStatus` for diagnostics.
    """

    SOURCES = {
        "scale-1": {"db": DB_SCALE1, "scale_no": 1, "quality_ok": True},
        "scale-2": {"db": DB_SCALE2, "scale_no": 2, "quality_ok": False},
        "et": {"db": DB_ET, "scale_no": 3, "quality_ok": False},
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

    def _read_frozen_params(self) -> tuple[int, int, int, int]:
        # One PDU read prevents a parameter tuple torn across multiple requests.
        start = O_REC_PRODUCT
        end = O_REC_TEMP + 2
        block = self.client.db_read(DB_PARAM, start, end - start)
        return (
            _int16(block, O_REC_PRODUCT - start),
            _int16(block, O_REC_SIZE - start),
            _int16(block, O_REC_DENSITY - start),
            _int16(block, O_REC_TEMP - start),
        )

    def _read_weighing_block(self, db_no: int) -> bytes:
        return self.client.db_read(db_no, 0, O_CAPTURED_WEIGHT + 4)

    def _normal_snapshot(self, source: str) -> dict:
        config = self.SOURCES[source]
        block = self._read_weighing_block(config["db"])
        if _int16(block, O_NODEJS_READ) != READY:
            raise RuntimeError(f"{source} changed state during snapshot")
        product, size, density, temperature = self._read_frozen_params()
        snapshot = {
            "scaleNo": config["scale_no"],
            "productCode": product,
            "temperature": temperature,
            "density": density,
            "sizeCode": size,
            "weight": round(_real32(block, O_CAPTURED_WEIGHT), 3),
            "status": 1 if config["quality_ok"] else 0,
            "plcStatus": int(
                _bit(block, O_RECORD_STATUS_BYTE, O_RECORD_STATUS_BIT)
            ),
        }
        if self._read_int(config["db"], O_NODEJS_READ) != READY:
            raise RuntimeError(f"{source} changed state before verification")
        return snapshot

    def _et_snapshot(self) -> dict:
        et_block = self.client.db_read(DB_ET, 0, O_ET_TEMP + 2)
        if _int16(et_block, O_NODEJS_READ) != READY:
            raise RuntimeError("et changed state during snapshot")
        weighing = self._read_weighing_block(DB_SCALE2)
        snapshot = {
            "scaleNo": 3,
            "productCode": _int16(et_block, O_ET_PRODUCT_CODE) or 5,
            "temperature": _int16(et_block, O_ET_TEMP),
            "density": 0,
            "sizeCode": 0,
            "weight": round(_real32(weighing, O_CAPTURED_WEIGHT), 3),
            "status": 0,
            "plcStatus": int(
                _bit(weighing, O_RECORD_STATUS_BYTE, O_RECORD_STATUS_BIT)
            ),
        }
        if self._read_int(DB_ET, O_NODEJS_READ) != READY:
            raise RuntimeError("et changed state before verification")
        return snapshot

    def read_ready_snapshot(self, source: str) -> dict:
        if source == "et":
            return self._et_snapshot()
        if source not in ("scale-1", "scale-2"):
            raise ValueError(f"Unsupported PLC source: {source}")
        return self._normal_snapshot(source)

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

