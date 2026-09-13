"""GoPlus Security adapter — Robinhood Chain (4663) token security, BUY-only.

This module cannot trade. It has no private key and signs no blockchain
transactions. It talks to GoPlus's standard (free) REST API via httpx.

Authentication (official Access Token API):
  POST https://api.gopluslabs.io/api/v1/token
  JSON {app_key, time, sign} where
  sign = sha1(app_key + time + app_secret)  (hex)
  time is unix seconds, must be within ±1000s of GoPlus's clock.
  Cache result.access_token until result.expires_in.

Token Security:
  GET https://api.gopluslabs.io/api/v1/token_security/{chain_id}
      ?contract_addresses={address}
  Authorization: Bearer <access_token>
  Robinhood Chain candidates MUST use chain_id 4663
  (GoPlus chain table: id 4663 = Robinhood).

Never log: GOPLUS_APP_SECRET, the SHA1 signature, or the access token.

Hard-block fields (value "1" only; documented GoPlus Token Security semantics):
  is_honeypot
      "HoneyPot" means the token maybe cannot be sold because of the token
      contract's function, or the token contains malicious code.
      Notice: "High risk, definitely scam."
  cannot_sell
      B20 nested object status, or a top-level "1" if present.
      Documented: cannot_sell permission enabled — token cannot be sold.
  sell_tax == "1"
      Documented: "sell-tax is 100% or this token cannot be sold."
      Exception: when trading_cooldown is also "1", GoPlus documents that the
      sandbox may return sell_tax "1" as an artifact — treat as WARNING then,
      not a hard block.
  cannot_buy == "1"
      Documented: token cannot be bought. A NEW BUY cannot complete safely.
  owner_change_balance == "1"
      Documented: owner can change any holder's balance, including to zero,
      or perform massive mint-and-sell.

Not hard-blocks (WARNING / informational only, per current docs):
  is_proxy, is_open_source==0, owner exists, is_mintable, hidden_owner,
  transfer_pausable, is_blacklisted, selfdestruct, can_take_back_ownership,
  slippage_modifiable, personal_slippage_modifiable, cannot_sell_all,
  trading_cooldown, gas_abuse, young token, high-but-not-100% tax.

Partial / incomplete:
  A 200 + code==1 response that is missing the contract payload, or that
  omits is_honeypot (the documented honeypot determination) without an
  explicit "0"/"1", cannot be treated as PASS. Fail closed for NEW BUY
  (goplus_partial). Closed-source / proxy is not itself the veto reason;
  missing critical determination is.

GOPLUS_ENABLED=false: zero HTTP, status DISABLED.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

if sys.stdout is None or sys.stderr is None:
    _devnull = open(os.devnull, "w", encoding="utf-8")
    sys.stdout = sys.stdout or _devnull
    sys.stderr = sys.stderr or _devnull
else:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

load_dotenv(dotenv_path=ROOT / ".env")

import rh_chain as rh  # noqa: E402

LOG = logging.getLogger("goplus_security")

GOPLUS_BASE = "https://api.gopluslabs.io"
ACCESS_TOKEN_PATH = "/api/v1/token"
TOKEN_SECURITY_PATH = "/api/v1/token_security/{chain_id}"
RH_CHAIN_ID = 4663
CACHE_TTL_S = 12 * 60  # 12 minutes (within the 10–15 minute band)
TOKEN_SKEW_S = 60
HTTP_TIMEOUT_S = 15.0
# Robinhood WETH9 from rh_chain.py (Uniswap router). Smoke-test only.
SMOKE_WETH = rh.WETH

_BEARER_RE = re.compile(r"(?i)(bearer\s+)(\S+)")

STATUS_PASS = "PASS"
STATUS_WARN = "WARN"
STATUS_BLOCK = "BLOCK"
STATUS_UNAVAILABLE = "UNAVAILABLE"
STATUS_DISABLED = "DISABLED"

# Fields classified as hard-block when the documented flag is on ("1").
HARD_BLOCK_FIELDS = {
    "is_honeypot": "goplus_honeypot",
    "cannot_sell": "goplus_cannot_sell",
    "cannot_buy": "goplus_cannot_buy",
    "owner_change_balance": "goplus_balance_control",
}

WARNING_FIELDS = {
    "is_proxy": "goplus_proxy",
    "is_mintable": "goplus_mintable",
    "hidden_owner": "goplus_hidden_owner",
    "transfer_pausable": "goplus_transfer_pausable",
    "is_blacklisted": "goplus_blacklist",
    "selfdestruct": "goplus_selfdestruct",
    "can_take_back_ownership": "goplus_take_back_ownership",
    "slippage_modifiable": "goplus_tax_modifiable",
    "personal_slippage_modifiable": "goplus_personal_tax",
    "cannot_sell_all": "goplus_cannot_sell_all",
    "trading_cooldown": "goplus_trading_cooldown",
    "gas_abuse": "goplus_gas_abuse",
    "honeypot_with_same_creator": "goplus_honeypot_creator",
    "is_whitelisted": "goplus_whitelist",
    "anti_whale_modifiable": "goplus_anti_whale_modifiable",
    "is_anti_whale": "goplus_anti_whale",
}


class _RedactFilter(logging.Filter):
    """Strip bearer tokens and known secrets from log records."""

    def __init__(self) -> None:
        super().__init__()
        self._secrets: list[str] = []

    def set_secrets(self, secrets: list[str]) -> None:
        self._secrets = [s for s in secrets if s and len(s) >= 4]

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact_text(str(record.msg), self._secrets)
            if record.args:
                if isinstance(record.args, dict):
                    record.args = {
                        k: redact_text(str(v), self._secrets)
                        for k, v in record.args.items()
                    }
                else:
                    record.args = tuple(
                        redact_text(str(a), self._secrets) for a in record.args
                    )
        except Exception:
            record.msg = "[log redacted]"
            record.args = ()
        return True


_REDACT_FILTER = _RedactFilter()
LOG.addFilter(_REDACT_FILTER)


def redact_text(text: str, extra_secrets: list[str] | None = None) -> str:
    out = _BEARER_RE.sub(r"\1[redacted]", text)
    for secret in extra_secrets or []:
        if secret and secret in out:
            out = out.replace(secret, "[redacted]")
    return out


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _truthy_flag(value: Any) -> Optional[bool]:
    """Map GoPlus 0/1 (string, int, or {status: ...}) to bool. None = unknown."""
    if value is None or value == "":
        return None
    if isinstance(value, dict):
        return _truthy_flag(value.get("status"))
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in ("1", "true", "yes"):
        return True
    if s in ("0", "false", "no"):
        return False
    return None


def sign_access_token_request(app_key: str, timestamp: int, app_secret: str) -> str:
    """Official sign: sha1(app_key + time + app_secret), hex digest.

    Documented example (GoPlus OpenAPI GetAccessTokenRequest):
      app_key=mBOMg20QW11BbtyH4Zh0 time=1647847498
      app_secret=V6aRfxlPJwN3ViJSIFSCdxPvneajuJsh
      sign=7293d385b9225b3c3f232b76ba97255d0e21063e
    """
    material = f"{app_key}{int(timestamp)}{app_secret}"
    return hashlib.sha1(material.encode("utf-8")).hexdigest()


def is_evm_address(value: str) -> bool:
    s = (value or "").strip()
    if not s.startswith("0x") and not s.startswith("0X"):
        return False
    body = s[2:]
    return len(body) == 40 and all(c in "0123456789abcdefABCDEF" for c in body)


@dataclass
class GoPlusConfig:
    enabled: bool
    app_key: str
    app_secret: str

    @property
    def configured(self) -> bool:
        return bool(self.app_key.strip() and self.app_secret.strip())

    def __repr__(self) -> str:
        return (
            f"GoPlusConfig(enabled={self.enabled}, "
            f"app_key_set={bool(self.app_key)}, "
            f"app_secret_set={bool(self.app_secret)})"
        )


def read_config() -> GoPlusConfig:
    return GoPlusConfig(
        enabled=_env_bool("GOPLUS_ENABLED", False),
        app_key=(os.getenv("GOPLUS_APP_KEY") or "").strip(),
        app_secret=(os.getenv("GOPLUS_APP_SECRET") or "").strip(),
    )


@dataclass
class GoPlusSecurityResult:
    provider: str = "goplus"
    chain_id: int = RH_CHAIN_ID
    contract_address: str = ""
    checked_at: int = 0
    status: str = STATUS_UNAVAILABLE
    hard_block: bool = False
    hard_block_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    raw_status_code: Optional[int] = None
    data_freshness: dict[str, Any] = field(default_factory=dict)
    error_code: str = ""
    reason_codes: list[str] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)

    def decision_security_status(self) -> str:
        if self.status == STATUS_PASS:
            return "PASS"
        if self.status == STATUS_WARN:
            return "WARN"
        if self.status == STATUS_BLOCK:
            return "BLOCK"
        if self.status == STATUS_DISABLED:
            return "UNKNOWN"
        return "UNKNOWN"

    def to_extra(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "chain_id": self.chain_id,
            "contract_address": self.contract_address,
            "checked_at": self.checked_at,
            "status": self.status,
            "hard_block": self.hard_block,
            "hard_block_reasons": list(self.hard_block_reasons),
            "warnings": list(self.warnings),
            "raw_status_code": self.raw_status_code,
            "data_freshness": dict(self.data_freshness),
            "error_code": self.error_code,
            "reason_codes": list(self.reason_codes),
            "payload": dict(self.payload),
        }

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "chain_id": self.chain_id,
            "contract_address": self.contract_address,
            "checked_at": self.checked_at,
            "status": self.status,
            "hard_block": self.hard_block,
            "hard_block_reasons": list(self.hard_block_reasons),
            "warnings": list(self.warnings),
            "raw_status_code": self.raw_status_code,
            "data_freshness": dict(self.data_freshness),
            "error_code": self.error_code,
            "reason_codes": list(self.reason_codes),
        }


def blocks_new_buy(result: GoPlusSecurityResult) -> bool:
    """Fail-closed for NEW BUY when enabled and not a clean PASS/WARN."""
    if result.status == STATUS_DISABLED:
        return False
    if result.hard_block or result.status == STATUS_BLOCK:
        return True
    if result.status == STATUS_UNAVAILABLE:
        return True
    return False


def _freshness(*, now: float, checked_at: float, from_cache: bool) -> dict[str, Any]:
    age = max(0.0, float(now) - float(checked_at))
    return {
        "cache_age_s": round(age, 3),
        "ttl_s": CACHE_TTL_S,
        "from_cache": from_cache,
        "stale": age > CACHE_TTL_S,
    }


def _result(
    *,
    chain_id: int,
    contract_address: str,
    now: float,
    status: str,
    hard_block: bool = False,
    hard_block_reasons: list[str] | None = None,
    warnings: list[str] | None = None,
    raw_status_code: int | None = None,
    error_code: str = "",
    reason_codes: list[str] | None = None,
    payload: dict[str, Any] | None = None,
    from_cache: bool = False,
    checked_at: float | None = None,
) -> GoPlusSecurityResult:
    checked = int(checked_at if checked_at is not None else now)
    codes = list(reason_codes or [])
    return GoPlusSecurityResult(
        chain_id=chain_id,
        contract_address=contract_address,
        checked_at=checked,
        status=status,
        hard_block=hard_block,
        hard_block_reasons=list(hard_block_reasons or []),
        warnings=list(warnings or []),
        raw_status_code=raw_status_code,
        data_freshness=_freshness(now=now, checked_at=checked, from_cache=from_cache),
        error_code=error_code,
        reason_codes=codes,
        payload=dict(payload or {}),
    )


def _sanitize_payload(raw: dict[str, Any]) -> dict[str, Any]:
    """Keep audit-useful security fields; drop anything that looks like auth."""
    blocked_keys = {
        "access_token",
        "authorization",
        "app_secret",
        "app_key",
        "sign",
        "signature",
        "token",
    }
    out: dict[str, Any] = {}
    for k, v in (raw or {}).items():
        lk = str(k).lower()
        if lk in blocked_keys:
            continue
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[k] = v
        elif isinstance(v, dict) and str(k).lower() in (
            "cannot_sell",
            "cannot_buy",
            "owner_change_balance",
            "b20_token",
        ):
            nested = {}
            for nk, nv in v.items():
                if str(nk).lower() in blocked_keys:
                    continue
                if isinstance(nv, (str, int, float, bool, list)) or nv is None:
                    nested[nk] = nv
            out[k] = nested
    return out


def _lookup_token_entry(result_map: Any, address: str) -> Optional[dict[str, Any]]:
    if not isinstance(result_map, dict):
        return None
    want = address.lower()
    for key, val in result_map.items():
        if str(key).lower() == want and isinstance(val, dict):
            return val
    return None


def _extract_flag(payload: dict[str, Any], name: str) -> Optional[bool]:
    if name in payload:
        return _truthy_flag(payload.get(name))
    b20 = payload.get("b20_token")
    if isinstance(b20, dict) and name in b20:
        return _truthy_flag(b20.get(name))
    return None


def normalize_token_payload(
    payload: dict[str, Any],
    *,
    chain_id: int,
    contract_address: str,
    now: float,
    raw_status_code: int,
    checked_at: float | None = None,
) -> GoPlusSecurityResult:
    """Turn a GoPlus token object into PASS / WARN / BLOCK / UNAVAILABLE."""
    hard_reasons: list[str] = []
    warnings: list[str] = []
    reason_codes: list[str] = []

    is_honeypot = _extract_flag(payload, "is_honeypot")
    cannot_sell = _extract_flag(payload, "cannot_sell")
    cannot_buy = _extract_flag(payload, "cannot_buy")
    owner_change = _extract_flag(payload, "owner_change_balance")
    trading_cooldown = _extract_flag(payload, "trading_cooldown")
    sell_tax_raw = payload.get("sell_tax")
    sell_tax_flag = _truthy_flag(sell_tax_raw) if sell_tax_raw not in (None, "") else None

    if is_honeypot is True:
        hard_reasons.append("is_honeypot")
        reason_codes.append("goplus_honeypot")
    if cannot_sell is True:
        hard_reasons.append("cannot_sell")
        reason_codes.append("goplus_cannot_sell")
    if cannot_buy is True:
        hard_reasons.append("cannot_buy")
        reason_codes.append("goplus_cannot_buy")
    if owner_change is True:
        hard_reasons.append("owner_change_balance")
        reason_codes.append("goplus_balance_control")
    # sell_tax "1" = 100% or cannot sell, unless documented cooldown artifact.
    if sell_tax_flag is True:
        if trading_cooldown is True:
            warnings.append("sell_tax")
            reason_codes.append("goplus_high_tax")
        else:
            hard_reasons.append("sell_tax")
            if "goplus_cannot_sell" not in reason_codes:
                reason_codes.append("goplus_cannot_sell")

    if _extract_flag(payload, "is_open_source") is False:
        warnings.append("is_open_source")
        reason_codes.append("goplus_not_open_source")
    for field_name, code in WARNING_FIELDS.items():
        if _extract_flag(payload, field_name) is True:
            warnings.append(field_name)
            if code not in reason_codes:
                reason_codes.append(code)

    # Non-100% tax still informational.
    for tax_key in ("buy_tax", "sell_tax", "transfer_tax"):
        raw = payload.get(tax_key)
        if raw in (None, "", "0", 0):
            continue
        if _truthy_flag(raw) is True:
            continue  # already classified
        try:
            rate = float(raw)
        except (TypeError, ValueError):
            continue
        if rate > 0.10:
            warnings.append(tax_key)
            if "goplus_high_tax" not in reason_codes:
                reason_codes.append("goplus_high_tax")

    sanitized = _sanitize_payload(payload)

    if hard_reasons:
        uniq = list(dict.fromkeys(reason_codes))
        return _result(
            chain_id=chain_id,
            contract_address=contract_address,
            now=now,
            status=STATUS_BLOCK,
            hard_block=True,
            hard_block_reasons=hard_reasons,
            warnings=warnings,
            raw_status_code=raw_status_code,
            reason_codes=uniq,
            payload=sanitized,
            checked_at=checked_at,
        )

    # Incomplete determination: missing honeypot field cannot be a silent PASS.
    if is_honeypot is None:
        return _result(
            chain_id=chain_id,
            contract_address=contract_address,
            now=now,
            status=STATUS_UNAVAILABLE,
            raw_status_code=raw_status_code,
            error_code="goplus_partial",
            reason_codes=["goplus_partial"],
            warnings=warnings,
            payload=sanitized,
            checked_at=checked_at,
        )

    uniq = list(dict.fromkeys(reason_codes))
    if warnings:
        if "goplus_warn" not in uniq:
            uniq.insert(0, "goplus_warn")
        return _result(
            chain_id=chain_id,
            contract_address=contract_address,
            now=now,
            status=STATUS_WARN,
            warnings=warnings,
            raw_status_code=raw_status_code,
            reason_codes=uniq,
            payload=sanitized,
            checked_at=checked_at,
        )
    return _result(
        chain_id=chain_id,
        contract_address=contract_address,
        now=now,
        status=STATUS_PASS,
        raw_status_code=raw_status_code,
        reason_codes=["goplus_pass"],
        payload=sanitized,
        checked_at=checked_at,
    )


class GoPlusChecker:
    def __init__(
        self,
        *,
        config: GoPlusConfig | None = None,
        client: httpx.Client | None = None,
        now_fn: Callable[[], float] | None = None,
    ) -> None:
        self._config_override = config
        self._client = client
        self._now = now_fn or time.time
        self._access_token: str | None = None
        self._access_expires_at: float = 0.0
        self._result_cache: dict[tuple[int, str], tuple[float, GoPlusSecurityResult]] = {}

    def config(self) -> GoPlusConfig:
        return self._config_override if self._config_override is not None else read_config()

    def _secrets(self) -> list[str]:
        cfg = self.config()
        secrets = [cfg.app_secret, cfg.app_key, self._access_token or ""]
        _REDACT_FILTER.set_secrets(secrets)
        return [s for s in secrets if s]

    def _http(self) -> httpx.Client:
        if self._client is not None:
            return self._client
        return httpx.Client(timeout=HTTP_TIMEOUT_S)

    def _owns_client(self) -> bool:
        return self._client is None

    def check_token_security(
        self, chain_id: int, contract_address: str
    ) -> GoPlusSecurityResult:
        now = float(self._now())
        try:
            return self._check(int(chain_id), contract_address, now)
        except Exception as exc:  # noqa: BLE001 — live cycle must not see this
            LOG.warning(
                "goplus check failed: %s",
                redact_text(f"{type(exc).__name__}", self._secrets()),
            )
            addr = contract_address if is_evm_address(contract_address) else ""
            return _result(
                chain_id=int(chain_id) if isinstance(chain_id, int) else RH_CHAIN_ID,
                contract_address=addr,
                now=now,
                status=STATUS_UNAVAILABLE,
                error_code="goplus_unavailable",
                reason_codes=["goplus_unavailable"],
            )

    def _check(
        self, chain_id: int, contract_address: str, now: float
    ) -> GoPlusSecurityResult:
        cfg = self.config()
        if not cfg.enabled:
            return _result(
                chain_id=chain_id,
                contract_address=self._safe_addr(contract_address),
                now=now,
                status=STATUS_DISABLED,
                reason_codes=["goplus_disabled"],
            )
        if not cfg.configured:
            return _result(
                chain_id=chain_id,
                contract_address=self._safe_addr(contract_address),
                now=now,
                status=STATUS_UNAVAILABLE,
                error_code="goplus_auth_failed",
                reason_codes=["goplus_auth_failed"],
            )
        if not is_evm_address(contract_address):
            return _result(
                chain_id=chain_id,
                contract_address="",
                now=now,
                status=STATUS_UNAVAILABLE,
                error_code="goplus_invalid_address",
                reason_codes=["goplus_invalid_address"],
            )
        try:
            checksum = rh.checksum(contract_address)
        except Exception:
            return _result(
                chain_id=chain_id,
                contract_address="",
                now=now,
                status=STATUS_UNAVAILABLE,
                error_code="goplus_invalid_address",
                reason_codes=["goplus_invalid_address"],
            )
        cache_key = (int(chain_id), checksum.lower())
        cached = self._result_cache.get(cache_key)
        if cached is not None:
            stored_at, stored = cached
            if now - stored_at < CACHE_TTL_S:
                return _result(
                    chain_id=stored.chain_id,
                    contract_address=stored.contract_address,
                    now=now,
                    status=stored.status,
                    hard_block=stored.hard_block,
                    hard_block_reasons=stored.hard_block_reasons,
                    warnings=stored.warnings,
                    raw_status_code=stored.raw_status_code,
                    error_code=stored.error_code,
                    reason_codes=stored.reason_codes,
                    payload=stored.payload,
                    from_cache=True,
                    checked_at=stored.checked_at,
                )

        own = self._owns_client()
        client = self._http()
        try:
            token = self._ensure_access_token(client, cfg, now)
            if token is None:
                return _result(
                    chain_id=chain_id,
                    contract_address=checksum,
                    now=now,
                    status=STATUS_UNAVAILABLE,
                    error_code="goplus_auth_failed",
                    reason_codes=["goplus_auth_failed"],
                    raw_status_code=self._last_status,
                )
            fetched = self._fetch_token_security(
                client, token, chain_id, checksum, now
            )
            cacheable = fetched.status in (
                STATUS_PASS,
                STATUS_WARN,
                STATUS_BLOCK,
            ) or fetched.error_code == "goplus_partial"
            if cacheable:
                self._result_cache[cache_key] = (now, fetched)
            return fetched
        finally:
            if own:
                try:
                    client.close()
                except Exception:
                    pass

    _last_status: int | None = None

    def _ensure_access_token(
        self, client: httpx.Client, cfg: GoPlusConfig, now: float
    ) -> str | None:
        if self._access_token and now < self._access_expires_at:
            return self._access_token
        ts = int(now)
        sign = sign_access_token_request(cfg.app_key, ts, cfg.app_secret)
        _REDACT_FILTER.set_secrets(
            [cfg.app_secret, cfg.app_key, sign, self._access_token or ""]
        )
        url = GOPLUS_BASE + ACCESS_TOKEN_PATH
        try:
            resp = client.post(
                url,
                json={"app_key": cfg.app_key, "time": ts, "sign": sign},
                timeout=HTTP_TIMEOUT_S,
            )
        except httpx.TimeoutException:
            self._last_status = None
            LOG.warning("goplus access-token timeout")
            return None
        except httpx.HTTPError:
            self._last_status = None
            LOG.warning("goplus access-token http error")
            return None
        self._last_status = resp.status_code
        if resp.status_code in (401, 403):
            LOG.warning("goplus access-token auth rejected (%s)", resp.status_code)
            return None
        if resp.status_code == 429:
            LOG.warning("goplus access-token rate limited")
            return None
        if resp.status_code >= 500:
            LOG.warning("goplus access-token server error (%s)", resp.status_code)
            return None
        try:
            body = resp.json()
        except Exception:
            LOG.warning("goplus access-token malformed json")
            return None
        if not isinstance(body, dict) or int(body.get("code") or 0) != 1:
            LOG.warning("goplus access-token rejected")
            return None
        result = body.get("result") or {}
        if not isinstance(result, dict):
            return None
        token = result.get("access_token")
        if not token or not isinstance(token, str):
            LOG.warning("goplus access-token missing")
            return None
        try:
            expires_in = int(result.get("expires_in") or 3600)
        except (TypeError, ValueError):
            expires_in = 3600
        self._access_token = token
        self._access_expires_at = now + max(30, expires_in - TOKEN_SKEW_S)
        _REDACT_FILTER.set_secrets([cfg.app_secret, token])
        return token

    def _fetch_token_security(
        self,
        client: httpx.Client,
        access_token: str,
        chain_id: int,
        checksum: str,
        now: float,
    ) -> GoPlusSecurityResult:
        url = GOPLUS_BASE + TOKEN_SECURITY_PATH.format(chain_id=int(chain_id))
        headers = {"Authorization": f"Bearer {access_token}"}
        try:
            resp = client.get(
                url,
                params={"contract_addresses": checksum.lower()},
                headers=headers,
                timeout=HTTP_TIMEOUT_S,
            )
        except httpx.TimeoutException:
            return _result(
                chain_id=chain_id,
                contract_address=checksum,
                now=now,
                status=STATUS_UNAVAILABLE,
                error_code="goplus_unavailable",
                reason_codes=["goplus_unavailable"],
            )
        except httpx.HTTPError:
            return _result(
                chain_id=chain_id,
                contract_address=checksum,
                now=now,
                status=STATUS_UNAVAILABLE,
                error_code="goplus_unavailable",
                reason_codes=["goplus_unavailable"],
            )
        status_code = resp.status_code
        if status_code in (401, 403):
            self._access_token = None
            return _result(
                chain_id=chain_id,
                contract_address=checksum,
                now=now,
                status=STATUS_UNAVAILABLE,
                raw_status_code=status_code,
                error_code="goplus_auth_failed",
                reason_codes=["goplus_auth_failed"],
            )
        if status_code == 429:
            return _result(
                chain_id=chain_id,
                contract_address=checksum,
                now=now,
                status=STATUS_UNAVAILABLE,
                raw_status_code=status_code,
                error_code="goplus_rate_limited",
                reason_codes=["goplus_rate_limited"],
            )
        if status_code >= 500:
            return _result(
                chain_id=chain_id,
                contract_address=checksum,
                now=now,
                status=STATUS_UNAVAILABLE,
                raw_status_code=status_code,
                error_code="goplus_unavailable",
                reason_codes=["goplus_unavailable"],
            )
        try:
            body = resp.json()
        except Exception:
            return _result(
                chain_id=chain_id,
                contract_address=checksum,
                now=now,
                status=STATUS_UNAVAILABLE,
                raw_status_code=status_code,
                error_code="goplus_malformed",
                reason_codes=["goplus_malformed"],
            )
        if not isinstance(body, dict):
            return _result(
                chain_id=chain_id,
                contract_address=checksum,
                now=now,
                status=STATUS_UNAVAILABLE,
                raw_status_code=status_code,
                error_code="goplus_malformed",
                reason_codes=["goplus_malformed"],
            )
        code = body.get("code")
        try:
            code_i = int(code)
        except (TypeError, ValueError):
            return _result(
                chain_id=chain_id,
                contract_address=checksum,
                now=now,
                status=STATUS_UNAVAILABLE,
                raw_status_code=status_code,
                error_code="goplus_malformed",
                reason_codes=["goplus_malformed"],
            )
        if code_i != 1:
            return _result(
                chain_id=chain_id,
                contract_address=checksum,
                now=now,
                status=STATUS_UNAVAILABLE,
                raw_status_code=status_code,
                error_code="goplus_unavailable",
                reason_codes=["goplus_unavailable"],
                payload={"message": str(body.get("message") or "")[:200]},
            )
        entry = _lookup_token_entry(body.get("result"), checksum)
        if not isinstance(entry, dict) or not entry:
            return _result(
                chain_id=chain_id,
                contract_address=checksum,
                now=now,
                status=STATUS_UNAVAILABLE,
                raw_status_code=status_code,
                error_code="goplus_partial",
                reason_codes=["goplus_partial"],
            )
        return normalize_token_payload(
            payload=entry,
            chain_id=chain_id,
            contract_address=checksum,
            now=now,
            raw_status_code=status_code,
        )

    @staticmethod
    def _safe_addr(contract_address: str) -> str:
        if is_evm_address(contract_address):
            try:
                return rh.checksum(contract_address)
            except Exception:
                return ""
        return ""


_CHECKER: GoPlusChecker | None = None


def get_checker() -> GoPlusChecker:
    global _CHECKER
    if _CHECKER is None:
        _CHECKER = GoPlusChecker()
    return _CHECKER


def reset_checker() -> None:
    global _CHECKER
    _CHECKER = None


def check_token_security(
    chain_id: int, contract_address: str
) -> GoPlusSecurityResult:
    return get_checker().check_token_security(chain_id, contract_address)


def smoke_test(address: str | None = None) -> int:
    """Non-trading API/auth validation. Never prints secrets or tokens."""
    cfg = read_config()
    if not cfg.configured:
        print("GoPlus smoke-test FAIL: GOPLUS_APP_KEY / GOPLUS_APP_SECRET missing")
        return 2
    addr = address or SMOKE_WETH
    checker = GoPlusChecker(
        config=GoPlusConfig(enabled=True, app_key=cfg.app_key, app_secret=cfg.app_secret)
    )
    result = checker.check_token_security(RH_CHAIN_ID, addr)
    public = result.to_public_dict()
    dumped = json.dumps(public, indent=2, sort_keys=True)
    secrets = [cfg.app_secret, checker._access_token or ""]
    print(redact_text(dumped, secrets))
    if result.status in (STATUS_UNAVAILABLE, STATUS_DISABLED):
        print(f"GoPlus smoke-test FAIL: status={result.status} error={result.error_code}")
        return 1
    print(
        f"GoPlus smoke-test OK: status={result.status} hard_block={str(result.hard_block).lower()}"
    )
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="GoPlus RH token security (no trading)")
    p.add_argument("--smoke-test", action="store_true")
    p.add_argument("--address", default="", help="optional contract for smoke-test")
    args = p.parse_args()
    if args.smoke_test:
        raise SystemExit(smoke_test(args.address or None))
    p.print_help()
    raise SystemExit(2)


if __name__ == "__main__":
    main()
