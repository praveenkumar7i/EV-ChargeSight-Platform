# Metric definitions

## Live operations (tickets)

| Metric | Unit | Definition |
|---|---|---|
| MTTA | minutes | mean(`assigned_at − detected_at`) over tickets that have been assigned. "Acknowledge" follows the research report: a technician has been assigned. The separate `acknowledged_at` timestamp records when the technician accepted the job (En Route). |
| MTTR | minutes | mean(`resolved_at − detected_at`) over resolved tickets, all resolution routes (technician, remote reset, reconnect, admin). |
| Overdue | count | Open tickets older than the SLA for their severity. Default SLA (ASSUMPTION): critical 4 h, major 12 h, minor 48 h. |
| Able to charge | count / % | Chargers with status `available` or `charging`. |
| Site downtime | hours | For each charger at the site: the union of [detected, resolved or now) intervals of its tickets in the current run, summed. |

## Priority score

`P = wS·S + wU·U + wR·R + wA·A`, with weights normalised to sum to 1 (default 0.4/0.3/0.2/0.1, a design ASSUMPTION).

| Factor | Range | Definition |
|---|---|---|
| S | 0–1 | critical 1.0 (cannot charge), major 0.6 (degraded), minor 0.2. Unknown categories → 1.0 with a warning. |
| U | 0–1 | site expected sessions/day ÷ maximum among sites of the same data source; 0 when that maximum is 0. |
| R | 0–1 | charger kW ÷ (charger kW + kW of other chargers at the site that can charge now). |
| A | 0–1 | min(hours waiting ÷ age cap, 1); age cap 24 h by default. |

Ordering: tickets waiting at least the starvation cap (24 h) are escalated and served first, oldest first. Otherwise higher P comes first; ties go to the earlier detection, then the ticket code. Missing, negative or NaN inputs become 0 with a warning, and the score always stays within 0–1.

## Policy experiment (per seed, then paired across seeds)

| Metric | Unit | Numerator / denominator, exclusions |
|---|---|---|
| Fleet availability | % | 100 × (1 − total downtime ÷ (chargers × horizon)). |
| Total charger downtime | charger-hours | Σ over chargers of the union of [detected, resolved) fault intervals, clipped to the horizon. Faults unresolved at the horizon count until the horizon. |
| Utilisation-weighted downtime | weighted charger-hours | Σ charger downtime × (site expected sessions ÷ mean expected sessions per charger). |
| Mean / P95 wait, high severity | minutes | (assigned − detected) over technician-dispatched critical faults; P95 uses linear interpolation. |
| Max wait, lower severity | minutes | Max (assigned − detected) over dispatched S < 1 faults; unassigned ones count as horizon − detected. |
| MTTA | minutes | Mean (assigned − detected) over dispatched faults. |
| MTTR | minutes | Mean (resolved − detected) over faults resolved within the horizon, including remote resets. |
| Energy not delivered | kWh, ESTIMATED | Σ downtime h × charger kW × site utilisation fraction (ASSUMPTION). |
| Downtime cost | INR, ESTIMATED | energy not delivered × margin per kWh (ASSUMPTION). Lost margin only. |

Comparison statistics: difference = priority − FIFO for each seed. The table reports the mean, sample SD, a two-sided 95% paired t-interval (Student t critical values) and the relative difference (mean diff ÷ FIFO mean). "Significant" means the interval excludes 0.

## Investment model

All formulas are in the docstring of `backend/app/core/finance.py` and on the Site feasibility page under "Formulas". Gross revenue is never called profit; operating contribution = revenue − electricity − variable costs − fixed opex (pre-tax, no financing or depreciation).
