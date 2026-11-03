"""Candidate-site planning: coordinate validation, nearby public listings and the explainable
suitability score.

Everything here is computed from evidence that is actually available:
  * PUBLIC LISTING records stored in ChargeSight (distances, counts, declared connectors);
  * OpenStreetMap context fetched on explicit request (amenities, nearest road class);
  * values the user entered (USER-ENTERED), clearly separated from ASSUMPTION defaults;
  * outputs of the existing Site Feasibility financial model (ESTIMATED).
Nothing is inferred about traffic, land, grid capacity, EV demand or occupancy.
"""
from __future__ import annotations

import math

from .areas import AREA_BY_KEY, BENGALURU_BBOX, area_name, assign_area, haversine_km

RINGS_KM = (0.5, 1.0, 2.0, 5.0)


class CoordinateError(ValueError):
    pass


def validate_point(lat, lng) -> tuple[float, float]:
    """Return floats or raise CoordinateError with a user-readable reason."""
    try:
        lat_f, lng_f = float(lat), float(lng)
    except (TypeError, ValueError):
        raise CoordinateError("Latitude and longitude must be numbers.")
    if not (math.isfinite(lat_f) and math.isfinite(lng_f)):
        raise CoordinateError("Latitude and longitude must be finite numbers.")
    if not (-90 <= lat_f <= 90 and -180 <= lng_f <= 180):
        raise CoordinateError("Latitude must be between -90 and 90 and longitude between -180 and 180.")
    s, w, n, e = BENGALURU_BBOX
    if not (s <= lat_f <= n and w <= lng_f <= e):
        hint = " (latitude and longitude look swapped)" if s <= lng_f <= n and w <= lat_f <= e else ""
        raise CoordinateError(f"{lat_f:.5f}, {lng_f:.5f} is outside the Bengaluru study area "
                              f"(latitude {s}–{n}, longitude {w}–{e}){hint}.")
    return round(lat_f, 7), round(lng_f, 7)


def nearby(stations: list[dict], lat: float, lng: float, *, limit: int = 10) -> dict:
    """Distances from the point to stored public listings (merged duplicates counted once)."""
    rows = []
    for s in stations:
        d = haversine_km(lat, lng, s["lat"], s["lng"])
        dc = sum((c.get("quantity") or 1) for c in s.get("connections") or [] if "DC" in (c.get("current") or "").upper()
                 or (not c.get("current") and (c.get("power_kw") or 0) > 22))
        conns = sum((c.get("quantity") or 1) for c in s.get("connections") or [])
        rows.append({"id": s["id"], "name": s["name"], "operator": s.get("operator"), "provider_label": s.get("provider_label"),
                     "lat": s["lat"], "lng": s["lng"], "distance_km": round(d, 3), "max_power_kw": s.get("max_power_kw"),
                     "connectors": conns, "dc_connectors": dc, "origin": s.get("origin"), "source_url": s.get("source_url")})
    rows.sort(key=lambda r: r["distance_km"])
    within = {str(r): sum(1 for x in rows if x["distance_km"] <= r) for r in RINGS_KM}
    two = [x for x in rows if x["distance_km"] <= 2.0]
    return {
        "stored_listings_considered": len(rows),
        "nearest": rows[:limit],
        "nearest_km": rows[0]["distance_km"] if rows else None,
        "within_km": within,
        "connectors_within_2km": sum(x["connectors"] for x in two),
        "dc_connectors_within_2km": sum(x["dc_connectors"] for x in two),
        "operators_within_2km": sorted({x["operator"] or "Unknown operator" for x in two}),
        "label": "PUBLIC LISTING",
        "caveat": "Distances use stored public listings, which may be incomplete or out of date; a listed charger may not be working.",
    }


def locate(lat: float, lng: float) -> dict:
    key, d = assign_area(lat, lng)
    return {"area": key, "area_name": area_name(key), "distance_to_area_centre_km": round(d, 2),
            "in_focus_area": key in AREA_BY_KEY}


# ====================================================================== explainable suitability score
# Weights are ASSUMPTIONS chosen for this academic prototype and can be changed by the user.
DEFAULT_WEIGHTS = {
    "coverage_gap": 0.25,
    "competition": 0.10,
    "demand_indicators": 0.20,
    "road_access": 0.10,
    "site_area": 0.10,
    "grid_capacity": 0.10,
    "financial": 0.15,
}
# Scoring thresholds (ASSUMPTIONS, documented in docs/PLANNING.md and returned by /api/planning/method)
COVERAGE_FULL_KM = 2.0          # nearest listed charger this far away or more -> full coverage-gap score
COMPETITION_HALF_CONNECTORS = 8  # this many listed connectors within 2 km halves the competition score
DEMAND_FULL_AMENITIES = 30       # this many shops/offices/amenities within 500 m -> full demand-indicator score
ROAD_SCORE = {"arterial": 1.0, "main": 0.75, "local": 0.4}
AREA_PER_CHARGER_M2 = {"DC": 30.0, "AC": 15.0}   # bay + equipment + clearance
POWER_FACTOR = 0.95              # kVA = kW / PF
PAYBACK_FULL_YEARS = 2.0         # payback at or below this -> full financial score
MIN_COMPLETENESS = 0.5           # below this share of weight with evidence -> "Insufficient data to rank reliably"

FACTOR_INFO = {
    "coverage_gap": ("Charging coverage gap", "Distance to the nearest stored public listing. Farther = larger gap.", "SOURCE DATA"),
    "competition": ("Competition nearby", "Listed connectors within 2 km. Fewer = less competition.", "SOURCE DATA"),
    "demand_indicators": ("Demand indicators", "Shops, offices, food, parking and other amenities within 500 m (OpenStreetMap, or your own count).", "SOURCE DATA / USER-ENTERED"),
    "road_access": ("Road access", "Class of the best nearby road (your observation, or OpenStreetMap within 150 m).", "USER-ENTERED / SOURCE DATA"),
    "site_area": ("Site area", "Usable area you entered vs. the area the planned chargers need.", "USER-ENTERED"),
    "grid_capacity": ("Grid capacity", "Available capacity you entered vs. the planned load. Unconfirmed values are ASSUMPTIONS.", "USER-ENTERED / ASSUMPTION"),
    "financial": ("Financial outlook", "Payback from the Site Feasibility model using your demand estimate and shared assumptions.", "ESTIMATED"),
}
FIELD_CHECKS = [
    "Visit the site: confirm the land/parking is available and can be leased for chargers.",
    "Ask BESCOM (or the landlord) for the sanctioned load and the cost and time of a new connection.",
    "Check that the nearby listed chargers exist and work; public listings are not live status.",
    "Observe or count traffic and parking turnover at the times you expect demand.",
    "Confirm access, safety and local permissions (setbacks, fire, signage).",
]


def normalise_weights(w: dict | None) -> dict:
    out = dict(DEFAULT_WEIGHTS)
    for k, v in (w or {}).items():
        if k not in DEFAULT_WEIGHTS:
            raise ValueError(f"Unknown factor '{k}'.")
        v = float(v)
        if not math.isfinite(v) or v < 0:
            raise ValueError(f"Weight for '{k}' must be a non-negative number.")
        out[k] = v
    tot = sum(out.values())
    if tot <= 0:
        raise ValueError("At least one weight must be greater than zero.")
    return {k: v / tot for k, v in out.items()}


def _plan(inputs: dict, base: dict) -> tuple[int, str, float, list[str]]:
    """Charger plan: the user's candidate values, otherwise the shared ASSUMPTION inputs."""
    notes = []
    n = inputs.get("n_chargers") or base.get("n_chargers")
    t = inputs.get("charger_type") or base.get("charger_type") or "DC"
    p = inputs.get("power_kw") or base.get("power_kw")
    for k, label in (("n_chargers", "charger count"), ("charger_type", "charger type"), ("power_kw", "power per charger")):
        if not inputs.get(k):
            notes.append(f"{label} from the shared assumptions")
    return int(n), str(t), float(p), notes


def finance_inputs(cand: dict, base: dict, multipliers: dict | None = None):
    """Shared (identical) assumptions + the candidate's own location, plan and demand estimate."""
    from . import finance

    d = dict(base)
    d.update({"site_name": cand["name"], "lat": cand["lat"], "lng": cand["lng"]})
    for k in ("n_chargers", "charger_type", "power_kw", "sessions_per_day"):
        if cand.get("inputs", {}).get(k) is not None:
            d[k] = cand["inputs"][k]
    for k, m in (multipliers or {}).items():
        if k == "capex":
            for c in ("equipment_cost_per_charger", "installation_cost", "grid_connection_cost", "site_preparation_cost"):
                d[c] = d[c] * m
        else:
            d[k] = d[k] * m
    return finance.InvestmentInputs.from_dict(d)


def score_candidate(cand: dict, evidence: dict, weights: dict, base: dict, multipliers: dict | None = None) -> dict:
    from . import finance

    inputs = cand.get("inputs") or {}
    osm = (cand.get("context") or {}).get("osm")
    nb = evidence["nearby"]
    n, ctype, pkw, plan_notes = _plan(inputs, base)
    f = {}

    def put(key, score, cls, why, value=None):
        f[key] = {"key": key, "label": FACTOR_INFO[key][0], "weight": weights[key], "score": None if score is None else round(max(0.0, min(1.0, score)), 3),
                  "evidence_class": cls, "explanation": why, "value": value, "definition": FACTOR_INFO[key][1]}

    # coverage gap
    if nb["stored_listings_considered"] and nb["nearest_km"] is not None:
        d = nb["nearest_km"]
        put("coverage_gap", d / COVERAGE_FULL_KM, "SOURCE DATA",
            f"Nearest listed charger is {d:.2f} km away ({'a clear gap' if d >= COVERAGE_FULL_KM else 'partly served' if d >= 0.5 else 'already served nearby'}); "
            f"{COVERAGE_FULL_KM:g} km or more scores full marks. Listings may be incomplete.", d)
    else:
        put("coverage_gap", None, "MISSING", "No stored public listings to measure coverage against.")
    # competition
    if nb["stored_listings_considered"]:
        c = nb["connectors_within_2km"]
        put("competition", 1 / (1 + c / COMPETITION_HALF_CONNECTORS), "SOURCE DATA",
            f"{c} listed connector(s) within 2 km ({nb['dc_connectors_within_2km']} DC) from {len(nb['operators_within_2km'])} operator(s); "
            f"{COMPETITION_HALF_CONNECTORS} connectors halve this score.", c)
    else:
        put("competition", None, "MISSING", "No stored public listings to assess competition.")
    # demand indicators
    if osm is not None:
        a = osm.get("amenity_total", 0)
        put("demand_indicators", a / DEMAND_FULL_AMENITIES, "SOURCE DATA",
            f"{a} mapped amenities within {osm.get('amenities_within_m', 500)} m in OpenStreetMap ({DEMAND_FULL_AMENITIES} or more scores full marks). "
            "OSM coverage varies; this indicates activity, not EV demand." +
            (f" You counted {inputs['amenities_observed']}." if inputs.get("amenities_observed") is not None else ""), a)
    elif inputs.get("amenities_observed") is not None:
        a = inputs["amenities_observed"]
        put("demand_indicators", a / DEMAND_FULL_AMENITIES, "USER-ENTERED",
            f"You counted {a} shops/offices/amenities within ~500 m ({DEMAND_FULL_AMENITIES} or more scores full marks). Indicates activity, not EV demand.", a)
    else:
        put("demand_indicators", None, "MISSING", "No amenity evidence: look up OpenStreetMap context or enter what you observed.")
    # road access
    ra = inputs.get("road_access")
    if ra in ROAD_SCORE:
        put("road_access", ROAD_SCORE[ra], "USER-ENTERED", f"You observed: {ra} road.", ra)
    elif osm and osm.get("best_road"):
        br = osm["best_road"]
        put("road_access", ROAD_SCORE[br["class"]], "SOURCE DATA",
            f"Best mapped road within {osm.get('roads_within_m', 150)} m: {br['highway']}{(' (' + br['name'] + ')') if br.get('name') else ''} → {br['class']}.", br["class"])
    else:
        put("road_access", None, "MISSING", "Road access unknown: enter what you observed or look up OpenStreetMap context.")
    # site area
    need = n * AREA_PER_CHARGER_M2.get(ctype, 30.0)
    if inputs.get("site_area_m2"):
        a = inputs["site_area_m2"]
        put("site_area", a / need, "USER-ENTERED",
            f"You entered {a:g} m²; {n} {ctype} charger(s) need about {need:g} m² ({AREA_PER_CHARGER_M2.get(ctype, 30):g} m² each, ASSUMPTION)" +
            (f"; {', '.join(plan_notes)}" if plan_notes else "") + ".", a)
    else:
        put("site_area", None, "MISSING", f"Site area unknown; {n} {ctype} charger(s) would need about {need:g} m² (ASSUMPTION).")
    # grid capacity
    req = n * pkw / POWER_FACTOR
    if inputs.get("grid_available_kva") is not None:
        g = inputs["grid_available_kva"]
        confirmed = inputs.get("grid_confirmed") is True
        put("grid_capacity", g / req if req else None, "USER-ENTERED" if confirmed else "ASSUMPTION",
            f"{g:g} kVA available vs about {req:.0f} kVA needed ({n} × {pkw:g} kW ÷ {POWER_FACTOR} power factor). " +
            ("Confirmed by the utility/landlord, as stated by you." if confirmed else "Not confirmed: treat as an assumption until BESCOM confirms."), g)
    else:
        put("grid_capacity", None, "MISSING", f"Grid capacity unknown; the plan needs about {req:.0f} kVA. Ask BESCOM or the landlord.")
    # financial (existing Site Feasibility model)
    fin = None
    if inputs.get("sessions_per_day") is not None:
        try:
            fi = finance_inputs(cand, base, multipliers)
            r = finance.evaluate(fi)
            pb, yrs = r["payback_years"], fi.analysis_years
            fin = {"initial_investment": r["initial_investment"]["total"], "year1_operating_contribution": r["year1"]["operating_contribution"],
                   "npv": r["npv"], "irr": r["irr"], "payback_years": pb, "breakeven_sessions_per_day": r["breakeven_sessions_per_day"],
                   "implied_utilisation": r["year1"]["implied_utilisation"], "analysis_years": yrs}
            s = 0.0 if pb is None else (yrs - pb) / (yrs - PAYBACK_FULL_YEARS) if yrs > PAYBACK_FULL_YEARS else (1.0 if pb <= yrs else 0.0)
            why = (f"Payback {pb:.1f} years" if pb else f"No payback within {yrs} years") + f", NPV ₹{r['npv']:,.0f} with your estimate of {fi.sessions_per_day:g} sessions/day and the shared assumptions. " \
                  f"Payback ≤ {PAYBACK_FULL_YEARS:g} years scores full marks; none within the analysis period scores zero."
            budget = inputs.get("budget_inr")
            if budget and fin["initial_investment"] > budget:
                s *= 0.5
                why += f" Investment ₹{fin['initial_investment']:,.0f} exceeds your budget ₹{budget:,.0f} (score halved)."
            put("financial", s, "ESTIMATED", why, pb)
        except finance.FinanceInputError as e:
            put("financial", None, "MISSING", f"Financial model could not run: {e}")
    else:
        put("financial", None, "MISSING", "Enter your expected sessions/day for this candidate to estimate payback.")

    factors = [f[k] for k in DEFAULT_WEIGHTS]
    have = [x for x in factors if x["score"] is not None]
    w_have = sum(x["weight"] for x in have)
    completeness = w_have / sum(weights.values())
    score = sum(x["weight"] * x["score"] for x in have) / w_have if w_have else None
    missing = [x for x in factors if x["score"] is None]
    unconfirmed = [x for x in factors if x["evidence_class"] == "ASSUMPTION"]
    reasons = []
    if completeness < MIN_COMPLETENESS:
        reasons.append(f"only {completeness:.0%} of the weighted evidence is available (needs at least {MIN_COMPLETENESS:.0%})")
    if f["coverage_gap"]["score"] is None:
        reasons.append("coverage cannot be measured without stored listings")
    if f["demand_indicators"]["score"] is None and f["financial"]["score"] is None:
        reasons.append("there is no demand evidence (amenities) and no demand estimate")
    sufficient = not reasons
    if not sufficient:
        conf = "insufficient"
    elif completeness >= 0.85 and not unconfirmed:
        conf = "high"
    elif completeness >= 0.65:
        conf = "medium"
    else:
        conf = "low"
    return {
        "candidate_id": cand["id"], "name": cand["name"], "lat": cand["lat"], "lng": cand["lng"], "area_name": evidence["area_name"],
        "score": round(score * 100, 1) if (score is not None and sufficient) else None,
        "partial_score": round(score * 100, 1) if score is not None else None,
        "completeness": round(completeness, 3), "confidence": conf, "sufficient": sufficient,
        "status": "ranked" if sufficient else "Insufficient data to rank reliably",
        "insufficient_reasons": reasons,
        "factors": factors,
        "missing_evidence": [{"factor": x["label"], "needed": x["explanation"]} for x in missing],
        "unconfirmed_assumptions": [x["label"] for x in unconfirmed],
        "field_checks": FIELD_CHECKS,
        "financial": fin,
        "plan": {"n_chargers": n, "charger_type": ctype, "power_kw": pkw, "notes": plan_notes},
    }


def rank(results: list[dict]) -> list[int]:
    ok = [r for r in results if r["sufficient"]]
    ok.sort(key=lambda r: (-r["score"], r["candidate_id"]))
    return [r["candidate_id"] for r in ok]


SENS_FINANCE = {
    "demand -25%": {"sessions_per_day": 0.75},
    "demand +25%": {"sessions_per_day": 1.25},
    "capex +20%": {"capex": 1.20},
    "charging price -10%": {"price_per_kwh": 0.90},
    "electricity cost +15%": {"electricity_cost_per_kwh": 1.15},
}


def evaluate_candidates(cands: list[dict], evidences: dict, weights: dict | None, base: dict) -> dict:
    w = normalise_weights(weights)
    results = [score_candidate(c, evidences[c["id"]], w, base) for c in cands]
    base_rank = rank(results)
    sens = []
    if len(base_rank) >= 2:
        for k in DEFAULT_WEIGHTS:
            for m in (0.5, 1.5):
                ww = normalise_weights({**{kk: vv for kk, vv in w.items()}, k: w[k] * m})
                rr = rank([score_candidate(c, evidences[c["id"]], ww, base) for c in cands])
                sens.append({"scenario": f"weight of {FACTOR_INFO[k][0].lower()} ×{m:g}", "kind": "weights", "ranking": rr,
                             "top_changes": bool(rr and rr[0] != base_rank[0])})
        for name, mult in SENS_FINANCE.items():
            res = [score_candidate(c, evidences[c["id"]], w, base, mult) for c in cands]
            rr = rank(res)
            sens.append({"scenario": name, "kind": "financial", "ranking": rr, "top_changes": bool(rr and rr[0] != base_rank[0]),
                         "payback_years": {r["candidate_id"]: (r["financial"] or {}).get("payback_years") for r in res},
                         "npv": {r["candidate_id"]: (r["financial"] or {}).get("npv") for r in res}})
    robust = bool(sens) and not any(s["top_changes"] for s in sens)
    if len(base_rank) >= 3:
        note = None
    elif len(base_rank) == 2:
        note = "Only two candidates have enough evidence to rank; add evidence for at least one more to compare three."
    elif len(base_rank) == 1:
        note = "Only one candidate has enough evidence; a ranking needs at least two (three recommended)."
    else:
        note = "Insufficient data to rank reliably: no candidate has enough evidence yet. See each candidate's missing evidence."
    return {"weights": w, "results": results, "ranking": base_rank, "sensitivity": sens, "robust_top": robust if sens else None,
            "note": note, "label": "ESTIMATED",
            "disclaimer": "Ranks candidates for further investigation only. A high score does not mean a site is construction-ready, "
                          "and no traffic, land, grid, EV-demand or occupancy data has been assumed."}


def method() -> dict:
    return {
        "default_weights": DEFAULT_WEIGHTS,
        "factors": [{"key": k, "label": v[0], "definition": v[1], "evidence": v[2], "default_weight": DEFAULT_WEIGHTS[k]} for k, v in FACTOR_INFO.items()],
        "thresholds": {"coverage_full_km": COVERAGE_FULL_KM, "competition_half_connectors": COMPETITION_HALF_CONNECTORS,
                       "demand_full_amenities": DEMAND_FULL_AMENITIES, "road_score": ROAD_SCORE, "area_per_charger_m2": AREA_PER_CHARGER_M2,
                       "power_factor": POWER_FACTOR, "payback_full_years": PAYBACK_FULL_YEARS, "min_completeness": MIN_COMPLETENESS},
        "formula": "score = Σ(weight × factor score) ÷ Σ(weight of factors with evidence) × 100; completeness = Σ(weight with evidence) ÷ Σ(all weights). "
                   "Factor scores are 0–1. A candidate is ranked only if completeness ≥ 50 %, coverage is measurable and there is some demand evidence.",
        "evidence_classes": {"SOURCE DATA": "retrieved from a named source with a timestamp (stored public listings, OpenStreetMap)",
                             "USER-ENTERED": "a value you entered (stated as confirmed where relevant)",
                             "ASSUMPTION": "an unconfirmed value or a default used for the calculation",
                             "ESTIMATED": "calculated by the financial model from assumptions",
                             "MISSING": "no evidence; excluded from the score and listed as needed"},
        "field_checks": FIELD_CHECKS,
        "sensitivity": {"weights": "each weight ×0.5 and ×1.5", "financial": SENS_FINANCE},
    }
