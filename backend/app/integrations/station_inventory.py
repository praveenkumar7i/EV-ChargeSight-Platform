"""Parser, validator and importer for Station Inventory CSV datasets.

Imports EV charging station inventory records (state and national datasets).
Preserves source facts, coordinates, connector counts and ownership models.
Never deletes or overwrites existing fleet records, and prevents duplicate imports.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..clock import wall_now
from ..core.areas import BENGALURU_BBOX
from ..models import ImportedStation, PublicStation
from ..tenancy import effective_org

log = logging.getLogger("chargesight.station_inventory")

CANONICAL_FIELDS = [
    "Operator",
    "Ownership",
    "State",
    "District",
    "CityOrVillage",
    "StationName",
    "Latitude",
    "Longitude",
    "ChargerType",
    "ChargerRating",
    "ConnectorRating",
    "TotalConnectors",
]

INVENTORY_ALIASES: dict[str, list[str]] = {
    "Operator": ["operator", "cpo", "company", "service_provider", "network", "agency"],
    "Ownership": ["government/private ownership", "ownership", "govt/pvt", "govt_pvt", "sector", "category"],
    "State": ["state", "state_name", "province", "st"],
    "District": ["district", "district_name", "dist"],
    "CityOrVillage": ["city or village", "city/village", "city_or_village", "city", "village", "town", "locality"],
    "StationName": ["station/location name", "station_name", "station name", "location_name", "location", "station", "name"],
    "Latitude": ["latitude", "lat", "geo_lat", "y"],
    "Longitude": ["longitude", "lng", "lon", "long", "geo_lng", "geo_lon", "x"],
    "ChargerType": ["charger type", "charger_type", "type_of_charger", "type", "connector_type"],
    "ChargerRating": ["charger rating", "charger_rating", "rating_kw", "capacity_kw", "power_kw", "power_rating"],
    "ConnectorRating": ["connector rating", "connector_rating", "connector_kw", "gun_rating"],
    "TotalConnectors": ["total connectors", "total_connectors", "no_of_connectors", "connectors", "num_connectors", "ports", "guns", "points"],
}

KARNATAKA_DISTRICTS = {
    "bagalkot", "ballari", "bellary", "belagavi", "belgaum", "bengaluru rural", "bengaluru urban", "bangalore",
    "bangalore rural", "bangalore urban", "bidar", "chamarajanagar", "chikkaballapur", "chikkamagaluru",
    "chitradurga", "dakshina kannada", "mangaluru", "mangalore", "davanagere", "dharwad", "hubballi", "hubli",
    "gadag", "hassan", "haveri", "kalaburagi", "gulbarga", "kodagu", "coorg", "kolar", "koppal", "mandya",
    "mysuru", "mysore", "raichur", "ramanagara", "shivamogga", "shimoga", "tumakuru", "tumkur", "udupi",
    "uttara kannada", "karwar", "vijayanagara", "yadgir",
}


class StationInventoryCsvError(ValueError):
    pass


def _map_header(header: list[str]) -> dict[str, str]:
    """Map CSV column headers to canonical Station Inventory names."""
    mapped: dict[str, str] = {}
    normalized_cols = {h.strip().lower().replace("_", "").replace(" ", "").replace("/", ""): h.strip() for h in header if h}

    for canonical, aliases in INVENTORY_ALIASES.items():
        canon_norm = canonical.lower().replace("_", "").replace("/", "")
        if canon_norm in normalized_cols:
            mapped[canonical] = normalized_cols[canon_norm]
            continue
        for alias in aliases:
            alias_norm = alias.lower().replace("_", "").replace(" ", "").replace("/", "")
            if alias_norm in normalized_cols:
                mapped[canonical] = normalized_cols[alias_norm]
                break
    return mapped


def is_in_karnataka(state: str, district: str, lat: float, lng: float) -> bool:
    st_clean = (state or "").lower().strip()
    dist_clean = (district or "").lower().strip()
    if "karnataka" in st_clean:
        return True
    if any(kd in dist_clean for kd in KARNATAKA_DISTRICTS):
        return True
    # Geolocation bounding box for Karnataka
    if 11.5 <= lat <= 18.6 and 74.0 <= lng <= 78.6:
        return True
    return False


def is_in_bengaluru(state: str, district: str, city: str, lat: float, lng: float) -> bool:
    dist_clean = (district or "").lower()
    city_clean = (city or "").lower()
    if "bengaluru" in dist_clean or "bangalore" in dist_clean:
        return True
    if "bengaluru" in city_clean or "bangalore" in city_clean:
        return True
    # Metropolitan BBox check
    s, w, n, e = BENGALURU_BBOX
    if s <= lat <= n and w <= lng <= e:
        return True
    return False


def _calc_station_hash(operator: str, name: str, lat: float, lng: float) -> str:
    raw = f"{operator.strip().lower()}|{name.strip().lower()}|{lat:.5f}|{lng:.5f}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def preview_station_inventory_csv(
    text: str,
    *,
    filter_karnataka: bool = False,
    filter_bengaluru: bool = False,
    max_preview: int = 10,
) -> dict[str, Any]:
    """Validate and preview a Station Inventory CSV before importing."""
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    if not lines:
        raise StationInventoryCsvError("The file is empty.")

    reader = csv.DictReader(io.StringIO("\n".join(lines)))
    if reader.fieldnames is None:
        raise StationInventoryCsvError("No header row found.")

    header = [h.strip() for h in reader.fieldnames if h]
    col_map = _map_header(header)

    if "StationName" not in col_map:
        raise StationInventoryCsvError("Missing required Station Name / Location column.")
    if "Latitude" not in col_map or "Longitude" not in col_map:
        raise StationInventoryCsvError("Missing required Latitude and Longitude columns.")

    rows: list[dict[str, Any]] = []
    row_errors: list[dict[str, Any]] = []
    missing_counts: dict[str, int] = {k: 0 for k in CANONICAL_FIELDS}
    seen_hashes: set[str] = set()
    duplicate_rows = 0

    karnataka_count = 0
    bengaluru_count = 0
    total_connectors_sum = 0
    unique_operators: set[str] = set()

    for line_idx, r in enumerate(reader, start=2):
        if line_idx > 50000:
            break
        canon_row = {canon: r.get(orig, "").strip() if r.get(orig) else "" for canon, orig in col_map.items()}

        name = canon_row.get("StationName", "").strip()
        if not name:
            row_errors.append({"line": line_idx, "error": "Missing Station Name"})
            continue

        lat_raw = canon_row.get("Latitude", "")
        lng_raw = canon_row.get("Longitude", "")
        try:
            lat = float(str(lat_raw).replace(",", ""))
            lng = float(str(lng_raw).replace(",", ""))
        except (ValueError, TypeError):
            row_errors.append({"line": line_idx, "error": f"Invalid coordinates (lat='{lat_raw}', lng='{lng_raw}')"})
            continue

        if not (-90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0) or (lat == 0.0 and lng == 0.0):
            row_errors.append({"line": line_idx, "error": f"Coordinates out of range ({lat}, {lng})"})
            continue

        op = canon_row.get("Operator", "").strip() or "Independent CPO"
        unique_operators.add(op)

        state = canon_row.get("State", "")
        dist = canon_row.get("District", "")
        city = canon_row.get("CityOrVillage", "")

        is_kar = is_in_karnataka(state, dist, lat, lng)
        is_blr = is_in_bengaluru(state, dist, city, lat, lng)

        if is_kar:
            karnataka_count += 1
        if is_blr:
            bengaluru_count += 1

        if filter_karnataka and not is_kar:
            continue
        if filter_bengaluru and not is_blr:
            continue

        conn_raw = canon_row.get("TotalConnectors", "")
        if conn_raw:
            try:
                connectors = int(float(conn_raw))
                if connectors < 1:
                    row_errors.append({"line": line_idx, "error": f"Total Connectors must be positive: {conn_raw}"})
                    continue
            except (ValueError, TypeError):
                row_errors.append({"line": line_idx, "error": f"Invalid Total Connectors numeric format: {conn_raw}"})
                continue
        else:
            connectors = 1

        total_connectors_sum += connectors

        for col in CANONICAL_FIELDS:
            if not canon_row.get(col):
                missing_counts[col] += 1

        st_hash = _calc_station_hash(op, name, lat, lng)
        if st_hash in seen_hashes:
            duplicate_rows += 1
            continue
        seen_hashes.add(st_hash)

        if len(rows) < max_preview:
            rows.append({
                "line": line_idx,
                "Operator": op,
                "Ownership": canon_row.get("Ownership", ""),
                "State": state,
                "District": dist,
                "CityOrVillage": city,
                "StationName": name,
                "Latitude": lat,
                "Longitude": lng,
                "ChargerType": canon_row.get("ChargerType", ""),
                "ChargerRating": canon_row.get("ChargerRating", ""),
                "ConnectorRating": canon_row.get("ConnectorRating", ""),
                "TotalConnectors": connectors,
                "is_karnataka": is_kar,
                "is_bengaluru": is_blr,
            })

    total_rows = len(seen_hashes) + duplicate_rows + len(row_errors)
    valid_count = len(seen_hashes)

    return {
        "dataset_type": "EV Station Inventory",
        "detected_columns": list(col_map.keys()),
        "original_columns": header,
        "total_rows": total_rows,
        "valid_rows": valid_count,
        "duplicate_rows": duplicate_rows,
        "invalid_rows": len(row_errors),
        "karnataka_stations_detected": karnataka_count,
        "bengaluru_stations_detected": bengaluru_count,
        "applied_filters": {
            "filter_karnataka": filter_karnataka,
            "filter_bengaluru": filter_bengaluru,
        },
        "missing_counts": missing_counts,
        "row_errors": row_errors[:50],
        "preview": rows,
        "summary": {
            "unique_operators": len(unique_operators),
            "total_connectors": total_connectors_sum,
            "connectors_note": "Connectors recorded as ports/plugs; not assumed to be independently managed chargers.",
            "karnataka_count": karnataka_count,
            "bengaluru_count": bengaluru_count,
        },
    }


def import_station_inventory_csv(
    db: Session,
    text: str,
    *,
    dataset_label: str = "Imported Station Inventory",
    filter_karnataka: bool = False,
    filter_bengaluru: bool = False,
    sync_to_map: bool = True,
) -> dict[str, Any]:
    """Import station inventory records idempotently, with optional geographic filtering."""
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    if not lines:
        raise StationInventoryCsvError("The file is empty.")

    reader = csv.DictReader(io.StringIO("\n".join(lines)))
    if reader.fieldnames is None:
        raise StationInventoryCsvError("No header row found.")

    header = [h.strip() for h in reader.fieldnames if h]
    col_map = _map_header(header)

    if "StationName" not in col_map or "Latitude" not in col_map or "Longitude" not in col_map:
        raise StationInventoryCsvError("CSV must contain Station Name, Latitude and Longitude.")

    org_id = effective_org(db)
    received = 0
    accepted = 0
    duplicates = 0
    rejected = 0
    filtered_out = 0
    errors: list[dict[str, Any]] = []

    existing_hashes = set(
        db.scalars(
            select(ImportedStation.record_hash).where(
                ImportedStation.org_id == org_id
            )
        ).all()
    )

    now = wall_now()
    batch_inventory: list[ImportedStation] = []
    batch_public: list[PublicStation] = []

    for line_idx, r in enumerate(reader, start=2):
        received += 1
        canon_row = {canon: r.get(orig, "").strip() if r.get(orig) else "" for canon, orig in col_map.items()}

        name = canon_row.get("StationName", "").strip()
        if not name:
            rejected += 1
            errors.append({"line": line_idx, "error": "Missing Station Name"})
            continue

        try:
            lat = float(str(canon_row.get("Latitude", "")).replace(",", ""))
            lng = float(str(canon_row.get("Longitude", "")).replace(",", ""))
        except (ValueError, TypeError):
            rejected += 1
            errors.append({"line": line_idx, "error": "Invalid coordinates"})
            continue

        if not (-90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0) or (lat == 0.0 and lng == 0.0):
            rejected += 1
            errors.append({"line": line_idx, "error": "Coordinates out of range"})
            continue

        op = canon_row.get("Operator", "").strip() or "Independent CPO"
        state = canon_row.get("State", "").strip()
        dist = canon_row.get("District", "").strip()
        city = canon_row.get("CityOrVillage", "").strip()

        is_kar = is_in_karnataka(state, dist, lat, lng)
        is_blr = is_in_bengaluru(state, dist, city, lat, lng)

        if filter_karnataka and not is_kar:
            filtered_out += 1
            continue
        if filter_bengaluru and not is_blr:
            filtered_out += 1
            continue

        st_hash = _calc_station_hash(op, name, lat, lng)
        if st_hash in existing_hashes:
            duplicates += 1
            continue
        existing_hashes.add(st_hash)

        conn_raw = canon_row.get("TotalConnectors", "")
        if conn_raw:
            try:
                connectors = int(float(conn_raw))
                if connectors < 1:
                    rejected += 1
                    errors.append({"line": line_idx, "error": f"Total Connectors must be positive: {conn_raw}"})
                    continue
            except (ValueError, TypeError):
                rejected += 1
                errors.append({"line": line_idx, "error": f"Invalid Total Connectors numeric format: {conn_raw}"})
                continue
        else:
            connectors = 1

        stable_id = f"IMP-{st_hash[:10].upper()}"

        stn = ImportedStation(
            org_id=org_id,
            station_id=stable_id,
            operator=op,
            ownership=canon_row.get("Ownership") or None,
            state=state or None,
            district=dist or None,
            city_or_village=city or None,
            station_name=name,
            lat=lat,
            lng=lng,
            charger_type=canon_row.get("ChargerType") or None,
            charger_rating=canon_row.get("ChargerRating") or None,
            connector_rating=canon_row.get("ConnectorRating") or None,
            total_connectors=connectors,
            is_karnataka=is_kar,
            is_bengaluru=is_blr,
            dataset_label=dataset_label,
            record_hash=st_hash,
            raw_payload={k: v for k, v in r.items() if v},
            created_at=now,
        )
        batch_inventory.append(stn)

        if sync_to_map:
            # Sync to public_stations table so existing Map page displays them
            pub_exists = db.scalar(
                select(PublicStation.id).where(
                    PublicStation.provider == "imported_inventory",
                    PublicStation.provider_id == stable_id,
                )
            )
            if not pub_exists:
                power_kw = None
                p_raw = canon_row.get("ChargerRating", "")
                if p_raw:
                    try:
                        power_kw = float(p_raw.lower().replace("kw", "").strip())
                    except ValueError:
                        power_kw = None

                conn_obj = [{
                    "type": canon_row.get("ChargerType") or "Type 2 / CCS2",
                    "power_kw": power_kw,
                    "quantity": connectors,
                    "current": None,
                }]

                addr_parts = [p for p in [city, dist, state] if p]
                pub = PublicStation(
                    provider="imported_inventory",
                    provider_id=stable_id,
                    name=name,
                    operator_name=op,
                    lat=lat,
                    lng=lng,
                    address=", ".join(addr_parts) if addr_parts else "Karnataka, India",
                    connections=conn_obj,
                    data_provider=dataset_label,
                    licence="Imported Station Inventory Record",
                    classification="IMPORTED INVENTORY",
                    usage_type=canon_row.get("Ownership") or "Public/Commercial",
                    attribution=f"{dataset_label} · Operator: {op}",
                    origin="imported_inventory",
                    retrieved_at=now,
                )
                batch_public.append(pub)

        accepted += 1
        if len(batch_inventory) >= 500:
            db.add_all(batch_inventory)
            if batch_public:
                db.add_all(batch_public)
                batch_public.clear()
            db.flush()
            batch_inventory.clear()

    if batch_inventory:
        db.add_all(batch_inventory)
    if batch_public:
        db.add_all(batch_public)
    db.flush()

    return {
        "dataset_label": dataset_label,
        "dataset_type": "Station Inventory",
        "received": received,
        "accepted": accepted,
        "duplicates": duplicates,
        "filtered_out": filtered_out,
        "rejected": rejected,
        "errors": errors[:50],
        "ok": accepted > 0 or (duplicates > 0 and rejected == 0),
    }
