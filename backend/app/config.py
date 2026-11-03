"""Application settings, read from environment variables or a `.env` file.

Every secret (operator API key, Open Charge Map key, admin token) stays on the
server. The only value ever sent to the browser is the Google Maps *browser*
key, which Google designs to be public and which must be restricted by HTTP
referrer in the Google Cloud console (see docs/GOOGLE_MAPS.md).
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "chargesight.db"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Core -----------------------------------------------------------
    database_url: str = f"sqlite:///{DEFAULT_DB_PATH.as_posix()}"
    log_level: str = "INFO"
    host: str = "127.0.0.1"
    port: int = 8000

    # --- Live simulator -------------------------------------------------
    sim_seed: int = 42
    sim_step_seconds: int = 15          # simulated seconds advanced per step
    sim_tick_ms: int = 1000             # wall-clock interval between batches of steps
    sim_autostart: bool = False
    demo_autoseed: bool = True          # build the synthetic demo dataset on first start

    # --- Google Maps (browser key only; restrict by HTTP referrer) -------
    google_maps_browser_key: str = ""
    google_maps_map_id: str = ""

    # --- Open Charge Map (public charger listings, server-side key) -----
    ocm_api_key: str = ""
    ocm_base_url: str = "https://api.openchargemap.io/v3"

    # --- OpenStreetMap via Overpass (no key; ODbL; be polite: refresh on demand only)
    osm_overpass_url: str = "https://overpass-api.de/api/interpreter"
    osm_enabled: bool = True

    # --- Google Places API (New), server-side key; results are shown live, never stored
    google_places_api_key: str = ""

    # --- Load the bundled Open Charge Map snapshot of Bengaluru listings on start
    public_snapshot_autoload: bool = True

    # --- Authorised operator REST API (server-side only) ----------------
    operator_api_base_url: str = ""
    operator_api_key: str = ""
    operator_api_timeout_s: float = 10.0
    operator_api_max_retries: int = 3
    operator_api_poll_seconds: int = 0          # 0 = manual sync only; otherwise >= 60
    operator_api_stale_after_s: int = 900       # data older than this is shown as STALE

    # --- Optional OCPP 1.6J central system (separate process) -----------
    ocpp_host: str = "127.0.0.1"
    ocpp_port: int = 9000

    # --- Administrative override for ticket workflow -------------------
    admin_token: str = ""

    # --- v9 accounts, sessions and SaaS ---------------------------------
    environment: str = "development"            # "production" forces Secure cookies and disables the dev outbox
    cookie_secure: bool | None = None           # None = automatic (True in production)
    session_idle_minutes: int = 120             # sign-out after this long without activity
    session_absolute_hours: int = 12            # maximum session length without "remember me"
    session_remember_days: int = 30             # "remember me" (persistent cookie)
    public_base_url: str = ""                   # used in links written to the development outbox
    dev_outbox_enabled: bool = True             # write verification / reset / invite messages to data/dev_outbox (development only)
    auth_signin_limit: int = 10                 # attempts per e-mail+IP per 15 minutes
    auth_signin_ip_limit: int = 50              # attempts per IP per 15 minutes
    auth_signup_limit: int = 10                 # sign-ups per IP per hour
    auth_reset_limit: int = 5                   # reset requests per e-mail per hour
    public_form_limit: int = 10                 # enquiries per IP per hour
    integration_encryption_key: str = ""        # Fernet key for stored operator credentials (else data/keys/integration.key)
    integration_key_file: str = ""
    allow_private_integration_hosts: bool = False   # only for an administrator-controlled deployment on a private network

    @property
    def is_production(self) -> bool:
        return self.environment.strip().lower() == "production"

    @property
    def secure_cookies(self) -> bool:
        return self.is_production if self.cookie_secure is None else bool(self.cookie_secure)

    @property
    def outbox_active(self) -> bool:
        return self.dev_outbox_enabled and not self.is_production

    def secret_values(self) -> list[str]:
        """Values that must never appear in logs, API output or exports."""
        return [
            v
            for v in (self.ocm_api_key, self.operator_api_key, self.admin_token, self.google_places_api_key,
                      self.integration_encryption_key)
            if v and len(v) >= 4
        ]


@lru_cache
def get_settings() -> Settings:
    return Settings()
