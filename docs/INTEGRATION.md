# Connecting authorised real data

No operator system is connected in this repository. Every path below is implemented and tested without external access. A connector is shown as **Connected** only after a verified response (REST), an accepted BootNotification (OCPP), or a successful import.

## Internal schema (`integrations/schema.py`)

| Field | Required | Notes |
|---|---|---|
| `external_event_id` | yes | Used for deduplication per source |
| `external_charger_id` | yes | Stored as `<SOURCE>-<id>`; charger auto-registered on first sight |
| `timestamp` | yes | ISO 8601 **with timezone offset**; stored as UTC |
| `event_type` | yes | `boot, heartbeat, status_available, status_change, session_start, session_stop, meter_values, fault, offline, reconnect, fault_resolved` |
| `status` | no | Operator vocabulary mapped by `STATUS_MAP` (OCPP statuses included) |
| `fault_category` | for faults | Internal key or OCPP 1.6 error code (`GroundFailure`, `HighTemperature`, …) |
| `site_code`, `site_name`, `lat`, `lng`, `power_kw`, `connector_type`, `meter_kwh` | no | Used to register sites and chargers |

Invalid rows and invalid status transitions are rejected individually and reported. They do not abort the batch.

## 1. CSV import (works now)
*Data integrations → Import authorised operator events.* Give a dataset label that records who shared the data and for which period.
**Download sample CSV** serves `sample_data/SYNTHETIC_operator_events_sample.csv`; **Import the synthetic sample now** runs it through the same importer.
The file is synthetic (no openly licensed Indian operator event export was found) and contains deliberate errors (duplicate id, missing timezone, unknown status) to demonstrate validation: expect 42 accepted, 1 duplicate, 2 rejected. Imported records appear only under *Data → Imported data*, labelled IMPORTED.

## 2. Operator REST API (e.g. Kazam, once access is granted)
Expected contract (adapt `OperatorApiConnector._map_event` and the paths for a specific operator):
```
GET {OPERATOR_API_BASE_URL}/health              -> 200 {"status": "ok"}
GET {OPERATOR_API_BASE_URL}/events?since=<ISO>  -> 200 {"events": [ ExternalEvent, ... ]}
Authorization: Bearer {OPERATOR_API_KEY}         (sent from the server only)
```
* **Diagnose** (Data integrations) walks the path and stops at the first failing step: `missing_base_url → missing_key → unreachable → auth_failed → incompatible_response → zero_records → ok`. It is read-only.
* **Sync events** ingests with deduplication (`source + external_event_id`), so repeated syncs never duplicate events or tickets.
* Polling: `OPERATOR_API_POLL_SECONDS` (0 = manual only, otherwise at least 60 s). Data older than `OPERATOR_API_STALE_AFTER_S` (default 900 s) is shown as STALE.
* With no credentials the dashboard shows NOT CONFIGURED; with credentials but no events, NO LIVE DATA. Nothing simulated is ever substituted.

**Needed from the operator:** written permission and data-use terms; base URL and API documentation (pagination, rate limits); a read-only revocable key or OAuth client; charger/connector ids and site metadata; an events or status endpoint with timezone-aware timestamps and their status vocabulary; their fault/error codes (and mapping to OCPP 1.6 codes); a sandbox/test account and the allowed polling interval.

## 3. OCPP 1.6J central system + local test simulator
Two extra terminals (PowerShell, venv active):
```powershell
# Terminal 2: central system (listens on ws://OCPP_HOST:OCPP_PORT/<charge_point_id>, default ws://127.0.0.1:9000/<id>)
cd backend
python -m app.integrations.ocpp_central

# Terminal 3: test charge point (SIMULATED OCPP traffic)
python scripts\ocpp_simulator.py --id SIMOCPP-001
python scripts\ocpp_simulator.py --id SIMOCPP-002 --fault HighTemperature --recover   # another run
python scripts\ocpp_simulator.py --id SIMOCPP-003 --stay 30                          # stay connected, heartbeat every 30 s
```
The simulator (`scripts/ocpp_simulator.py`, uses the pinned MIT-licensed `ocpp` and BSD `websockets` packages; nothing downloaded) connects with subprotocol `ocpp1.6` and sends BootNotification → Heartbeat → StatusNotification(Available) → Authorize → StartTransaction → StatusNotification(Charging) → MeterValues (energy + power) → StopTransaction → StatusNotification(Available) → StatusNotification(Faulted, GroundFailure).

* Its vendor is `ChargeSight-Sim`, so the charger is stored with `source=ocpp`, `test_traffic=true` and is labelled **SIMULATED OCPP** everywhere (chargers, tickets, dashboard badge "RECEIVING TEST DATA"). Any other charge point that completes a BootNotification is shown as real OCPP (LIVE).
* Dedupe: each stored event is keyed by charge point id + event type + the OCPP message unique id, so a retransmitted CALL is stored once.
* Faults go through the normal ticket pipeline (one open ticket per charger and fault type).
* Verify: *Data → OCPP (live)* on the Operations console, *Maintenance tickets* (OCPP ticket), *Simulator & event timeline* with source OCPP, and *Data integrations → OCPP* card.

Production use would additionally need OCPP security profiles (TLS, basic auth), which are not implemented.

## 4. Historical logs for validation
*Validation → Import historical ticket data.* Required: `record_id, charger_id, fault_category, detected_at`. Optional: `site_code, site_daily_sessions, charger_kw, acknowledged_at, resolved_at, repair_minutes, energy_kwh`. A permission note is mandatory unless the file is flagged as a synthetic sample. Sample: `/api/integrations/samples/ticket-history.csv` (synthetic).

## 5. Open Charge Map
`OCM_API_KEY` in the project-root `.env` is read by the backend only and sent as the `X-API-Key` header (never in the URL, logs, API responses or browser code). One request to `GET https://api.openchargemap.io/v3/poi/` with `boundingbox=(12.75,77.45),(13.2,77.8)`, `countrycode=IN`, `maxresults=1000`, `compact=false`, `verbose=false`; timeout 20 s (10 s to connect), one retry on 5xx or network errors, no retry on 401/403/429.

Run it from *Data integrations → Public listings → Fetch Bengaluru listings* (Open Charge Map only) or *Map → Refresh listings* (all configured providers). The button shows *Fetching…* and is disabled while the request runs. Each provider can have one refresh in flight: a second refresh of the **same** provider gets HTTP 409 naming the provider and how long it has been running, while other providers are not blocked. `GET /api/public-stations/refresh/status` shows what is running. Refreshes within 10 minutes are served from stored records unless you confirm a forced refresh (OCM asks clients not to repeat requests).

After a refresh the panel shows: received, added, updated, merged with other providers, skipped (unparseable POIs), rejected (failed validation) and stored-but-not-in-this-refresh. Fetched records replace the bundled snapshot record by record (origin **Fetched from the provider API**); records the API did not return stay visible with **NOT IN LATEST REFRESH**. Nothing is ever deleted.

Errors are reported per provider with a safe message and stored records stay unchanged: missing key, key rejected (401/403), rate limit (429, honouring `Retry-After`), timeout, network/DNS/proxy failure, server error after retry, non-JSON body (often a proxy or captive portal), non-list JSON, empty body. An empty list is accepted and flags nothing.

**Fixed in v6 — repeated "A listings refresh is already running" (409).** The browser was still running the cached v3 Integrations page, whose button sends a request without a body; the server treats that as OpenStreetMap + Open Charge Map. OpenStreetMap is on by default (`OSM_ENABLED=true`) and was unreachable from the network (ConnectTimeout), so each such request spent ~51 s in OSM retries while holding one global lock, and every refresh in that window, including the Open Charge Map button, got 409. Now: a per-provider guard (OSM can no longer block OCM), OSM retries a failed connection only once (~32 s), the page loads its scripts from content-hashed URLs (`/static/<build>/…`) so an updated server cannot keep running old JavaScript, an open tab with an outdated build shows a *Reload* banner, and the Map page no longer sends the click event as `force` (that caused HTTP 422). If OpenStreetMap is unreachable from your network, you can set `OSM_ENABLED=false` in `.env`.

**Fixed in v5 — "Internal server error" on refresh.** The old refresh held a SQLite read transaction open during the HTTP call. If the simulator, OCPP central system or operator sync committed in the meantime, the following INSERT failed with `database is locked` (a WAL "busy snapshot", which `busy_timeout` cannot fix), which surfaced as HTTP 500. Now no transaction is open during network I/O, and all writers start their write transactions with `BEGIN IMMEDIATE` (`db.begin_immediate`), with retries on a busy database. Static files are served with `Cache-Control: no-cache` so an updated server never runs stale JavaScript.

## 6. Checking real connections from your computer
```powershell
python scripts\verify_integrations.py            # Open Charge Map + operator API diagnosis (keys never printed)
python scripts\verify_integrations.py --osm      # plus one OpenStreetMap Overpass query
python scripts\verify_integrations.py --places   # plus ONE billed Google Places request
```
