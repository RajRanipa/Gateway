import hashlib
import json
from dataclasses import dataclass

import requests

from config import (
    GATEWAY_ID,
    GATEWAY_KEY,
    HTTP_CONNECT_TIMEOUT_SEC,
    HTTP_READ_TIMEOUT_SEC,
    NODE_URL,
)
from utility.time_utils import utc_now_iso


@dataclass(frozen=True)
class DeliveryResult:
    accepted_record_ids: frozenset[str]
    retryable_record_ids: frozenset[str]
    rejected: dict[str, str]
    error: str | None = None


_session = requests.Session()


def _client_batch_id(records: list[dict]) -> str:
    identity = "|".join(
        sorted(
            f"{record.get('recordId')}:{record.get('scaleNo')}"
            for record in records
        )
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _headers(batch_id: str) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Gateway-ID": GATEWAY_ID,
        "X-Idempotency-Key": batch_id,
    }
    if GATEWAY_KEY:
        headers["X-Gateway-Key"] = GATEWAY_KEY
    return headers


def _record_results(response_json: dict) -> list[dict] | None:
    data = response_json.get("data")
    if not isinstance(data, dict):
        return None
    results = data.get("recordResults")
    if isinstance(results, list):
        return results
    summary = data.get("summary")
    if isinstance(summary, dict) and isinstance(summary.get("recordResults"), list):
        return summary["recordResults"]
    return None


def post_batch(rows) -> DeliveryResult:
    records = [json.loads(row["payload"]) for row in rows]
    expected_ids = {str(record["recordId"]) for record in records}
    batch_id = _client_batch_id(records)
    payload = {
        "contractVersion": "2.0",
        "clientBatchId": batch_id,
        "gatewayId": GATEWAY_ID,
        "sentAt": utc_now_iso(),
        "records": records,
    }

    print(
        "gateway_http_send "
        f"url={NODE_URL} "
        f"batchId={batch_id} "
        f"records={len(records)} "
        f"recordIds={','.join(sorted(expected_ids))}",
        flush=True,
    )

    try:
        response = _session.post(
            NODE_URL,
            json=payload,
            headers=_headers(batch_id),
            timeout=(HTTP_CONNECT_TIMEOUT_SEC, HTTP_READ_TIMEOUT_SEC),
        )
    except requests.RequestException as exc:
        print(
            f"gateway_http_error batchId={batch_id} error={exc}",
            flush=True,
        )
        return DeliveryResult(
            frozenset(),
            frozenset(expected_ids),
            {},
            str(exc),
        )

    try:
        body = response.json()
    except ValueError:
        body = None

    print(
        "gateway_http_response "
        f"batchId={batch_id} "
        f"status={response.status_code} "
        f"bodyType={'json' if isinstance(body, dict) else 'non-json'}",
        flush=True,
    )

    if response.status_code not in (200, 201, 207):
        message = (
            body.get("message")
            if isinstance(body, dict)
            else response.text[:500]
        )
        return DeliveryResult(
            frozenset(),
            frozenset(expected_ids),
            {},
            f"HTTP {response.status_code}: {message}",
        )

    results = _record_results(body or {})
    if results is None:
        # Never delete a local event merely because an unrecognized endpoint
        # returned 2xx. Deploy backend contract v2 before this sender.
        return DeliveryResult(
            frozenset(),
            frozenset(expected_ids),
            {},
            "Backend returned 2xx without contract-v2 record acknowledgements",
        )
    print(
        "❇️ just test for response:\n"
        + json.dumps(body.get("data") if isinstance(body, dict) else body, indent=2, default=str),
        flush=True,
    )
    accepted: set[str] = set()
    retryable: set[str] = set()
    rejected: dict[str, str] = {}
    for result in results:
        record_id = str(result.get("recordId") or "")
        if record_id not in expected_ids:
            continue
        if result.get("accepted") is True:
            accepted.add(record_id)
        elif result.get("retryable", True):
            retryable.add(record_id)
        else:
            rejected[record_id] = str(
                result.get("message") or result.get("code") or "rejected"
            )

    unacknowledged = expected_ids - accepted - retryable - set(rejected)
    retryable.update(unacknowledged)
    return DeliveryResult(
        frozenset(accepted),
        frozenset(retryable),
        rejected,
        None if not unacknowledged else "Backend omitted record acknowledgements",
    )
