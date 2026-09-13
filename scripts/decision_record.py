"""Formal DecisionRecord for Robinhood live evaluations.

Decisions are audit facts. Execution (tx hash) is separate and lives in
rh_live_trades. An invalid DecisionRecord must never reach the executor.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

RH_CHAIN_ID = 4663
STRATEGY_NAME = "rh_live"
STRATEGY_VERSION = "rh_live_v1"


class Decision(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"
    NO_TRADE = "NO_TRADE"
    BLOCKED = "BLOCKED"


class DecisionRecord(BaseModel):
    decision_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    evaluation_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: int = Field(
        default_factory=lambda: int(datetime.now(tz=timezone.utc).timestamp())
    )
    strategy: str = STRATEGY_NAME
    strategy_version: str = STRATEGY_VERSION
    asset: str = ""
    symbol: str = ""
    chain: str = "robinhood"
    chain_id: int = RH_CHAIN_ID
    decision: Decision
    confidence: Optional[float] = None
    market_price: Optional[float] = None
    proposed_notional_usd: Optional[float] = None
    sources: list[str] = Field(default_factory=list)
    source_freshness: dict[str, Any] = Field(default_factory=dict)
    reason_codes: list[str] = Field(default_factory=list)
    security_status: str = "UNKNOWN"
    risk_status: str = "UNKNOWN"  # ALLOW | VETO | UNKNOWN
    risk_code: str = ""
    expires_at: Optional[int] = None
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("decision", mode="before")
    @classmethod
    def _decision_enum(cls, v: Any) -> Any:
        if isinstance(v, Decision):
            return v
        if isinstance(v, str):
            key = v.strip().upper()
            if key not in Decision.__members__:
                raise ValueError(f"invalid decision: {v!r}")
            return Decision(key)
        raise ValueError(f"invalid decision: {v!r}")

    @field_validator("confidence")
    @classmethod
    def _confidence_range(cls, v: Optional[float]) -> Optional[float]:
        if v is None:
            return v
        if not (0.0 <= float(v) <= 1.0):
            raise ValueError("confidence must be between 0 and 1")
        return float(v)

    @field_validator("proposed_notional_usd")
    @classmethod
    def _notional_nonneg(cls, v: Optional[float]) -> Optional[float]:
        if v is None:
            return v
        if float(v) < 0:
            raise ValueError("proposed_notional_usd cannot be negative")
        return float(v)

    @field_validator("timestamp", "expires_at")
    @classmethod
    def _ts_ok(cls, v: Optional[int]) -> Optional[int]:
        if v is None:
            return v
        iv = int(v)
        if iv < 0:
            raise ValueError("timestamp cannot be negative")
        return iv

    @field_validator("reason_codes", mode="before")
    @classmethod
    def _reason_list(cls, v: Any) -> list[str]:
        if v is None:
            return []
        if isinstance(v, str):
            if len(v) > 200:
                raise ValueError(
                    "reason_codes must be structured list values, not prose"
                )
            return [v]
        if isinstance(v, (list, tuple)):
            out: list[str] = []
            for item in v:
                s = str(item).strip()
                if not s:
                    continue
                if len(s) > 120:
                    raise ValueError("reason_codes entries must be short codes")
                out.append(s)
            return out
        raise ValueError("reason_codes must be a list of short codes")

    @model_validator(mode="after")
    def _executable_buy_rules(self) -> "DecisionRecord":
        if self.decision == Decision.BUY:
            if self.chain_id != RH_CHAIN_ID:
                raise ValueError("BUY on wrong chain_id for Robinhood live")
            if self.risk_status != "ALLOW":
                raise ValueError("BUY requires risk_status=ALLOW")
        return self

    def is_executable(self) -> bool:
        if self.decision not in (Decision.BUY, Decision.SELL):
            return False
        return self.risk_status == "ALLOW" and self.chain_id == RH_CHAIN_ID

    def to_storage(self) -> dict[str, Any]:
        d = self.model_dump()
        d["decision"] = self.decision.value
        d["sources_json"] = json.dumps(self.sources, separators=(",", ":"))
        d["source_freshness_json"] = json.dumps(
            self.source_freshness, separators=(",", ":")
        )
        d["reason_codes_json"] = json.dumps(
            self.reason_codes, separators=(",", ":")
        )
        d["extra_json"] = json.dumps(self.extra, separators=(",", ":"))
        return d


def make_decision(**kwargs: Any) -> DecisionRecord:
    return DecisionRecord(**kwargs)
