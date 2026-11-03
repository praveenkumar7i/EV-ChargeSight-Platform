"""CSV import for authorised operator event exports and historical ticket logs."""
from __future__ import annotations

import csv
import io
from datetime import datetime

from pydantic import ValidationError

from ..clock import to_naive_utc
from ..core.catalog import FAULT_TYPES
from .schema import ExternalEvent

EVENT_COLUMNS_REQUIRED = ("external_event_id", "external_charger_id", "timestamp", "event_type")
EVENT_COLUMNS_OPTIONAL = ("status", "fault_category", "site_code", "site_name", "lat", "lng",
                          "power_kw", "connector_type", "meter_kwh")

TICKET_COLUMNS_REQUIRED = ("record_id", "charger_id", "fault_category", "detected_at")
TICKET_COLUMNS_OPTIONAL = ("site_code", "site_daily_sessions", "charger_kw", "acknowledged_at",
                           "resolved_at", "repair_minutes", "energy_kwh")

MAX_ROWS = 50_000


class CsvFormatError(ValueError):
    pass


def _rows(text: str) -> tuple[list[str], list[dict]]:
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    if not lines:
        raise CsvFormatError("The file is empty.")
    reader = csv.DictReader(io.StringIO("\n".join(lines)))
    if reader.fieldnames is None:
        raise CsvFormatError("No header row found.")
    header = [h.strip() for h in reader.fieldnames]
    rows = []
    for i, r in enumerate(reader):
        if i >= MAX_ROWS:
            raise CsvFormatError(f"Too many rows (limit {MAX_ROWS}).")
        rows.append({(k or "").strip(): (v.strip() if isinstance(v, str) else v) for k, v in r.items()})
    return header, rows


def parse_events_csv(text: str) -> tuple[list[ExternalEvent], list[dict]]:
    header, rows = _rows(text)
    missing = [c for c in EVENT_COLUMNS_REQUIRED if c not in header]
    if missing:
        raise CsvFormatError(f"Missing required column(s): {', '.join(missing)}.")
    good, errors = [], []
    for line_no, r in enumerate(rows, start=2):
        data = {k: (r.get(k) or None) for k in EVENT_COLUMNS_REQUIRED + EVENT_COLUMNS_OPTIONAL}
        for k in ("lat", "lng", "power_kw", "meter_kwh"):
            if data.get(k) is not None:
                try:
                    data[k] = float(data[k])
                except ValueError:
                    errors.append({"line": line_no, "external_event_id": r.get("external_event_id"),
                                   "error": f"{k} is not a number"})
                    data = None
                    break
        if data is None:
            continue
        data["raw"] = {k: v for k, v in r.items() if v}
        try:
            good.append(ExternalEvent(**data))
        except ValidationError as e:
            msg = "; ".join(f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors())
            errors.append({"line": line_no, "external_event_id": r.get("external_event_id"), "error": msg[:300]})
    return good, errors


def _parse_ts(v: str | None, field: str, problems: list[str]) -> datetime | None:
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        problems.append(f"{field} is not an ISO-8601 timestamp")
        return None
    if dt.tzinfo is None:
        problems.append(f"{field} has no timezone offset")
        return None
    return to_naive_utc(dt)


def parse_ticket_history_csv(text: str) -> tuple[list[dict], dict]:
    """Parse authorised historical ticket records for validation.
    Returns (records, quality report). Records keep ISO strings for JSON storage."""
    header, rows = _rows(text)
    missing = [c for c in TICKET_COLUMNS_REQUIRED if c not in header]
    if missing:
        raise CsvFormatError(f"Missing required column(s): {', '.join(missing)}.")

    records, row_errors = [], []
    seen = set()
    duplicates = 0
    field_present = {c: 0 for c in TICKET_COLUMNS_REQUIRED + TICKET_COLUMNS_OPTIONAL}
    order_errors = 0
    for line_no, r in enumerate(rows, start=2):
        for c in field_present:
            if r.get(c):
                field_present[c] += 1
        rid = r.get("record_id")
        if rid in seen:
            duplicates += 1
            continue
        seen.add(rid)
        problems: list[str] = []
        det = _parse_ts(r.get("detected_at"), "detected_at", problems)
        ack = _parse_ts(r.get("acknowledged_at"), "acknowledged_at", problems)
        res = _parse_ts(r.get("resolved_at"), "resolved_at", problems)
        cat = r.get("fault_category") or ""
        if cat not in FAULT_TYPES:
            from ..core.catalog import OCPP_ERROR_TO_CATEGORY

            cat = OCPP_ERROR_TO_CATEGORY.get(cat, "")
            if not cat:
                problems.append(f"unknown fault_category '{r.get('fault_category')}'")
        if det is None and "detected_at" not in " ".join(problems):
            problems.append("detected_at missing")
        if det and ack and ack < det:
            problems.append("acknowledged_at earlier than detected_at")
            order_errors += 1
        if det and res and res < det:
            problems.append("resolved_at earlier than detected_at")
            order_errors += 1
        nums = {}
        for k in ("site_daily_sessions", "charger_kw", "repair_minutes", "energy_kwh"):
            if r.get(k):
                try:
                    nums[k] = float(r[k])
                except ValueError:
                    problems.append(f"{k} is not a number")
        if problems:
            row_errors.append({"line": line_no, "record_id": rid, "errors": problems})
            continue
        records.append({
            "record_id": rid,
            "charger_id": r.get("charger_id"),
            "site_code": r.get("site_code") or "UNKNOWN",
            "fault_category": cat,
            "detected_at": det.isoformat() + "Z",
            "acknowledged_at": ack.isoformat() + "Z" if ack else None,
            "resolved_at": res.isoformat() + "Z" if res else None,
            **nums,
        })
    n = len(rows)
    quality = {
        "rows": n,
        "valid_records": len(records),
        "rejected_rows": len(row_errors),
        "duplicate_record_ids": duplicates,
        "timestamp_order_errors": order_errors,
        "field_completeness_pct": {c: round(100 * v / n, 1) if n else 0.0 for c, v in field_present.items()},
        "row_errors": row_errors[:200],
    }
    return records, quality
