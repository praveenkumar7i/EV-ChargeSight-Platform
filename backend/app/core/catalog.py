"""Static definitions shared by the live simulator, priority engine and experiments.

Everything numeric in this file is an ASSUMPTION chosen for the academic
prototype (see docs/ASSUMPTIONS.md). None of it is measured Indian field data.
"""
from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Data provenance labels (shown in the UI and written into every export)
# ---------------------------------------------------------------------------
SOURCES = (
    "simulation",
    "operator_api",
    "ocpp",
    "operator_import",
    "simulated_imported",
    "station_inventory",
    "historical_sessions",
)
SOURCE_LABELS = {
    "simulation": "SIMULATED",
    "simulated_imported": "SIMULATED IMPORTED",
    "station_inventory": "IMPORTED INVENTORY",
    "historical_sessions": "HISTORICAL RESEARCH",
    "operator_api": "LIVE",
    "ocpp": "LIVE",
    "operator_import": "IMPORTED",
}

# ---------------------------------------------------------------------------
# Charger status values (internal schema, aligned with OCPP 1.6 where possible)
# ---------------------------------------------------------------------------
STATUSES = ("available", "charging", "faulted", "offline", "unavailable")
CAN_CHARGE = {"available", "charging"}

# Allowed status transitions in the live simulator / ingestion pipeline.
# "offline" is entered when heartbeats stop and left only through a reconnect.
VALID_TRANSITIONS: dict[str, set[str]] = {
    "available": {"charging", "faulted", "offline", "unavailable"},
    "charging": {"available", "faulted", "offline"},
    "faulted": {"available", "offline", "unavailable"},
    "offline": {"available", "faulted", "unavailable"},
    "unavailable": {"available", "faulted", "offline"},
}

# ---------------------------------------------------------------------------
# Severity scale (S factor of the priority score)
# ---------------------------------------------------------------------------
SEVERITY_VALUES = {"critical": 1.0, "major": 0.6, "minor": 0.2}
SEVERITY_MEANING = {
    "critical": "charger cannot charge",
    "major": "charging degraded or unreliable",
    "minor": "cosmetic / no effect on charging",
}


@dataclass(frozen=True)
class FaultType:
    key: str
    label: str
    ocpp_error_code: str | None      # OCPP 1.6 ChargePointErrorCode, None if detected by silence
    severity: str                    # key into SEVERITY_VALUES
    remote_reset_success: float      # probability a remote Reset clears it (ASSUMPTION)
    repair_mean_min: float           # mean on-site repair time (ASSUMPTION)
    default_share: float             # share of all faults in the default mix (ASSUMPTION)


FAULT_TYPES: dict[str, FaultType] = {
    "high_temperature": FaultType("high_temperature", "High temperature", "HighTemperature", "critical", 0.0, 90, 0.20),
    "overcurrent": FaultType("overcurrent", "Overcurrent", "OverCurrentFailure", "critical", 0.0, 120, 0.15),
    "ground_fault": FaultType("ground_fault", "Ground fault", "GroundFailure", "critical", 0.0, 180, 0.15),
    "comm_loss": FaultType("comm_loss", "Communication loss", None, "major", 0.6, 45, 0.30),
    "payment_reader": FaultType("payment_reader", "Payment / reader failure", "ReaderFailure", "major", 0.4, 60, 0.20),
    # v8: categories for user-reported tickets and the richer simulator. default_share 0 keeps the Repair
    # Policy Lab's default fault mix (and its results) unchanged. Values are ASSUMPTIONS.
    "connector_fault": FaultType("connector_fault", "Connector / cable fault (intermittent)", "ConnectorLockFailure", "major", 0.5, 60, 0.0),
    "power_delivery": FaultType("power_delivery", "Power delivery failure", "PowerSwitchFailure", "critical", 0.2, 120, 0.0),
    "display_fault": FaultType("display_fault", "Display / screen fault", None, "minor", 0.3, 45, 0.0),
    "physical_damage": FaultType("physical_damage", "Physical damage", None, "major", 0.0, 150, 0.0),
    "safety_hazard": FaultType("safety_hazard", "Safety hazard", None, "critical", 0.0, 120, 0.0),
    "other_fault": FaultType("other_fault", "Other issue", None, "minor", 0.2, 60, 0.0),
}

# ---------------------------------------------------------------------------
# Raise a Ticket: user-facing issue categories -> internal fault category
# ---------------------------------------------------------------------------
ISSUE_CATEGORIES = {
    "connector": ("Connector / cable", "connector_fault"),
    "power_delivery": ("Power delivery", "power_delivery"),
    "communication": ("Communication / network", "comm_loss"),
    "payment": ("Payment / RFID reader", "payment_reader"),
    "display": ("Display / screen", "display_fault"),
    "safety": ("Safety hazard", "safety_hazard"),
    "overheating": ("Overheating", "high_temperature"),
    "physical_damage": ("Physical damage", "physical_damage"),
    "other": ("Other", "other_fault"),
}
IMPACTS = {
    "unavailable": "Charger unavailable",
    "reduced_capacity": "Reduced capacity",
    "intermittent": "Intermittent problem",
    "informational": "Informational",
}
# Ticket origin (how the ticket was raised). Separate from `source`, the data domain used to keep
# simulated / imported / operator-API / OCPP records apart.
ORIGINS = ("user_reported", "simulator", "ocpp", "operator_api", "imported_data")
ORIGIN_LABELS = {"user_reported": "USER_REPORTED", "simulator": "SIMULATOR", "ocpp": "OCPP",
                 "operator_api": "OPERATOR_API", "imported_data": "IMPORTED_DATA"}
ORIGIN_FOR_SOURCE = {"simulation": "simulator", "ocpp": "ocpp", "operator_api": "operator_api",
                     "operator_import": "imported_data"}
# Skill a technician needs for each fault category (ASSUMPTION, docs/TICKETING.md)
REQUIRED_SKILL = {
    "high_temperature": "overheating", "overcurrent": "power_delivery", "ground_fault": "safety",
    "comm_loss": "communication", "payment_reader": "payment", "connector_fault": "connector",
    "power_delivery": "power_delivery", "display_fault": "display", "physical_damage": "physical_damage",
    "safety_hazard": "safety", "other_fault": "other",
}
SKILLS = ("connector", "power_delivery", "communication", "payment", "display", "safety", "overheating",
          "physical_damage", "other")
# Faults with an electrical or fire risk: always critical, need a safety-certified technician, and the
# charger returns to service only after a recorded post-repair inspection (ticket closed).
SAFETY_CATEGORIES = {"ground_fault", "high_temperature", "overcurrent", "safety_hazard"}

# Map OCPP 1.6 error codes (and a few common operator synonyms) to categories.
OCPP_ERROR_TO_CATEGORY = {
    "HighTemperature": "high_temperature",
    "OverCurrentFailure": "overcurrent",
    "GroundFailure": "ground_fault",
    "ReaderFailure": "payment_reader",
    "EVCommunicationError": "comm_loss",
    "PowerMeterFailure": "overcurrent",
    "PowerSwitchFailure": "overcurrent",
    "OverVoltage": "overcurrent",
    "UnderVoltage": "overcurrent",
    "InternalError": "comm_loss",
    "ConnectorLockFailure": "payment_reader",
}


def severity_of(category: str) -> tuple[str, float, list[str]]:
    """Return (level, value, warnings). Unknown categories are treated as
    critical, the safe choice for a maintenance queue, and flagged."""
    ft = FAULT_TYPES.get(category)
    if ft is None:
        return "critical", 1.0, [f"Unknown fault category '{category}' treated as critical."]
    return ft.severity, SEVERITY_VALUES[ft.severity], []


# ---------------------------------------------------------------------------
# Demonstration fleet: 6 representative Bengaluru areas, 40 chargers.
# Coordinates are approximate public area centroids, NOT real charging stations.
# daily_sessions and travel times are ASSUMPTIONS.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SiteDef:
    code: str
    name: str
    lat: float
    lng: float
    daily_sessions: int
    travel_min: tuple[int, int]
    chargers: tuple[tuple[str, float], ...]   # (connector type, kW)


CONNECTORS = {
    "dc60": ("CCS2 DC", 60.0),
    "dc30": ("CCS2 DC", 30.0),
    "bdc": ("Bharat DC-001", 15.0),
    "ac7": ("Type 2 AC", 7.4),
    "bac": ("Bharat AC-001", 3.3),
}


def _c(*keys: str) -> tuple[tuple[str, float], ...]:
    return tuple(CONNECTORS[k] for k in keys)


DEMO_SITES: tuple[SiteDef, ...] = (
    SiteDef("KRM", "Koramangala (demo)", 12.9352, 77.6245, 120, (20, 35), _c("dc60", "dc60", "dc30", "dc30", "bdc", "ac7", "ac7", "bac")),
    SiteDef("WFD", "Whitefield (demo)", 12.9698, 77.7500, 110, (40, 60), _c("dc60", "dc60", "dc60", "dc30", "bdc", "ac7", "ac7", "bac")),
    SiteDef("HSR", "HSR Layout (demo)", 12.9116, 77.6474, 80, (20, 35), _c("dc60", "dc30", "dc30", "bdc", "ac7", "ac7", "bac")),
    SiteDef("ECY", "Electronic City (demo)", 12.8452, 77.6602, 70, (35, 55), _c("dc60", "dc30", "bdc", "ac7", "ac7", "bac")),
    SiteDef("HBL", "Hebbal (demo)", 13.0358, 77.5970, 60, (30, 50), _c("dc60", "dc30", "bdc", "ac7", "bac", "bac")),
    SiteDef("YLK", "Yelahanka (demo)", 13.1007, 77.5963, 30, (45, 60), _c("dc30", "bdc", "ac7", "bac", "bac")),
)

assert sum(len(s.chargers) for s in DEMO_SITES) == 40, "demo fleet must have 40 chargers"

DEMO_TECHNICIANS = (
    ("TECH-01", "Technician A", "South"),
    ("TECH-02", "Technician B", "North"),
    ("TECH-03", "Technician C", "East"),
    ("TECH-04", "Technician D", "West"),
)
# v8 profile of the DEMO technicians (fictional; not real employees). Base = a demo area centroid code.
DEMO_TECH_PROFILE = {
    "TECH-01": {"skills": list(SKILLS), "equipment": ["ac", "dc"], "safety_certified": True, "base": "HSR"},
    "TECH-02": {"skills": ["communication", "payment", "display", "connector", "other"], "equipment": ["ac", "dc"],
                "safety_certified": False, "base": "HBL"},
    "TECH-03": {"skills": ["power_delivery", "overheating", "safety", "connector", "physical_damage", "other"],
                "equipment": ["dc"], "safety_certified": True, "base": "WFD"},
    "TECH-04": {"skills": ["communication", "payment", "connector", "display", "power_delivery", "other"],
                "equipment": ["ac", "dc"], "safety_certified": False, "base": "KRM"},
}


def charger_id_for(site_code: str, index: int) -> str:
    return f"SIM-{site_code}-{index:02d}"
