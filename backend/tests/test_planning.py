"""Candidate-site workflow: coordinate validation, distances, candidates, OSM context, prefill."""
from __future__ import annotations

import math

import httpx
import pytest


# ------------------------------------------------------------------ coordinates and distances
def test_validate_point():
    from app.core.planning import CoordinateError, validate_point

    assert validate_point("12.9352", 77.6245) == (12.9352, 77.6245)
    for bad, msg in [((13.5, 77.6), "outside the Bengaluru study area"), ((77.6245, 12.9352), "outside"),
                     ((float("nan"), 77.6), "finite"), (("abc", 77.6), "numbers"), ((95, 77.6), "between -90 and 90")]:
        with pytest.raises(CoordinateError, match=msg):
            validate_point(*bad)


def test_haversine_matches_independent_calculation():
    from app.core.areas import haversine_km

    # Koramangala centre -> HSR Layout centre, cross-checked with an equirectangular approximation
    a, b = (12.9352, 77.6245), (12.9116, 77.6474)
    x = math.radians(b[1] - a[1]) * math.cos(math.radians((a[0] + b[0]) / 2))
    y = math.radians(b[0] - a[0])
    approx = 6371.0088 * math.hypot(x, y)
    assert abs(haversine_km(*a, *b) - approx) < 0.005
    assert 3.60 < haversine_km(*a, *b) < 3.63          # 2.624 km N-S, 2.485 km E-W -> 3.614 km
    assert haversine_km(*a, *a) == 0


def test_nearby_orders_by_distance_and_counts_rings():
    from app.core.planning import nearby

    def st(i, lat, lng, conns):
        return {"id": i, "name": f"S{i}", "operator": None, "provider_label": "Open Charge Map", "lat": lat, "lng": lng,
                "max_power_kw": 60, "connections": conns, "origin": "api", "source_url": None}
    dc = [{"type": "CCS", "power_kw": 60, "quantity": 2, "current": "DC"}]
    ac = [{"type": "Type 2", "power_kw": 7.4, "quantity": 1, "current": "AC"}]
    p = (12.9352, 77.6245)
    stations = [st(1, 12.9352, 77.6345, dc),     # ~1.08 km east
                st(2, 12.9362, 77.6245, ac),     # ~0.11 km north
                st(3, 12.9800, 77.6245, dc)]     # ~4.98 km north
    r = nearby(stations, *p)
    assert [n["id"] for n in r["nearest"]] == [2, 1, 3]
    assert 0.10 < r["nearest_km"] < 0.12
    assert r["within_km"] == {"0.5": 1, "1.0": 1, "2.0": 2, "5.0": 3}
    assert r["connectors_within_2km"] == 3 and r["dc_connectors_within_2km"] == 2
    assert nearby([], *p)["nearest_km"] is None


# ------------------------------------------------------------------ candidates API
def test_preview_and_candidate_crud_are_separate_from_other_data(client):
    pub_before = client.get("/api/public-stations").json()["total"]
    sites_before = len(client.get("/api/investment/sites").json())
    assert client.get("/api/planning/preview?lat=13.6&lng=77.6").status_code == 422
    pv = client.get("/api/planning/preview?lat=12.9698&lng=77.7500").json()
    assert pv["area"] == "whitefield" and pv["nearby"]["label"] == "PUBLIC LISTING"
    assert pv["nearby"]["nearest_km"] is not None and pv["nearby"]["nearest"] == sorted(pv["nearby"]["nearest"], key=lambda n: n["distance_km"])

    c = client.post("/api/planning/candidates", json={"name": "Whitefield A", "lat": 12.9698, "lng": 77.75}).json()
    assert c["label"] == "CANDIDATE" and c["area"] == "whitefield" and c["inputs"] == {}
    bad = client.put(f"/api/planning/candidates/{c['id']}", json={"inputs": {"n_chargers": 0}})
    assert bad.status_code == 422
    up = client.put(f"/api/planning/candidates/{c['id']}", json={"inputs": {"n_chargers": 4, "charger_type": "DC", "site_area_m2": 200}}).json()
    assert up["inputs"] == {"n_chargers": 4, "charger_type": "DC", "site_area_m2": 200}
    assert client.put(f"/api/planning/candidates/{c['id']}", json={"lat": 13.9}).status_code == 422
    assert len(client.get("/api/planning/candidates").json()) == 1
    # nothing else changed
    assert client.get("/api/public-stations").json()["total"] == pub_before
    assert len(client.get("/api/investment/sites").json()) == sites_before
    assert client.delete(f"/api/planning/candidates/{c['id']}").json() == {"deleted": c["id"]}
    assert client.get(f"/api/planning/candidates/{c['id']}").status_code == 404


def test_prefill_only_location_and_user_values_with_sources(client):
    c = client.post("/api/planning/candidates", json={"name": "HSR B", "lat": 12.9116, "lng": 77.6474,
                                                      "inputs": {"n_chargers": 3, "sessions_per_day": 30, "site_area_m2": 150}}).json()
    r = client.get(f"/api/planning/candidates/{c['id']}/prefill").json()
    assert r["values"] == {"site_name": "HSR B", "lat": 12.9116, "lng": 77.6474, "n_chargers": 3, "sessions_per_day": 30}
    assert set(r["sources"]) == set(r["values"])
    assert r["sources"]["sessions_per_day"]["label"] == "ASSUMPTION" and r["sources"]["lat"]["label"] == "USER-ENTERED"
    assert r["locality"] == "HSR Layout" and "nothing about demand" in r["not_prefilled_note"].lower()


def test_save_link_and_delete_never_removes_proposed_site(client):
    c = client.post("/api/planning/candidates", json={"name": "Hebbal C", "lat": 13.0358, "lng": 77.597}).json()
    site = client.post("/api/investment/sites", json={"name": "Hebbal C", "inputs": {"lat": 13.0358, "lng": 77.597}}).json()
    linked = client.post(f"/api/planning/candidates/{c['id']}/link", json={"site_id": site["id"]}).json()
    assert linked["saved_site_id"] == site["id"]
    assert client.post(f"/api/planning/candidates/{c['id']}/link", json={"site_id": 99999}).status_code == 404
    client.delete(f"/api/planning/candidates/{c['id']}")
    assert any(s["id"] == site["id"] for s in client.get("/api/investment/sites").json())


# ------------------------------------------------------------------ OpenStreetMap context (mocked)
OSM_OK = {"elements": [
    {"type": "node", "id": 1, "lat": 12.9353, "lon": 77.6246, "tags": {"amenity": "cafe", "name": "Cafe One"}},
    {"type": "node", "id": 2, "lat": 12.9354, "lon": 77.6247, "tags": {"shop": "supermarket"}},
    {"type": "way", "id": 3, "center": {"lat": 12.9355, "lon": 77.6248}, "tags": {"amenity": "parking"}},
    {"type": "node", "id": 2, "lat": 12.9354, "lon": 77.6247, "tags": {"shop": "supermarket"}},          # duplicate ignored
    {"type": "way", "id": 10, "center": {"lat": 12.9356, "lon": 77.6245}, "tags": {"highway": "residential"}},
    {"type": "way", "id": 11, "center": {"lat": 12.9360, "lon": 77.6245}, "tags": {"highway": "primary", "name": "Sarjapur Rd"}},
]}


@pytest.fixture
def osm(client, settings_env, monkeypatch):
    from app.integrations import osm_context as M

    settings_env(osm_enabled="true")
    state = {"handler": lambda r: httpx.Response(200, json=OSM_OK), "calls": 0}
    orig = M.OverpassContextClient.__init__

    def init(self, settings=None, transport=None):
        def h(req):
            state["calls"] += 1
            return state["handler"](req)
        orig(self, settings, transport or httpx.MockTransport(h))

    monkeypatch.setattr(M.OverpassContextClient, "__init__", init)
    c = client.post("/api/planning/candidates", json={"name": "Kora D", "lat": 12.9352, "lng": 77.6245}).json()
    state["cid"] = c["id"]
    return state


def test_osm_context_success_and_throttle(client, osm):
    r = client.post(f"/api/planning/candidates/{osm['cid']}/context").json()
    assert r["ok"] and r["osm"]["amenity_total"] == 3
    assert r["osm"]["amenity_categories"] == {"food": 1, "retail": 1, "parking": 1}
    assert r["osm"]["best_road"]["class"] == "arterial" and r["osm"]["best_road"]["name"] == "Sarjapur Rd"
    assert "ODbL" in r["osm"]["attribution"] and r["osm"]["label"] == "SOURCE DATA"
    again = client.post(f"/api/planning/candidates/{osm['cid']}/context").json()
    assert again["cached"] and osm["calls"] == 1                                 # no second request within 10 min
    stored = client.get(f"/api/planning/candidates/{osm['cid']}").json()["context"]["osm"]
    assert stored["amenity_total"] == 3


@pytest.mark.parametrize("handler,needle", [
    (lambda r: httpx.Response(429), "rate limit"),
    (lambda r: httpx.Response(504), "busy"),
    (lambda r: httpx.Response(200, text="<html>"), "not valid JSON"),
    (lambda r: httpx.Response(200, json={"x": 1}), "no 'elements'"),
    (lambda r: (_ for _ in ()).throw(httpx.ConnectTimeout("t", request=r)), "did not respond in time"),
    (lambda r: (_ for _ in ()).throw(httpx.ConnectError("dns", request=r)), "Could not reach Overpass"),
])
def test_osm_context_failures_store_nothing(client, osm, handler, needle):
    osm["handler"] = handler
    r = client.post(f"/api/planning/candidates/{osm['cid']}/context")
    assert r.status_code == 200 and r.json()["ok"] is False and needle in r.json()["message"]
    assert client.get(f"/api/planning/candidates/{osm['cid']}").json()["context"] == {}


def test_osm_context_disabled_is_reported(client, settings_env):
    settings_env(osm_enabled="false")
    c = client.post("/api/planning/candidates", json={"name": "E", "lat": 12.8452, "lng": 77.6602}).json()
    r = client.post(f"/api/planning/candidates/{c['id']}/context").json()
    assert r["ok"] is False and "OSM_ENABLED=false" in r["message"]
