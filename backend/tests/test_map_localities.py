"""Locality-first Map: counts derived from stored records, true zero vs unknown, freshness."""
from __future__ import annotations

from collections import Counter
from datetime import timedelta

import httpx


def _rec(pid, lat, lng, op="Op A", provider="open_charge_map", **kw):
    return {"provider": provider, "provider_id": str(pid), "name": f"Station {pid}", "operator_name": op, "lat": lat, "lng": lng,
            "address": "", "connections": [{"type": "CCS (Type 2)", "power_kw": 60, "quantity": 2, "current": "DC"}],
            "licence": "CC BY 4.0", "source_url": f"https://openchargemap.org/poi/details/{pid}", "origin": "api", **kw}


def test_locality_counts_match_stored_records(client):
    loc = client.get("/api/public-stations/localities").json()
    stations = client.get("/api/public-stations").json()["stations"]
    by_area = Counter(s["area"] for s in stations)
    assert loc["all"]["stations"] == len(stations) > 0
    assert sum(a["stations"] for a in loc["areas"]) + loc["other"]["stations"] == len(stations)
    for a in loc["areas"]:
        assert a["stations"] == by_area.get(a["key"], 0), a["key"]
        mine = [s for s in stations if s["area"] == a["key"]]
        assert a["operators"] == dict(Counter(s["operator"] or "Unknown operator" for s in mine).most_common()) or \
            Counter(a["operators"]) == Counter(s["operator"] or "Unknown operator" for s in mine)
        assert a["key"] and a["lat"] and a["radius_km"]                 # needed by the map to centre/zoom
    assert {a["key"] for a in loc["areas"]} == {"whitefield", "electronic_city", "hsr_layout", "koramangala", "hebbal", "yelahanka"}


def test_zero_is_unknown_with_snapshot_only_and_true_zero_after_refresh(db, settings_env, monkeypatch):
    from app.integrations import public_listings as PL
    from app.integrations.base import set_health
    from app.clock import wall_now

    # Only snapshot-origin records, all in Whitefield
    PL.upsert(db, [_rec(1, 12.97, 77.75, origin="snapshot")], retrieved_at=wall_now())
    db.commit()
    loc = PL.localities(db)
    yel = next(a for a in loc["areas"] if a["key"] == "yelahanka")
    assert yel["stations"] == 0 and yel["coverage_status"] == "unknown" and "not a complete inventory" in yel["coverage_message"]
    assert loc["refreshed_sources"] == []

    # A successful full-area API refresh makes a zero a true zero for that source
    PL.upsert(db, [_rec(2, 12.97, 77.751)], retrieved_at=wall_now())
    set_health(db, "open_charge_map", "connected", success=True, detail={"received": 1})
    db.commit()
    loc = PL.localities(db)
    yel = next(a for a in loc["areas"] if a["key"] == "yelahanka")
    assert yel["coverage_status"] == "true_zero" and "true zero" in yel["coverage_message"]
    wf = next(a for a in loc["areas"] if a["key"] == "whitefield")
    assert wf["coverage_status"] == "listings" and wf["stations"] >= 1 and wf["dc_connectors"] >= 2


def test_freshness_and_stale_flag(db):
    from app.integrations import public_listings as PL
    from app.clock import wall_now

    PL.upsert(db, [_rec(10, 13.10, 77.596)], retrieved_at=wall_now() - timedelta(days=45))
    db.commit()
    yel = next(a for a in PL.localities(db)["areas"] if a["key"] == "yelahanka")
    assert yel["stations"] == 1 and yel["stale"] and yel["data_age_days"] >= 44


def test_duplicates_counted_once_and_invalid_records_never_stored(db):
    from app.integrations import public_listings as PL
    from app.clock import wall_now

    res = PL.upsert(db, [{**_rec(20, 12.9352, 77.6245), "name": "Forum Mall EV Hub"}, {**_rec("node/1", 12.93521, 77.62451, provider="osm"), "name": "Forum Mall EV Hub"},
                         _rec(21, 99.0, 77.6), _rec(22, 12.93, 200.0)], retrieved_at=wall_now())
    db.commit()
    assert res["rejected"] == 2                                            # out-of-range coordinates rejected
    kor = next(a for a in PL.localities(db)["areas"] if a["key"] == "koramangala")
    assert kor["stations"] == 1                                            # OSM duplicate merged into the OCM record


def test_public_listings_never_claim_live_status(client):
    loc = client.get("/api/public-stations/localities").json()
    assert loc["label"] == "PUBLIC LISTING" and "not mean it is working" in loc["status_note"]
    for s in client.get("/api/public-stations").json()["stations"]:
        assert s["status"] == "STATUS UNKNOWN" and s["classification"] in ("PUBLIC LISTING", "IMPORTED")
