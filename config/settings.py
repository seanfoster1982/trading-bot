"""
Settings loaded from .env via pydantic-settings.

Single source of truth for config. Strategies and exchanges read from here,
never directly from os.environ. Never log a Settings instance — it contains
secrets. Use settings.public_dict() for any debug output.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Mode ---------------------------------------------------------------
    live_trading_enabled: bool = False
    environment: Literal["development", "staging", "production"] = "development"

    # --- Polymarket ---------------------------------------------------------
    polymarket_private_key: SecretStr = SecretStr("")
    polymarket_funder_address: str = ""
    polymarket_signature_type: int = 1
    polymarket_host: str = "https://clob.polymarket.com"
    polymarket_gamma_host: str = "https://gamma-api.polymarket.com"
    polymarket_chain_id: int = 137

    # --- Solana -------------------------------------------------------------
    solana_private_key: SecretStr = SecretStr("")
    solana_rpc_url: str = "https://api.mainnet-beta.solana.com"
    jupiter_api_host: str = "https://quote-api.jup.ag/v6"

    # --- External data ------------------------------------------------------
    news_api_key: SecretStr = SecretStr("")
    odds_api_key: SecretStr = SecretStr("")
    kalshi_api_key: SecretStr = SecretStr("")
    kalshi_api_secret: SecretStr = SecretStr("")
    dune_api_key: SecretStr = SecretStr("")

    # --- LLM ----------------------------------------------------------------
    anthropic_api_key: SecretStr = SecretStr("")

    # --- DB -----------------------------------------------------------------
    # SQLite default for easy local dev. Override to Postgres in .env when
    # you want multi-process access or want to ship to a server.
    database_url: str = "sqlite:///./trading_bot.db"

    # --- Monitoring ---------------------------------------------------------
    discord_webhook_url: SecretStr = SecretStr("")
    log_level: str = "INFO"

    # --- Risk limits --------------------------------------------------------
    max_position_usd: float = 50.0
    max_total_exposure_usd: float = 500.0
    max_daily_loss_usd: float = 100.0
    max_open_positions: int = 20
    kill_on_daily_loss: bool = True

    def public_dict(self) -> dict:
        """Return non-sensitive settings for logging."""
        d = self.model_dump()
        # redact every SecretStr field
        for k, v in list(d.items()):
            if isinstance(v, SecretStr) or "key" in k or "secret" in k or "private" in k:
                d[k] = "***REDACTED***"
        return d


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
