# Assumptions register

Every number below was chosen for the prototype. None is measured Indian field data. All are configurable.

| Area | Assumption | Where to change |
|---|---|---|
| Demo sites | 6 Bengaluru area centroids (Koramangala, Whitefield, HSR Layout, Electronic City, Hebbal, Yelahanka). Not real stations. | `core/catalog.py` |
| Site demand | 120, 110, 80, 70, 60, 30 expected sessions/day | `core/catalog.py` |
| Technician travel | 20–35 to 45–60 min by site | `core/catalog.py` |
| Fault mix | high temperature 20%, overcurrent 15%, ground fault 15%, communication loss 30%, payment/reader 20% (research report) | Simulator page, Repair policy lab |
| Severity | thermal, overcurrent, ground = critical; communication loss, reader = major | `core/catalog.py` |
| Remote reset success | communication loss 60%, reader 40%, others 0% | Repair policy lab |
| Repair time means | 90, 120, 180, 45, 60 min (lognormal, σ = 0.5) | Repair policy lab |
| Experiment baseline | 40 chargers, 30 days, 30 seeds, 2 technicians, 0.05 faults per charger-day, 10 min remote-reset delay (research report) | Repair policy lab |
| Live simulator rate | 0.6 faults per charger-day (demo-accelerated so faults appear within minutes) | Simulator page |
| Priority weights | 0.4 / 0.3 / 0.2 / 0.1; age cap and starvation cap 24 h | Priority engine page |
| SLA thresholds | critical 4 h, major 12 h, minor 48 h | `settings` table key `sla_hours` |
| Downtime cost | site utilisation fractions 7–25%, margin ₹8/kWh | Repair policy lab |
| Investment defaults | 2 × 60 kW DC, equipment ₹14 lakh each, installation ₹3 lakh, grid ₹8 lakh, site prep ₹2 lakh, price ₹18/kWh, electricity ₹7.5/kWh, 24 sessions/day × 22 kWh, rent ₹25k/month, other fixed ₹30k/month, maintenance ₹60k per charger per year, 8% growth, 8 years, 12% discount | Site feasibility page |
| Scenarios | conservative: sessions × 0.7, electricity × 1.1, growth × 0.5; optimistic: sessions × 1.25, electricity × 0.95, growth × 1.25 | `core/finance.py` |
