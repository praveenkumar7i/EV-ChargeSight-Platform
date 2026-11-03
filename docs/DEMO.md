# Demonstration script

## 5-minute version (populated demo, no manual setup)

Start the server (`python run.py`) on a new database, or press **Reset / restore demo** first.

1. **Operations console (1 min).** SIMULATED tag and scenario bar. KPIs: uptime 85% (▼ trend), 18 charging, 5 faulted, 1 offline, 6 open, 4 critical, 1 overdue, energy, sessions, power, MTTA/MTTR (hover for definitions). Uptime chart against the 97% target (switch 30 min / 2 h / 6 h); status donut.
2. **Alerts and selected charger (1 min).** TKT-00008 is rank 1 with “Assign the nearest available technician now”. TKT-00001 is OVERDUE. Click SIM-ECY-03 in the table: 55 °C, HighTemperature, power and temperature sparklines.
3. **Ticket workflow (1 min).** Open TKT-00008 → Acknowledge & assign → confirm (Technician D). Note: assignment is recorded, nobody is physically dispatched. Show the factor table and history.
4. **Scenario (1 min).** Demo mode → *Critical fault burst* → confirm. Five new critical tickets; alerts reorder; uptime line drops. Pause, Step 15 min. Then *Recovery demonstration* and watch tickets resolve and uptime climb.
5. **Analytics and policy (1 min).** Analytics page: faults by type, power, energy and sessions, MTTA/MTTR, ticket age/backlog, site uptime vs target, FIFO vs priority for the busy network with real CIs and the queue-contention note, three investment scenarios. Repair policy lab: the baseline run shows *no difference* and explains why (no queues).

Finish with **Reset / restore demo** (type RESET) to return to the baseline.

## 10-minute extended version

Add: Simulator & event timeline (filters, search, “Faults & tickets only”, pagination, inject a fault with a severity override, dedup on a second injection); Priority engine (preview alternative weights); Map (site detail, click-through to chargers; fallback note without a Google key); Site feasibility (edit sessions/day, loss-making flag, save and compare sites); Validation (evidence gating); Reports (CSV with provenance header, printable HTML).
