import json

def should_print_label(result: dict) -> bool:
    """Return True only for a complete, printer-safe Blanket label result.

    storageStatus is deliberately not checked. A DUPLICATE response can be the
    first response that reached the Pi after the backend completed an earlier
    request, so jobId-based local deduplication is the authoritative safeguard.
    """
    print(
        "❇️ just test for results:\n",
        json.dumps(result, indent=2, default=str)
    )
    if not isinstance(result, dict):
        return False
    if result.get("accepted") is not True or result.get("retryable") is True:
        return False
    if result.get("inventoryStatus") != "POSTED":
        return False
    if result.get("printStatus") != "READY":
        return False

    job = result.get("printJob")
    if not isinstance(job, dict):
        return False
    if job.get("schemaVersion") != "1.0" or job.get("copies") != 1:
        return False
    if job.get("kind") != "SERIAL_LABEL":
        return False
    if job.get("template") != "BLANKET_ROLL_TRACE_V1":
        return False

    data = job.get("data")
    qr = job.get("qr")
    job_id = str(job.get("jobId") or "")
    serial_no = str(data.get("serialNo") or "") if isinstance(data, dict) else ""
    weight = data.get("weight") if isinstance(data, dict) else None
    qr_value = str(qr.get("value") or "") if isinstance(qr, dict) else ""
    trace_url = str(job.get("traceUrl") or "")
    gateway_record_id = (
        str(data.get("gatewayRecordId") or "") if isinstance(data, dict) else ""
    )
    try:
        weight_value = float(weight.get("value") or 0) if isinstance(weight, dict) else 0
    except (TypeError, ValueError):
        return False
    return bool(
        serial_no
        and job_id == f"SERIAL_LABEL:{serial_no}"
        and data.get("manufacturerName")
        and data.get("productName")
        and data.get("sku")
        and data.get("lotNo")
        and data.get("manufacturedAt")
        and isinstance(weight, dict)
        and weight_value > 0
        and weight.get("uom")
        and qr.get("format") == "QR_CODE"
        and qr_value.startswith("https://")
        and qr_value == trace_url
        and gateway_record_id == str(result.get("recordId") or "")
    )
