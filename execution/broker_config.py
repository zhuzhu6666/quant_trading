"""Canonical effective cTrader connection configuration.

The live bridge and all execution-risk classification must consume the same
resolved values.  Environment variables have explicit precedence over YAML;
the process-level accessor resolves them once and returns an immutable value.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass
from typing import Any, Mapping


def _value(mapping: Mapping[str, str], key: str, fallback: Any = "") -> Any:
    raw = mapping.get(key)
    return fallback if raw is None or str(raw).strip() == "" else raw


def _env_name(section: dict[str, Any], key: str, default: str) -> str:
    value = str(section.get(key) or default).strip()
    return value or default


def _as_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off"}


@dataclass(frozen=True)
class BrokerConnectionConfig:
    client_id: str
    client_secret: str
    access_token: str
    account_id: int
    host: str
    port: int
    symbol: str
    request_timeout_sec: float
    proxy_url: str
    proxy_rdns: bool
    environment: str
    config_hash: str

    @property
    def is_demo(self) -> bool:
        return self.environment == "demo"

    @property
    def credentials_present(self) -> bool:
        return bool(self.client_id and self.client_secret and self.access_token and self.account_id > 0)

    def bridge_kwargs(self) -> dict[str, Any]:
        return {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "access_token": self.access_token,
            "account_id": self.account_id,
            "host": self.host,
            "port": self.port,
            "symbol": self.symbol,
            "request_timeout_sec": self.request_timeout_sec,
            "proxy_url": self.proxy_url,
            "proxy_rdns": self.proxy_rdns,
        }

    def to_safe_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "host": self.host,
            "port": self.port,
            "symbol": self.symbol,
            "request_timeout_sec": self.request_timeout_sec,
            "proxy_configured": bool(self.proxy_url),
            "proxy_rdns": self.proxy_rdns,
            "environment": self.environment,
            "credentials_present": self.credentials_present,
            "config_hash": self.config_hash,
        }

    @classmethod
    def from_sources(
        cls,
        settings: dict[str, Any] | None,
        environ: Mapping[str, str] | None = None,
    ) -> "BrokerConnectionConfig":
        source_env = os.environ if environ is None else environ
        ctrader = (settings or {}).get("ctrader") if isinstance(settings, dict) else {}
        ctrader = ctrader if isinstance(ctrader, dict) else {}

        client_id_env = _env_name(ctrader, "client_id_env", "CTRADER_CLIENT_ID")
        client_secret_env = _env_name(ctrader, "client_secret_env", "CTRADER_CLIENT_SECRET")
        access_token_env = _env_name(ctrader, "access_token_env", "CTRADER_ACCESS_TOKEN")
        account_id_env = _env_name(ctrader, "account_id_env", "CTRADER_ACCOUNT_ID")

        host = str(_value(source_env, "CTRADER_HOST", ctrader.get("host", "demo.ctraderapi.com"))).strip()
        host = host or "demo.ctraderapi.com"
        # Risk semantics may grant the bounded demo profile only to the
        # canonical cTrader demo endpoint.  Prefix matching (for example
        # ``demo.attacker.invalid``) can misclassify an arbitrary host as demo
        # and therefore apply the wrong account-risk contract.
        host_lower = host.lower().rstrip(".")
        if host_lower == "demo.ctraderapi.com":
            environment = "demo"
        elif host_lower:
            environment = "live"
        else:
            environment = "unknown"

        account_raw = _value(source_env, account_id_env, ctrader.get("account_id", 0))
        port_raw = _value(source_env, "CTRADER_PORT", ctrader.get("port", 5035))
        timeout_raw = _value(
            source_env,
            "CTRADER_REQUEST_TIMEOUT_SEC",
            ctrader.get("request_timeout_sec", 10),
        )
        proxy_rdns_raw = _value(source_env, "CTRADER_PROXY_RDNS", ctrader.get("proxy_rdns", True))
        safe_identity = {
            "account_id": int(account_raw or 0),
            "host": host,
            "port": int(port_raw or 5035),
            "symbol": str(_value(source_env, "CTRADER_SYMBOL", ctrader.get("symbol", "XAUUSD")) or "XAUUSD"),
            "request_timeout_sec": float(timeout_raw or 10),
            "proxy_url": str(_value(source_env, "CTRADER_PROXY_URL", ctrader.get("proxy_url", "")) or ""),
            "proxy_rdns": _as_bool(proxy_rdns_raw),
            "environment": environment,
        }
        config_hash = hashlib.sha256(
            json.dumps(safe_identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return cls(
            client_id=str(_value(source_env, client_id_env, ctrader.get("client_id", "")) or ""),
            client_secret=str(_value(source_env, client_secret_env, ctrader.get("client_secret", "")) or ""),
            access_token=str(_value(source_env, access_token_env, ctrader.get("access_token", "")) or ""),
            config_hash=config_hash,
            **safe_identity,
        )


_SHARED_LOCK = threading.Lock()
_SHARED_CONFIG: BrokerConnectionConfig | None = None


def shared_broker_connection_config() -> BrokerConnectionConfig:
    """Return the process-wide immutable effective broker configuration."""
    global _SHARED_CONFIG
    with _SHARED_LOCK:
        if _SHARED_CONFIG is None:
            try:
                from execution._env import load_env

                load_env()
            except Exception:
                # Source resolution still works from YAML when no .env loader
                # is available (for example isolated test environments).
                pass
            from config import load_config

            _SHARED_CONFIG = BrokerConnectionConfig.from_sources(load_config())
        return _SHARED_CONFIG


def reset_broker_connection_config_for_tests() -> None:
    global _SHARED_CONFIG
    with _SHARED_LOCK:
        _SHARED_CONFIG = None
