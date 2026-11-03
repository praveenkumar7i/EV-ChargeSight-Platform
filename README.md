# ChargeSight

**Explainable EV charger operations, repair prioritisation and charging-site investment intelligence, as a multi-organisation SaaS prototype.**
An academic prototype for the PME course (23IS7PCPME, BMSCE). It runs entirely on software-generated data, with tested paths for authorised real data.

v9 adds a public website, secure accounts, organisations with roles and strict data isolation, a central plan
catalogue with a simulated (no-payment) checkout, onboarding and per-organisation operator API / OCPP
connections. Plans (proposed prototype prices): Demo ₹0, Starter ₹499, Growth ₹1,499, Professional ₹4,999 per month,
Enterprise by quotation. Read **[docs/SAAS.md](docs/SAAS.md)** for the design, security controls, migration and limitations.

> Fleet data is **SIMULATED** (40 synthetic chargers at 6 Bengaluru demonstration areas). Investment figures are **ESTIMATED** from **ASSUMPTION** inputs and **NOT YET VALIDATED**. No real operator is connected.

---

## 1. Quick start

**Requirements:** Python 3.10 or newer, and an internet connection for the first install. Node.js and a database server are not needed.

### Windows
1. Install Python from <https://www.python.org/downloads/> and tick **"Add python.exe to PATH"**.
2. Double-click **`run.bat`**, or run in a terminal:
   ```bat
   cd %USERPROFILE%\Desktop\EV-ChargeSight-Platform
   run.bat
   ```
3. Open <http://127.0.0.1:8000> (public site) and sign up, or open <http://127.0.0.1:8000/app> (console, sign-in required).

**First time on an existing database (v9):** create your developer account, which owns the system "ChargeSight Demo
Workspace" holding all earlier prototype data and is a platform administrator (the password is typed interactively):
```powershell
python scripts\manage.py create-admin --email you@example.com --name "Your Name"
```

The first run creates `.venv`, installs dependencies, copies `.env.example` to `.env` and creates the SQLite database. On a new database it also builds the **synthetic demo dataset** (takes about 10 seconds; see §2a), so every page has content without injecting faults by hand.

### Windows PowerShell in VS Code
```powershell
cd "$env:USERPROFILE\Desktop\EV-ChargeSight-Platform"
python -m venv .venv                       # first time only
.\.venv\Scripts\Activate.ps1               # if blocked: Set-ExecutionPolicy -Scope Process Bypass
python -m pip install -r requirements.txt  # first time, and after pulling changes
python scripts\manage.py create-admin --email you@example.com --name "Your Name"   # first time on v9
python run.py                              # then open http://127.0.0.1:8000 (site) or /app (console)
python -m pytest -q                        # run the tests (second terminal, venv activated)
```

### macOS / Linux
```bash
cd ~/Desktop/EV-ChargeSight-Platform
chmod +x run.sh test.sh
./run.sh
```

### Manual (any OS)
```bash
python -m venv .venv
# Windows: .venv\Scripts\activate     macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python run.py
```

### Run the tests
```bat
test.bat              (Windows)
./test.sh             (macOS/Linux)
python -m pytest      (inside the activated venv)
```

### Separate terminals (dashboard + OCPP)
```powershell
# Terminal 1: dashboard
cd "$env:USERPROFILE\Desktop\EV-ChargeSight-Platform"; .\.venv\Scripts\Activate.ps1; python run.py
# Terminal 2: OCPP central system
cd "$env:USERPROFILE\Desktop\EV-ChargeSight-Platform"; .\.venv\Scripts\Activate.ps1; cd backend; python -m app.integrations.ocpp_central
# Terminal 3: OCPP test charge point (SIMULATED OCPP)
cd "$env:USERPROFILE\Desktop\EV-ChargeSight-Platform"; .\.venv\Scripts\Activate.ps1; python scripts\ocpp_simulator.py --id SIMOCPP-001
```
Stop any of them with **Ctrl+C** (data is in `data/chargesight.db` and survives restarts). Details: [docs/INTEGRATION.md](docs/INTEGRATION.md).

### Sign in, sign up and the free demo (v10.1)
* The public header always offers **Sign in**, **Sign up** and **Open workspace**. Signed in, it shows *Signed in as …*, **Sign out** and **Open workspace**.
* **Open workspace** without a session goes to Sign in and returns to the requested view afterwards. `/app` itself redirects to Sign in on the server when there is no valid session; only `/app…` destinations are accepted after sign-in (no open redirects).
* **Start free demo** (`/demo`) never signs anyone in silently: visitors sign up or sign in first, then their private, SIMULATED Presentation Demo opens. There is no shared or anonymous demo account.

### My Fleet and the Presentation Demo (v9.2)
* **My Fleet** is your own workspace: only your own records, nothing synthetic is added automatically. With no data it shows an empty state with *Open Presentation Demo*, *Import an authorised CSV* and *Configure operator API or OCPP*.
* **Presentation Demo**: sidebar → *Workspace* → **Open Presentation Demo**. A separate SIMULATED workspace (6 sites, 40 chargers, technicians, 6 h of history, 8 tickets), populated immediately; the first build takes about 15–60 s. *Back to My Fleet* returns to your workspace.

### Reset / restore the demo
* **Presentation Demo → Operations console → Reset / restore demo** (type `RESET` to confirm) rebuilds the demo dataset. It deletes only SIMULATED events, tickets and telemetry plus `[DEMO]` sample runs/sites; imported and live records and your own runs/sites are kept. API: `POST /api/demo/restore` with `{"confirm": "RESET"}`.
* **Simulator → Reset simulation** clears simulated activity to an empty run (no demo history).
* Full wipe: stop the server, delete `data/chargesight.db*`, start again.
* Existing databases from v1 are upgraded in place (new columns/tables are added; nothing is dropped). A v1 database that already has simulated tickets is **not** auto-seeded; use Restore demo if you want the demo dataset. Set `DEMO_AUTOSEED=false` to disable the first-run seed.

---

## 2. What it does

| Module | Where in the app | Status |
|---|---|---|
| A. Operations console: uptime % and trend, status counts, tickets (open/critical/overdue), energy, sessions, power, MTTA/MTTR, critical alerts with recommended action, selected-charger panel (power, temperature, heartbeat, fault code, session, 6 h readings), filters, site health, timeline, quick scenarios | Fleet dashboard | Implemented |
| A2. Analytics: 9 trend charts from stored history (status, faults, power, energy, sessions, MTTA/MTTR, ticket age, backlog, site uptime, FIFO vs priority, investment scenarios) | Analytics | Implemented |
| B. Event and fault simulator (seeded, start/pause/reset/step, fault injection, event log) | Simulator & events | Implemented (internal event bus, OCPP 1.6-style messages) |
| C. Maintenance tickets: **Raise a Ticket** (Tickets, Dashboard, charger detail), one pipeline for USER_REPORTED / SIMULATOR / OCPP / OPERATOR_API / IMPORTED_DATA, lifecycle Open → Assigned → En Route → On Site (→ waiting for parts/access) → Resolved → Closed (verified), SLA escalation, full audit trail, MTTA/MTTR, export | Maintenance tickets | Implemented ([docs/TICKETING.md](docs/TICKETING.md)) |
| C2. Technician registry and automatic/manual assignment (skills, AC/DC, safety certification, capacity, shifts; no-eligible fallback, overrides audited) | Technicians, ticket drawer | Implemented; DEMO technicians are fictional |
| D. Explainable priority engine `P = 0.4S + 0.3U + 0.2R + 0.1A` | Priority engine, every ticket | Implemented |
| E. FIFO vs priority policy simulation with paired 95% CIs and sensitivity analysis | Repair policy lab | Implemented |
| F. Google Maps visualisation with fallback | Map | Implemented; Google path needs your key (see §6) |
| G. Real charger integration (REST adapter, CSV import, OCPP 1.6J central system) | Data integrations | Implemented and tested with mocks and a local OCPP handshake; no real operator connected |
| H. Site investment feasibility (SiteSure-inspired) with scenarios and site comparison | Site feasibility | Implemented; Monte Carlo not implemented |
| I. Validation workflow (interviews, historical data, retrospective replay, financial checks, evidence-gated status) | Validation | Implemented; no real evidence recorded |
| J. Reports and CSV/HTML exports with provenance metadata | Reports & exports | Implemented |

### 2a. Synthetic demo dataset (SIMULATED)

Built deterministically (seed 42) from the live simulator itself, covering 02:00–08:00 IST on the demo day:
40 chargers at 6 sites; real engine sessions, heartbeats and meter values; fleet snapshots every simulated minute and power/temperature readings every 5 minutes; and 8 scripted ticket stories:

| Ticket | Charger | Story |
|---|---|---|
| TKT-00001 | SIM-YLK-02 ground fault | open, **overdue** critical |
| TKT-00002 | SIM-HSR-03 overcurrent | assigned → en route → on site → resolved with notes |
| TKT-00003 | SIM-KRM-06 reader failure | closed by a successful remote reset |
| TKT-00004 | SIM-HBL-03 comm loss | charger **offline**, remote reset failed, open |
| TKT-00005 | SIM-ECY-03 high temperature | technician on site (connector stays hot) |
| TKT-00006 | SIM-WFD-03 overcurrent | technician en route |
| TKT-00007 | SIM-YLK-04 reader failure | reset failed, assigned |
| TKT-00008 | SIM-KRM-01 ground fault | fresh critical, **rank 1** |

Plus two `[DEMO]` policy experiments (baseline and busy network) and two `[DEMO]` investment sites. Seeding happens once (setting `demo_seeded`), never duplicates, and never touches non-simulated data.

### 2b. Quick scenarios (Operations console → Demo mode)

Normal operations · Critical fault burst · Site connectivity outage · High demand · Maintenance backlog · Recovery demonstration · Repair policy comparison. Each changes real simulator state (parameters, technician availability, injected faults, or a stored experiment). The console shows the scenario name, source, start time and Running/Paused state with Start/Resume, Pause, Step and Reset/restore controls.

### 2c. New endpoints (v2)

`GET /api/analytics/timeseries|faults|maintenance|sites`, `GET /api/chargers/{id}/readings`, `GET /api/timeline` (events + ticket lifecycle; filters `site, charger, type, severity, q, operational`, `limit/offset`), `GET /api/demo/status`, `POST /api/demo/restore`, `GET /api/demo/scenarios`, `POST /api/demo/scenarios/{key}`. `POST /api/sim/inject` accepts an optional `severity` and `note`; `GET /api/tickets` accepts `q`, `technician`, `sort=priority|severity|age|status|newest`. Experiments accept `fleet_multiplier` (1–5) and report **queue contention** to explain when FIFO and priority give the same result.

Full detail: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) · metric definitions: [docs/METRICS.md](docs/METRICS.md) · assumptions: [docs/ASSUMPTIONS.md](docs/ASSUMPTIONS.md) · demo script: [docs/DEMO.md](docs/DEMO.md) · audit: [docs/AUDIT.md](docs/AUDIT.md).

---

## 3. Architecture at a glance

```
Browser (vanilla JS SPA, no build step)  ── /api/* JSON ──>  FastAPI (backend/app)
                                                            ├── core/        pure, unit-tested logic
                                                            │   priority.py   explainable score + ranking
                                                            │   policy_sim.py discrete-event FIFO vs priority
                                                            │   finance.py    investment model
                                                            ├── simulator/   seeded live fleet generator
                                                            ├── services/    events, tickets, validation, reports
                                                            ├── integrations/ REST adapter, CSV, OCPP 1.6J, Open Charge Map
                                                            └── SQLite (SQLAlchemy 2.0)  ←  provenance on every row
```

Every site, charger, event and ticket carries `source` = `simulation`, `operator_api`, `ocpp` or `operator_import`. The UI shows these as SIMULATED, LIVE or IMPORTED, and never merges them in one view. The data selector at the top switches between sources.

---

## 4. Environment variables

Copy `.env.example` to `.env` (the start scripts do this for you). Everything is optional.

| Variable | Purpose |
|---|---|
| `HOST`, `PORT` | Server address (default `127.0.0.1:8000`) |
| `DATABASE_URL` | Default `sqlite:///data/chargesight.db` |
| `SIM_SEED`, `SIM_STEP_SECONDS`, `SIM_TICK_MS`, `SIM_AUTOSTART` | Live simulator |
| `DEMO_AUTOSEED` | Build the synthetic demo dataset on a new database (default `true`) |
| `GOOGLE_MAPS_BROWSER_KEY`, `GOOGLE_MAPS_MAP_ID` | Google Maps (browser key; restrict it) |
| `GOOGLE_PLACES_API_KEY` | Google Places API (New), server-side; live lookup only |
| `OSM_ENABLED`, `OSM_OVERPASS_URL` | OpenStreetMap public listings via Overpass (no key) |
| `PUBLIC_SNAPSHOT_AUTOLOAD` | Load the bundled Open Charge Map Bengaluru snapshot at start |
| `OCM_API_KEY` | Open Charge Map public listings (server-side) |
| `OPERATOR_API_BASE_URL`, `OPERATOR_API_KEY`, `OPERATOR_API_TIMEOUT_S`, `OPERATOR_API_MAX_RETRIES` | Authorised operator REST API (server-side) |
| `OCPP_HOST`, `OCPP_PORT` | Optional OCPP 1.6J central system |
| `ADMIN_TOKEN` | Enables administrative override of the ticket workflow |

Secrets stay on the server. They are redacted from logs, API responses and exports, and a test checks this.

---

## 5. Testing

`python -m pytest` runs **397 tests** (about 20 minutes; 25 of them drive a real headless browser and are skipped if Playwright is not installed) across:

- `test_v9_auth.py`: sign-up, duplicate e-mail, validation messages, Argon2id hashes, generic invalid-credential errors, sign-out invalidation, expired and idle sessions, every protected route needs a session, every route has a guard rule, CSRF and cross-origin refusal, sign-in rate limit, password reset and e-mail verification via the development outbox, no secrets in logs, password change revokes other sessions
- `test_v9_tenancy.py`: one organisation cannot read or modify another's tickets, chargers, technicians, exports, checkouts, receipts or connections (IDs changed in URLs and bodies), ORM-level filter, new organisations never receive prototype data, viewer read-only, technician limited to assigned tickets, admin vs owner billing rights, platform-admin-only endpoints, removed members lose access
- `test_v9_billing.py`: five-plan catalogue (Demo ₹0, Starter ₹499, Growth ₹1,499, Professional ₹4,999, Enterprise quoted) with limits and features, 1/6/12-month totals for every paid plan, Decimal half-up rounding, INR grouping, browser cannot change prices or state, idempotent checkout completion (no duplicate receipts or events), acknowledgement and Idempotency-Key required, DEMO receipts, validated transitions, renewal → past due → expired read-only with data kept and tickets still workable, cancel at period end, upgrade credit and downgrade at renewal, feature and limit enforcement (chargers and sites via registration, CSV and OCPP, users, integrations, Professional-only reports and audit log), no plan spoofing, audited suspension, Enterprise needs negotiated limits, stored enterprise enquiries
- `test_v9_pricing_migration.py`: v9.0 → v9.1 catalogue update is idempotent, leaves administrator-edited plans alone, never rewrites completed checkouts or DEMO receipts, keeps active subscriptions on their purchased limits until the next checkout, records the catalogue version; the legacy demo workspace keeps its capabilities
- `test_v9_onboarding_integrations.py`: demo onboarding without credentials, progress kept across sign-out, missing credentials → Not configured, mocked 401/403/500/404 never Connected, private/metadata destinations blocked, mocked success → Connected and sync into this organisation only, secrets encrypted/masked/not logged, paid plan alone never connects, reconfiguration needs a new test, .env credentials not shared, SSRF unit checks, OCPP test point vs real charge point over a real local WebSocket with per-organisation endpoints
- `test_v92_workspaces.py`: empty-dashboard root cause (no simulator run, step or 0-charger history without a fleet), start-up repair after a verified backup, restore refused in My Fleet, Presentation Demo populated immediately (40/6/8, paused yet charting), dashboard counts equal the records, idempotent open/restore keeping imported records, no leaks between users or into customer workspaces, misleading seed flag repaired
- `test_v92_checkout.py`: no paid plan without the payment step, unchanged 1/6/12-month totals with client prices ignored, every test payment method validated and masked, no card number / expiry / CVV / OTP / PIN stored, logged or returned, declined test payments change nothing, no network connection during checkout, TEST receipts, completed/expired/foreign checkouts refused, Enterprise and free plans outside checkout, historical checkouts kept
- `test_v101_auth_routes.py` / `test_browser_v101.py` (Playwright): Sign in / Sign up / Open workspace in the header (desktop and mobile), server-side console redirect, deep link kept across sign-in, safe redirect parameter, signed-in visitors see a notice instead of a loop, invalid credentials never create a session, sign-out and Back, duplicate sign-up refused, Start free demo requires an account and then opens the private demo
- `test_browser_v92.py` (Playwright): navy design on authentication pages only, public pages and console theme unchanged, auth validation / password toggle / focus ring, no grey console fields (e-mail included), 260 px sidebar without wrapping and mobile off-canvas navigation, My Fleet empty state → Presentation Demo → back, checkout payment step refusing a non-test card in the browser
- `test_browser_saas.py` (Playwright): landing and pricing (server quotes, calculator, comparison), mobile without horizontal scroll, sign-up validation, sign-in errors, password toggle, sign-out, full demo onboarding → checkout → DEMO receipt, tenant isolation in the UI, integration Not configured state, subscription expiry shows the read-only banner

- `test_priority.py`: weighted score, normalisation boundaries, invalid inputs, ageing and escalation, ties, contributions = total
- `test_simulation.py`: seed reproducibility, valid transitions, persistence, ordering, recovery, no duplicate tickets, DB constraint
- `test_tickets.py`: workflow, timestamps, MTTA/MTTR, unresolved tickets, admin override, busy technician
- `test_policy_sim.py`: identical traces, hand-computed metrics, reordering, censoring, reproducibility, no stale sensitivity results
- `test_finance.py`: revenue/costs, payback, break-even, loss-making, zero cash flow, unreachable payback, scenarios, invalid inputs
- `test_integrations.py`: missing or failed maps, operator retries/timeouts/invalid responses, dedupe, invalid CSV, secrets, Open Charge Map, and a **real OCPP 1.6J WebSocket handshake**
- `test_app.py`: start-up, seed, static UI, endpoints, fault → ticket → priority, policy run, investment, exports, validation gating, consent
- `test_live_integrations.py`: OCM refresh replacing the snapshot record by record, absent flags, 10-minute throttle and forced refresh, rejected key keeps records, key header and 429; Google Places never shown as verified without a successful request and never called without billing confirmation; operator API diagnose states, sync dedupe, no fabricated data, polling clamp; OCPP central + bundled simulator over a real local WebSocket (SIMULATED OCPP labelling, one ticket), retransmission dedupe; ticket codes unique across sources; source-mode states; sample download and import
- `test_public_stations.py`: snapshot values match the OCM source pages, idempotent reload, STATUS UNKNOWN and attribution on every listing, computed inventory and gap messages, invalid/missing fields, cross-provider dedup, Overpass/OCM/Google parsing and failures (mocked; no real API calls), Google results never stored, filters, refresh with a failing/unconfigured provider, separation from simulated/imported data, secret protection, CSV export
- `test_map_localities.py`: locality counts equal the stored records, true zero vs coverage unknown, freshness/stale flag, duplicates counted once, invalid coordinates rejected, no live status claimed
- `test_planning.py`: coordinate validation (bounds, swapped lat/lng, non-numbers), distance maths cross-checked, nearby ordering and ring counts, candidate CRUD kept separate from listings and proposed sites, Site Feasibility prefill with sources, link-on-save, OpenStreetMap context success/throttle and every failure mode (mocked)
- `test_planning_scoring.py`: weight normalisation and validation, insufficient-data state, score = weighted mean of available factors, evidence classes, unconfirmed grid as ASSUMPTION, financial factor equals the existing model, budget penalty, three-candidate ranking and sensitivity, comparison under identical assumptions
- `test_browser_map.py` (Playwright, headless Chromium, signed in): locality selection updates markers/list/summary, marker ↔ list selection incl. keyboard, operator filter, coordinate validation, station source record, plan → candidate → Site Feasibility → back to map, save needs confirmation and never overwrites, ranking and financial comparison in the UI, no unqualified LIVE label
- `test_ticketing.py`: user ticket submission and validation (unknown charger, empty description, bad category/impact), idempotent and duplicate reports, severity from the existing definitions (safety never downgraded), priority = existing engine with audited snapshot changes and the site-wide capacity rule, deterministic technician choice, skill / availability / capacity / safety constraints, no-eligible fallback and retry after availability changes, emergency override policy, manual assignment conflicts and override audit, reassignment and unassignment, DEMO technicians kept off real chargers, lifecycle incl. waiting/close/reopen, safety inspection before return to service, SLA deadline and single escalation, duplicate fault events and recurrence reopening, simulator controls, Normal Operations staying healthy under the old fault rate, scenario rates, cross-endpoint count consistency, imported tickets never auto-assigned, technician CRUD, tickets-table migration preserving rows
- `test_demo.py`: demo mixture and timestamp consistency, deterministic rebuild, idempotent auto-seed on restart, no reseed of existing simulations, restore confirmation and safety for imported/user data, history growth and pruning, chart endpoints, timeline filters/pagination, severity override and dedup, ticket search/sort/persistence, scenarios, recovery, queue-contention explanation, fleet size, finance scenario series, column migration, exports

---

## 6. Map: locality-first exploration and site planning (Bengaluru)

**Explore.** Seven locality cards at the top of the Map (All Bengaluru, Whitefield, Electronic City, HSR Layout, Koramangala,
Hebbal, Yelahanka; plus Other Bengaluru) show the stored listing count, operators, DC connectors and data freshness, all
computed from the database. Clicking a card centres the map on that locality (blue circle = its radius, an ASSUMPTION) and
updates the markers, the station list, the summary figures and the operator/connector/power charts together. A zero is
labelled **true zero** only after a successful full-area refresh of a provider; with only the bundled snapshot it is
**coverage unknown**. Selecting a marker selects its list row and vice versa (keyboard: Tab to a row, Enter). The details panel
shows the full stored source record, licence, attribution, retrieval time and a link to the source. Every public listing shows
**STATUS UNKNOWN**. Links such as `#map?area=hebbal` restore the view.

**Map controls.** Search (station, operator, locality, “lat, lng”, or an address via Google Geocoding), map type
(roadmap/satellite/hybrid/terrain), photorealistic **3D view**, tilt/rotate (vector Map ID), North up, Reset camera, Fit locality,
Fit visible, Street View, pointer and selected-coordinate readouts, and a legend for every layer. Without a Google key the same
data is shown on OpenStreetMap tiles (or a coordinate plot offline).

**Plan a new charging site.** Press *Plan a new charging site*, then click the map, type coordinates or search an address.
ChargeSight validates the point (Bengaluru study area), shows the nearest listed chargers with distances and counts within
0.5/1/2/5 km, and lets you save it as a **planning candidate** (stored separately from listings and proposed sites). For each
candidate you can record what you know (chargers, type, power, budget, expected sessions/day, site area, grid capacity and whether
it is confirmed, road access, amenities you counted) and fetch OpenStreetMap context (amenities within 500 m, nearest road class;
one bounded request on demand). **Continue to Site Feasibility** prefills only the name, coordinates and the values you entered,
each with its source; **Back to map** restores the locality and candidate. Saving a proposed site always asks first and never
overwrites a saved site without asking.

**Rank and compare.** *Rank ticked candidates* produces an explainable score (weights you can change), evidence completeness,
confidence, per-factor explanations with evidence classes (SOURCE DATA, USER-ENTERED, ASSUMPTION, ESTIMATED, MISSING), missing
evidence, field checks and a sensitivity analysis. Candidates without enough evidence show **Insufficient data to rank reliably**.
Site Feasibility compares candidates under identical financial assumptions with the existing model (investment, year-1 operating
contribution, NPV, IRR, payback, break-even demand, utilisation). Method and weights: [docs/PLANNING.md](docs/PLANNING.md).

* Bundled fallback: **41 Open Charge Map listings** (CC BY 4.0) copied on 23 Oct 2026; not a complete inventory.
* Refresh: Open Charge Map (`OCM_API_KEY`) and OpenStreetMap/Overpass (no key); explicit, throttled, deduplicated.
* Live lookup: Google Places API (New) (`GOOGLE_PLACES_API_KEY`), shown but never stored, only after you confirm the billed request.

Setup, sources, licences and costs: [docs/GOOGLE_MAPS.md](docs/GOOGLE_MAPS.md).

---

## 7. Real charger data

What works now:
- **CSV import** of authorised operator event exports (validated, deduplicated, provenance `operator_import`).
- **Operator REST adapter** with timeouts, retries and validation, tested against mocked responses. Connecting a real operator needs their base URL, an API key and (usually) a small mapping change in `integrations/operator_api.py`.
- **OCPP 1.6J central system** (`python -m app.integrations.ocpp_central`, from the `backend` folder) that accepts real charge points or OCPP simulators. A test verifies a genuine WebSocket BootNotification → StatusNotification(Faulted) → ticket.

What is missing: authorised access to a real operator's API or chargers. Nothing is marked "connected" until a real handshake or verified response happens. See [docs/INTEGRATION.md](docs/INTEGRATION.md).

---

## 8. Financial model

`backend/app/core/finance.py` documents every formula: initial investment, energy, gross revenue, electricity and variable costs, gross margin, fixed opex, operating contribution (pre-tax cash flow), monthly cumulative cash flow, simple payback, NPV, IRR, break-even sessions per day and implied utilisation. Loss-making, unreachable-payback and impossible-utilisation cases are flagged. The model has **not** been checked against real operator figures. The Validation page compares estimates with real reference values once an operator supplies them, and shows **NOT YET VALIDATED** until then.

---

## 9. Known limitations

- All operational data is synthetic, and the default rates are assumptions, not Indian field statistics. Connector temperature is an illustrative first-order model, not measured.
- Fleet history charts exist only for the simulator; imported/live sources show current state and the charts say so.
- The 97% uptime target and the SLA thresholds are demo assumptions.
- At the research report's baseline load (0.05 faults per charger-day, 2 technicians), queues rarely form, so FIFO and priority produce **identical** results. Differences appear only under heavier load (for example 1 technician and 0.3 faults per charger-day), and they involve trade-offs: lower weighted downtime and high-severity waits, but higher total downtime and longer low-severity waits. The app reports whatever the experiment produces.
- The live simulator is an internal event generator; it does not speak OCPP over a network. The separate OCPP central system does.
- Ticket actions are recorded as the signed-in user. The optional admin token is still required for safety-rule and workflow overrides.
- Notifications are in-app only (dashboard, ticket list, timeline); no e-mail or SMS is sent. No file attachments (no secure upload storage).
- DEMO technicians, their skills, bases, travel and repair times are ASSUMPTIONS; travel time is never estimated from demo area centroids.
- The Google-specific map controls (map types, 3D, tilt/rotate, Street View, address search) follow Google's current documentation but were not exercised against Google from the development environment; check them on your computer. Everything else on the Map is covered by browser tests on the OpenStreetMap/coordinate-plot fallback.
- The suitability score's weights and thresholds are judgement-based ASSUMPTIONS (docs/PLANNING.md); it ranks places to investigate, not construction-ready sites.
- Investment demand is user-supplied; nothing is inferred from location. Monte Carlo uncertainty analysis is not implemented (only conservative/base/optimistic scenarios).
- Accounts, sessions, CSRF protection, rate limits, roles and tenant isolation are implemented and tested, but there has been no independent security review: this is not production-grade security. Rate limits are per server process. E-mail is not configured (development outbox only). Checkout is simulated; no payments, invoices or tax. See docs/SAAS.md §9.
- SQLite is suitable for a prototype. Use PostgreSQL via `DATABASE_URL` and a shared rate-limit store for a real multi-user deployment.

---

## 10. Next actions that need external access

1. Obtain a Google Maps browser key and confirm the map loads.
2. Approach one charge-point operator or maintenance contractor for an interview and an authorised historical ticket export, then run *Validation → Compare with model* and *Retrospective replay*.
3. Obtain real financial reference values for one site to validate the investment model.
4. If an operator agrees, connect a test charger to the OCPP central system or provide API credentials.
