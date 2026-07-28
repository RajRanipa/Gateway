import os
from pathlib import Path


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


BASE_DIR = Path(__file__).resolve().parent

# PLC connection
PLC_IP = os.getenv("PLC_IP", "192.168.1.108")
PLC_RACK = _int("PLC_RACK", 0)
PLC_SLOT = _int("PLC_SLOT", 1)
PLC_CONNECT_RETRY_SEC = _float("PLC_CONNECT_RETRY_SEC", 2.0)
PLC_POLL_MS = _int("PLC_POLL_MS", 200)

# TIA Portal DB numbers
DB_SCALE1 = _int("PLC_DB_SCALE1", 1)
DB_SCALE2 = _int("PLC_DB_SCALE2", 7)
DB_ET = _int("PLC_DB_ET", 12)

# DB1/DB7 source-specific gateway snapshot offsets.
# These addresses are from the non-optimized PLC_NEW TIA export.
O_NODEJS_READ = 0
O_RECORD_STATUS_BYTE = 112
O_RECORD_STATUS_BIT = 0
O_CAPTURED_WEIGHT = 118
O_GW_EVENT_SEQUENCE = 132
O_GW_PRODUCT_CODE = 136
O_GW_SIZE_CODE = 138
O_GW_DENSITY = 140
O_GW_TEMPERATURE = 142
O_GW_QUALITY_BYTE = 144
O_GW_QUALITY_BIT = 0

# DB12 source-specific ET gateway snapshot offsets.
O_ET_GW_EVENT_SEQUENCE = 10
O_ET_GW_PRODUCT_CODE = 14
O_ET_GW_SIZE_CODE = 16
O_ET_GW_DENSITY = 18
O_ET_GW_TEMPERATURE = 20
O_ET_GW_QUALITY_BYTE = 22
O_ET_GW_QUALITY_BIT = 0
O_ET_GW_CAPTURED_WEIGHT = 24

# Local durable storage
DATA_DIR = Path(os.getenv("GATEWAY_DATA_DIR", str(BASE_DIR / "data")))
SQLITE_PATH = Path(os.getenv("GATEWAY_SQLITE_PATH", str(BASE_DIR / "queue.db")))
JSONL_NAME_FMT = "CSP_%Y_%m_%d.jsonl"

# Backend delivery
NODE_URL = os.getenv(
    "GATEWAY_NODE_URL",
    "https://api.orientfibertech.com/gateway/blanket/production",
).strip()
GATEWAY_ID = os.getenv("GATEWAY_ID", "pi-gateway-1").strip()
GATEWAY_KEY = os.getenv("GATEWAY_KEY", "").strip()

SEND_IF_PENDING_AT_LEAST = _int("GATEWAY_SEND_THRESHOLD", 5)
SEND_BATCH_SIZE = _int("GATEWAY_SEND_BATCH_SIZE", 5)
SEND_IF_OLDEST_PENDING_SECONDS = _int("GATEWAY_SEND_MAX_AGE_SECONDS", 1200)
SENDER_POLL_SEC = _float("GATEWAY_SENDER_POLL_SEC", 2.0)
HTTP_CONNECT_TIMEOUT_SEC = _float("GATEWAY_HTTP_CONNECT_TIMEOUT_SEC", 5.0)
HTTP_READ_TIMEOUT_SEC = _float("GATEWAY_HTTP_READ_TIMEOUT_SEC", 30.0)
DELIVERY_LEASE_SECONDS = _int("GATEWAY_DELIVERY_LEASE_SECONDS", 120)
SENT_RETENTION_DAYS = _int("GATEWAY_SENT_RETENTION_DAYS", 0)

SENDER_RETRY_DELAYS_SEC = (15, 30, 60, 120, 240)
SENDER_RETRY_MAX_SEC = 300
