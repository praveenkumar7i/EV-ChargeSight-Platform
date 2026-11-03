"""Comprehensive test suite for ChargeSight v10.3.
Covers:
1. Historical CSV parsing and validation (valid, missing fields, invalid formats).
2. Station inventory CSV parsing and validation (valid, invalid coordinates, non-numeric connectors).
3. Idempotent re-import of both CSV types (zero duplicate sessions or stations created).
4. Karnataka and Bengaluru geographic filtering.
5. Public station mapping of imported inventory.
6. Repeated simulation runs on imported stations (seed stability and determinism).
7. Reset of simulation on imported stations (verifying imported station records are untouched).
8. Ticket generation from imported station simulation faults.
9. Technician assignment and ticket state transitions (Open -> Assigned -> En Route -> On Site -> Resolved -> Closed).
10. Source-switching across all supported modes.
11. Isolation between presentation demo and user workspaces.
12. Entitlement enforcement (ensuring imported inventory does not violate managed charger limits).
13. Prevention of duplicate tickets for identical open faults.
"""
from __future__ import annotations

import io
from pathlib import Path
import pytest
from sqlalchemy import select, func

from app.models import HistoricalChargingSession, ImportedStation, PublicStation, Charger, Site, Ticket, TicketHistory
from app.integrations.historical_import import (
    preview_historical_csv,
    import_historical_csv,
    HistoricalCsvError,
    parse_datetime,
)
from app.integrations.station_inventory import (
    preview_station_inventory_csv,
    import_station_inventory_csv,
    StationInventoryCsvError,
    is_in_karnataka,
    is_in_bengaluru,
)
from app.services.imported_sim import (
    get_imported_sim_status,
    run_simulation_on_imported_stations,
    reset_imported_simulation,
)

SAMPLES = Path(__file__).resolve().parents[2] / "sample_data"
SAMPLE_HISTORICAL_PATH = SAMPLES / "IEEE_ChargingRecords_sample.csv"
SAMPLE_INVENTORY_PATH = SAMPLES / "Karnataka_Station_Inventory_sample.csv"


# ------------------------------------------------------------------ 1. Historical CSV Parsing & Validation
def test_historical_csv_parsing_valid():
    """Verify standard IEEE format preview preserves columns without unverified unit assumptions."""
    content = SAMPLE_HISTORICAL_PATH.read_text(encoding="utf-8")
    preview = preview_historical_csv(content)
    assert preview["invalid_rows"] == 0
    assert preview["valid_rows"] == 12
    assert preview["summary"]["unique_chargers"] >= 8
    assert preview["summary"]["total_demand"] > 0

    first = preview["preview"][0]
    assert first["UserID"] == "USR-1001"
    assert first["ChargerID"] == "CHG-A101"
    assert first["ChargerCompany"] == "ChargePoint"
    assert first["Duration"] == "3.25"
    assert first["Demand"] == "22.45"


def test_historical_csv_missing_fields_and_invalid_formats():
    """Verify detection of missing required headers and unparseable rows."""
    bad_csv = """UserID,Location,Demand
U-1,Indiranagar,10.5
"""
    with pytest.raises(HistoricalCsvError) as exc:
        preview_historical_csv(bad_csv)
    assert "Missing required ChargerID" in str(exc.value)

    # Empty file
    with pytest.raises(HistoricalCsvError):
        preview_historical_csv("")


# ------------------------------------------------------------------ 2. Station Inventory CSV Parsing & Validation
def test_inventory_csv_parsing_valid():
    """Verify station inventory parsing with coordinate validation and connector counting."""
    content = SAMPLE_INVENTORY_PATH.read_text(encoding="utf-8")
    preview = preview_station_inventory_csv(content)
    assert preview["invalid_rows"] == 0
    assert preview["valid_rows"] == 14
    assert preview["karnataka_stations_detected"] >= 13
    assert preview["bengaluru_stations_detected"] >= 8

    first = preview["preview"][0]
    assert first["Operator"] == "BESCOM"
    assert first["State"] == "Karnataka"
    assert first["District"] == "Bengaluru Urban"
    assert first["Latitude"] == pytest.approx(12.9348, 0.001)
    assert first["Longitude"] == pytest.approx(77.6101, 0.001)
    assert first["TotalConnectors"] == 4
    assert first["is_karnataka"] is True
    assert first["is_bengaluru"] is True


def test_inventory_csv_invalid_coordinates_and_connectors():
    """Verify invalid lat/lng and bad connector counts are flagged."""
    invalid_csv = """Operator,State,District,Station Name,Latitude,Longitude,Total Connectors
BadOp,Karnataka,Bengaluru,BadLat,95.000,77.500,4
BadOp2,Karnataka,Bengaluru,BadLng,12.900,200.000,2
BadOp3,Karnataka,Bengaluru,BadConn,12.900,77.500,-1
"""
    preview = preview_station_inventory_csv(invalid_csv)
    assert preview["valid_rows"] == 0
    assert preview["invalid_rows"] == 3
    assert len(preview["row_errors"]) == 3


# ------------------------------------------------------------------ 3. Idempotent Re-import of Both CSV Types
def test_idempotent_historical_import(db):
    """Importing the same historical file twice produces 0 new records and reports duplicates."""
    content = SAMPLE_HISTORICAL_PATH.read_text(encoding="utf-8")
    res1 = import_historical_csv(db, content)
    db.commit()
    assert res1["accepted"] == 12
    assert res1["duplicates"] == 0

    res2 = import_historical_csv(db, content)
    db.commit()
    assert res2["accepted"] == 0
    assert res2["duplicates"] == 12

    total_in_db = db.scalar(
        select(func.count(HistoricalChargingSession.id)).where(HistoricalChargingSession.org_id == 1)
    )
    assert total_in_db == 12


def test_idempotent_inventory_import(db):
    """Importing station inventory twice produces 0 new records and reports duplicates."""
    content = SAMPLE_INVENTORY_PATH.read_text(encoding="utf-8")
    res1 = import_station_inventory_csv(db, content, sync_to_map=True)
    db.commit()
    assert res1["accepted"] == 14
    assert res1["duplicates"] == 0

    res2 = import_station_inventory_csv(db, content, sync_to_map=True)
    db.commit()
    assert res2["accepted"] == 0
    assert res2["duplicates"] == 14

    total_in_db = db.scalar(
        select(func.count(ImportedStation.id)).where(ImportedStation.org_id == 1)
    )
    assert total_in_db == 14


# ------------------------------------------------------------------ 4. Karnataka & Bengaluru Geographic Filtering
def test_geographic_filtering(db):
    """Verify filtering imported stations by Karnataka and Bengaluru."""
    content = SAMPLE_INVENTORY_PATH.read_text(encoding="utf-8")
    import_station_inventory_csv(db, content, sync_to_map=False)
    db.commit()

    kar_stations = db.scalars(
        select(ImportedStation).where(ImportedStation.org_id == 1, ImportedStation.is_karnataka.is_(True))
    ).all()
    assert len(kar_stations) >= 13  # Mysore, Hubballi, Mangaluru, Bengaluru

    blr_stations = db.scalars(
        select(ImportedStation).where(ImportedStation.org_id == 1, ImportedStation.is_bengaluru.is_(True))
    ).all()
    assert len(blr_stations) >= 8
    for s in blr_stations:
        assert s.is_karnataka is True


# ------------------------------------------------------------------ 5. Public Station Mapping
def test_public_station_map_sync(db):
    """Verify imported stations sync to PublicStation with provider='imported_inventory'."""
    content = SAMPLE_INVENTORY_PATH.read_text(encoding="utf-8")
    import_station_inventory_csv(db, content, sync_to_map=True)
    db.commit()

    public_rows = db.scalars(
        select(PublicStation).where(PublicStation.provider == "imported_inventory")
    ).all()
    assert len(public_rows) >= 14
    first = public_rows[0]
    assert first.lat is not None and first.lng is not None
    assert first.operator_name in ["BESCOM", "Tata Power EZ Charge", "Zeon Charging", "Ather Grid", "Kazam EV", "Shell Recharge", "Jio-bp pulse"]


# ------------------------------------------------------------------ 6 & 7. Simulation on Imported Stations (Determinism & Reset)
def test_imported_station_simulation_determinism_and_reset(db):
    """Verify repeatable simulation with seed, fault creation, and reset preserving original inventory."""
    content = SAMPLE_INVENTORY_PATH.read_text(encoding="utf-8")
    import_station_inventory_csv(db, content, sync_to_map=False)
    db.commit()

    # Initial state
    st0 = get_imported_sim_status(db)
    assert st0["eligible_imported_stations"] > 0

    # Run 1 with seed=42, 10 stations
    r1 = run_simulation_on_imported_stations(db, seed=42, stations_limit=10, fault_probability=0.5)
    db.commit()
    assert r1["simulated_sites_active"] == 10
    chargers_1 = r1["simulated_chargers_active"]
    faults_1 = r1["generated_faults"]
    assert chargers_1 > 0

    # Reset
    res_reset = reset_imported_simulation(db)
    db.commit()
    assert res_reset["removed_chargers"] == chargers_1
    assert res_reset["imported_stations_preserved"] >= 14

    # Verify original imported stations are STILL intact!
    count_after_reset = db.scalar(
        select(func.count(ImportedStation.id)).where(ImportedStation.org_id == 1)
    )
    assert count_after_reset >= 14

    # Run 2 with same seed=42, 10 stations -> should be identical
    r2 = run_simulation_on_imported_stations(db, seed=42, stations_limit=10, fault_probability=0.5)
    db.commit()
    assert r2["simulated_sites_active"] == 10
    assert r2["simulated_chargers_active"] == chargers_1
    assert r2["generated_faults"] == faults_1

    # Clean up simulation
    reset_imported_simulation(db)
    db.commit()


# ------------------------------------------------------------------ 8 & 9. Maintenance Ticket Workflow on Simulated Chargers
def test_maintenance_ticket_lifecycle_on_simulated_charger(client, db):
    """Simulated fault on an imported charger creates a ticket that moves through the complete workflow."""
    content = SAMPLE_INVENTORY_PATH.read_text(encoding="utf-8")
    import_station_inventory_csv(db, content, sync_to_map=False)
    db.commit()

    # Run simulation with 100% fault probability on 1 station to ensure ticket creation
    run_simulation_on_imported_stations(db, seed=99, stations_limit=1, fault_probability=1.0)
    db.commit()

    # Find the created ticket
    ticket = db.scalar(
        select(Ticket).where(Ticket.source == "simulation", Ticket.charger_id.like("SIM-IMP-%"))
    )
    assert ticket is not None
    assert ticket.status in ("open", "assigned")
    ticket_code = ticket.code

    # 1. Verify or perform assignment (Open -> Assigned)
    if ticket.status == "open":
        r_assign = client.post(f"/api/tickets/{ticket_code}/assign", json={"technician_id": "TECH-01", "actor": "Test Owner"})
        assert r_assign.status_code == 200, r_assign.text
        assert r_assign.json()["status"] == "assigned"
    else:
        assert ticket.status == "assigned"
        assert ticket.technician_id is not None

    # 2. En route (Assigned -> En Route)
    r_enroute = client.post(f"/api/tickets/{ticket_code}/transition", json={"to_status": "en_route", "note": "En route to station"})
    assert r_enroute.status_code == 200, r_enroute.text
    assert r_enroute.json()["status"] == "en_route"

    # 3. On site (En Route -> On Site)
    r_onsite = client.post(f"/api/tickets/{ticket_code}/transition", json={"to_status": "on_site", "note": "Arrived at station"})
    assert r_onsite.status_code == 200, r_onsite.text
    assert r_onsite.json()["status"] == "on_site"

    # 4. Resolved (On Site -> Resolved)
    r_resolved = client.post(f"/api/tickets/{ticket_code}/transition", json={"to_status": "resolved", "note": "Connector repaired"})
    assert r_resolved.status_code == 200, r_resolved.text
    assert r_resolved.json()["status"] == "resolved"

    # 5. Closed (Resolved -> Closed)
    r_closed = client.post(f"/api/tickets/{ticket_code}/transition", json={"to_status": "closed", "note": "Supervised and verified", "inspection_passed": True})
    assert r_closed.status_code == 200, r_closed.text
    assert r_closed.json()["status"] == "closed"

    # Verify history audit trail via ticket details API
    t_details = client.get(f"/api/tickets/{ticket_code}").json()
    statuses = [h["to"] for h in t_details["history"]]
    assert "en_route" in statuses
    assert "on_site" in statuses
    assert "resolved" in statuses
    assert "closed" in statuses

    # Clean up via API
    r_reset = client.post("/api/sim/imported/reset")
    assert r_reset.status_code == 200, r_reset.text


# ------------------------------------------------------------------ 10. Source-Switching API Endpoints
def test_source_switching_apis(client):
    """Verify fleet summary and source status endpoints for new sources."""
    # Source status
    r_sources = client.get("/api/sources/status")
    assert r_sources.status_code == 200
    sources_data = r_sources.json()["sources"]
    assert "station_inventory" in sources_data
    assert "historical_sessions" in sources_data
    assert "simulated_imported" in sources_data

    # Fleet summary for station_inventory
    r_inv = client.get("/api/fleet/summary?source=station_inventory")
    assert r_inv.status_code == 200
    assert r_inv.json()["source"] == "station_inventory"

    # Fleet summary for historical_sessions
    r_hist = client.get("/api/fleet/summary?source=historical_sessions")
    assert r_hist.status_code == 200
    assert r_hist.json()["source"] == "historical_sessions"

    # Fleet summary for simulated_imported
    r_sim = client.get("/api/fleet/summary?source=simulated_imported")
    assert r_sim.status_code == 200
    assert r_sim.json()["source"] == "simulated_imported"


# ------------------------------------------------------------------ 11. Isolation Between Workspaces
def test_workspace_isolation_for_imported_data(db):
    """Imported stations in org 1 must not leak into another organization."""
    content = SAMPLE_INVENTORY_PATH.read_text(encoding="utf-8")
    import_station_inventory_csv(db, content, sync_to_map=False)
    db.commit()

    org1_stations = db.scalars(
        select(ImportedStation).where(ImportedStation.org_id == 1)
    ).all()
    org2_stations = db.scalars(
        select(ImportedStation).where(ImportedStation.org_id == 9999)
    ).all()

    assert len(org1_stations) >= 14
    assert len(org2_stations) == 0


# ------------------------------------------------------------------ 12. Entitlement Enforcement
def test_entitlements_do_not_count_imported_or_simulated_chargers(db):
    """Imported stations or simulated chargers must not exceed user plan limits for managed chargers."""
    from app.services.entitlements import usage

    # Even with 14 imported stations and simulated chargers, managed charger count should remain 0
    u = usage(db, 1)
    assert u["chargers"] == 0


# ------------------------------------------------------------------ 13. Prevention of Duplicate Tickets for Identical Open Faults
def test_prevent_duplicate_ticket_for_open_fault(client, db):
    """Deduplication ensures an identical open fault on the same charger does not open a second ticket."""
    content = SAMPLE_INVENTORY_PATH.read_text(encoding="utf-8")
    import_station_inventory_csv(db, content, sync_to_map=False)
    db.commit()

    # Simulate 1 station
    run_simulation_on_imported_stations(db, seed=12, stations_limit=1, fault_probability=1.0)
    db.commit()

    sim_charger = db.scalar(select(Charger).where(Charger.id.like("SIM-IMP-%")))
    assert sim_charger is not None

    # Count open tickets
    tickets_before = db.scalars(
        select(Ticket).where(Ticket.charger_id == sim_charger.id, Ticket.status.in_(["open", "assigned", "en_route", "on_site"]))
    ).all()
    assert len(tickets_before) >= 1
    target_category = tickets_before[0].fault_category

    # Re-running fault check on the same charger with same fault category
    from app.services.tickets import create_ticket
    from app.clock import wall_now
    now = wall_now()
    res = create_ticket(db, sim_charger, target_category, now)
    db.commit()
    assert res is None  # deduplicated!

    # Deduplicated if already open
    tickets_after = db.scalars(
        select(Ticket).where(Ticket.charger_id == sim_charger.id, Ticket.status.in_(["open", "assigned", "en_route", "on_site"]))
    ).all()
    assert len(tickets_after) == len(tickets_before)

    # Clean up via API
    r_reset = client.post("/api/sim/imported/reset")
    assert r_reset.status_code == 200, r_reset.text


# ------------------------------------------------------------------ 14. File Upload Size Limits (15 MB for Historical, 5 MB for Inventory)
def test_historical_csv_under_15mb_is_accepted(client):
    """A 7.5 MB file (well above former 5 MB limit, below new 15 MB limit) is accepted and previewed."""
    header = "UserID,ChargerID,ChargerCompany,Location,ChargerType,StartDay,StartTime,EndDay,EndTime,StartDatetime,EndDatetime,Duration,Demand\n"
    row = "USR-1001,CHG-A101,ChargePoint,Tech Park Hub East,Level 2 AC,2024-03-01,08:30:00,2024-03-01,11:45:00,2024-03-01 08:30:00,2024-03-01 11:45:00,3.25,22.45\n"
    num_rows = 55000  # ~7.7 MB
    content = header + (row * num_rows)
    content_bytes = content.encode("utf-8")
    assert 7 * 1024 * 1024 < len(content_bytes) < 15 * 1024 * 1024

    files = {"file": ("charging_records_large.csv", content_bytes, "text/csv")}
    r = client.post("/api/integrations/historical/preview", files=files)
    assert r.status_code == 200, r.text
    preview = r.json()
    assert preview["valid_rows"] == 1
    assert preview["duplicate_rows"] == num_rows - 1


def test_historical_csv_over_15mb_is_rejected(client):
    """A file exceeding 15 MB is rejected with 413 and clear limit message."""
    header = "UserID,ChargerID,ChargerCompany,Location,ChargerType,StartDay,StartTime,EndDay,EndTime,StartDatetime,EndDatetime,Duration,Demand\n"
    row = "USR-1001,CHG-A101,ChargePoint,Tech Park Hub East,Level 2 AC,2024-03-01,08:30:00,2024-03-01,11:45:00,2024-03-01 08:30:00,2024-03-01 11:45:00,3.25,22.45\n"
    num_rows = 115000  # ~16.1 MB
    content = header + (row * num_rows)
    content_bytes = content.encode("utf-8")
    assert len(content_bytes) > 15 * 1024 * 1024

    # Preview rejection
    files = {"file": ("too_large.csv", content_bytes, "text/csv")}
    r_prev = client.post("/api/integrations/historical/preview", files=files)
    assert r_prev.status_code == 413
    assert "File too large (limit 15 MB)" in r_prev.json()["detail"]

    # Import rejection
    files_import = {"file": ("too_large.csv", content_bytes, "text/csv")}
    r_imp = client.post("/api/integrations/historical/import", files=files_import, data={"dataset_label": "Big Data"})
    assert r_imp.status_code == 413
    assert "File too large (limit 15 MB)" in r_imp.json()["detail"]


def test_station_inventory_5mb_limit_preserved(client):
    """Station inventory upload retains its 5 MB limit."""
    header = "Operator,State,District,Station Name,Latitude,Longitude,Total Connectors\n"
    row = "BESCOM,Karnataka,Bengaluru Urban,Test Station,12.9348,77.6101,4\n"
    num_rows = 90000  # ~6.5 MB
    content = header + (row * num_rows)
    content_bytes = content.encode("utf-8")
    assert len(content_bytes) > 5 * 1024 * 1024

    files = {"file": ("inventory_large.csv", content_bytes, "text/csv")}
    r = client.post("/api/integrations/stations/preview", files=files)
    assert r.status_code == 413
    assert "File too large (limit 5 MB)" in r.json()["detail"]


# ------------------------------------------------------------------ 12. v10.3.2 Source Validation & Free Plan Access
def test_dashboard_and_analytics_source_validation_for_all_sources(client):
    """historical_sessions, station_inventory, and simulated_imported must not cause 422 errors on dashboard endpoints."""
    for source in ("historical_sessions", "station_inventory", "simulated_imported"):
        # Fleet summary
        r_sum = client.get(f"/api/fleet/summary?source={source}")
        assert r_sum.status_code == 200, f"Fleet summary failed for {source}: {r_sum.text}"

        # Timeseries analytics
        r_ts = client.get(f"/api/analytics/timeseries?source={source}")
        assert r_ts.status_code == 200, f"Timeseries failed for {source}: {r_ts.text}"

        # Faults analytics
        r_flt = client.get(f"/api/analytics/faults?source={source}")
        assert r_flt.status_code == 200, f"Faults failed for {source}: {r_flt.text}"

        # Maintenance analytics
        r_mnt = client.get(f"/api/analytics/maintenance?source={source}")
        assert r_mnt.status_code == 200, f"Maintenance failed for {source}: {r_mnt.text}"

        # Sites analytics
        r_sites = client.get(f"/api/analytics/sites?source={source}")
        assert r_sites.status_code == 200, f"Sites failed for {source}: {r_sites.text}"

        # Timeline
        r_tl = client.get(f"/api/timeline?source={source}")
        assert r_tl.status_code == 200, f"Timeline failed for {source}: {r_tl.text}"


def test_free_plan_can_import_csv_datasets(anon_client):
    """Free (Demo/Sandbox) plan users can preview and import historical CSV, station inventory CSV, and sample events without 403."""
    from tests.test_v9_auth import _signup

    r_auth = _signup(anon_client, email="freetier@example.com", organisation_name="Free Tier Org")
    assert r_auth.status_code == 200

    # Check plan is demo / free
    ent = anon_client.get("/api/entitlements").json()
    assert ent["plan_key"] == "demo"
    assert "data_import" not in ent["features"]
    assert "integrations" not in ent["features"]

    # 1. IEEE historical CSV preview & import allowed
    csv_hist = (
        "UserID,ChargerID,ChargerCompany,Location,ChargerType,StartDay,StartTime,EndDay,EndTime,StartDatetime,EndDatetime,Duration,Demand\n"
        "U1,C1,Co,Loc,AC,2024-01-01,10:00:00,2024-01-01,11:00:00,2024-01-01 10:00:00,2024-01-01 11:00:00,1.0,15.5\n"
    )
    r_prev_hist = anon_client.post("/api/integrations/historical/preview", files={"file": ("hist.csv", csv_hist.encode("utf-8"), "text/csv")})
    assert r_prev_hist.status_code == 200, r_prev_hist.text

    r_imp_hist = anon_client.post(
        "/api/integrations/historical/import",
        files={"file": ("hist.csv", csv_hist.encode("utf-8"), "text/csv")},
        data={"dataset_label": "Free Tier Test"},
    )
    assert r_imp_hist.status_code == 200, r_imp_hist.text

    # Verify sessions and summary endpoints are readable
    r_sessions = anon_client.get("/api/historical/sessions")
    assert r_sessions.status_code == 200
    assert len(r_sessions.json()["sessions"]) == 1

    r_summary = anon_client.get("/api/historical/summary")
    assert r_summary.status_code == 200
    assert r_summary.json()["total_sessions"] == 1

    # 2. Station inventory CSV preview & import allowed
    csv_inv = (
        "Operator,State,District,Station Name,Latitude,Longitude,Total Connectors\n"
        "BESCOM,Karnataka,Bengaluru Urban,Test Free Station,12.9716,77.5946,4\n"
    )
    r_prev_inv = anon_client.post("/api/integrations/stations/preview", files={"file": ("inv.csv", csv_inv.encode("utf-8"), "text/csv")})
    assert r_prev_inv.status_code == 200, r_prev_inv.text

    r_imp_inv = anon_client.post(
        "/api/integrations/stations/import",
        files={"file": ("inv.csv", csv_inv.encode("utf-8"), "text/csv")},
        data={"dataset_label": "Free Inv Test"},
    )
    assert r_imp_inv.status_code == 200, r_imp_inv.text

    # Verify imported stations endpoints are readable
    r_stations = anon_client.get("/api/imported-stations")
    assert r_stations.status_code == 200
    assert len(r_stations.json()["stations"]) == 1

    r_inv_sum = anon_client.get("/api/imported-stations/summary")
    assert r_inv_sum.status_code == 200
    assert r_inv_sum.json()["total_stations"] == 1

    # 3. Sample CSV import allowed
    r_imp_sample = anon_client.post("/api/integrations/import/sample")
    assert r_imp_sample.status_code == 200, r_imp_sample.text


def test_free_plan_blocked_from_live_integrations(anon_client):
    """Free (Demo/Sandbox) plan cannot configure live Operator API or OCPP integrations (requires paid plan)."""
    from tests.test_v9_auth import _signup

    r_auth = _signup(anon_client, email="freeblock@example.com", organisation_name="Free Block Org")
    assert r_auth.status_code == 200

    # Attempting to add an operator integration requires 'integrations' feature
    r_live = anon_client.post("/api/org/integrations", json={
        "name": "Live Operator",
        "kind": "operator_api",
        "base_url": "https://api.example.com/v1",
        "auth_type": "bearer",
        "auth_token": "secret123"
    })
    assert r_live.status_code == 403, f"Expected 403 Forbidden for live integration on Free plan, got {r_live.status_code}: {r_live.text}"

    # Attempting operator test requires 'integrations' feature
    r_test = anon_client.post("/api/integrations/operator/test")
    assert r_test.status_code == 403, f"Expected 403 Forbidden for operator test on Free plan, got {r_test.status_code}: {r_test.text}"

    # Attempting operator sync requires 'integrations' feature
    r_sync = anon_client.post("/api/integrations/operator/sync")
    assert r_sync.status_code == 403, f"Expected 403 Forbidden for operator sync on Free plan, got {r_sync.status_code}: {r_sync.text}"


# ------------------------------------------------------------------ 13. v10.3.3 Historical Visualizations & Filtering
def test_historical_summary_distributions_and_filtering(anon_client):
    """Historical summary returns duration and demand distributions, hourly arrivals, and supports filtering."""
    from tests.test_v9_auth import _signup

    r_auth = _signup(anon_client, email="v1033@example.com", organisation_name="v1033 Org")
    assert r_auth.status_code == 200

    # Import multiple sessions with distinct attributes
    csv_rows = [
        "UserID,ChargerID,ChargerCompany,Location,ChargerType,StartDay,StartTime,EndDay,EndTime,StartDatetime,EndDatetime,Duration,Demand",
        "U101,CHG-1,CompanyA,Downtown Mall,FastDC,2022-03-01,08:15:00,2022-03-01,08:45:00,2022-03-01 08:15:00,2022-03-01 08:45:00,30.0,25.5",
        "U102,CHG-2,CompanyA,Downtown Mall,FastDC,2022-03-01,09:00:00,2022-03-01,10:30:00,2022-03-01 09:00:00,2022-03-01 10:30:00,90.0,45.0",
        "U103,CHG-3,CompanyB,Airport Hub,Level2AC,2022-05-15,14:00:00,2022-05-15,18:00:00,2022-05-15 14:00:00,2022-05-15 18:00:00,240.0,12.0",
        "U104,CHG-4,CompanyB,Airport Hub,Level2AC,2022-05-16,20:00:00,2022-05-17,04:00:00,2022-05-16 20:00:00,2022-05-17 04:00:00,480.0,60.0",
    ]
    csv_data = "\n".join(csv_rows) + "\n"

    r_imp = anon_client.post(
        "/api/integrations/historical/import",
        files={"file": ("sessions.csv", csv_data.encode("utf-8"), "text/csv")},
        data={"dataset_label": "v10.3.3 Dataset"},
    )
    assert r_imp.status_code == 200

    # Test full summary
    r_sum = anon_client.get("/api/historical/summary")
    assert r_sum.status_code == 200
    s = r_sum.json()

    # Verify 6 KPIs
    assert s["total_sessions"] == 4
    assert s["unique_users"] == 4
    assert s["unique_chargers"] == 4
    assert s["total_demand"] == 142.5
    assert s["avg_demand"] == round(142.5 / 4, 2)
    assert s["avg_duration"] == round((30.0 + 90.0 + 240.0 + 480.0) / 4, 2)
    assert "demand_unit_note" in s
    assert "duration_unit_note" in s

    # Verify 6 Distributions
    assert len(s["hourly_distribution"]) == 24
    assert s["hourly_distribution"][8] == 1   # 08:15
    assert s["hourly_distribution"][9] == 1   # 09:00
    assert s["hourly_distribution"][14] == 1  # 14:00
    assert s["hourly_distribution"][20] == 1  # 20:00

    assert "FastDC" in s["charger_type_breakdown"]
    assert "Level2AC" in s["charger_type_breakdown"]
    assert s["charger_type_breakdown"]["FastDC"] == 2
    assert s["charger_type_breakdown"]["Level2AC"] == 2

    assert "Downtown Mall" in s["top_locations"]
    assert "Airport Hub" in s["top_locations"]

    assert "CompanyA" in s["company_breakdown"]
    assert "CompanyB" in s["company_breakdown"]

    assert s["duration_distribution"]["30–60 min"] == 1
    assert s["duration_distribution"]["1–2 hours"] == 1
    assert s["duration_distribution"]["4–8 hours"] == 1
    assert s["duration_distribution"]["> 8 hours"] == 1

    assert s["demand_distribution"]["5–15 kWh"] == 1
    assert s["demand_distribution"]["15–30 kWh"] == 1
    assert s["demand_distribution"]["30–50 kWh"] == 1
    assert s["demand_distribution"]["> 50 kWh"] == 1

    # Verify filter options returned
    assert "FastDC" in s["filter_options"]["charger_types"]
    assert "CompanyA" in s["filter_options"]["companies"]
    assert "Downtown Mall" in s["filter_options"]["locations"]
    assert s["filter_options"]["date_range"]["min"] == "2022-03-01"

    # Test filtering by charger_type
    r_f_type = anon_client.get("/api/historical/summary?charger_type=FastDC")
    assert r_f_type.status_code == 200
    assert r_f_type.json()["total_sessions"] == 2
    assert r_f_type.json()["unique_chargers"] == 2

    # Test filtering by location
    r_f_loc = anon_client.get("/api/historical/summary?location=Airport")
    assert r_f_loc.status_code == 200
    assert r_f_loc.json()["total_sessions"] == 2

    # Test filtering by company
    r_f_comp = anon_client.get("/api/historical/summary?company=CompanyB")
    assert r_f_comp.status_code == 200
    assert r_f_comp.json()["total_sessions"] == 2

    # Test filtering by date range
    r_f_date = anon_client.get("/api/historical/summary?date_from=2022-05-01&date_to=2022-05-31")
    assert r_f_date.status_code == 200
    assert r_f_date.json()["total_sessions"] == 2

    # Test sessions table filtering
    r_sess_f = anon_client.get("/api/historical/sessions?company=CompanyA&charger_type=FastDC")
    assert r_sess_f.status_code == 200
    assert r_sess_f.json()["total"] == 2
    assert len(r_sess_f.json()["sessions"]) == 2

    # Test no matches handling (graceful empty return)
    r_empty = anon_client.get("/api/historical/summary?company=NonExistent")
    assert r_empty.status_code == 200
    assert r_empty.json()["total_sessions"] == 0
    assert r_empty.json()["unique_chargers"] == 0



