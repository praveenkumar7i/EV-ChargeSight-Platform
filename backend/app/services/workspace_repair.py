"""v9.2 start-up checks for simulator state that does not match the stored fleet.

Root cause addressed (found in a real v9.1 database): an organisation registered with the "Operator API"
data mode has no simulated chargers, but its simulator state could still be started. The background loop
then advanced an empty simulation and recorded fleet snapshots with 0 chargers, so the console reported
"Simulating" next to zero KPIs and 0 % uptime charts.

The repair only pauses such simulators (``running = False``). It deletes nothing. A verified database backup is
written first, and the repair runs only when there is something to repair.
"""
from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Charger, SimState

log = logging.getLogger("chargesight.repair")


def fleet_size(db: Session, org_id: int | None = None) -> int:
    """SIMULATED chargers in one organisation (system session: pass org_id) or the session's organisation."""
    q = select(func.count(Charger.id)).where(Charger.source == "simulation")
    if org_id is not None:
        q = q.where(Charger.org_id == org_id)
    return db.scalar(q) or 0


def running_without_fleet(db: Session) -> list[int]:
    """Read-only: organisations whose simulator is marked running but which have no simulated chargers."""
    running = list(db.scalars(select(SimState.id).where(SimState.running.is_(True))))
    return [org_id for org_id in running if fleet_size(db, org_id) == 0]


def repair_running_without_fleet() -> dict:
    """Pause empty simulators after a verified backup. Idempotent; returns what was done."""
    from ..db import backup_sqlite_verified, session_scope
    from . import audit

    with session_scope() as db:
        pending = running_without_fleet(db)
    if not pending:
        return {"repaired": [], "backup": None}
    backup = backup_sqlite_verified("before-v9-2-simulator-repair")
    with session_scope() as db:
        for org_id in pending:
            st = db.get(SimState, org_id)
            if st is not None and st.running and fleet_size(db, org_id) == 0:
                st.running = False
                audit.record(db, "simulator.paused_no_fleet", org_id=org_id, actor_label="system",
                             data={"reason": "Simulator was running with no simulated chargers (v9.2 start-up check)."})
    log.warning("Paused simulators with no simulated chargers for organisation(s) %s (backup: %s)", pending, backup)
    return {"repaired": pending, "backup": str(backup) if backup else None}
