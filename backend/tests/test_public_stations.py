"""Public charging-station listings: snapshot accuracy, validation, dedup, connectors, API, separation, secrets.

These tests never contact Google, Open Charge Map or Overpass: connectors are
exercised with httpx.MockTransport. A passing suite does NOT show that the
Google map or the live providers work with real keys.
"""
import json
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select

SNAP = Path(__file__).resolve().parents[2] / "sample_data" / "PUBLIC_ocm_bengaluru_snapshot.json"


def _osm_el(i, lat, lon, **tags):
    return {"type": "node", "id": i, "lat": lat, "lon": lon, "tags": {"amenity": "charging_station", **tags}}


# ------------------------------------------------------------------ bundled snapshot
def test_snapshot_file_integrity():
    doc = json.loads(SNAP.read_text(encoding="utf-8"))
    assert doc["licence"] == "CC BY 4.0" and "openchargemap" in doc["attribution"].lower()
    ids = [s["provider_id"] for s in doc["stations"]]
    assert len(ids) == len(set(ids))
    for s in doc["stations"]:
        assert s["source_url"] == f"https://openchargemap.org/poi/details/{s['provider_id']}"
        assert 12.75 <= s["lat"] <= 13.20 and 77.45 <= s["lng"] <= 77.80
        assert s["connections"], s["name"]


def test_snapshot_loaded_with_exact_source_values(client):
    st = {s["provider_id"]: s for s in client.get("/api/public-stations").json()["stations"]}
    assert len(st) == len(json.loads(SNAP.read_text(encoding="utf-8"))["stations"])
    ec = st["308920"]                                   # values copied from openchargemap.org/poi/details/308920
    assert ec["name"] == "JioBP Pulse Charging Hub - Electronic City Phase II"
    assert (ec["lat"], ec["lng"]) == (12.86462, 77.67486)
    assert ec["operator"] == "JIO BP Pulse (India)" and ec["area"] == "electronic_city"
    assert {"type": "CCS (Type 2)", "power_kw": 60.0, "quantity": 16, "current": "DC"} in ec["connections"]
    assert st["309186"]["lat"] == 13.087848141186939 and st["309186"]["area"] == "other"
    assert st["309187"]["area"] == "hebbal" and st["309297"]["area"] == "koramangala"
    assert st["188200"]["operator"] is None and st["188200"]["usage_cost"] is None       # unknown stays unknown


def test_snapshot_reload_is_idempotent(client):
    n = client.get("/api/public-stations").json()["total"]
    r = client.post("/api/public-stations/reload-snapshot").json()
    assert r["inserted"] == 0 and r["updated"] == n
    assert client.get("/api/public-stations").json()["total"] == n


def test_every_listing_is_status_unknown_and_attributed(client):
    body = client.get("/api/public-stations").json()
    assert "not live status" in body["label"]
    for s in body["stations"]:
        assert s["status"] == "STATUS UNKNOWN"
        assert s["classification"] == "PUBLIC LISTING"
        assert s["source_url"] and s["licence"] and s["attribution"] and s["retrieved_at"]
        assert "not live" in (s["listed_status"] or "not live")


def test_inventory_counts_are_computed(client):
    inv = client.get("/api/public-stations/inventory").json()
    total = client.get("/api/public-stations").json()["total"]
    assert inv["total_stations"] == total == sum(a["stations"] for a in inv["by_area"])
    areas = {a["key"]: a for a in inv["by_area"]}
    assert set(areas) == {"electronic_city", "hebbal", "hsr_layout", "koramangala", "whitefield", "yelahanka", "other"}
    yl = areas["yelahanka"]
    assert yl["stations"] == 0 and "No verified public listing" in yl["message"]
    assert sum(inv["by_operator"].values()) == total
    assert any(k.startswith("DC") for k in inv["power_classes"])


# ------------------------------------------------------------------ validation + dedup
def test_validation_rejects_bad_records(db):
    from app.integrations.public_listings import upsert

    base = dict(provider="osm", name="X", operator_name="", address="", connections=[])
    res = upsert(db, [
        dict(base, provider_id="a", lat=None, lng=77.6),
        dict(base, provider_id="b", lat=0, lng=0),
        dict(base, provider_id="c", lat=19.07, lng=72.87),                 # Mumbai: outside Bengaluru
        dict(base, provider_id="d", lat=12.95, lng=77.6, name=""),
        dict(base, provider_id="e", lat=12.95, lng=77.6, connections=[{"type": "CCS", "power_kw": -5, "quantity": "two"}]),
        dict(base, provider_id="e", lat=12.95, lng=77.6),                   # repeated in batch
    ])
    assert res["inserted"] == 1 and res["rejected"] == 5
    from app.models import PublicStation
    row = db.scalar(select(PublicStation).where(PublicStation.provider_id == "e"))
    assert row.connections == [{"type": "CCS", "power_kw": None, "quantity": None, "current": None}]


def test_cross_provider_duplicates_are_merged(client, db_url):
    from app import db as D
    from app.integrations.public_listings import upsert

    before = client.get("/api/public-stations").json()["total"]
    with D.session_scope() as s:
        res = upsert(s, [
            # same place as OCM-309329 "Nexus Whitefield Jio-BP", ~20 m away, similar name
            {"provider": "osm", "provider_id": "node/1", "name": "Jio-bp pulse Nexus Whitefield", "lat": 12.96015, "lng": 77.74735},
            # 40 m away but a different business name: kept separate
            {"provider": "osm", "provider_id": "node/2", "name": "Ather Grid Hoodi", "lat": 12.96040, "lng": 77.74719},
        ])
    assert res["duplicates_linked"] == 1 and res["inserted"] == 2
    body = client.get("/api/public-stations").json()
    assert body["total"] == before + 1
    nexus = next(s for s in body["stations"] if s["provider_id"] == "309329")
    assert nexus["also_listed_by"][0]["provider"] == "osm"
    # re-running the same batch updates in place, never duplicates
    with D.session_scope() as s:
        again = upsert(s, [{"provider": "osm", "provider_id": "node/2", "name": "Ather Grid Hoodi", "lat": 12.96040, "lng": 77.74719}])
    assert again["inserted"] == 0 and again["updated"] == 1
    assert client.get("/api/public-stations").json()["total"] == before + 1


# ------------------------------------------------------------------ connectors (mocked)
def test_overpass_parsing(settings_env):
    from app.integrations.osm_overpass import OverpassClient

    s = settings_env(osm_enabled="true")
    payload = {"elements": [
        _osm_el(11, 12.97, 77.75, name="Test hub", operator="Statiq", **{"socket:type2_combo": "2", "socket:type2_combo:output": "60 kW",
                                                                          "socket:type2": "1", "opening_hours": "24/7", "fee": "yes"}),
        {"type": "way", "id": 22, "center": {"lat": 12.93, "lon": 77.62}, "tags": {"amenity": "charging_station"}},
        {"type": "node", "id": 33, "tags": {"amenity": "charging_station"}},          # no coordinates
    ]}
    c = OverpassClient(s, transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload)), backoff_s=0)
    rows = c.fetch()
    assert len(rows) == 2
    a = rows[0]
    assert a["provider_id"] == "node/11" and a["source_url"] == "https://www.openstreetmap.org/node/11"
    assert {"type": "CCS (Type 2)", "power_kw": 60.0, "quantity": 2, "current": "DC"} in a["connections"]
    assert a["opening_hours"] == "24/7" and a["licence"] == "ODbL 1.0"
    assert rows[1]["name"].startswith("Unnamed") and rows[1]["connections"] == []


def test_overpass_errors(settings_env):
    from app.integrations.base import ConnectorError
    from app.integrations.osm_overpass import OverpassClient

    s = settings_env(osm_enabled="true")
    calls = []

    def busy(r):
        calls.append(1)
        return httpx.Response(429)
    with pytest.raises(ConnectorError, match="rate limit"):
        OverpassClient(s, transport=httpx.MockTransport(busy), max_retries=2, backoff_s=0).fetch()
    assert len(calls) == 3
    with pytest.raises(ConnectorError, match="JSON"):
        OverpassClient(s, transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>")), backoff_s=0).fetch()
    with pytest.raises(ConnectorError, match="disabled"):
        OverpassClient(settings_env(osm_enabled="false")).fetch()


def test_ocm_parse_keeps_source_fields(settings_env):
    from app.integrations.open_charge_map import OpenChargeMapClient

    s = settings_env(ocm_api_key="OCMKEY-123")
    poi = [{"ID": 7, "UsageCost": "20/kWh", "UsageType": {"Title": "Public"}, "StatusType": {"Title": "Operational"},
            "OperatorInfo": {"Title": "Statiq (IN)"}, "DataProvider": {"Title": "Open Charge Map Contributors", "License": "CC BY 4.0"},
            "AddressInfo": {"Title": "Mall", "Latitude": 12.93, "Longitude": 77.62, "Town": "Bengaluru", "Postcode": "560034"},
            "Connections": [{"ConnectionType": {"Title": "CCS (Type 2)"}, "PowerKW": 60, "Quantity": 2, "CurrentType": {"Title": "DC"}}]}]
    rows = OpenChargeMapClient(s, transport=httpx.MockTransport(lambda r: httpx.Response(200, json=poi))).fetch()
    r = rows[0]
    assert r["source_url"].endswith("/poi/details/7") and r["usage_cost"] == "20/kWh"
    assert r["connections"][0]["current"] == "DC" and "not live" in r["listed_status"]


def test_google_places_live_lookup_not_stored(client, settings_env, db_url):
    from app import db as D
    from app.integrations import google_places
    from app.models import PublicStation

    s = settings_env(google_places_api_key="GKEY-SECRET-777")
    pages = {None: {"places": [{"id": "p1", "displayName": {"text": "Zeon Charging Koramangala"}, "formattedAddress": "Koramangala",
                                "location": {"latitude": 12.934, "longitude": 77.626}, "googleMapsUri": "https://maps.google.com/?cid=1",
                                "evChargeOptions": {"connectorCount": 2, "connectorAggregation": [
                                    {"type": "EV_CONNECTOR_TYPE_CCS_COMBO_2", "maxChargeRateKw": 60, "count": 2}]}}],
                    "nextPageToken": "T2"},
             "T2": {"places": [{"id": "p2", "displayName": {"text": "Far away"}, "location": {"latitude": 19.0, "longitude": 72.8}}]}}
    seen = []

    def handler(req):
        body = json.loads(req.content)
        seen.append((req.headers["X-Goog-FieldMask"], body.get("pageToken")))
        assert req.headers["X-Goog-Api-Key"] == "GKEY-SECRET-777"
        return httpx.Response(200, json=pages[body.get("pageToken")])
    client_g = google_places.GooglePlacesClient(s, transport=httpx.MockTransport(handler))
    with D.session_scope() as db:
        before = db.scalar(select(func.count(PublicStation.id)))
    out = google_places.search(["koramangala"], client_g)
    assert [r["provider_id"] for r in out["results"]] == ["p1"]            # out-of-area result dropped
    assert out["results"][0]["connections"] == [{"type": "CCS (Type 2)", "power_kw": 60, "quantity": 2, "current": "DC"}]
    assert len(seen) == 2 and "places.evChargeOptions" in seen[0][0]
    with D.session_scope() as db:
        assert db.scalar(select(func.count(PublicStation.id))) == before    # never written to the database

    def denied(req):
        return httpx.Response(403, json={"error": {"message": "API key GKEY-SECRET-777 not authorized"}})
    bad = google_places.search(["hebbal"], google_places.GooglePlacesClient(s, transport=httpx.MockTransport(denied)))
    assert bad["results"] == [] and "GKEY-SECRET-777" not in bad["errors"][0]["error"]


def test_google_endpoint_without_key(client):
    r = client.get("/api/public-stations/google?areas=hebbal")
    assert r.status_code == 400 and "GOOGLE_PLACES_API_KEY" in r.json()["detail"]


# ------------------------------------------------------------------ API, refresh, separation, secrets
def test_filters_and_search(client):
    allst = client.get("/api/public-stations").json()
    wf = client.get("/api/public-stations?area=whitefield").json()
    assert wf["count"] > 0 and all(s["area"] == "whitefield" for s in wf["stations"])
    st = client.get("/api/public-stations?operator=Statiq (IN)").json()
    assert st["count"] > 0 and all(s["operator"] == "Statiq (IN)" for s in st["stations"])
    q = client.get("/api/public-stations?q=hsr").json()
    assert any("HSR" in s["name"] or "HSR" in (s["address"] or "") for s in q["stations"])
    assert client.get("/api/public-stations?area=mars").status_code == 422
    assert allst["total"] == wf["total"]


def test_refresh_endpoint(client, monkeypatch, settings_env):
    from app.integrations import osm_overpass

    r = client.post("/api/public-stations/refresh")
    assert r.status_code == 400 and "configured" in r.json()["detail"]        # OSM disabled + no OCM key in tests
    settings_env(osm_enabled="true")
    monkeypatch.setattr(osm_overpass.OverpassClient, "fetch", lambda self, bbox=None: [
        {"provider": "osm", "provider_id": "node/900", "name": "OSM-only station", "lat": 13.10, "lng": 77.596,
         "operator_name": "", "address": "", "connections": [], "licence": "ODbL 1.0", "source_url": "https://www.openstreetmap.org/node/900"}])
    r = client.post("/api/public-stations/refresh", json={"providers": ["osm", "open_charge_map"]}).json()
    assert r["results"]["osm"]["ok"] and r["results"]["osm"]["inserted"] == 1
    assert r["results"]["open_charge_map"]["state"] == "not_configured"
    yl = client.get("/api/public-stations?area=yelahanka").json()
    assert [s["name"] for s in yl["stations"]] == ["OSM-only station"]
    assert client.post("/api/public-stations/refresh", json={"providers": ["google_places"]}).status_code == 422


def test_public_listings_are_separate_from_operational_data(client):
    pub = client.get("/api/public-stations").json()["stations"]
    sim_ids = {c["id"] for c in client.get("/api/chargers?source=simulation").json()}
    imp = client.get("/api/chargers?source=operator_import").json()
    assert not ({str(s["id"]) for s in pub} & sim_ids) and imp == []
    client.post("/api/sim/inject", json={"charger_id": "SIM-KRM-01", "category": "overcurrent"})
    after = client.get("/api/public-stations").json()["stations"]
    assert all(s["status"] == "STATUS UNKNOWN" for s in after)               # simulated faults never touch public listings
    assert all("ticket" not in json.dumps(s).lower() for s in after)


def test_secrets_never_exposed(client, settings_env):
    s = settings_env(google_places_api_key="GKEY-SECRET-777", ocm_api_key="OCM-SECRET-888")
    assert "GKEY-SECRET-777" in s.secret_values()
    blob = json.dumps([client.get(u).json() for u in ("/api/config", "/api/public-stations", "/api/public-stations/inventory",
                                                      "/api/integrations/health")])
    assert "GKEY-SECRET-777" not in blob and "OCM-SECRET-888" not in blob
    csv = client.get("/api/reports/public_stations.csv").text
    assert "SECRET" not in csv


def test_public_stations_csv_export(client):
    csv = client.get("/api/reports/public_stations.csv")
    assert csv.status_code == 200
    t = csv.text
    assert "CC BY 4.0" in t and "STATUS UNKNOWN" in t and "unknown" in t
    rows = [l for l in t.splitlines() if l and not l.startswith("#")]
    assert len(rows) - 1 == client.get("/api/public-stations").json()["total"]
