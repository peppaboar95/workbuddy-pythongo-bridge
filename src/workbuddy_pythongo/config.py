import ipaddress
import json
import math
import os
from dataclasses import dataclass, field

from .errors import BridgeError
from .modes import normalize_mode
from .util import normalize_instrument, safe_relative_name


TOP_LEVEL_FIELDS = {
    "data_dir", "host", "port", "default_mode", "max_message_bytes",
    "key_file", "worker_token_file", "accounts",
}
ACCOUNT_FIELDS = {
    "alias", "account_type", "adapter_instance", "enabled",
    "investor_fingerprint", "instrument_allowlist", "close_policy", "risk_limits",
}
BASE_RISK_FIELDS = {
    "max_order_volume", "max_order_notional",
    "max_position_volume_per_instrument", "max_total_position_volume",
    "max_margin_per_order", "max_total_margin", "max_risk_ratio",
    "max_daily_orders", "max_daily_cancels", "max_daily_loss",
    "max_snapshot_age_seconds", "max_quote_age_seconds",
    "preview_ttl_seconds", "command_ttl_seconds",
}
AUTO_RISK_DEFAULTS = {
    "max_auto_authorization_minutes": 720,
    "max_auto_session_notional": 1000000.0,
    "max_auto_orders": 100,
    "min_auto_order_interval_seconds": 1,
    "max_auto_concurrent_orders": 2,
    "max_auto_instrument_position_notional": 1000000.0,
    "max_auto_account_drawdown": 20000.0,
    "auto_heartbeat_max_age_seconds": 15,
    "auto_max_queue_depth": 20,
}
RISK_FIELDS = BASE_RISK_FIELDS | set(AUTO_RISK_DEFAULTS)
INTEGER_RISK_FIELDS = {
    "max_order_volume", "max_position_volume_per_instrument",
    "max_total_position_volume", "max_daily_orders", "max_daily_cancels",
    "max_snapshot_age_seconds", "max_quote_age_seconds",
    "preview_ttl_seconds", "command_ttl_seconds",
    "max_auto_authorization_minutes", "max_auto_orders",
    "min_auto_order_interval_seconds", "max_auto_concurrent_orders",
    "auto_heartbeat_max_age_seconds", "auto_max_queue_depth",
}
CLOSE_POLICIES = {"TODAY_FIRST", "YESTERDAY_FIRST", "EXPLICIT_ONLY"}


@dataclass(frozen=True)
class RiskLimits:
    max_order_volume: int
    max_order_notional: float
    max_position_volume_per_instrument: int
    max_total_position_volume: int
    max_margin_per_order: float
    max_total_margin: float
    max_risk_ratio: float
    max_daily_orders: int
    max_daily_cancels: int
    max_daily_loss: float
    max_snapshot_age_seconds: int
    max_quote_age_seconds: int
    preview_ttl_seconds: int
    command_ttl_seconds: int
    max_auto_authorization_minutes: int
    max_auto_session_notional: float
    max_auto_orders: int
    min_auto_order_interval_seconds: int
    max_auto_concurrent_orders: int
    max_auto_instrument_position_notional: float
    max_auto_account_drawdown: float
    auto_heartbeat_max_age_seconds: int
    auto_max_queue_depth: int


@dataclass(frozen=True)
class AccountConfig:
    alias: str
    account_type: str
    adapter_instance: str
    enabled: bool
    investor_fingerprint: str
    instrument_allowlist: tuple
    close_policy: str
    risk_limits: RiskLimits

    def instrument_allowed(self, exchange, instrument_id):
        if not self.instrument_allowlist:
            return True
        return (exchange, instrument_id) in self.instrument_allowlist


@dataclass(frozen=True)
class BridgeConfig:
    path: str
    data_dir: str
    host: str
    port: int
    default_mode: str
    max_message_bytes: int
    key_file: str
    worker_token_file: str
    accounts: dict = field(default_factory=dict)

    def account(self, alias):
        account = self.accounts.get(alias)
        if not account or not account.enabled:
            raise BridgeError("ACCOUNT_NOT_FOUND", "unknown or disabled account alias")
        return account


def _reject_json_constant(value):
    raise ValueError("non-finite JSON number is forbidden: %s" % value)


def _absolute(base, value, name):
    if not isinstance(value, str) or not value.strip():
        raise BridgeError("CONFIG_ERROR", "%s must be a non-empty path" % name)
    return os.path.abspath(value if os.path.isabs(value) else os.path.join(base, value))


def _integer(value, name, minimum=1, maximum=2147483647):
    if isinstance(value, bool) or not isinstance(value, int):
        raise BridgeError("CONFIG_ERROR", "%s must be an integer" % name)
    if value < minimum or value > maximum:
        raise BridgeError("CONFIG_ERROR", "%s is outside the allowed range" % name)
    return value


def _number(value, name, minimum=0.000001, maximum=1e15):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BridgeError("CONFIG_ERROR", "%s must be a finite number" % name)
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < minimum or parsed > maximum:
        raise BridgeError("CONFIG_ERROR", "%s is outside the allowed range" % name)
    return parsed


def _risk_limits(raw, alias):
    if not isinstance(raw, dict) or BASE_RISK_FIELDS - set(raw) or set(raw) - RISK_FIELDS:
        missing = sorted(BASE_RISK_FIELDS - set(raw or {})) if isinstance(raw, dict) else sorted(BASE_RISK_FIELDS)
        unknown = sorted(set(raw or {}) - RISK_FIELDS) if isinstance(raw, dict) else []
        raise BridgeError("CONFIG_ERROR", "risk_limits must contain all required fields and only supported P1 fields", {"account_alias": alias, "missing": missing, "unknown": unknown})
    values = {}
    for name in sorted(RISK_FIELDS):
        full = "%s.risk_limits.%s" % (alias, name)
        value = raw[name] if name in raw else AUTO_RISK_DEFAULTS[name]
        if name in INTEGER_RISK_FIELDS:
            if name == "max_auto_authorization_minutes":
                maximum = 720
            elif name == "auto_heartbeat_max_age_seconds":
                maximum = 60
            elif name.endswith("_seconds"):
                maximum = 86400
            elif name == "auto_max_queue_depth":
                maximum = 10000
            else:
                maximum = 2147483647
            minimum = 5 if name == "auto_heartbeat_max_age_seconds" else 1
            values[name] = _integer(value, full, minimum, maximum)
        elif name == "max_risk_ratio":
            values[name] = _number(value, full, 0.000001, 100.0)
        else:
            values[name] = _number(value, full)
    return RiskLimits(**values)


def load_config(path=None):
    path = os.path.abspath(path or os.environ.get("WORKBUDDY_PYTHONGO_CONFIG", "config/bridge.json"))
    try:
        with open(path, "r", encoding="utf-8") as stream:
            raw = json.load(stream, parse_constant=_reject_json_constant)
    except (OSError, ValueError) as exc:
        raise BridgeError("CONFIG_ERROR", "cannot load bridge config: %s" % exc)
    if not isinstance(raw, dict) or set(raw) - TOP_LEVEL_FIELDS:
        raise BridgeError("CONFIG_ERROR", "bridge config contains unknown fields")
    accounts_raw = raw.get("accounts")
    if not isinstance(accounts_raw, list) or not accounts_raw:
        raise BridgeError("CONFIG_ERROR", "accounts must be a non-empty array")
    base = os.path.dirname(path)
    host = raw.get("host", "127.0.0.1")
    try:
        if not isinstance(host, str) or not ipaddress.ip_address(host).is_loopback:
            raise ValueError
    except ValueError:
        raise BridgeError("CONFIG_ERROR", "host must be a numeric loopback address")
    try:
        mode = normalize_mode(raw.get("default_mode", "OBSERVE_ONLY"), allow_legacy=True)
    except ValueError:
        raise BridgeError("CONFIG_ERROR", "invalid default_mode")
    accounts = {}
    adapters = set()
    for item in accounts_raw:
        if not isinstance(item, dict) or set(item) != ACCOUNT_FIELDS:
            raise BridgeError("CONFIG_ERROR", "each account must contain the exact v1 fields")
        alias = item.get("alias")
        adapter = item.get("adapter_instance")
        if not safe_relative_name(alias) or not safe_relative_name(adapter):
            raise BridgeError("CONFIG_ERROR", "unsafe account alias or adapter_instance")
        if alias in accounts or adapter in adapters:
            raise BridgeError("CONFIG_ERROR", "duplicate account alias or adapter_instance")
        if item.get("account_type") != "FUTURES":
            raise BridgeError("CONFIG_ERROR", "account_type must be FUTURES")
        if not isinstance(item.get("enabled"), bool):
            raise BridgeError("CONFIG_ERROR", "enabled must be a boolean")
        fingerprint = item.get("investor_fingerprint")
        if not isinstance(fingerprint, str) or not fingerprint.strip():
            raise BridgeError("CONFIG_ERROR", "investor_fingerprint must be non-empty")
        allowlist_raw = item.get("instrument_allowlist")
        if not isinstance(allowlist_raw, list):
            raise BridgeError("CONFIG_ERROR", "instrument_allowlist must be an array")
        allowlist = []
        for instrument in allowlist_raw:
            if not isinstance(instrument, dict) or set(instrument) != {"exchange", "instrument_id"}:
                raise BridgeError("CONFIG_ERROR", "invalid instrument allowlist entry")
            try:
                normalized = normalize_instrument(instrument["exchange"], instrument["instrument_id"])
            except ValueError as exc:
                raise BridgeError("CONFIG_ERROR", str(exc))
            if normalized in allowlist:
                raise BridgeError("CONFIG_ERROR", "duplicate instrument allowlist entry")
            allowlist.append(normalized)
        close_policy = item.get("close_policy")
        if close_policy not in CLOSE_POLICIES:
            raise BridgeError("CONFIG_ERROR", "invalid close_policy")
        account = AccountConfig(
            alias=alias,
            account_type="FUTURES",
            adapter_instance=adapter,
            enabled=item["enabled"],
            investor_fingerprint=fingerprint,
            instrument_allowlist=tuple(allowlist),
            close_policy=close_policy,
            risk_limits=_risk_limits(item.get("risk_limits"), alias),
        )
        accounts[alias] = account
        adapters.add(adapter)
    return BridgeConfig(
        path=path,
        data_dir=_absolute(base, raw.get("data_dir", "../data"), "data_dir"),
        host=host,
        port=_integer(raw.get("port", 17662), "port", 1, 65535),
        default_mode=mode,
        max_message_bytes=_integer(raw.get("max_message_bytes", 65536), "max_message_bytes", 1024, 16777216),
        key_file=_absolute(base, raw.get("key_file", "../data/secrets/message_keys.json"), "key_file"),
        worker_token_file=_absolute(base, raw.get("worker_token_file", "../data/secrets/worker.token"), "worker_token_file"),
        accounts=accounts,
    )
