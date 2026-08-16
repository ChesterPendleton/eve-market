"""Runtime configuration, loaded from environment / .env."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Region IDs for the major trade hubs. These are stable and safe to hardcode.
REGIONS: dict[str, int] = {
    "the_forge": 10000002,  # Jita
    "domain": 10000043,  # Amarr
    "sinq_laison": 10000032,  # Dodixie
    "heimatar": 10000030,  # Rens
    "metropolis": 10000042,  # Hek
    "genesis": 10000067,  # Ahbazon — VERIFY with `eve-market resolve` before trusting
}

# Station IDs for the hub stations inside those regions.
STATIONS: dict[str, int] = {
    "jita_4_4": 60003760,
    "amarr_emperor_family": 60008494,
    "dodixie_fnap": 60011866,
    "rens_brutor": 60004588,
    "hek_boundless": 60005686,
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="EVE_", extra="ignore"
    )

    # --- ESI ---------------------------------------------------------------
    # CCP requires a descriptive User-Agent with a contact address. Requests
    # without one get rate-limited aggressively or blocked outright.
    contact_email: str = Field(default="", description="Your email, sent in User-Agent")
    app_name: str = "eve-market"
    esi_base_url: str = "https://esi.evetech.net"
    esi_datasource: str = "tranquility"

    # Flip to False to run entirely against recorded fixtures (no network).
    esi_live: bool = False
    fixture_dir: str = "tests/fixtures"

    # Max concurrent in-flight ESI requests. ESI tolerates a fair amount of
    # concurrency but the error limit is shared across your whole app.
    esi_concurrency: int = 8
    esi_timeout: float = 30.0

    # --- SSO (only needed for character-scoped endpoints) ------------------
    client_id: str = ""
    client_secret: str = ""
    callback_url: str = "http://localhost:8000/callback"

    # --- Infrastructure ----------------------------------------------------
    database_url: str = "postgresql://eve:eve@localhost:5432/eve_market"
    redis_url: str = "redis://localhost:6379/0"

    # --- Trading defaults --------------------------------------------------
    # Base sales tax is 8%, reduced 11% per level of Accounting (3.6% at V).
    sales_tax: float = 0.036
    # Base broker fee is 3%, reduced by Broker Relations and standings.
    broker_fee: float = 0.015

    # --- Source and destination hubs ---------------------------------------
    source_region_id: int = 10000002  # The Forge
    source_station_id: int = 60003760  # Jita IV-4

    # Destination: the market you're stocking. These default to Ahbazon, but
    # the ids are NOT verified — run `eve-market resolve Ahbazon` on a
    # networked machine and it will write the confirmed values into .env.
    dest_name: str = "Ahbazon"
    dest_region_id: int = 10000067  # Genesis — verify
    dest_system_id: int = 0  # 0 means "not yet resolved"
    dest_station_id: int = 0  # optional; 0 means "any station in the system"
    # Set by `resolve` from the system's real security status. Drives the
    # default risk model, so a wrong value here quietly distorts every margin.
    dest_is_lowsec: bool = True

    # --- Logistics ---------------------------------------------------------
    ship: str = "dst"  # blockade_runner | dst | freighter
    cargo_m3: float = 0.0  # 0 means "use the ship's default capacity"
    self_hauling: bool = True
    haul_cost_per_m3: float | None = None  # None means derive from the route
    haul_risk_pct: float | None = None  # None means derive from ship and route

    # --- Pricing behaviour -------------------------------------------------
    # Standard undercut. EVE prices to 0.01 ISK.
    undercut_isk: float = 0.01
    # Markup used only when nobody else is selling and you set the price.
    greenfield_markup: float = 0.35
    # How many days of destination turnover to stock, and the share of that
    # turnover you assume you win against other sellers.
    days_of_stock: float = 7.0
    capture_rate: float = 0.25

    @property
    def user_agent(self) -> str:
        contact = self.contact_email or "no-contact-configured"
        return f"{self.app_name}/0.1.0 ({contact})"


settings = Settings()
