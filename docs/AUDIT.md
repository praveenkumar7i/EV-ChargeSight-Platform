# Repository audit (23 Oct 2026)

## What existed before this implementation
- **No code repository.** The user's Desktop held no ChargeSight project, so there was nothing to inspect, preserve or refactor.
- A single-file **HTML dashboard prototype** (a published artifact) that simulated a charger fleet in the browser. It had no backend, persistence, tests or real data paths. It informed the visual direction only.
- Two research reports, which the requirements name as `ChargeSight_Research.pdf` and `EV_Project_Alternatives.pdf`. Their content was used as the specification:
  - From the first report: the priority score, the FIFO-vs-priority experiment design (40 chargers, 6 sites, 30 days, 30 seeds, 2 technicians, paired 95% CIs), metric definitions, OCPP 1.6J message subset, scope exclusions, risk register and business model.
  - From the second report: the SiteSure concept, implemented as an additional investment module. The core ChargeSight scope was kept.

## What was built
Everything in this folder was written new: backend (FastAPI, SQLAlchemy, core modules, simulator, integrations, validation, reports), frontend SPA, 122 automated tests, synthetic sample data, start scripts and documentation.

## Decisions taken without asking
- **Stack:** Python/FastAPI + SQLite + a vanilla-JS frontend with no build step. One `pip install` on Windows, no Node toolchain, no database server.
- **UI:** an operations-console layout with a left navigation grouped by Operations, Analysis, Investment and Evidence, provenance tags on every view, a severity-coloured factor bar for scores, and drawers for detail so the list stays visible. It adapts to dark mode and phone width.
- **Charts:** small hand-written SVG helpers instead of a charting library, so the app works offline.
- **Simulator transport:** an internal event bus for the simulator (reliable and deterministic), plus a separate real OCPP 1.6J endpoint for authorised chargers, rather than routing the simulator through WebSockets.
- **Anti-starvation:** a hard 24-hour escalation rule in addition to the A factor, because A alone (maximum 0.1) cannot guarantee that low-priority faults are eventually served.
