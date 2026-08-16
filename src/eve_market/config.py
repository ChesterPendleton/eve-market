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

    @property
    def user_agent(self) -> str:
        contact = self.contact_email or "no-contact-configured"
        return f"{self.app_name}/0.1.0 ({contact})"


settings = Settings()
