"""Parser, validator and importer for historical EV charging session datasets.

Specifically supports the IEEE `ChargingRecords.csv` dataset and compatible schemas.
This represents historical research data, NOT live telemetry. Original column names,
source values, and units are preserved without unwarranted assumptions.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..clock import wall_now
from ..models import HistoricalChargingSession
from ..tenancy import effective_org

log = logging.getLogger("chargesight.historical_import")

# Canonical column names expected from IEEE ChargingRecords.csv (case-insensitive)
IEEE_CANONICAL = [
    "UserID",
    "ChargerID",
    "ChargerCompany",
    "Location",
    "ChargerType",
    "StartDay",
    "StartTime",
    "EndDay",
    "EndTime",
    "StartDatetime",
    "EndDatetime",
    "Duration",
    "Demand",
]

# Field mappings to support minor column spelling/case differences
COLUMN_ALIASES: dict[str, list[str]] = {
    "UserID": ["userid", "user_id", "user id", "driver_id"],
    "ChargerID": ["chargerid", "charger_id", "charger id", "cp_id", "station_id"],
    "ChargerCompany": ["chargercompany", "charger_company", "company", "operator", "cpo"],
    "Location": ["location", "site", "address", "station_name"],
    "ChargerType": ["chargertype", "charger_type", "type", "connector_type"],
    "StartDay": ["startday", "start_day", "start_date"],
    "StartTime": ["starttime", "start_time"],
    "EndDay": ["endday", "end_day", "end_date"],
    "EndTime": ["endtime", "end_time"],
    "StartDatetime": ["startdatetime", "start_datetime", "start_time_stamp", "start_timestamp", "start"],
    "EndDatetime": ["enddatetime", "end_datetime", "end_time_stamp", "end_timestamp", "end"],
    "Duration": ["duration", "session_duration", "duration_min", "duration_hrs"],
    "Demand": ["demand", "energy", "energy_kwh", "kwh", "power_demand"],
}

DATE_FORMATS = [
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y/%m/%d %H:%M:%S",
    "%d-%m-%Y %H:%M:%S",
    "%d/%m/%Y %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M",
    "%d-%m-%Y %H:%M",
    "%d/%m/%Y %H:%M",
    "%Y-%m-%d",
    "%d-%m-%Y",
    "%Y/%m/%d",
]


class HistoricalCsvError(ValueError):
    pass


def _map_header(header: list[str]) -> dict[str, str]:
    """Map CSV column headers to canonical IEEE names."""
    mapped: dict[str, str] = {}
    normalized_cols = {h.strip().lower().replace("_", "").replace(" ", ""): h.strip() for h in header if h}

    for canonical, aliases in COLUMN_ALIASES.items():
        # Check canonical match first
        canon_norm = canonical.lower().replace("_", "")
        if canon_norm in normalized_cols:
            mapped[canonical] = normalized_cols[canon_norm]
            continue
        for alias in aliases:
            alias_norm = alias.lower().replace("_", "").replace(" ", "")
            if alias_norm in normalized_cols:
                mapped[canonical] = normalized_cols[alias_norm]
                break
    return mapped


def parse_datetime(val: Any) -> datetime | None:
    """Attempt parsing multiple datetime formats safely without throwing."""
    if val is None:
        return None
    val_str = str(val).strip()
    if not val_str:
        return None
    # Strip trailing fractional seconds if needed
    clean_str = val_str.replace("Z", "+00:00")
    if "." in clean_str:
        clean_str = clean_str.split(".")[0]
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(clean_str, fmt)
        except ValueError:
            continue
    # Try ISO-8601 fromisoformat
    try:
        return datetime.fromisoformat(val_str.replace("Z", ""))
    except Exception:
        return None


def _clean_float(val: Any) -> float | None:
    if val is None:
        return None
    s = str(val).strip().replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _calc_hash(row_dict: dict[str, Any]) -> str:
    """Stable hash for idempotent imports."""
    parts = [
        str(row_dict.get("UserID", "")).strip(),
        str(row_dict.get("ChargerID", "")).strip(),
        str(row_dict.get("StartDatetime", "") or row_dict.get("StartDay", "")).strip(),
        str(row_dict.get("EndDatetime", "") or row_dict.get("EndDay", "")).strip(),
        str(row_dict.get("Demand", "")).strip(),
        str(row_dict.get("Duration", "")).strip(),
    ]
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _clean_csv_stream(text: str):
    """Memory-efficient generator streaming non-empty, non-comment lines without copying the dataset."""
    for line in io.StringIO(text):
        s = line.strip()
        if s and not s.startswith("#"):
            yield line


def preview_historical_csv(text: str, max_preview: int = 10) -> dict[str, Any]:
    """Validate and preview a historical charging sessions CSV file before import."""
    if not text or not text.strip():
        raise HistoricalCsvError("The file is empty.")

    stream = _clean_csv_stream(text)
    reader = csv.DictReader(stream)
    if reader.fieldnames is None:
        raise HistoricalCsvError("No header row found.")

    header = [h.strip() for h in reader.fieldnames if h]
    if not header:
        raise HistoricalCsvError("No header row found.")
    col_map = _map_header(header)

    # ChargerID is the only essential identifier for a charging session record
    if "ChargerID" not in col_map:
        raise HistoricalCsvError(
            f"Missing required ChargerID column. Expected one of: ChargerID, charger_id, station_id, cp_id. Found: {', '.join(header[:8])}"
        )

    rows: list[dict[str, Any]] = []
    row_errors: list[dict[str, Any]] = []
    missing_counts: dict[str, int] = {k: 0 for k in IEEE_CANONICAL}
    seen_hashes: set[str] = set()
    duplicate_rows = 0

    valid_chargers: set[str] = set()
    total_demand = 0.0
    demand_count = 0
    min_date: datetime | None = None
    max_date: datetime | None = None

    for line_idx, r in enumerate(reader, start=2):
        # Map row to canonical fields
        canon_row = {canon: r.get(orig, "").strip() if r.get(orig) else "" for canon, orig in col_map.items()}

        cid = canon_row.get("ChargerID")
        if not cid:
            row_errors.append({"line": line_idx, "error": "Missing ChargerID"})
            continue

        for col in IEEE_CANONICAL:
            if not canon_row.get(col):
                missing_counts[col] += 1

        r_hash = _calc_hash(canon_row)
        if r_hash in seen_hashes:
            duplicate_rows += 1
            continue
        seen_hashes.add(r_hash)

        # Datetime parse test
        s_dt = parse_datetime(canon_row.get("StartDatetime")) or parse_datetime(canon_row.get("StartDay"))
        e_dt = parse_datetime(canon_row.get("EndDatetime")) or parse_datetime(canon_row.get("EndDay"))

        if s_dt and (min_date is None or s_dt < min_date):
            min_date = s_dt
        if e_dt and (max_date is None or e_dt > max_date):
            max_date = e_dt

        # Demand parse test
        demand_val = _clean_float(canon_row.get("Demand"))
        if demand_val is not None:
            total_demand += demand_val
            demand_count += 1

        valid_chargers.add(cid)

        if len(rows) < max_preview:
            rows.append({
                "line": line_idx,
                "UserID": canon_row.get("UserID", ""),
                "ChargerID": cid,
                "ChargerCompany": canon_row.get("ChargerCompany", ""),
                "Location": canon_row.get("Location", ""),
                "ChargerType": canon_row.get("ChargerType", ""),
                "StartDay": canon_row.get("StartDay", ""),
                "StartTime": canon_row.get("StartTime", ""),
                "EndDay": canon_row.get("EndDay", ""),
                "EndTime": canon_row.get("EndTime", ""),
                "StartDatetime": canon_row.get("StartDatetime", ""),
                "EndDatetime": canon_row.get("EndDatetime", ""),
                "Duration": canon_row.get("Duration", ""),
                "Demand": canon_row.get("Demand", ""),
            })

    total_rows = len(seen_hashes) + duplicate_rows + len(row_errors)
    valid_count = len(seen_hashes)

    return {
        "dataset_type": "Historical EV Charging Sessions",
        "standard": "IEEE ChargingRecords schema compatible",
        "detected_columns": list(col_map.keys()),
        "original_columns": header,
        "total_rows": total_rows,
        "valid_rows": valid_count,
        "duplicate_rows": duplicate_rows,
        "invalid_rows": len(row_errors),
        "missing_counts": missing_counts,
        "row_errors": row_errors[:50],
        "preview": rows,
        "summary": {
            "unique_chargers": len(valid_chargers),
            "records_with_demand": demand_count,
            "total_demand": round(total_demand, 2),
            "demand_note": "Demand unit preserved as original without unverified assumption (commonly kWh in IEEE dataset).",
            "date_range_start": min_date.isoformat() if min_date else None,
            "date_range_end": max_date.isoformat() if max_date else None,
            "duration_note": "Duration preserved as original without unit assumption.",
        },
    }


def import_historical_csv(
    db: Session,
    text: str,
    *,
    dataset_label: str = "IEEE ChargingRecords Research Dataset",
) -> dict[str, Any]:
    """Parse and insert historical charging records idempotently into the database."""
    if not text or not text.strip():
        raise HistoricalCsvError("The file is empty.")

    stream = _clean_csv_stream(text)
    reader = csv.DictReader(stream)
    if reader.fieldnames is None:
        raise HistoricalCsvError("No header row found.")

    header = [h.strip() for h in reader.fieldnames if h]
    if not header:
        raise HistoricalCsvError("No header row found.")
    col_map = _map_header(header)

    if "ChargerID" not in col_map:
        raise HistoricalCsvError("Missing required ChargerID column.")

    org_id = effective_org(db)
    received = 0
    accepted = 0
    duplicates = 0
    rejected = 0
    errors: list[dict[str, Any]] = []

    # Get already stored hashes for this org to guarantee idempotency
    existing_hashes = set(
        db.scalars(
            select(HistoricalChargingSession.record_hash).where(
                HistoricalChargingSession.org_id == org_id
            )
        ).all()
    )

    now = wall_now()
    batch: list[HistoricalChargingSession] = []

    for line_idx, r in enumerate(reader, start=2):
        received += 1
        canon_row = {canon: r.get(orig, "").strip() if r.get(orig) else "" for canon, orig in col_map.items()}
        cid = canon_row.get("ChargerID")
        if not cid:
            rejected += 1
            errors.append({"line": line_idx, "error": "Missing ChargerID"})
            continue

        r_hash = _calc_hash(canon_row)
        if r_hash in existing_hashes:
            duplicates += 1
            continue
        existing_hashes.add(r_hash)

        s_dt = parse_datetime(canon_row.get("StartDatetime"))
        if not s_dt and canon_row.get("StartDay"):
            t_str = canon_row.get("StartTime", "00:00:00")
            s_dt = parse_datetime(f"{canon_row['StartDay']} {t_str}")

        e_dt = parse_datetime(canon_row.get("EndDatetime"))
        if not e_dt and canon_row.get("EndDay"):
            t_str = canon_row.get("EndTime", "00:00:00")
            e_dt = parse_datetime(f"{canon_row['EndDay']} {t_str}")

        dur_num = _clean_float(canon_row.get("Duration"))
        demand_num = _clean_float(canon_row.get("Demand"))

        session = HistoricalChargingSession(
            org_id=org_id,
            user_id=canon_row.get("UserID") or None,
            charger_id=cid,
            charger_company=canon_row.get("ChargerCompany") or None,
            location=canon_row.get("Location") or None,
            charger_type=canon_row.get("ChargerType") or None,
            start_day=canon_row.get("StartDay") or None,
            start_time=canon_row.get("StartTime") or None,
            end_day=canon_row.get("EndDay") or None,
            end_time=canon_row.get("EndTime") or None,
            start_datetime=s_dt,
            end_datetime=e_dt,
            duration=dur_num,
            duration_raw=canon_row.get("Duration") or None,
            demand=demand_num,
            demand_raw=canon_row.get("Demand") or None,
            demand_unit="kWh" if demand_num is not None else "unspecified",
            dataset_label=dataset_label,
            record_hash=r_hash,
            raw_payload={k: v for k, v in r.items() if v},
            created_at=now,
        )
        batch.append(session)
        accepted += 1

        if len(batch) >= 500:
            db.add_all(batch)
            db.flush()
            batch.clear()

    if batch:
        db.add_all(batch)
        db.flush()

    return {
        "dataset_label": dataset_label,
        "dataset_type": "Historical Charging Sessions (IEEE format)",
        "received": received,
        "accepted": accepted,
        "duplicates": duplicates,
        "rejected": rejected,
        "errors": errors[:50],
        "ok": accepted > 0 or (duplicates > 0 and rejected == 0),
    }

