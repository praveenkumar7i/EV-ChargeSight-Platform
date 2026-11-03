# ChargeSight v9: accounts, organisations, subscriptions, onboarding and integrations

ChargeSight is now a multi-organisation B2B prototype. The operations console (Fleet dashboard, Map,
Tickets, Priority engine, Technicians, Repair policy lab, Simulator, Analytics, Site feasibility,
Data integrations, Validation, Reports) is unchanged in behaviour, but every request is made by a
signed-in member of one organisation and sees only that organisation's data.

| URL | What |
|---|---|
| `/` `/features` `/pricing` `/enterprise` `/contact` | Public website (`frontend/site/`) |
| `/signup` `/signin` `/forgot-password` `/reset-password` `/verify-email` `/accept-invite` | Account pages |
| `/terms` `/privacy` `/security` | Prototype policy pages (to be finalised before any commercial launch) |
| `/app` | Operations console (sign-in required). Old bookmarks `/#dashboard` redirect to `/app#dashboard` |

## 1. First start after upgrading (existing database)

1. `pip install -r requirements.txt` (adds `argon2-cffi` and `cryptography`).
2. `python run.py`. On first start the database is backed up to
   `data/backups/chargesight-before-v9-saas-migration-<UTC time>.db`, then migrated (see §8).
3. Create your own account for the existing prototype data:
   ```powershell
   python scripts\manage.py create-admin --email you@example.com --name "Your Name"
   ```
   The password is typed interactively. This account is a **platform administrator** and the **owner of the
   "ChargeSight Demo Workspace"**, the system organisation that holds every record that existed before v9.
4. Sign in at <http://127.0.0.1:8000/signin>.

No password, token or admin account is hard-coded anywhere.

## 2. Authentication and security design

| Control | Implementation |
|---|---|
| Password hashing | Argon2id (`argon2-cffi`), automatic re-hash when parameters change |
| Password rules | ≥ 10 characters and 3 character classes (or ≥ 16 characters), not common/repetitive, not containing the e-mail or name |
| Sessions | Server-side `auth_sessions` rows; the browser holds a random 256-bit token in the `cs_session` cookie, **HttpOnly, SameSite=Lax, Secure in production** (`ENVIRONMENT=production` or `COOKIE_SECURE=true`). Only the SHA-256 of the token is stored |
| Expiry | 12 h absolute and 2 h idle; "Keep me signed in" = 30 days. Sign-out, password change and password reset revoke sessions |
| CSRF | Every state-changing request needs the `X-CSRF-Token` header equal to the session's token (held in page memory, never in localStorage), plus an Origin check that refuses cross-site requests |
| Session fixation | A new session is created at every sign-in; a pre-existing session cookie is revoked |
| Rate limits | Sign-in per e-mail+IP (10/15 min) and per IP (50/15 min); sign-up 10/h per IP; reset 5/h per e-mail; enquiries 10/h per IP; connection tests 10/10 min per organisation (in-process limiter, see limitations) |
| Generic errors | One message for unknown e-mail and wrong password; reset requests always get the same answer; no stack traces reach the browser |
| Logs | Passwords, tokens, CSRF values and credentials are never logged; configured secrets are redacted by a logging filter; tests check this |
| Headers | `X-Content-Type-Options`, `X-Frame-Options: DENY`, `Referrer-Policy`, `Permissions-Policy`, HSTS when cookies are Secure; `/docs` disabled in production |
| Authorisation | `backend/app/security/guard.py`: one rule per route (method + path template) with the required role permission, plan feature and read-only behaviour. **Deny by default**: a route without a rule is refused, and a test fails if any registered route lacks a rule |

**E-mail is not configured.** Verification, password-reset and invitation messages are written to a local
**development outbox** (`data/dev_outbox/*.txt`, marked "THIS MESSAGE WAS NOT E-MAILED") when
`ENVIRONMENT` is not production. The UI says exactly that; nothing claims an e-mail was sent. Unverified
accounts are labelled "unverified". Invitation links are also shown to the inviting owner/admin to share.
An administrator can reset a password with `python scripts\manage.py reset-password --email …`.

## 3. Organisations, roles and tenant isolation

Tables: `organisations`, `users`, `memberships`, `auth_sessions`, `one_time_tokens`, `subscription_plans`,
`subscriptions`, `subscription_events`, `demo_checkouts`, `demo_receipts`, `integration_configs`,
`audit_events`, `enquiries`.

**Isolation (`backend/app/tenancy.py`).** Every operational table (sites, chargers, events, readings,
snapshots, technicians, tickets, ticket history, experiment runs, investment sites, financial references,
ingestion runs, interviews, validation datasets and tracks, planning candidates) has `org_id`. The request's
database session is bound to the signed-in member's organisation, and an SQLAlchemy `do_orm_execute` hook
adds `org_id = <organisation>` to **every** ORM SELECT/UPDATE/DELETE, including `Session.get`, joins,
sub-queries and relationship loads. Another organisation's ticket or charger is therefore "not found",
whatever ID is put in a URL or request body. New rows get the organisation in `before_flush`, and an
`org_id` can never be changed. Settings, simulator state, ticket codes and charger/site/technician IDs are
per organisation (`TKT-<CODE>-00001`, `<CODE>-SIM-HSR-01`). Assignment additionally checks that the
technician belongs to the ticket's organisation. Public Open Charge Map / OpenStreetMap listings are shared
read-only reference data; refreshing them and live Google lookups are platform-administrator actions.

| Role | Can |
|---|---|
| Owner | Everything in the organisation, including subscription and billing actions |
| Admin | Users, invitations, settings, integrations, operations; view billing (no purchases) |
| Operations manager | Tickets, dispatch, technicians, simulator, planning, validation, reports |
| Technician | Operational pages; raise tickets; update **only tickets assigned to their linked technician record** |
| Viewer | Read-only (including what-if calculations that store nothing) |
| Platform administrator | Separate flag: organisations, subscription states, plan catalogue, enquiries, platform audit |

## 4. Plans, prices and entitlements (catalogue v9.1, proposed prototype prices)

The catalogue lives in `subscription_plans` (seeded from `services/billing.py`, editable centrally by a
platform administrator in **Platform admin → Plans & policy**). The pricing page, the landing-page preview,
Subscription & billing, checkout and the server-side entitlement checks all read it; no price is hard-coded
in the frontend. These are proposed prices for an early-stage offering, not validated market prices.

| Plan | Price | Managed chargers | Users | Sites | Configured integrations |
|---|---|---|---|---|---|
| Demo / Sandbox | ₹0 | simulated only | 2 | simulated only | 0 |
| Starter (recommended starting plan) | ₹499 / month | 5 | 2 | 1 | 0 (CSV imports included) |
| Growth | ₹1,499 / month | 25 | 5 | 3 | 3 |
| Professional | ₹4,999 / month | 100 | 15 | 10 | 10 |
| Enterprise | custom quotation | negotiated | negotiated | negotiated | negotiated |

Features:
* **Demo / Sandbox:** simulated fleet only (labelled SIMULATED / DEMO), demo tickets, simulated technicians and
  automatic assignment, priority engine, sample analytics and reports, demo site planning. No imports or integrations.
* **Starter:** basic fleet dashboard, maintenance tickets and priority engine, manual technician assignment, basic SLA
  tracking, basic analytics and standard CSV exports, public station map, demo data and supported CSV imports.
* **Growth:** Starter + automatic assignment, SLA escalation, advanced analytics, maintenance reporting (downtime-cost
  export), site feasibility and planning, the repair policy lab and up to 3 configured integrations (operator API / OCPP,
  subject to provider compatibility and operator authorisation).
* **Professional:** Growth + expanded and historical reporting (printable operations summary) and enhanced administration
  (organisation audit log), up to 10 configured integrations.
* **Enterprise:** Professional + contract administration, audit/retention configuration and custom reporting; every limit
  is recorded per contract by a platform administrator and is never described as unlimited.

"Recommended starting plan" on Starter is a product recommendation, labelled as such, not a popularity claim.
Managed chargers and sites are the organisation's real imported, registered, operator-API and OCPP records; simulated
chargers and sites, the local OCPP test point and automatic grouping sites (UNASSIGNED-…, OCPP) never count.

**Calculation (server-side only).** Python `Decimal` arithmetic in rupees, rounded half-up to the paisa, stored as
integer paise. Discounts are a configurable policy (monthly 0 %, 6 months 5 %, 12 months 10 %).

| Plan | Monthly | 6 months | 12 months |
|---|---|---|---|
| Starter | ₹499.00 | ₹2,994.00 − ₹149.70 = **₹2,844.30** (₹474.05/month) | ₹5,988.00 − ₹598.80 = **₹5,389.20** (₹449.10/month) |
| Growth | ₹1,499.00 | ₹8,994.00 − ₹449.70 = **₹8,544.30** (₹1,424.05/month) | ₹17,988.00 − ₹1,798.80 = **₹16,189.20** (₹1,349.10/month) |
| Professional | ₹4,999.00 | ₹29,994.00 − ₹1,499.70 = **₹28,494.30** (₹4,749.05/month) | ₹59,988.00 − ₹5,998.80 = **₹53,989.20** (₹4,499.10/month) |

Every quote returns the monthly price × months, the discount, the total payable for the commitment and the monthly
equivalent of the discounted total. Prices exclude GST; the application does not calculate tax and says so
(`tax.applied = false`). The browser sends only a plan key and a period; any price, discount or state it sends is
ignored, and the price is re-verified when the checkout is completed.

**Enforcement.** Features: e.g. Starter cannot open site planning, the repair policy lab, advanced analytics,
auto-assignment or configured integrations; Growth cannot open the printable summary or the audit log (HTTP 403
`plan_feature` with an upgrade message). Limits: new managed chargers (registration, CSV import, operator API sync,
OCPP), new real sites (registration, imports, sync), invitations and integrations are refused at the limit (HTTP 403
`plan_limit`, e.g. "You have reached your plan's 5-managed-charger limit…"). Import/sync rows over a limit are rejected
per row with code `plan_limit`; nothing existing is deleted when a plan changes or expires.

**Catalogue changes and existing customers (v9.0 → v9.1).** On start, `billing.upgrade_catalogue` runs once:
* a plan row that still holds its exact v9.0 seed values is moved to v9.1; a row an administrator edited is left
  unchanged and listed in the `plan_catalogue` setting; a database backup
  (`data/backups/chargesight-before-v9-1-plan-catalogue-<UTC>.db`) is written before any existing row changes;
* completed checkouts and DEMO receipts keep their historical amounts and are never rewritten;
* a subscription active on a changed plan keeps its purchased limits and features ("grandfathered", shown in
  Subscription & billing) until its next checkout (renewal, upgrade or reactivation), which then uses the current
  catalogue. The renewal estimate shows the current catalogue price. Nothing is deleted.

## 5. Subscription lifecycle

States: `demo`, `trial`, `pending_checkout`, `active`, `past_due`, `expired`, `cancelled`, `suspended`.
Allowed transitions are enforced by `billing.transition()`; every change writes a `subscription_events` row.

* New organisations start on **Demo / Sandbox** (`demo`).
* Starting a checkout from demo/trial/expired/cancelled → `pending_checkout`; abandoning or expiry (60 min) restores the previous state.
* Completing the demo checkout → `active` with a period of 1, 6 or 12 calendar months.
* Renewal date reached: DEMO mode has no saved payment method and never charges, so the subscription becomes
  `past_due` with a **7-day grace period** (full access), then `expired`.
* "Cancel renewal" keeps access to the end of the paid period, then `cancelled`; the current period is not refunded.
* Upgrades take effect immediately with a pro-rata credit for the unused part of the paid period; downgrades and shorter periods are scheduled for the next renewal.
* Platform administrators can suspend/reactivate (with a reason), grant a 14-day Growth trial, record an Enterprise contract with negotiated limits, and – for demonstrations – move a subscription's own dates to "renewal date reached" or "grace ended".

**Read-only policy (expired, cancelled, suspended).** All data stays readable, including safety history and
billing; existing tickets can still be progressed, annotated and closed (safety work is never blocked);
billing and reactivation remain available to the owner. Other changes, simulator steps and paid features
pause. Nothing is deleted.

## 6. Demo checkout, TEST payment step and receipts (v9.2)

No payment gateway is connected and no money ever moves. The checkout looks like a real subscription checkout,
but every step says it is a TEST.

1. **Review**: plan, limits, base price × months, commitment discount (0 / 5 / 10 %), credit, total payable, monthly
   equivalent and tax treatment, all calculated on the server. Billing name, e-mail and optional address.
   The server creates a pending checkout (`demo_checkouts`) with the price snapshot. Prices or states sent by a
   browser are ignored.
2. **Payment** (`POST /api/billing/checkout/{id}/payment-method`): choose UPI (Google Pay, PhonePe, Paytm, BHIM or
   another app, plus a test UPI ID), credit card, debit card or net banking. **Only test values are accepted**:

   | Method | Approves | Declines |
   |---|---|---|
   | UPI | `success@test` (any `name@test`) | `failure@test` |
   | Credit / debit card | 4111 1111 1111 1111 (Visa), 5555 5555 5555 4444 (Mastercard) | 4000 0000 0000 0002 |
   | Net banking | ChargeSight Test Bank | ChargeSight Test Bank (declines) |

   Real-looking UPI IDs, unlisted card numbers (even Luhn-valid ones), past expiry dates and real bank names are
   refused. The browser refuses a non-test card number before it is sent. **No CVV, OTP, UPI PIN or bank password
   is ever requested.** Extra fields such as `cvv`, `otp` or `pin` are dropped by the API. Only the method and a
   masked label are stored, for example "Test credit card · Visa ending 1111". The full number, the expiry date
   and anything typed by mistake are never stored, logged or echoed.
3. **Confirm**: summary, a "TEST payment, no money moves, not a tax invoice" acknowledgement, and an
   `Idempotency-Key`. Completion **requires the payment step** (`payment_method_required`, HTTP 409, otherwise).
   No button can claim or activate a paid plan directly. The server re-verifies state and price, then
   `DemoPaymentProvider` confirms or declines **from the test value**. A decline (`test_payment_declined`, HTTP 402)
   changes nothing, clears the method and records a `test_payment_declined` event.
4. **Result**, shown only after the server confirmed it: "Test payment approved (simulated)". The plan is active in
   DEMO/TEST mode, and receipt `DEMO-RCPT-…` shows the masked test method. Its label still reads "simulation only, no
   payment was taken, not a tax invoice". The `DEMO-…` reference is labelled "Test reference (not a bank, card or
   UPI transaction ID)".

Repeated confirmation (double click, retry, another key) returns the same result: no second activation, receipt or
event. Upgrade credit, downgrade-at-renewal, grace and read-only rules are unchanged. Enterprise stays
contact-sales with negotiated limits, and the free Demo / Sandbox plan is never sold through checkout.
Historical checkouts and receipts are not rewritten. Those completed before v9.2 show "before v9.2: no payment
step (DEMO/TEST)". Every checkout and receipt carries `record_type: "TEST"`, so a future real-provider record can
be told apart.

`services/payments.py` defines the `PaymentProvider` interface. A real gateway must set `verified=True` only
after server-side verification of the provider's signed webhook or payment status, never from a browser redirect.

## 7. Onboarding and integrations

**Wizard (Workspace setup):** 1 organisation profile → 2 use cases → 3 data mode → 4 workspace (sites,
technicians/users, SLA policy, alert preferences, retention preference) → 5 verify checklist → 6 enter the
dashboard (banner with a link back to setup). Progress is stored in `organisations.onboarding`.
Data modes: **Demo** (creates a private simulated fleet: 6 sites, 40 SIMULATED chargers, 4 DEMO technicians,
6 h scripted history, 10–20 s), **CSV import** (done only after a successful import), **Operator API**,
**OCPP**. No API key is needed to finish onboarding.

**Connections (Data integrations → Connections).** States: Not configured, Draft, Awaiting operator credentials,
Configured, Testing, Connected / verified, Authentication failed, Connection failed, Permission insufficient,
Stale, Disabled.
* Operator REST API: adapter (ChargeSight REST contract v1), https base URL, Bearer credential (write-only,
  Fernet-encrypted at rest, key outside the database: `INTEGRATION_ENCRYPTION_KEY` or `data/keys/integration.key`),
  timeout, polling, mapping notes, and the operator's written authorisation (required before testing).
  The test checks the URL (no private/loopback/link-local/metadata addresses, no credentials in the URL),
  calls `GET /health` and `GET /events` with timeouts and bounded retries, never follows redirects, and maps
  401 → authentication failed, 403 → permission insufficient, other failures → connection failed. Only a
  passing test sets **Connected**. "Sync" imports into this organisation only. Any configuration change requires a new test.
* OCPP 1.6J: each connection gets its own endpoint `ws://<host>:9000/<endpoint key>/<charge point id>`
  (unknown/disabled keys are refused; an optional allow-list of charge point IDs). The local simulator
  (`SIMOCPP…` / vendor `ChargeSight-Sim`) is labelled test traffic and never verifies the connection or counts
  as a managed charger. A real charge point's accepted BootNotification sets **Connected**; last boot,
  heartbeat and status notification and staleness (> 12 min) are shown.
* The server's `.env` operator credentials belong to the system demo workspace only and are never shown to other organisations.
* A paid plan never enables an integration by itself.

## 8. Database migration, backup and rollback

`db.create_schema()` on first start of v9 (SQLite):
1. **Backup** with the SQLite online-backup API to `data/backups/chargesight-before-v9-saas-migration-<UTC>.db`
   before anything is changed.
2. Create the new tables; rebuild `connector_health` and `validation_tracks` (keyed by organisation) in one
   transaction with row counts checked; add `org_id` columns.
3. Create organisation 1, **"ChargeSight Demo Workspace"** (system demo, explicit provenance), and assign every
   existing record to it in one transaction (counts stored in the `v9_migration` setting). No existing record is
   given to a customer organisation, deleted or changed otherwise.

Verified on a copy of the current database: all 9 tickets, 28 ticket-history rows, 4,338 events, 41 chargers,
163 public listings, 1 planning candidate, 2 investment sites and 13 ingestion runs kept; integrity and
foreign-key checks clean; a second start is a no-op.

**Restore:** stop the server, copy the backup over `data\chargesight.db`, delete `data\chargesight.db-wal` and
`data\chargesight.db-shm`, then start the previous code version (`Desktop\ChargeSight-backups\code-before-v9-saas-2026-10-24.zip`).

## 8a. v9.2: My Fleet, Presentation Demo and the empty-dashboard repair

**Root cause of the empty dashboard (from the real database):** organisation 2 was registered with the
*Operator API* data mode, so it has 0 chargers, 0 sites and 0 tickets. The 40-charger demo fleet belongs to the
system demo workspace (organisation 1), of which the user is not a member. Nevertheless organisation 2's simulator
was `running = 1`. The background loop advanced the empty simulation and wrote 1,320 fleet snapshots with
`total = 0`. The console therefore showed "Simulating" beside zero KPIs, and the uptime chart showed "0 %, 31 of 31
samples below target". Nothing was missing from the seed and no data had been lost.

**Fixes:** the simulator cannot be started or stepped without simulated chargers (`no_fleet`, HTTP 409). The loop
pauses a fleetless simulation and never writes 0-charger snapshots. History ignores `total = 0` snapshots (they are
kept, not deleted). The dashboard shows an honest empty state with actions. At start-up, simulators that run
without a fleet are paused **after a verified backup**
(`data/backups/chargesight-before-v9-2-simulator-repair-<UTC>.db`); this is the only data change. A
`demo_seeded` flag without records is no longer trusted.

* **My Fleet**: the operator's own organisation. Only its own records; nothing synthetic is added automatically;
  "Reset / restore demo" is refused there (`not_demo_workspace`).
* **Presentation Demo** (sidebar → Workspace → *Open Presentation Demo*, or `POST /api/auth/presentation-demo`): a
  separate organisation per user (`workspace_kind = "presentation"`) populated by the existing generator: 6 demo
  sites, 40 SIMULATED chargers, 4 fictional technicians, 6 h of history, 8 scripted tickets in every state,
  priority factors, timeline, 2 sample policy runs and 2 sample investment sites. The data is there as soon as the
  workspace opens, and history charts work while the simulator is paused. Opening it again reuses it.
  Restore is idempotent and keeps imported, live and user-created records. *Back to My Fleet* is
  `POST /api/auth/my-fleet`.

**Schema (v9.2):** `organisations.workspace_kind`, `organisations.presentation_owner_user_id`,
`demo_checkouts.payment_method`, `payment_display`, `payment_attached_at`, `payment_test_outcome`. They are added
with `ADD COLUMN` only, after a verified backup (`chargesight-before-v9-2-schema-<UTC>.db`). Verification opens the
copy read-only, runs `PRAGMA quick_check` and compares tables; start-up stops if verification fails.

## 9. Limitations (honest list)

* Prototype security: controls above are implemented and tested, but there has been **no independent security review or penetration test**. Do not call it production-grade.
* Rate limits are in-memory per server process; multiple processes or a restart reset them.
* No e-mail delivery: verification, reset and invitations use the development outbox (disabled in production, where reset requires an administrator).
* No real payments, invoices or tax; DEMO receipts only. Renewal never charges automatically.
* DNS rebinding can bypass the outbound URL check between the check and the connection (documented in `security/ssrf.py`).
* Retention days are a recorded preference; automatic deletion is not implemented.
* SQLite and a single process are fine for the prototype; use PostgreSQL and a shared rate-limit store for real multi-user deployment (the SQLite-specific migration steps are no-ops elsewhere).
* Building a demo workspace briefly pauses other simulators (it holds the simulator lock for ~10–20 s).
* The OCPP test simulator targets the system demo workspace by default; for an organisation's endpoint run `python scripts\ocpp_simulator.py --url ws://127.0.0.1:9000/<endpoint key>` (it is still labelled test traffic).
