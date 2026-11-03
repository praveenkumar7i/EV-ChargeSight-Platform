# Maintenance ticketing, priority, technician assignment and simulation (v8)

One pipeline for every ticket source: **USER_REPORTED** (Raise a Ticket), **SIMULATOR**, **OCPP**,
**OPERATOR_API** and **IMPORTED_DATA**. The *origin* (how a ticket was raised) is stored separately from the
*source* (data domain: SIMULATED / LIVE / IMPORTED), so simulated, imported, operator-API and OCPP records are
never mixed. A user report is never presented as charger telemetry.

Code: `services/tickets.py` (creation, triage, lifecycle, SLA), `services/assignment.py` (technicians),
`services/priority_service.py` + `core/priority.py` (the only priority calculation), `simulator/engine.py`.

## 1. Root cause of "7.5 % uptime, 37 faulted, 37 open tickets, almost static"
Measured by running the engine forward on a copy of the user's database:

* **No repair path by default.** The simulator's `auto_dispatch` was `False`, so no technician ever worked a ticket.
  Only remote resets closed tickets, and half of the fault mix (high temperature, overcurrent, ground fault:
  critical, reset success 0) can never be reset. Tickets already assigned/en route/on site never progressed.
* **Fault rate 12x the research baseline** (0.6 vs 0.05 faults per charger-day).
* Together: faults accumulate monotonically (85 % → 45 % uptime in 48 simulated hours; ≈ 7.5 % after ~5 simulated
  days, about 2 real hours at speed 4). A faulted charger cannot fault again or run sessions, so the fleet then
  looks frozen. The dashboard numbers were correct; the simulation had no recovery.

Fix: simulated DEMO technicians now work tickets assigned through the shared assignment service; Normal
Operations uses 0.15 faults/charger-day (3x baseline, so a demo shows a fault every few minutes at speed 4);
an existing database still on the exact old defaults is moved to the new defaults once (recorded in the
`sim_params_v8` setting); customised parameters are left alone. Measured on the user's migrated data:
92.5–100 % uptime over 72 simulated hours with continuous faults, repairs, inspections and reopenings.

## 2. Priority (existing engine, unchanged formula)
`P = 0.4·S + 0.3·U + 0.2·R + 0.1·A` (weights configurable on the Priority Engine page, normalised to 1).

| Factor | Rule (existing) | v8 inputs |
|---|---|---|
| S severity | critical 1.0 (cannot charge), major 0.6 (degraded), minor 0.2 (no effect) | see below |
| U utilisation | site daily sessions ÷ network max | unchanged |
| R capacity | charger kW ÷ (charger kW + kW of other chargers at the site that can still charge) | site-wide report of N chargers: N × charger kW lost (ASSUMPTION: same rating); N = 1 is the original rule |
| A age | min(age h ÷ 24, 1) | unchanged; on the simulation clock for SIMULATED tickets |

Ranking (existing): tickets waiting ≥ 24 h (starvation cap) first, oldest first; then score; then detection time.

**Severity** uses the existing definitions:
* safety faults (ground fault, high temperature, overcurrent, reported safety hazard) → **critical, never downgraded**;
* reported "unable to charge" or impact *charger unavailable* → critical (the definition of critical);
* impact *reduced capacity* / *intermittent* → major; *informational* → minor;
* otherwise the catalogue severity of the fault type. The basis is stored and shown (`severity_basis`).

Measured vs reported: the explanation lists where inputs came from ("Reported by a user; not yet confirmed by
charger telemetry", "Raised from simulator data"). When telemetry later reports the same fault, the user ticket
is noted as confirmed.

**Audit:** at triage and on each event (reopen, reassignment, severity change) the score is recomputed by the
engine; any change of the stored snapshot ≥ 0.005 writes a `priority` history row with previous value, new
value, rank, reason and time. The live ranking still moves continuously with the age factor. Technician workload
never changes a ticket's priority.

## 3. Lifecycle (existing status names kept)
```
open ─assign→ assigned ─accept→ en_route ─arrive→ on_site ─repair→ resolved ─verify→ closed
                           └→ waiting_access ─┘     └→ waiting_parts ─(resume)→ on_site
resolved / closed ─reopen (reason)→ open
```
* NEW = open before triage; TRIAGED = open with severity, SLA deadline and priority (immediately at creation);
  IN_PROGRESS = en_route / on_site; ESCALATED = flag; REOPENED = resolved/closed → open (`reopen_count`).
* Validated in the backend (`ALLOWED`); anything else needs the admin token and is recorded as an override.
* Waiting states and reopening require a reason. Reassignment/unassignment use the assignment endpoints
  (a reassigned ticket returns to *assigned* so the new technician accepts it).
* Existing system exception kept: an open ticket closes automatically after a successful remote reset or reconnect.
* **Safety:** resolving a safety fault holds the charger *unavailable*; it returns to service only when the
  ticket is closed with a passed post-repair inspection (recorded as an `inspection` history row).
* A fault of the same kind on the same charger within 24 h of a repair **reopens** that ticket (fault recurrence).
* Every change is a `ticket_history` row with `kind` = status | priority | assignment | escalation | note | inspection,
  actor, time, note and data; the event timeline shows all of them.

## 4. SLA and escalation (existing thresholds)
* Deadline = detection (or reopen) + 4 h critical / 12 h major / 48 h minor (ASSUMPTION, `sla_hours` setting).
* Shown as remaining time or overdue duration, with the time basis (simulation clock or wall clock).
* When the deadline passes the ticket is **escalated once** (`escalated_at`); unassigned escalated tickets get
  *dispatch needed*. Repeated polling never duplicates escalation rows. Checks run every simulated minute for
  SIMULATED tickets, every 30 s for OCPP / operator-API tickets, and at start-up; imported history is not escalated.
* Notifications are in-app only (dashboard alert, ticket list, timeline). No e-mail or SMS is sent or claimed.

## 5. Technician assignment
Eligibility (all required; otherwise listed with the reasons):
1. active, availability *available*, on shift if a shift is set (at the ticket's clock time);
2. has the skill for the fault (`REQUIRED_SKILL`: e.g. payment → *payment*, ground fault → *safety*);
3. equipped for the charger (DC for CCS/Bharat DC or > 22 kW, otherwise AC);
4. safety-certified for safety faults;
5. workload (assigned, en route, on site, waiting) < capacity (default 1, the existing one-job rule);
6. DEMO technicians only take tickets on SIMULATED chargers.

Choice among eligible technicians (deterministic): can arrive before the SLA deadline (only when a real travel
estimate exists) → lower workload share → lower workload → shorter distance (only when both locations are known)
→ technician id. Queue order when technicians are scarce: the existing priority ranking (or FIFO when the
simulator's dispatch policy is FIFO). Travel time is estimated only for registered technicians with real
coordinates (straight line ÷ travel speed, ASSUMPTION); for DEMO area centroids the distance only breaks ties
and no travel time is claimed.

When automatic assignment applies: SIMULATED tickets while the simulator's auto-dispatch is on; OCPP /
operator-API tickets only if *auto_assign_live_sources* is enabled (registered technicians); imported records
never. A scheduled remote reset is given its chance first (existing behaviour), except for safety faults.

**No eligible technician:** the ticket stays open and unassigned, the reasons are stored ("No eligible technician
available (0 of 4): 2 not safety-certified; 1 availability: off duty; 1 at capacity"), critical or escalated
tickets get *dispatch needed* (dashboard alert), and assignment is retried when a technician's availability,
skills, capacity or workload changes, when work finishes, when SLA escalates, and every 5 simulated minutes.
**Emergency override** (policy, default off): a qualified technician may exceed capacity for critical/escalated
tickets; skills and safety certification are never bypassed automatically.

**Manual assignment:** the ticket drawer lists every technician with why they are or are not eligible. Conflicts
(skill, equipment, availability, capacity, SLA) must be confirmed with a reason; the safety-certification rule can
only be overridden with the admin token. The actor, time, reason and conflicts are recorded. Pre-v8 tickets that
were assigned to an uncertified technician for a safety fault are flagged "Review: … reassign" (never silently
changed), and simulated technicians do not perform safety work they are not certified for.

DEMO technicians (TECH-01…04) are fictional, flagged `is_demo`, and labelled DEMO everywhere.

## 6. Simulator
Per 15-second step: heartbeats, sessions (start probability scaled by site utilisation, energy = power × time),
faults (rate × category mix; ×2 for 24 h after a repair, mostly the same fault = recurrence), intermittent connector
faults (often cleared by remote reset), power-delivery failures, communication loss detected by missed heartbeats
and recovered by reconnects or resets, remote resets, simulated technicians (accept after 3 min, travel = site
travel ASSUMPTION, repair = catalogue time ×0.7–1.3, 10 % wait for parts for 2 h, 15-min safety inspection then
close), SLA checks, snapshots every simulated minute (the uptime chart uses these timestamped samples). Steps are
deterministic for a seed; state changes come from the engine, never from dashboard polling.

| Scenario | What changes in the engine |
|---|---|
| Normal operations | defaults (0.15 faults/charger-day, auto-assignment, all technicians), speed 4 |
| High demand | session arrivals ×3, 60-min sessions |
| Degraded network | 0.5 faults/charger-day, 70 % communication loss, offline after 2 missed heartbeats, reconnects 5× slower |
| Major incident | power-delivery failure on every DC charger at Koramangala + ground fault at Whitefield, 0.8 faults/charger-day, TECH-04 off duty |
| Critical fault burst, Site outage, Maintenance backlog, Recovery, Policy comparison | as before |

## 7. Data integrity
Tickets table migrated in place on first start (SQLite cannot alter a CHECK constraint): a consistent backup is
written to `data/backups/chargesight-before-v8-ticket-migration-<time>.db`, rows are copied, counts and foreign
keys are checked, and nothing is dropped. One active ticket per charger and fault (partial unique index);
`client_request_id` makes form submissions idempotent; ticket creation, assignment and audit rows commit in one
transaction under the simulator lock.
