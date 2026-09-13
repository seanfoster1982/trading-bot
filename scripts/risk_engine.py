"""Deterministic risk engine — LLMs cannot override a veto.

Pure functions, no I/O. rh_live_trader.py is the only caller that may
submit a transaction after ALLOW.
"""
from __future__ import annotations

from dataclasses import dataclass

RH_CHAIN_ID = 4663


@dataclass(frozen=True)
class RiskVerdict:
    allow: bool
    code: str
    detail: str = ""

    @property
    def action(self) -> str:
        return "ALLOW" if self.allow else "VETO"


def veto(code: str, detail: str = "") -> RiskVerdict:
    return RiskVerdict(False, code, detail)


def allow(code: str = "ok") -> RiskVerdict:
    return RiskVerdict(True, code, "")


def evaluate_rh_buy(
    *,
    live_enabled: bool,
    halted: bool,
    chain_id: int,
    trade_usd: float,
    budget_left: float,
    open_count: int,
    max_open: int,
    realized_pnl: float,
    max_realized_loss: float,
    seconds_since_last_buy: float | None,
    min_seconds_between_buys: float,
    security_blocked: bool,
    security_reason: str = "",
) -> RiskVerdict:
    if not live_enabled:
        return veto("strategy_disabled")
    if halted:
        return veto("emergency_stop")
    if chain_id != RH_CHAIN_ID:
        return veto("chain_not_allowed", str(chain_id))
    if security_blocked:
        return veto("security_block", security_reason or "blocked")
    if trade_usd <= 0:
        return veto("invalid_size")
    if budget_left + 1e-9 < trade_usd:
        return veto("budget_exhausted", f"{budget_left:.2f}")
    if open_count >= max_open:
        return veto("max_open", str(open_count))
    if realized_pnl <= -abs(max_realized_loss):
        return veto("loss_limit", f"{realized_pnl:+.2f}")
    if seconds_since_last_buy is not None and seconds_since_last_buy < min_seconds_between_buys:
        return veto("cooldown", f"{seconds_since_last_buy:.0f}s")
    return allow()
