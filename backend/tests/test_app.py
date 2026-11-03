import csv
import io
from pathlib import Path

import pytest

from app.core import finance

SAMPLES = Path(__file__).resolve().parents[2] / "sample_data"


def read_csv(text):
    rows = [ln for ln in text.splitlines() if not ln.startswith("#")]
    return list(csv.DictReader(io.StringIO("\n".join(rows))))


def test_app_starts_and_seeds(client):
    h = client.get("/api/health").json()
    assert h["status"] == "ok" and h["chargers"] == 40
    sites = client.get("/api/sites?source=simulation").json()
    assert len(sites) == 6 and all(s["is_demo_location"] for s in sites)
    assert client.get("/api/sim/state").json()["label"] == "SIMULATED"


def test_frontend_is_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "ChargeSight" in r.text
    for p in ("/static/js/app.js", "/static/css/app.css", "/static/js/views/map.js"):
        assert client.get(p).status_code == 200


@pytest.mark.parametrize("path", ["/api/fleet/summary", "/api/chargers", "/api/tickets", "/api/events", "/api/catalog",
                                  "/api/technicians", "/api/priority/config", "/api/priority/ranking",
                                  "/api/experiments/defaults", "/api/investment/defaults", "/api/integrations/health",
                                  "/api/validation/tracks", "/api/reports", "/api/public-stations", "/api/sources"])
def test_main_endpoints_respond(client, path):
    assert client.get(path).status_code == 200


def test_simulated_fault_creates_prioritised_ticket(client):
    client.post("/api/sim/step", json={"n": 100})
    r = client.post("/api/sim/inject", json={"charger_id": "SIM-WFD-02", "category": "high_temperature"}).json()
    assert r["ticket"] and not r["deduplicated"]
    t = client.get(f"/api/tickets/{r['ticket']}").json()
    p = t["priority"]
    assert p and p["rank"] == 1
    assert sum(f["contribution"] for f in p["factors"]) == pytest.approx(p["score"], abs=1e-3)
    ch = [c for c in client.get("/api/chargers").json() if c["id"] == "SIM-WFD-02"][0]
    assert ch["status"] == "faulted" and ch["top_priority_score"] == pytest.approx(p["score"], abs=1e-4)
    detail = client.get("/api/chargers/SIM-WFD-02").json()
    assert detail["current_ticket"]["code"] == r["ticket"]


def test_dashboard_totals_come_from_data(client):
    client.post("/api/sim/inject", json={"charger_id": "SIM-KRM-01", "category": "ground_fault"})
    s = client.get("/api/fleet/summary").json()
    chargers = client.get("/api/chargers").json()
    assert s["total_chargers"] == len(chargers)
    for st, n in s["status_counts"].items():
        assert n == sum(1 for c in chargers if c["status"] == st)
    assert s["open_tickets"] == 1


def test_refresh_does_not_duplicate(client):
    client.post("/api/sim/inject", json={"charger_id": "SIM-KRM-01", "category": "ground_fault"})
    a = client.get("/api/tickets").json()
    b = client.get("/api/tickets").json()
    assert len(a) == len(b) == 1


def test_sim_controls(client):
    assert client.post("/api/sim/start").json()["running"] is True
    assert client.post("/api/sim/pause").json()["running"] is False
    st = client.post("/api/sim/step", json={"n": 10}).json()
    assert st["step"] == 10
    st = client.patch("/api/sim/config", json={"speed": 8, "params": {"fault_rate_per_charger_day": 2}}).json()
    assert st["speed"] == 8 and st["params"]["fault_rate_per_charger_day"] == 2
    assert client.patch("/api/sim/config", json={"params": {"bogus": 1}}).status_code == 422
    st = client.post("/api/sim/reset", json={"seed": 7}).json()
    assert st["step"] == 0 and st["seed"] == 7 and st["events_total"] == 0


def test_policy_comparison_endpoint(client):
    r = client.post("/api/experiments/run", json={"mode": "quick", "config": {"technicians": 1, "fault_rate_per_charger_day": 0.3}})
    assert r.status_code == 200
    res = r.json()["results"]
    assert len(res["per_seed"]) == 5 and "weighted_downtime_h" in res["summary"]
    csv_rows = read_csv(client.get(f"/api/reports/experiment.csv?run_id={r.json()['id']}").text)
    summ = {row["metric"]: row for row in csv_rows if row["row"] == "SUMMARY"}
    assert float(summ["downtime_h"]["fifo"]) == pytest.approx(res["summary"]["downtime_h"]["mean_a"], rel=1e-3)
    assert client.post("/api/experiments/run", json={"mode": "custom", "config": {"technicians": 0}}).status_code == 422


def test_investment_endpoint_consistent_with_model(client):
    inputs = {"sessions_per_day": 30, "price_per_kwh": 19}
    r = client.post("/api/investment/evaluate", json={"inputs": inputs}).json()
    direct = finance.evaluate(finance.InvestmentInputs.from_dict(inputs))
    assert r["npv"] == pytest.approx(direct["npv"]) and r["payback_months"] == direct["payback_months"]
    assert client.post("/api/investment/evaluate", json={"inputs": {"n_chargers": -2}}).status_code == 422
    a = client.post("/api/investment/sites", json={"name": "Site A", "inputs": {"lat": 12.9, "lng": 77.6}}).json()
    b = client.post("/api/investment/sites", json={"name": "Site B", "inputs": {"sessions_per_day": 5}}).json()
    cmp = client.post("/api/investment/compare", json={"site_ids": [a["id"], b["id"]]}).json()
    assert [x["site_name"] for x in cmp["rows"]] == ["Site A", "Site B"]
    inv_csv = read_csv(client.get("/api/reports/investment.csv").text)
    assert {row["site_name"] for row in inv_csv} == {"Site A", "Site B"}


@pytest.mark.parametrize("name", ["fleet", "tickets", "priority", "events", "experiment", "sensitivity",
                                  "downtime_cost", "investment", "integrations", "validation"])
def test_exports_open_and_carry_metadata(client, name):
    client.post("/api/sim/inject", json={"charger_id": "SIM-KRM-01", "category": "ground_fault"})
    r = client.get(f"/api/reports/{name}.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "# data_source:" in r.text and "# generated_at_utc:" in r.text
    read_csv(r.text)


def test_fleet_export_matches_api(client):
    client.post("/api/sim/inject", json={"charger_id": "SIM-HSR-02", "category": "overcurrent"})
    rows = read_csv(client.get("/api/reports/fleet.csv").text)
    api = {c["id"]: c for c in client.get("/api/chargers").json()}
    assert len(rows) == 40
    for row in rows:
        assert row["status"] == api[row["charger_id"]]["status"]
        assert row["label"] == "SIMULATED"


def test_full_html_report(client):
    r = client.get("/api/reports/full.html")
    assert r.status_code == 200 and "ChargeSight report" in r.text and "SIMULATED" in r.text


def test_validation_requires_evidence(client):
    put = lambda track, status, note="": client.put(f"/api/validation/tracks/{track}", json={"status": status, "note": note})
    assert put("operational", "completed", "done").status_code == 409
    assert put("operational", "awaiting_access").status_code == 200
    data = (SAMPLES / "SYNTHETIC_ticket_history_sample.csv").read_bytes()
    syn = client.post("/api/validation/datasets", files={"file": ("h.csv", data, "text/csv")},
                      data={"label": "synthetic", "is_synthetic_sample": "true"}).json()
    assert syn["is_synthetic_sample"] and syn["records"] == 60
    client.post(f"/api/validation/datasets/{syn['id']}/replay", json={"technicians": 2})
    assert put("operational", "data_received").status_code == 409          # synthetic never counts
    r = client.post("/api/validation/datasets", files={"file": ("h.csv", data, "text/csv")}, data={"label": "no permission"})
    assert r.status_code == 422                                              # real data needs a permission note
    real = client.post("/api/validation/datasets", files={"file": ("h.csv", data, "text/csv")},
                       data={"label": "operator X history", "permission_note": "Email consent 2026-10-20"}).json()
    assert put("operational", "data_received").status_code == 200
    assert put("operational", "completed", "replayed").status_code == 409   # needs a replay first
    rep = client.post(f"/api/validation/datasets/{real['id']}/replay", json={"technicians": 2}).json()
    assert rep["label"] == "RETROSPECTIVE OFFLINE SIMULATION" and "not a real-world intervention" in rep["disclaimer"]
    assert put("operational", "completed", "").status_code == 409            # note required
    assert put("operational", "completed", "Replay on operator X data").status_code == 200
    cmpr = client.post(f"/api/validation/datasets/{real['id']}/compare").json()
    assert any(r["metric"] == "Mean repair duration" for r in cmpr["rows"])


def test_interview_consent_and_anonymised_export(client):
    r = client.post("/api/validation/interviews", json={"interview_date": "2026-10-20", "role": "maintenance lead",
                                                        "participant_ref": "Ravi at OperatorX", "consent_identifiable": False})
    assert r.status_code == 200 and r.json()["participant_ref"] == ""
    client.post("/api/validation/interviews", json={"interview_date": "2026-10-21", "role": "site owner",
                                                    "participant_ref": "Anita", "consent_identifiable": True,
                                                    "confidentiality_notes": "do not quote"})
    text = client.get("/api/reports/validation.csv").text
    assert "Anita" not in text and "Ravi" not in text and "do not quote" not in text and "P1" in text
    assert client.post("/api/validation/interviews", json={"interview_date": "20-10-2026", "role": "x"}).status_code == 422


def test_financial_validation_states(client):
    s = client.post("/api/investment/sites", json={"name": "Pilot", "inputs": {}}).json()
    assert client.get(f"/api/validation/financial/{s['id']}").json()["status"] == "NOT YET VALIDATED"
    client.post("/api/validation/financial-references", json={"investment_site_id": s["id"], "metric": "revenue_year1",
                                                              "reference_value": 3_000_000, "period": "FY25", "data_source": "operator"})
    r = client.get(f"/api/validation/financial/{s['id']}").json()
    row = r["comparisons"][0]
    assert r["status"] != "NOT YET VALIDATED"
    assert row["abs_error"] == pytest.approx(row["estimate"] - 3_000_000)
    assert row["pct_error"] == pytest.approx(row["abs_error"] / 3_000_000 * 100)
    client.post("/api/validation/financial-references", json={"investment_site_id": s["id"], "metric": "sessions_per_day",
                                                              "reference_value": 0, "period": "FY25", "data_source": "operator"})
    rows = client.get(f"/api/validation/financial/{s['id']}").json()["comparisons"]
    assert [x for x in rows if x["metric"] == "sessions_per_day"][0]["pct_error"] is None


def test_priority_weights_api(client):
    client.post("/api/sim/inject", json={"charger_id": "SIM-KRM-01", "category": "payment_reader"})
    client.post("/api/sim/inject", json={"charger_id": "SIM-YLK-02", "category": "ground_fault"})
    current = client.get("/api/priority/ranking").json()
    assert current[0]["ticket_ref"] == "TKT-00001"        # busy-site reader fault outranks quiet-site ground fault
    prev = client.post("/api/priority/preview", json={"weights": {"severity": 1, "utilisation": 0, "capacity": 0, "age": 0}}).json()
    assert prev[0]["ticket_ref"] == "TKT-00002"           # severity-only weights put the critical fault first
    assert prev[0]["current_rank"] == 2
    assert client.get("/api/priority/config").json()["weights"]["severity"] == 0.4      # preview did not save
    assert client.put("/api/priority/config", json={"weights": {"severity": -1, "utilisation": 1, "capacity": 0, "age": 0}}).status_code == 422
    ok = client.put("/api/priority/config", json={"weights": {"severity": 2, "utilisation": 1, "capacity": 1, "age": 0}})
    assert ok.status_code == 200 and ok.json()["normalised_weights"]["severity"] == pytest.approx(0.5)
