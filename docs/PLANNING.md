# Candidate-site planning and the suitability score

ChargeSight helps you pick **locations worth investigating** for a new public charging site in Bengaluru. It does not declare
any site construction-ready, and it never invents traffic volumes, land availability, grid capacity, EV demand, charger
occupancy or construction costs.

## 1. Workflow
1. **Map → choose a locality** (cards at the top). Counts, operators and freshness come from stored listings.
2. **Plan a new charging site** → click the map, type coordinates, or search an address (Google Geocoding, if enabled).
   The point must lie in the study area (latitude 12.75–13.20, longitude 77.45–77.80); swapped latitude/longitude is detected.
3. Review the **nearby public listings**: nearest distance, counts within 0.5/1/2/5 km, connectors and operators within 2 km.
4. **Save as candidate** and record what you know (all optional, all USER-ENTERED): chargers, type, power, budget,
   expected sessions/day, usable site area, available grid capacity and whether the utility/landlord confirmed it,
   road access, amenities you counted, notes.
5. Optional: **Look up OpenStreetMap context** (one bounded Overpass request: amenities within 500 m, roads within 150 m;
   at most once per candidate per 10 minutes unless you confirm; respects `OSM_ENABLED`).
6. **Rank ticked candidates** (Map) and/or **Continue to Site Feasibility** for the full financial model.
7. In Site Feasibility, **Compare planning candidates** under identical assumptions, and **Save as proposed site** only after
   the confirmation dialog. **Back to map** restores the locality and candidate.

Data separation: public listings (`public_stations`), planning candidates (`planning_candidates`), saved proposed sites
(`investment_sites`) and simulated/imported/live operational data are separate tables, API routes and map layers.

## 2. Evidence classes
| Class | Meaning |
|---|---|
| SOURCE DATA | Retrieved from a named source with a timestamp: stored public listings (Open Charge Map, OpenStreetMap), OpenStreetMap context. Not a ground survey. |
| USER-ENTERED | A value you entered. Grid capacity counts as USER-ENTERED only when you state it was confirmed by the utility/landlord. |
| ASSUMPTION | An unconfirmed value (e.g. grid capacity you have not confirmed, your demand estimate) or a default used for a calculation. |
| ESTIMATED | Calculated by the Site Feasibility financial model from assumptions. |
| MISSING | No evidence. The factor is left out of the score and listed as needed. |

## 3. Factors, default weights and thresholds
Weights are ASSUMPTIONS for this academic prototype; change them with the sliders (they are normalised to 100 %).

| Factor | Weight | Evidence | Factor score (0–1) |
|---|---|---|---|
| Charging coverage gap | 25 % | SOURCE DATA (stored listings) | nearest listed charger distance ÷ 2 km, capped at 1 |
| Competition nearby | 10 % | SOURCE DATA (stored listings) | 1 ÷ (1 + listed connectors within 2 km ÷ 8) |
| Demand indicators | 20 % | SOURCE DATA (OSM) or USER-ENTERED count | amenities within 500 m ÷ 30, capped at 1 |
| Road access | 10 % | USER-ENTERED or SOURCE DATA (OSM) | arterial 1.0 · main 0.75 · local 0.4 |
| Site area | 10 % | USER-ENTERED | usable area ÷ (chargers × 30 m² DC / 15 m² AC), capped at 1 |
| Grid capacity | 10 % | USER-ENTERED (confirmed) or ASSUMPTION | available kVA ÷ (chargers × kW ÷ 0.95), capped at 1 |
| Financial outlook | 15 % | ESTIMATED (existing model) | 1 at payback ≤ 2 years, falling linearly to 0 at the analysis period; 0 if no payback; halved if the investment exceeds your budget |

**Score** = Σ(weight × factor score) ÷ Σ(weights of factors with evidence) × 100.
**Evidence completeness** = Σ(weights of factors with evidence) ÷ Σ(all weights).

**Ranked only if** completeness ≥ 50 %, coverage is measurable (stored listings exist), and there is some demand evidence
(amenity data or your sessions/day estimate). Otherwise the candidate shows **“Insufficient data to rank reliably”** with the
reasons and the evidence needed; its partial score is shown as “not comparable”.

**Confidence**: high = completeness ≥ 85 % and no unconfirmed assumptions; medium = completeness ≥ 65 %; low otherwise.

Where the charger plan is not entered for a candidate, the shared Site Feasibility assumptions are used for the area and grid
calculations, and the explanation says so.

## 4. Financial outlook (reuses the existing model)
The financial factor and the Site Feasibility comparison call `core/finance.py` (`evaluate`, `compare`), the same model as the
Site Feasibility page: initial investment, year-1 operating contribution, NPV, IRR, simple payback, break-even sessions/day and
implied utilisation. All candidates use **identical shared assumptions**; only their location and the values you entered for them
(charger plan and sessions/day) differ. The location itself changes no financial number.

## 5. Sensitivity
* **Weights**: each weight ×0.5 and ×1.5 (renormalised); the panel reports whether the top candidate changes.
* **Financial**: demand −25 % / +25 %, capex +20 %, charging price −10 %, electricity cost +15 %; payback per candidate and
  the resulting ranking.

## 6. Field checks shown with every result
Site visit and lease; BESCOM sanctioned load and connection cost/time; whether nearby listed chargers exist and work; observed
traffic and parking turnover; access, safety and permissions.

## 7. Limitations
* Public listings are incomplete and are not live status; the coverage and competition factors inherit that.
* OpenStreetMap coverage varies; absence in OSM does not prove absence on the ground. Amenity counts indicate activity, not EV demand.
* Weights and thresholds are judgement-based ASSUMPTIONS, not calibrated against real site performance.
* The financial model uses illustrative default prices and costs (see ASSUMPTIONS.md) unless you enter your own.

API: `GET /api/planning/method`, `GET /api/planning/preview`, `GET|POST /api/planning/candidates`,
`GET|PUT|DELETE /api/planning/candidates/{id}`, `POST /api/planning/candidates/{id}/context`,
`GET /api/planning/candidates/{id}/prefill`, `POST /api/planning/candidates/{id}/link`, `POST /api/planning/evaluate`,
`POST /api/planning/compare-finance`, `GET /api/public-stations/localities`.
