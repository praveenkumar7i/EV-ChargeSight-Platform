"""Explainable suitability ranking and candidate financial comparison."""
from __future__ import annotations

import pytest


def _cand(client, name, lat, lng, **inputs):
    return client.post("/api/planning/candidates", json={"name": name, "lat": lat, "lng": lng, "inputs": inputs}).json()


FULL = dict(n_chargers=2, charger_type="DC", power_kw=60, sessions_per_day=30, site_area_m2=120,
            grid_available_kva=150, grid_confirmed=True, road_access="main", amenities_observed=24)


def test_weights_are_normalised_and_validated(client):
    from app.core.planning import DEFAULT_WEIGHTS, normalise_weights

    assert abs(sum(DEFAULT_WEIGHTS.values()) - 1) < 1e-9
    w = normalise_weights({"coverage_gap": 2, "financial": 0})
    assert abs(sum(w.values()) - 1) < 1e-9 and w["financial"] == 0
    with pytest.raises(ValueError):
        normalise_weights({k: 0 for k in DEFAULT_WEIGHTS})
    with pytest.raises(ValueError):
        normalise_weights({"traffic_volume": 1})                       # no invented factors
    c = _cand(client, "A", 12.9698, 77.75, **FULL)
    assert client.post("/api/planning/evaluate", json={"candidate_ids": [c["id"]], "weights": {"coverage_gap": -1}}).status_code == 422


def test_insufficient_data_is_reported_not_ranked(client):
    c = _cand(client, "Bare point", 12.9698, 77.75)                      # no user evidence, no OSM context
    r = client.post("/api/planning/evaluate", json={"candidate_ids": [c["id"]]}).json()
    x = r["results"][0]
    assert x["sufficient"] is False and x["score"] is None and x["status"] == "Insufficient data to rank reliably"
    assert x["confidence"] == "insufficient" and r["ranking"] == []
    missing = {m["factor"] for m in x["missing_evidence"]}
    assert {"Demand indicators", "Road access", "Site area", "Grid capacity", "Financial outlook"} <= missing
    assert any("demand" in reason for reason in x["insufficient_reasons"])
    assert "Insufficient data to rank reliably" in r["note"]
    classes = {f["key"]: f["evidence_class"] for f in x["factors"]}
    assert classes["coverage_gap"] == "SOURCE DATA" and classes["site_area"] == "MISSING"


def test_score_is_weighted_mean_of_available_factors_with_explanations(client):
    from app.core.planning import COVERAGE_FULL_KM

    c = _cand(client, "Full", 12.9698, 77.75, **FULL)
    r = client.post("/api/planning/evaluate", json={"candidate_ids": [c["id"]]}).json()
    x = r["results"][0]
    have = [f for f in x["factors"] if f["score"] is not None]
    expect = sum(f["weight"] * f["score"] for f in have) / sum(f["weight"] for f in have) * 100
    assert x["sufficient"] and abs(x["score"] - expect) < 0.2
    assert x["completeness"] == pytest.approx(1.0) and x["confidence"] == "high"
    f = {k["key"]: k for k in x["factors"]}
    assert f["coverage_gap"]["score"] == pytest.approx(min(f["coverage_gap"]["value"] / COVERAGE_FULL_KM, 1), abs=1e-3)
    assert f["site_area"]["score"] == 1.0 and "60 m²" in f["site_area"]["explanation"]          # 2 DC × 30 m² (ASSUMPTION)
    assert f["grid_capacity"]["evidence_class"] == "USER-ENTERED" and "126 kVA" in f["grid_capacity"]["explanation"]
    assert f["road_access"]["score"] == 0.75 and f["demand_indicators"]["evidence_class"] == "USER-ENTERED"
    assert f["financial"]["evidence_class"] == "ESTIMATED"
    for k in f.values():
        assert k["explanation"] and k["definition"]
    assert x["field_checks"] and "construction-ready" in r["disclaimer"]


def test_unconfirmed_grid_is_an_assumption_and_lowers_confidence(client):
    c = _cand(client, "Unconfirmed grid", 12.9698, 77.75, **{**FULL, "grid_confirmed": False})
    x = client.post("/api/planning/evaluate", json={"candidate_ids": [c["id"]]}).json()["results"][0]
    g = next(f for f in x["factors"] if f["key"] == "grid_capacity")
    assert g["evidence_class"] == "ASSUMPTION" and "Not confirmed" in g["explanation"]
    assert x["confidence"] != "high" and "Grid capacity" in x["unconfirmed_assumptions"]


def test_financial_factor_reuses_existing_model_and_budget(client):
    from app.core import finance

    c = _cand(client, "Fin", 12.9116, 77.6474, **FULL)
    x = client.post("/api/planning/evaluate", json={"candidate_ids": [c["id"]]}).json()["results"][0]
    direct = finance.evaluate(finance.InvestmentInputs.from_dict({**finance.InvestmentInputs().to_dict(),
                                                                 "n_chargers": 2, "charger_type": "DC", "power_kw": 60, "sessions_per_day": 30}))
    assert x["financial"]["payback_years"] == pytest.approx(direct["payback_years"])
    assert x["financial"]["npv"] == pytest.approx(direct["npv"])
    low = _cand(client, "Fin low budget", 12.9116, 77.6474, **{**FULL, "budget_inr": 100000})
    y = client.post("/api/planning/evaluate", json={"candidate_ids": [low["id"]]}).json()["results"][0]
    fx = next(f for f in x["factors"] if f["key"] == "financial")["score"]
    fy = next(f for f in y["factors"] if f["key"] == "financial")
    assert fy["score"] == pytest.approx(fx * 0.5, abs=1e-3) and "exceeds your budget" in fy["explanation"]


def test_three_candidates_ranked_with_sensitivity(client):
    a = _cand(client, "Gap site", 13.0700, 77.5000, **FULL)                      # far from listings
    b = _cand(client, "Dense site", 12.9698, 77.7500, **FULL)
    c = _cand(client, "Weak demand", 12.9116, 77.6474, **{**FULL, "amenities_observed": 2, "sessions_per_day": 8})
    r = client.post("/api/planning/evaluate", json={"candidate_ids": [a["id"], b["id"], c["id"]]}).json()
    assert len(r["ranking"]) == 3 and r["note"] is None
    scores = {x["candidate_id"]: x["score"] for x in r["results"]}
    assert r["ranking"] == sorted(scores, key=lambda i: -scores[i])
    assert r["ranking"][-1] == c["id"]
    kinds = {s["kind"] for s in r["sensitivity"]}
    assert kinds == {"weights", "financial"} and len([s for s in r["sensitivity"] if s["kind"] == "weights"]) == 14
    up = next(s for s in r["sensitivity"] if s["scenario"] == "demand +25%")
    dn = next(s for s in r["sensitivity"] if s["scenario"] == "demand -25%")
    for cid in (a["id"], b["id"]):
        assert up["payback_years"][str(cid)] < dn["payback_years"][str(cid)]      # more demand -> faster payback (JSON keys are strings)
    assert isinstance(r["robust_top"], bool)


def test_compare_finance_uses_identical_assumptions(client):
    from app.core import finance

    a = _cand(client, "X", 12.9698, 77.75, n_chargers=4, sessions_per_day=40)
    b = _cand(client, "Y", 13.0358, 77.597)
    base = {**finance.InvestmentInputs().to_dict(), "price_per_kwh": 20.0}
    r = client.post("/api/planning/compare-finance", json={"candidate_ids": [a["id"], b["id"]], "base_inputs": base}).json()
    rx, ry = r["rows"]
    assert rx["candidate_values_used"] == {"n_chargers": 4, "sessions_per_day": 40} and ry["candidate_values_used"] == {}
    direct_y = finance.evaluate(finance.InvestmentInputs.from_dict({**base, "site_name": "Y", "lat": 13.0358, "lng": 77.597}))
    assert ry["npv"] == pytest.approx(direct_y["npv"]) and ry["irr"] == pytest.approx(direct_y["irr"])
    assert ry["breakeven_sessions_per_day"] == pytest.approx(direct_y["breakeven_sessions_per_day"])
    same = client.post("/api/planning/compare-finance", json={"candidate_ids": [a["id"], b["id"]], "base_inputs": base,
                                                              "use_candidate_values": False}).json()["rows"]
    assert same[0]["npv"] == pytest.approx(same[1]["npv"])                       # location alone changes nothing
    bad = client.post("/api/planning/compare-finance", json={"candidate_ids": [a["id"]], "base_inputs": {"price_per_kwh": -5}})
    assert bad.status_code == 422


def test_method_documents_weights_thresholds_and_classes(client):
    m = client.get("/api/planning/method").json()
    assert set(m["default_weights"]) == {f["key"] for f in m["factors"]}
    assert {"SOURCE DATA", "USER-ENTERED", "ASSUMPTION", "ESTIMATED", "MISSING"} == set(m["evidence_classes"])
    assert "Σ" in m["formula"] and m["thresholds"]["min_completeness"] == 0.5
