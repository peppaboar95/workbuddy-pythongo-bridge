"""Standalone PythonGO v2 Embedded Adapter.

The setup command copies this file as ``WorkBuddyPythonGOAdapter.py`` next to a
``pythongo_adapter.path`` locator. Runtime JSON and Profile files stay in the
generated ready directory. Keep this module standard-library-only.
"""

import base64
import csv
import datetime as dt
import hashlib
import hmac
import io
import json
import math
import os
import tempfile
import threading
import time
import uuid

try:
    from pythongo.base import BaseStrategy
    PYTHONGO_BASE_AVAILABLE = True
    PYTHONGO_BASE_IMPORT_ERROR = None
except ImportError as exc:  # Allows packaging and offline validation outside InfiniTrader.
    PYTHONGO_BASE_AVAILABLE = False
    PYTHONGO_BASE_IMPORT_ERROR = str(exc)

    class BaseStrategy(object):
        def __init__(self):
            self.trading = False

        def on_init(self):
            return None

        def on_start(self):
            self.trading = True

        def on_stop(self):
            self.trading = False

        def on_tick(self, tick):
            return None

        def on_contract_status(self, status):
            return None

        def on_order(self, order):
            return None

        def on_trade(self, trade, log=True):
            return None

        def on_cancel(self, order):
            return None

        def on_error(self, error):
            return None


try:
    from pythongo.core import KLineStyle, MarketCenter
except ImportError:  # K-line support is optional for account/position/order operation.
    MarketCenter = None
    KLineStyle = None

try:
    from pythongo import infini as PYTHONGO_INFINI
except ImportError:
    PYTHONGO_INFINI = None


RUN_MODES = ("OBSERVE_ONLY", "SIM_SIGNAL", "MANUAL_LIVE", "LIMITED_AUTO")
P0_ISOLATED_MARGIN_GUARD_RATIO = 0.20
P0_VALIDATION_NOTIONAL_CAP = 1000000.0
P0_VALIDATION_LEG_MAPPINGS = {
    "OPEN_LONG": ("BUY", "0"),
    "CLOSE_TODAY_LONG": ("SELL", "3"),
    "OPEN_SHORT": ("SELL", "0"),
    "CLOSE_TODAY_SHORT": ("BUY", "3"),
}
QUEUE_FOLDERS = ("commands", "command_acks", "events", "control", "control_acks", "archive", "dead_letter")
BASE_CONFIG_FIELDS = {
    "account_alias", "account_type", "adapter_instance", "investor_id",
    "investor_fingerprint", "data_dir", "key_file", "mapping_profile",
    "pythongo_mode", "strategy_name", "expected_profile_id",
    "expected_infinitrader_build", "expected_pythongo_build", "expected_broker_build",
    "max_batch", "max_message_bytes", "adapter_max_order_volume",
    "adapter_max_order_notional", "adapter_max_margin_per_order",
    "adapter_max_total_margin", "adapter_max_risk_ratio",
    "max_snapshot_age_seconds", "max_quote_age_seconds",
    "adapter_max_price_deviation_pct", "allow_cancel_while_halted",
    "command_dispatch_mode", "heartbeat_seconds",
}
AUTO_CONFIG_DEFAULTS = {
    "adapter_max_auto_session_notional": 1000000.0,
    "adapter_max_auto_orders": 100,
    "adapter_min_auto_order_interval_seconds": 1,
    "adapter_max_auto_concurrent_orders": 2,
    "adapter_max_auto_instrument_position_notional": 1000000.0,
    "adapter_max_auto_account_drawdown": 20000.0,
}
MARGIN_REFERENCE_DEFAULTS = {
    "margin_reference_file": "",
    "margin_reference_schema_version": 2,
    "margin_reference_refresh_max_age_hours": 36,
    "margin_reference_source_warn_age_hours": 168,
    "margin_reference_safety_multiplier": 1.25,
    "margin_reference_require_signature": True,
}
MARGIN_POLICY_MATERIAL_FIELDS = (
    "margin_reference_file",
    "margin_reference_schema_version",
    "margin_reference_refresh_max_age_hours",
    "margin_reference_safety_multiplier",
    "margin_reference_require_signature",
)
MARGIN_POLICY_TRACKING_FIELDS = {
    "margin_reference_policy_generation", "margin_reference_policy_hash",
}
CONFIG_FIELDS = (
    BASE_CONFIG_FIELDS | set(AUTO_CONFIG_DEFAULTS) |
    set(MARGIN_REFERENCE_DEFAULTS) | MARGIN_POLICY_TRACKING_FIELDS
)
LEGACY_MARGIN_REFERENCE_FIELDS = {"margin_reference_max_age_hours"}
MARGIN_REFERENCE_FIELDS = {
    "交易所名称", "合约代码", "保证金-买", "保证金-卖", "保证金-每手",
    "手续费标准-开仓-万分之", "手续费标准-开仓-元",
    "手续费标准-平昨-万分之", "手续费标准-平昨-元",
    "手续费标准-平今-万分之", "手续费标准-平今-元", "手续费更新时间",
}
MARGIN_REFERENCE_EXCHANGES = {
    "上海期货交易所": "SHFE",
    "大连商品交易所": "DCE",
    "郑州商品交易所": "CZCE",
    "上海国际能源交易中心": "INE",
    "广州期货交易所": "GFEX",
    "中国金融期货交易所": "CFFEX",
}
COMMAND_FIELDS = {
    "action", "direction", "offset", "volume", "type", "intent_id",
    "child_order_id", "child_no", "client_order_key", "memo_token",
    "account_alias", "account_type", "adapter_instance", "exchange",
    "instrument_id", "limit_price", "hedge_flag", "execution_mode",
    "risk_decision_id", "source_signal_id", "risk_limits",
    "semantic_action", "sizing_type", "order_notional", "source", "auto_permit",
}
P0_TEST_FIELDS = {
    "type", "authorization_id", "account_alias", "account_type", "adapter_instance",
    "exchange", "instrument_id", "action", "order_direction", "offset", "order_type",
    "hedgeflag", "market", "volume", "limit_price", "price_policy", "memo_token",
    "quote_captured_at", "notional_cap", "isolated_notional_cap", "margin_guard_ratio",
}
P0_VALIDATION_LEG_FIELDS = {
    "type", "validation_id", "account_alias", "account_type", "adapter_instance",
    "exchange", "instrument_id", "action", "order_direction", "offset", "order_type",
    "hedgeflag", "market", "volume", "limit_price", "price_policy", "memo_token",
    "quote_captured_at", "notional_cap", "margin_guard_ratio",
}
PROFILE_FIELDS = {
    "protocol_version", "profile_id", "infinitrader_build", "pythongo_build",
    "broker_build", "account_type", "strategy_name", "adapter_binding",
    "verified", "capabilities", "mappings", "key_id", "signature",
}
PROFILE_CAPABILITY_FIELDS = {
    "dispatch_mode", "memo_max_bytes", "explicit_close_yesterday",
    "order_trade_replay_after_restart", "order_status_map",
}
PROFILE_ACTIONS = {
    "OPEN_LONG", "OPEN_SHORT", "CLOSE_LONG", "CLOSE_SHORT",
    "CLOSE_TODAY_LONG", "CLOSE_TODAY_SHORT",
    "CLOSE_YESTERDAY_LONG", "CLOSE_YESTERDAY_SHORT",
}
NORMALIZED_ORDER_STATES = {
    "QUEUED", "WORKING", "PARTIALLY_FILLED", "FILLED",
    "PARTIALLY_FILLED_CANCELLED", "CANCELLED", "REJECTED",
    "UNKNOWN_BROKER_STATUS",
}


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _iso(value=None):
    return (value or _now()).isoformat(timespec="milliseconds")


def _parse_time(value):
    parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timezone required")
    return parsed.astimezone(dt.timezone.utc)


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _margin_policy_hash(config):
    material = {name: config[name] for name in MARGIN_POLICY_MATERIAL_FIELDS}
    return hashlib.sha256(_canonical(material)).hexdigest()


def _atomic_json(path, value):
    directory = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(directory):
        os.makedirs(directory)
    descriptor, temporary = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_canonical(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _load_json(path):
    with open(path, "r", encoding="utf-8") as stream:
        return json.load(stream)


def _as_dict(value):
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    raw = getattr(value, "__dict__", None)
    return dict(raw or {})


def _json_value(value):
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            value = value.astimezone()
        return value.isoformat(timespec="milliseconds")
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return {str(key): _json_value(item) for key, item in _as_dict(value).items() if not str(key).startswith("_")}


def _pick(value, names, default=None):
    raw = _as_dict(value)
    for name in names:
        if name in raw and raw[name] is not None:
            return raw[name]
        if hasattr(value, name):
            found = getattr(value, name)
            if found is not None:
                return found
    return default


def _find_config_path(module_file):
    explicit = os.environ.get("WORKBUDDY_PYTHONGO_ADAPTER_CONFIG")
    if explicit:
        return os.path.abspath(explicit)
    directory = os.path.dirname(os.path.abspath(module_file))
    locator_candidates = [
        os.path.join(directory, "pythongo_adapter.path"),
        os.path.join(os.path.dirname(directory), "self_strategy", "pythongo_adapter.path"),
    ]
    for locator in locator_candidates:
        if not os.path.isfile(locator):
            continue
        with open(locator, "r", encoding="utf-8-sig") as stream:
            target = os.path.expandvars(stream.read().strip())
        if not target:
            raise RuntimeError("adapter config locator is empty")
        if not os.path.isabs(target):
            target = os.path.join(os.path.dirname(locator), target)
        return os.path.abspath(target)
    # Legacy fallback for installations that have not deployed the locator yet.
    candidates = [
        os.path.join(directory, "pythongo_adapter.json"),
        os.path.join(os.path.dirname(directory), "self_strategy", "pythongo_adapter.json"),
    ]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return candidates[0]


class WorkBuddyPythonGOAdapter(BaseStrategy):
    def __init__(self):
        super().__init__()
        self._config = None
        self._keys = {}
        self._active_key_id = None
        self._status = "STARTING"
        self._profile_status = "UNKNOWN"
        self._profile = None
        self._background_thread = None
        self._background_stop = threading.Event()
        self._latest_ticks = {}
        self._requested_instruments = set()
        self._pending_paths = []
        self._pending_lock = threading.Lock()
        self._poll_lock = threading.Lock()
        self._last_scan_at = 0.0
        self._last_heartbeat_at = 0.0
        self._orders = {}
        self._trades = {}
        self._margin_reference_cache_key = None
        self._margin_reference_cache = None
        self._margin_reference_last_check_at = 0.0

    def _redact(self, value):
        text = str(value)
        investor = (self._config or {}).get("investor_id")
        if isinstance(investor, str) and investor:
            text = text.replace(investor, "[REDACTED_INVESTOR]")
        return text[:200]

    @property
    def _config_path(self):
        return _find_config_path(__file__)

    @property
    def _partition(self):
        return os.path.join(os.path.abspath(self._config["data_dir"]), "queue", self._config["adapter_instance"])

    @property
    def _runtime(self):
        return os.path.join(os.path.abspath(self._config["data_dir"]), "pythongo_runtime", self._config["adapter_instance"])

    def _load_config(self):
        config = _load_json(self._config_path)
        if (
            not isinstance(config, dict) or BASE_CONFIG_FIELDS - set(config) or
            set(config) - (CONFIG_FIELDS | LEGACY_MARGIN_REFERENCE_FIELDS)
        ):
            raise RuntimeError("adapter config fields do not match the P1 schema")
        for name, value in AUTO_CONFIG_DEFAULTS.items():
            config.setdefault(name, value)
        missing_margin_fields = (set(MARGIN_REFERENCE_DEFAULTS) | MARGIN_POLICY_TRACKING_FIELDS) - set(config)
        if LEGACY_MARGIN_REFERENCE_FIELDS & set(config) or missing_margin_fields:
            raise RuntimeError(
                "margin policy migration required; run the Manager migrate-margin-policy command"
            )
        if config["account_type"] != "FUTURES" or config["pythongo_mode"] not in RUN_MODES:
            raise RuntimeError("invalid account type or mode")
        if config["command_dispatch_mode"] not in ("TICK_DISPATCH", "TIMER_DISPATCH"):
            raise RuntimeError("invalid command_dispatch_mode")
        for name in ("max_batch", "max_message_bytes", "adapter_max_order_volume", "max_snapshot_age_seconds", "max_quote_age_seconds", "heartbeat_seconds"):
            if isinstance(config[name], bool) or not isinstance(config[name], int) or config[name] <= 0:
                raise RuntimeError("invalid integer config: " + name)
        for name in ("adapter_max_order_notional", "adapter_max_margin_per_order", "adapter_max_total_margin", "adapter_max_risk_ratio", "adapter_max_price_deviation_pct"):
            if isinstance(config[name], bool) or not isinstance(config[name], (int, float)) or not math.isfinite(float(config[name])) or config[name] <= 0:
                raise RuntimeError("invalid numeric config: " + name)
        for name in ("adapter_max_auto_session_notional", "adapter_max_auto_instrument_position_notional", "adapter_max_auto_account_drawdown"):
            if isinstance(config[name], bool) or not isinstance(config[name], (int, float)) or not math.isfinite(float(config[name])) or config[name] <= 0:
                raise RuntimeError("invalid numeric config: " + name)
        for name in ("adapter_max_auto_orders", "adapter_min_auto_order_interval_seconds", "adapter_max_auto_concurrent_orders"):
            if isinstance(config[name], bool) or not isinstance(config[name], int) or config[name] <= 0:
                raise RuntimeError("invalid integer config: " + name)
        if not isinstance(config["allow_cancel_while_halted"], bool):
            raise RuntimeError("allow_cancel_while_halted must be a boolean")
        if not isinstance(config["margin_reference_file"], str) or not os.path.isabs(config["margin_reference_file"]):
            raise RuntimeError("margin_reference_file must be an absolute path")
        if not isinstance(config["margin_reference_require_signature"], bool):
            raise RuntimeError("margin_reference_require_signature must be a boolean")
        if config["margin_reference_schema_version"] != 2:
            raise RuntimeError("margin_reference_schema_version must be 2")
        for name, maximum in (("margin_reference_refresh_max_age_hours", 168), ("margin_reference_source_warn_age_hours", 8760)):
            if isinstance(config[name], bool) or not isinstance(config[name], int) or config[name] <= 0 or config[name] > maximum:
                raise RuntimeError("%s must be 1-%d" % (name, maximum))
        if (
            isinstance(config["margin_reference_safety_multiplier"], bool) or
            not isinstance(config["margin_reference_safety_multiplier"], (int, float)) or
            not math.isfinite(float(config["margin_reference_safety_multiplier"])) or
            not 1.0 <= float(config["margin_reference_safety_multiplier"]) <= 2.0
        ):
            raise RuntimeError("margin_reference_safety_multiplier must be 1.0-2.0")
        generation = config["margin_reference_policy_generation"]
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
            raise RuntimeError("margin_reference_policy_generation must be a positive integer")
        policy_hash = config["margin_reference_policy_hash"]
        if not isinstance(policy_hash, str) or not hmac.compare_digest(policy_hash, _margin_policy_hash(config)):
            raise RuntimeError("margin_reference_policy_hash mismatch; explicit migration required")
        self._config = config
        raw = _load_json(config["key_file"])
        if not isinstance(raw, dict) or set(raw) != {"active_key_id", "keys"}:
            raise RuntimeError("invalid keyring")
        self._active_key_id = raw["active_key_id"]
        self._keys = {key_id: base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)) for key_id, value in raw["keys"].items()}
        if self._active_key_id not in self._keys or any(len(value) < 32 for value in self._keys.values()):
            raise RuntimeError("invalid message key")
        if self._fingerprint(config["investor_id"]) != config["investor_fingerprint"]:
            raise RuntimeError("investor fingerprint mismatch")
        self._ensure_dirs()
        self._profile_status, self._profile = self._load_profile()

    def _require_native_base(self):
        if not PYTHONGO_BASE_AVAILABLE:
            raise RuntimeError("pythongo BaseStrategy is unavailable: %s" % (PYTHONGO_BASE_IMPORT_ERROR or "unknown import error"))

    def _ensure_dirs(self):
        for name in QUEUE_FOLDERS:
            path = os.path.join(self._partition, name)
            if not os.path.isdir(path):
                os.makedirs(path)
        for name in ("execution_journal", "heartbeat"):
            path = os.path.join(self._runtime, name)
            if not os.path.isdir(path):
                os.makedirs(path)

    def _signature(self, message):
        unsigned = dict(message)
        unsigned.pop("signature", None)
        key_id = unsigned.get("key_id", self._active_key_id)
        key = self._keys.get(key_id)
        if not key:
            raise RuntimeError("unknown key_id")
        return base64.urlsafe_b64encode(hmac.new(key, _canonical(unsigned), hashlib.sha256).digest()).decode("ascii").rstrip("=")

    def _verify_signature(self, message):
        supplied = message.get("signature") if isinstance(message, dict) else None
        if not isinstance(supplied, str) or not hmac.compare_digest(self._signature(message), supplied):
            raise RuntimeError("signature mismatch")

    def _verify_envelope(self, message, allowed_types, check_expiry=True):
        required = {"protocol_version", "message_id", "correlation_id", "message_type", "issued_at", "expires_at", "sender", "key_id", "payload", "signature"}
        if not isinstance(message, dict) or set(message) != required:
            raise RuntimeError("invalid envelope fields")
        if message["protocol_version"] != "1.0" or message["message_type"] not in allowed_types:
            raise RuntimeError("unsupported envelope")
        self._verify_signature(message)
        issued = _parse_time(message["issued_at"])
        expires = _parse_time(message["expires_at"])
        if check_expiry and expires <= _now():
            raise RuntimeError("message expired")
        if issued.timestamp() > _now().timestamp() + 5 or expires <= issued:
            raise RuntimeError("invalid message time")

    def _envelope(self, message_type, payload, ttl=300, correlation_id=None):
        issued = _now()
        message = {
            "protocol_version": "1.0",
            "message_id": "msg_" + uuid.uuid4().hex,
            "correlation_id": correlation_id or "corr_" + uuid.uuid4().hex,
            "message_type": message_type,
            "issued_at": _iso(issued),
            "expires_at": _iso(issued + dt.timedelta(seconds=ttl)),
            "sender": "pythongo-embedded-adapter",
            "key_id": self._active_key_id,
            "payload": payload,
        }
        message["signature"] = self._signature(message)
        return message

    def _write_event(self, message_type, payload, folder="events", correlation_id=None):
        envelope = self._envelope(message_type, payload, 24 * 3600, correlation_id)
        name = "%020d_%s.json" % (time.time_ns(), envelope["message_id"])
        _atomic_json(os.path.join(self._partition, folder, name), envelope)
        return envelope["message_id"]

    def _archive(self, path, suffix=".processed"):
        target = os.path.join(self._partition, "archive", os.path.basename(path) + suffix)
        if os.path.exists(target):
            target += ".%d" % time.time_ns()
        os.replace(path, target)

    def _dead_letter(self, path):
        target = os.path.join(self._partition, "dead_letter", os.path.basename(path) + ".invalid")
        if os.path.exists(target):
            target += ".%d" % time.time_ns()
        os.replace(path, target)

    def _fingerprint(self, investor_id):
        material = b"pythongo-investor-v1\0" + investor_id.strip().encode("utf-8")
        return "hmac-sha256:v1:" + hmac.new(self._keys[self._active_key_id], material, hashlib.sha256).hexdigest()

    def _load_profile(self):
        try:
            profile = _load_json(self._config["mapping_profile"])
        except Exception:
            return "MISSING", None
        if not isinstance(profile, dict) or set(profile) != PROFILE_FIELDS:
            return "SCHEMA_INVALID", None
        if profile.get("verified") is not True or not profile.get("signature"):
            return "UNVERIFIED", profile
        try:
            self._verify_signature(profile)
            expected = {
                "profile_id": "expected_profile_id",
                "infinitrader_build": "expected_infinitrader_build",
                "pythongo_build": "expected_pythongo_build",
                "broker_build": "expected_broker_build",
            }
            for profile_name, config_name in expected.items():
                value = self._config[config_name]
                if not isinstance(value, str) or not value.strip() or "REPLACE" in value.upper() or profile.get(profile_name) != value:
                    raise RuntimeError("profile build binding mismatch")
            binding = {
                "account_alias": self._config["account_alias"],
                "account_type": self._config["account_type"],
                "adapter_instance": self._config["adapter_instance"],
                "investor_fingerprint": self._config["investor_fingerprint"],
                "strategy_name": self._config["strategy_name"],
            }
            if profile.get("adapter_binding") != binding or profile.get("account_type") != "FUTURES" or profile.get("strategy_name") != self._config["strategy_name"]:
                raise RuntimeError("profile identity binding mismatch")
            if self._fingerprint(self._config["investor_id"]) != self._config["investor_fingerprint"]:
                raise RuntimeError("investor fingerprint mismatch")
            capabilities = profile.get("capabilities")
            if not isinstance(capabilities, dict) or set(capabilities) != PROFILE_CAPABILITY_FIELDS or capabilities.get("dispatch_mode") != self._config["command_dispatch_mode"]:
                raise RuntimeError("profile dispatch binding mismatch")
            if isinstance(capabilities.get("memo_max_bytes"), bool) or not isinstance(capabilities.get("memo_max_bytes"), int) or capabilities["memo_max_bytes"] < 16:
                raise RuntimeError("profile memo capability is insufficient")
            for name in ("explicit_close_yesterday", "order_trade_replay_after_restart"):
                if not isinstance(capabilities.get(name), bool):
                    raise RuntimeError("profile boolean capability is invalid")
            status_map = capabilities.get("order_status_map")
            if not isinstance(status_map, dict) or not status_map:
                raise RuntimeError("profile order status map is empty")
            for raw_status, normalized in status_map.items():
                if not isinstance(raw_status, str) or not raw_status or normalized not in NORMALIZED_ORDER_STATES:
                    raise RuntimeError("profile order status map is invalid")
            mappings = profile.get("mappings")
            if not isinstance(mappings, dict) or not mappings:
                raise RuntimeError("profile mappings are empty")
            required_mapping = {"order_direction", "offset", "order_type", "hedgeflag", "market"}
            for key, mapping in mappings.items():
                parts = key.split(":") if isinstance(key, str) else []
                if len(parts) != 4 or parts[0] != "FUTURES" or parts[1] not in PROFILE_ACTIONS or parts[2:] != ["LIMIT", "GFD"]:
                    raise RuntimeError("profile mapping key is invalid")
                if not isinstance(mapping, dict) or set(mapping) != required_mapping or mapping.get("market") is not False:
                    raise RuntimeError("profile mapping value is invalid")
                for name in required_mapping - {"market"}:
                    value = mapping.get(name)
                    if not isinstance(value, (str, int)) or isinstance(value, bool) or (isinstance(value, str) and not value):
                        raise RuntimeError("profile mapping primitive is invalid")
            if not capabilities["explicit_close_yesterday"] and any(
                key.split(":")[1].startswith("CLOSE_YESTERDAY_") for key in mappings
            ):
                raise RuntimeError("profile CloseYesterday capability contradicts mappings")
            return "VALID", profile
        except Exception:
            return "INVALID", profile

    def _local_halted(self):
        path = os.path.join(self._runtime, "local_halt.json")
        if not os.path.exists(path):
            return False
        try:
            message = _load_json(path)
            self._verify_envelope(message, {"LOCAL_HALT"}, check_expiry=False)
            return bool(message["payload"].get("halted", True))
        except Exception:
            return True

    @staticmethod
    def _auto_schedule_active(policy):
        local_now = _now().astimezone(dt.timezone(dt.timedelta(hours=8)))
        minute = local_now.hour * 60 + local_now.minute
        for window in policy.get("trading_windows", []):
            try:
                start_hour, start_minute = [int(item) for item in window["start"].split(":")]
                end_hour, end_minute = [int(item) for item in window["end"].split(":")]
                start = start_hour * 60 + start_minute
                end = end_hour * 60 + end_minute
            except Exception:
                return False
            if start < end and start <= minute < end:
                return True
            if start > end and (minute >= start or minute < end):
                return True
        return False

    def _auto_journal_usage(self, permit_id):
        directory = os.path.join(self._runtime, "execution_journal")
        count = 0
        notional = 0.0
        last_created = None
        for name in os.listdir(directory):
            if not name.endswith(".json"):
                continue
            value = _load_json(os.path.join(directory, name))
            permit = value.get("auto_permit") or {}
            if permit.get("permit_id") != permit_id:
                continue
            count += 1
            current_notional = value.get("order_notional")
            if isinstance(current_notional, bool) or not isinstance(current_notional, (int, float)):
                raise RuntimeError("invalid limited-auto execution journal")
            notional += float(current_notional)
            created = value.get("created_at")
            if created and (last_created is None or _parse_time(created) > _parse_time(last_created)):
                last_created = created
        return {"order_count": count, "notional": notional, "last_created_at": last_created}

    @property
    def _auto_pause_path(self):
        return os.path.join(self._runtime, "limited_auto_pause.json")

    def _pause_auto_locally(self, command, reason):
        permit = command.get("auto_permit") or {}
        if command.get("execution_mode") != "LIMITED_AUTO" or not permit.get("permit_id"):
            return
        _atomic_json(self._auto_pause_path, {
            "paused": True, "permit_id": permit.get("permit_id"),
            "generation": permit.get("generation"), "reason": str(reason)[:200],
            "paused_at": _iso(),
        })

    def _active_auto_pause(self, command=None):
        if not os.path.exists(self._auto_pause_path):
            return None
        pause = _load_json(self._auto_pause_path)
        if pause.get("paused") is not True:
            return None
        if command is not None:
            permit = command.get("auto_permit") or {}
        else:
            path = os.path.join(self._runtime, "local_authorization.json")
            if not os.path.exists(path):
                return pause
            try:
                message = _load_json(path)
                self._verify_envelope(message, {"LOCAL_AUTHORIZATION"})
                permit = message.get("payload", {}).get("auto_permit") or {}
            except Exception:
                return pause
        if pause.get("permit_id") == permit.get("permit_id") and pause.get("generation") == permit.get("generation"):
            return pause
        return None

    def _validate_auto_authorization(self, payload, command):
        if payload.get("authorization_type") != "LIMITED_AUTO_PERMIT":
            raise RuntimeError("limited-auto authorization type mismatch")
        if not isinstance(command, dict) or command.get("execution_mode") != "LIMITED_AUTO":
            raise RuntimeError("limited-auto command is missing")
        authorization = payload.get("auto_permit")
        command_permit = command.get("auto_permit")
        if not isinstance(authorization, dict) or not isinstance(command_permit, dict):
            raise RuntimeError("limited-auto permit binding is missing")
        if set(command_permit) != {"permit_id", "policy_hash", "generation"}:
            raise RuntimeError("limited-auto command binding is invalid")
        policy = authorization.get("policy")
        if not isinstance(policy, dict):
            raise RuntimeError("limited-auto policy is missing")
        policy_hash = hashlib.sha256(_canonical(policy)).hexdigest()
        if policy_hash != authorization.get("policy_hash") or policy_hash != command_permit.get("policy_hash"):
            raise RuntimeError("limited-auto policy hash mismatch")
        if (
            authorization.get("permit_id") != command_permit.get("permit_id") or
            authorization.get("generation") != command_permit.get("generation") or
            policy.get("permit_id") != command_permit.get("permit_id") or
            policy.get("generation") != command_permit.get("generation")
        ):
            raise RuntimeError("limited-auto permit generation mismatch")
        if self._active_auto_pause(command) is not None:
            raise RuntimeError("limited-auto is locally paused")
        if policy.get("account_alias") != self._config["account_alias"] or policy.get("account_type") != "FUTURES":
            raise RuntimeError("limited-auto policy account mismatch")
        if (
            int(policy.get("max_order_volume", 0)) > self._config["adapter_max_order_volume"] or
            float(policy.get("max_order_notional", 0)) > self._config["adapter_max_order_notional"] or
            float(policy.get("max_session_notional", 0)) > self._config["adapter_max_auto_session_notional"] or
            int(policy.get("max_orders", 0)) > self._config["adapter_max_auto_orders"] or
            int(policy.get("min_order_interval_seconds", 0)) < self._config["adapter_min_auto_order_interval_seconds"] or
            int(policy.get("max_concurrent_orders", 0)) > self._config["adapter_max_auto_concurrent_orders"] or
            float(policy.get("max_instrument_position_notional", 0)) > self._config["adapter_max_auto_instrument_position_notional"] or
            float(policy.get("max_account_drawdown", 0)) > self._config["adapter_max_auto_account_drawdown"]
        ):
            raise RuntimeError("limited-auto policy exceeds adapter hard limits")
        if _parse_time(policy["starts_at"]) > _now() or _parse_time(policy["expires_at"]) <= _now():
            raise RuntimeError("limited-auto policy is outside its lifetime")
        if policy.get("expires_at") != payload.get("live_until"):
            raise RuntimeError("limited-auto authorization lifetime mismatch")
        if not self._auto_schedule_active(policy):
            raise RuntimeError("limited-auto order is outside the trading window")
        instrument = "%s:%s" % (command.get("exchange"), command.get("instrument_id"))
        if instrument not in policy.get("allowed_instruments", []):
            raise RuntimeError("limited-auto instrument is not allowed")
        if command.get("semantic_action") not in policy.get("allowed_actions", []):
            raise RuntimeError("limited-auto semantic action is not allowed")
        if command.get("sizing_type") not in policy.get("allowed_sizing_types", []):
            raise RuntimeError("limited-auto sizing is not allowed")
        source = command.get("source") or {}
        for name in ("type", "rule_set_id", "rule_version"):
            if source.get(name) != policy.get("source", {}).get(name):
                raise RuntimeError("limited-auto strategy binding mismatch")
        volume = command.get("volume")
        notional = command.get("order_notional")
        if isinstance(volume, bool) or not isinstance(volume, int) or volume <= 0 or volume > int(policy.get("max_order_volume", 0)):
            raise RuntimeError("limited-auto order volume exceeded")
        if isinstance(notional, bool) or not isinstance(notional, (int, float)) or notional <= 0 or notional > float(policy.get("max_order_notional", 0)):
            raise RuntimeError("limited-auto order notional exceeded")
        usage = self._auto_journal_usage(command_permit["permit_id"])
        if usage["order_count"] + 1 > int(policy.get("max_orders", 0)):
            raise RuntimeError("limited-auto order count exceeded")
        if usage["notional"] + float(notional) > float(policy.get("max_session_notional", 0)):
            raise RuntimeError("limited-auto session notional exceeded")
        if usage["last_created_at"]:
            elapsed = (_now() - _parse_time(usage["last_created_at"])).total_seconds()
            if elapsed < int(policy.get("min_order_interval_seconds", 0)):
                raise RuntimeError("limited-auto order rate exceeded")
        return policy

    def _local_authorized(self, mode, command=None):
        if mode not in RUN_MODES or self._config.get("pythongo_mode") != mode:
            return None
        if mode == "OBSERVE_ONLY":
            return {}
        if self._profile_status != "VALID":
            return None
        path = os.path.join(self._runtime, "local_authorization.json")
        try:
            message = _load_json(path)
            self._verify_envelope(message, {"LOCAL_AUTHORIZATION"})
            payload = message["payload"]
            valid = (
                payload.get("account_alias") == self._config["account_alias"] and
                mode in payload.get("allowed_modes", []) and
                _parse_time(payload["live_until"]) > _now()
            )
            if not valid:
                return None
            if mode == "LIMITED_AUTO":
                return {"auto_policy": self._validate_auto_authorization(payload, command)}
            return {}
        except Exception:
            return None

    def _consume_p0_authorization(self, command):
        path = os.path.join(self._runtime, "p0_authorization.json")
        claimed = path + ".consumed-" + uuid.uuid4().hex
        try:
            os.replace(path, claimed)
        except FileNotFoundError:
            raise RuntimeError("P0 one-time authorization is missing or already consumed")
        message = _load_json(claimed)
        self._verify_envelope(message, {"P0_AUTHORIZATION"})
        payload = message["payload"]
        expected = {
            "authorization_id": command["authorization_id"],
            "account_alias": self._config["account_alias"],
            "adapter_instance": self._config["adapter_instance"],
            "investor_fingerprint": self._config["investor_fingerprint"],
            "command_hash": hashlib.sha256(_canonical(command)).hexdigest(),
        }
        if payload != expected:
            raise RuntimeError("P0 authorization binding mismatch")

    def _consume_p0_leg_authorization(self, command):
        path = os.path.join(self._runtime, "p0_leg_authorization.json")
        claimed = path + ".consumed-" + uuid.uuid4().hex
        try:
            os.replace(path, claimed)
        except FileNotFoundError:
            raise RuntimeError("P0 leg authorization is missing or already consumed")
        message = _load_json(claimed)
        self._verify_envelope(message, {"P0_LEG_AUTHORIZATION"})
        payload = message["payload"]
        expected = {
            "validation_id": command["validation_id"],
            "account_alias": self._config["account_alias"],
            "adapter_instance": self._config["adapter_instance"],
            "investor_fingerprint": self._config["investor_fingerprint"],
            "command_hash": hashlib.sha256(_canonical(command)).hexdigest(),
        }
        if payload != expected:
            raise RuntimeError("P0 leg authorization binding mismatch")

    def _execute_p0_test_order(self, command, message_id):
        if not isinstance(command, dict) or set(command) != P0_TEST_FIELDS:
            raise RuntimeError("P0 test command fields do not match the v1 schema")
        if self._config["pythongo_mode"] != "OBSERVE_ONLY" or not self._local_halted():
            raise RuntimeError("P0 test requires OBSERVE_ONLY with local halt enabled")
        if command["account_alias"] != self._config["account_alias"] or command["account_type"] != "FUTURES" or command["adapter_instance"] != self._config["adapter_instance"]:
            raise RuntimeError("P0 account partition mismatch")
        expected_mapping = {
            "action": "OPEN_LONG", "order_direction": "BUY", "offset": "0",
            "order_type": "GFD", "hedgeflag": "1", "market": False,
            "volume": 1, "price_policy": "LOWER_LIMIT",
        }
        if any(command.get(name) != value for name, value in expected_mapping.items()):
            raise RuntimeError("P0 command is outside the fixed one-lot candidate mapping")
        if command["volume"] > self._config["adapter_max_order_volume"]:
            raise RuntimeError("P0 volume exceeds Adapter hard limit")
        isolated_cap = command["isolated_notional_cap"]
        notional_cap = command["notional_cap"]
        margin_guard_ratio = command["margin_guard_ratio"]
        if not isinstance(isolated_cap, bool):
            raise RuntimeError("P0 isolated cap flag must be a boolean")
        if isinstance(notional_cap, bool) or not isinstance(notional_cap, (int, float)) or not math.isfinite(float(notional_cap)):
            raise RuntimeError("P0 notional cap is invalid")
        if isolated_cap:
            if (
                float(notional_cap) <= 0
                or float(notional_cap) > P0_VALIDATION_NOTIONAL_CAP
                or margin_guard_ratio != P0_ISOLATED_MARGIN_GUARD_RATIO
            ):
                raise RuntimeError("P0 isolated cap is outside the signed validation ceiling")
        elif float(notional_cap) != float(self._config["adapter_max_order_notional"]) or margin_guard_ratio is not None:
            raise RuntimeError("P0 standard cap does not match Adapter hard limit")
        quote = self._quote(command["exchange"], command["instrument_id"])
        for name in ("last_price", "bid_price1", "lower_limit_price", "price_tick", "volume_multiple"):
            if not isinstance(quote.get(name), (int, float)) or isinstance(quote.get(name), bool) or quote[name] <= 0:
                raise RuntimeError("P0 quote field missing: " + name)
        price = float(command["limit_price"])
        if price != float(quote["lower_limit_price"]) or price >= float(quote["bid_price1"]):
            raise RuntimeError("P0 order is not bound to the passive lower-limit price")
        if abs(round(price / quote["price_tick"]) * quote["price_tick"] - price) > 1e-8:
            raise RuntimeError("P0 price is not tick aligned")
        notional = price * quote["volume_multiple"]
        if notional > float(notional_cap):
            raise RuntimeError("P0 notional exceeds Adapter hard limit")
        account = self._account()
        for name in ("available", "margin", "risk"):
            if not isinstance(account.get(name), (int, float)) or isinstance(account.get(name), bool):
                raise RuntimeError("P0 account risk field missing: " + name)
        if account["available"] <= 0 or account["margin"] > self._config["adapter_max_total_margin"] or account["risk"] > self._config["adapter_max_risk_ratio"]:
            raise RuntimeError("P0 account hard risk gate rejected the probe")
        if isolated_cap:
            guarded_margin = notional * margin_guard_ratio
            if account["available"] < guarded_margin or account["margin"] + guarded_margin > self._config["adapter_max_total_margin"]:
                raise RuntimeError("P0 isolated conservative margin gate rejected the probe")

        journal_path = self._journal_path("p0_" + command["authorization_id"])
        if os.path.exists(journal_path):
            raise RuntimeError("P0 authorization was already journaled")
        self._consume_p0_authorization(command)
        journal = {
            "authorization_id": command["authorization_id"],
            "state": "PRE_SUBMIT",
            "updated_at": _iso(),
            "mapping_candidate": {
                "order_direction": command["order_direction"], "offset": command["offset"],
                "order_type": command["order_type"], "hedgeflag": command["hedgeflag"],
                "market": command["market"],
            },
            "memo_token": command["memo_token"],
        }
        _atomic_json(journal_path, journal)
        try:
            order_id = self.make_order_req(
                exchange=command["exchange"], instrument_id=command["instrument_id"],
                volume=1, price=price, order_direction=command["order_direction"],
                offset=command["offset"], order_type=command["order_type"],
                investor=self._config["investor_id"], hedgeflag=command["hedgeflag"],
                market=False, memo=command["memo_token"],
            )
        except Exception as exc:
            self._ack(command, "SUBMIT_UNKNOWN", message_id, {"phase": "P0_SUBMIT", "error": self._redact(exc)})
            return
        if isinstance(order_id, bool) or order_id is None:
            self._ack(command, "SUBMIT_UNKNOWN", message_id, {"phase": "P0_SUBMIT", "native_return_type": type(order_id).__name__})
            return
        if not isinstance(order_id, int) or order_id < 0:
            journal.update({"state": "FAILED_BEFORE_SEND", "updated_at": _iso()})
            _atomic_json(journal_path, journal)
            self._ack(command, "FAILED_BEFORE_SEND", message_id, {"phase": "P0_SUBMIT", "native_return": order_id})
            return
        journal.update({"state": "SEND_RETURNED", "updated_at": _iso(), "pythongo_order_id": order_id})
        _atomic_json(journal_path, journal)
        try:
            cancel_result = self.cancel_order(order_id)
        except Exception as exc:
            self._ack(command, "P0_CANCEL_UNKNOWN", message_id, {"phase": "P0_CANCEL", "pythongo_order_id": order_id, "error": self._redact(exc)})
            return
        journal.update({"state": "P0_CANCEL_REQUESTED", "updated_at": _iso(), "cancel_native_return": _json_value(cancel_result)})
        _atomic_json(journal_path, journal)
        self._ack(command, "P0_CANCEL_REQUESTED", message_id, {
            "phase": "P0_CANCEL", "pythongo_order_id": order_id,
            "cancel_native_return": _json_value(cancel_result), "mapping_candidate": "BUY/0/GFD/1/market=false",
        })

    def _execute_p0_validation_leg(self, command, message_id):
        if not isinstance(command, dict) or set(command) != P0_VALIDATION_LEG_FIELDS:
            raise RuntimeError("P0 validation leg fields do not match the v1 schema")
        if self._config["pythongo_mode"] != "OBSERVE_ONLY" or not self._local_halted():
            raise RuntimeError("P0 validation leg requires OBSERVE_ONLY with local halt")
        if (
            command["account_alias"] != self._config["account_alias"]
            or command["account_type"] != "FUTURES"
            or command["adapter_instance"] != self._config["adapter_instance"]
        ):
            raise RuntimeError("P0 validation leg binding mismatch")
        mapping = P0_VALIDATION_LEG_MAPPINGS.get(command["action"])
        expected = {
            "order_direction": mapping[0] if mapping else None,
            "offset": mapping[1] if mapping else None,
            "order_type": "GFD", "hedgeflag": "1", "market": False,
            "volume": 1, "price_policy": "CROSS_5_TICKS",
            "notional_cap": P0_VALIDATION_NOTIONAL_CAP, "margin_guard_ratio": P0_ISOLATED_MARGIN_GUARD_RATIO,
        }
        if not mapping or any(command.get(name) != value for name, value in expected.items()):
            raise RuntimeError("P0 validation leg is outside the fixed mapping")
        if command["volume"] > self._config["adapter_max_order_volume"]:
            raise RuntimeError("P0 validation volume exceeds Adapter hard limit")

        quote = self._quote(command["exchange"], command["instrument_id"])
        for name in (
            "last_price", "bid_price1", "ask_price1", "lower_limit_price",
            "upper_limit_price", "price_tick", "volume_multiple",
        ):
            if not isinstance(quote.get(name), (int, float)) or isinstance(quote.get(name), bool) or quote[name] <= 0:
                raise RuntimeError("P0 validation quote field missing: " + name)
        price = float(command["limit_price"])
        if not float(quote["lower_limit_price"]) <= price <= float(quote["upper_limit_price"]):
            raise RuntimeError("P0 validation price is outside daily limits")
        if abs(round(price / quote["price_tick"]) * quote["price_tick"] - price) > 1e-8:
            raise RuntimeError("P0 validation price is not tick aligned")
        if abs(price - float(quote["last_price"])) / float(quote["last_price"]) > 0.01:
            raise RuntimeError("P0 validation price deviation exceeds one percent")
        if command["order_direction"] == "BUY" and price < float(quote["ask_price1"]):
            raise RuntimeError("P0 validation buy is no longer marketable")
        if command["order_direction"] == "SELL" and price > float(quote["bid_price1"]):
            raise RuntimeError("P0 validation sell is no longer marketable")
        notional = price * float(quote["volume_multiple"])
        if notional > P0_VALIDATION_NOTIONAL_CAP:
            raise RuntimeError("P0 validation notional exceeds isolated limit")

        account = self._account()
        for name in ("available", "margin", "risk"):
            if not isinstance(account.get(name), (int, float)) or isinstance(account.get(name), bool):
                raise RuntimeError("P0 validation account field missing: " + name)
        if account["available"] <= 0 or account["margin"] > self._config["adapter_max_total_margin"] or account["risk"] > self._config["adapter_max_risk_ratio"]:
            raise RuntimeError("P0 validation account risk gate rejected the leg")

        positions = [
            item for item in self._positions(simple=False)
            if item.get("exchange") == command["exchange"]
            and item.get("instrument_id") == command["instrument_id"]
        ]
        long_position = sum(int((item.get("long") or {}).get("position") or 0) for item in positions)
        short_position = sum(int((item.get("short") or {}).get("position") or 0) for item in positions)
        long_today = sum(int((item.get("long") or {}).get("td_close_available") or 0) for item in positions)
        short_today = sum(int((item.get("short") or {}).get("td_close_available") or 0) for item in positions)
        if command["action"].startswith("OPEN_"):
            guarded_margin = notional * P0_ISOLATED_MARGIN_GUARD_RATIO
            if long_position or short_position:
                raise RuntimeError("P0 validation open leg requires zero position")
            if account["available"] < guarded_margin or account["margin"] + guarded_margin > self._config["adapter_max_total_margin"]:
                raise RuntimeError("P0 validation conservative margin gate rejected the open leg")
        elif command["action"] == "CLOSE_TODAY_LONG" and long_today < 1:
            raise RuntimeError("P0 validation requires one long today position")
        elif command["action"] == "CLOSE_TODAY_SHORT" and short_today < 1:
            raise RuntimeError("P0 validation requires one short today position")

        journal_path = self._journal_path("p0leg_" + command["validation_id"])
        if os.path.exists(journal_path):
            raise RuntimeError("P0 validation leg was already journaled")
        self._consume_p0_leg_authorization(command)
        journal = {
            "validation_id": command["validation_id"], "action": command["action"],
            "state": "PRE_SUBMIT", "updated_at": _iso(), "memo_token": command["memo_token"],
            "mapping_candidate": {
                "order_direction": command["order_direction"], "offset": command["offset"],
                "order_type": command["order_type"], "hedgeflag": command["hedgeflag"],
                "market": command["market"],
            },
        }
        _atomic_json(journal_path, journal)
        try:
            order_id = self.make_order_req(
                exchange=command["exchange"], instrument_id=command["instrument_id"],
                volume=1, price=price, order_direction=command["order_direction"],
                offset=command["offset"], order_type=command["order_type"],
                investor=self._config["investor_id"], hedgeflag=command["hedgeflag"],
                market=False, memo=command["memo_token"],
            )
        except Exception as exc:
            self._ack(command, "SUBMIT_UNKNOWN", message_id, {"phase": "P0_VALIDATION_SUBMIT", "error": self._redact(exc)})
            return
        if isinstance(order_id, bool) or order_id is None:
            self._ack(command, "SUBMIT_UNKNOWN", message_id, {"phase": "P0_VALIDATION_SUBMIT", "native_return_type": type(order_id).__name__})
            return
        try:
            parsed_order_id = int(order_id)
        except Exception:
            self._ack(command, "SUBMIT_UNKNOWN", message_id, {"phase": "P0_VALIDATION_SUBMIT", "native_return_type": type(order_id).__name__})
            return
        if parsed_order_id == -1:
            journal.update({"state": "FAILED_BEFORE_SEND", "updated_at": _iso(), "pythongo_order_id": parsed_order_id})
            _atomic_json(journal_path, journal)
            self._ack(command, "FAILED_BEFORE_SEND", message_id, {"phase": "P0_VALIDATION_SUBMIT", "pythongo_order_id": parsed_order_id})
            return
        if parsed_order_id < 0:
            self._ack(command, "SUBMIT_UNKNOWN", message_id, {"phase": "P0_VALIDATION_SUBMIT", "pythongo_order_id": parsed_order_id})
            return
        journal.update({"state": "P0_LEG_SEND_RETURNED", "updated_at": _iso(), "pythongo_order_id": parsed_order_id})
        _atomic_json(journal_path, journal)
        self._ack(command, "P0_LEG_SEND_RETURNED", message_id, {
            "phase": "P0_VALIDATION_SUBMIT", "action": command["action"],
            "pythongo_order_id": parsed_order_id,
            "mapping_candidate": "%s/%s/GFD/1/market=false" % (command["order_direction"], command["offset"]),
        })

    def _mapping(self, command):
        if self._profile_status != "VALID":
            raise RuntimeError("verified bound Profile is required")
        key = "FUTURES:%s:LIMIT:GFD" % command["action"]
        mapping = self._profile.get("mappings", {}).get(key)
        required = {"order_direction", "offset", "order_type", "hedgeflag", "market"}
        if not isinstance(mapping, dict) or set(mapping) != required:
            raise RuntimeError("unsupported Profile mapping: " + key)
        if not isinstance(mapping["market"], bool) or mapping["market"]:
            raise RuntimeError("only non-market orders are supported")
        return mapping

    def _journal_path(self, client_order_key):
        safe = "".join(c for c in client_order_key if c.isalnum() or c in "_-")
        return os.path.join(self._runtime, "execution_journal", safe + ".json")

    def _write_journal(self, command, state, details=None):
        path = self._journal_path(command["client_order_key"])
        existing = _load_json(path) if os.path.exists(path) else {}
        value = {
            "client_order_key": command["client_order_key"],
            "intent_id": command["intent_id"],
            "child_order_id": command["child_order_id"],
            "state": state,
            "created_at": existing.get("created_at") or _iso(),
            "updated_at": _iso(),
            "details": details or {},
            "auto_permit": command.get("auto_permit"),
            "order_notional": command.get("order_notional"),
        }
        _atomic_json(path, value)

    def _ack(self, command, status, original_message_id, details=None, folder="command_acks"):
        payload = {
            "type": "COMMAND_ACK",
            "message_id": original_message_id,
            "account_alias": self._config["account_alias"],
            "intent_id": command.get("intent_id"),
            "child_order_id": command.get("child_order_id"),
            "client_order_key": command.get("client_order_key"),
            "status": status,
            "occurred_at": _iso(),
            "details": details or {},
        }
        self._write_event("COMMAND_ACK", payload, folder, command.get("intent_id"))

    def _quote(self, exchange, instrument_id):
        item = self._latest_ticks.get((exchange, instrument_id))
        if not item:
            raise RuntimeError("fresh quote is unavailable")
        if time.time() - item["received_epoch"] > self._config["max_quote_age_seconds"]:
            raise RuntimeError("quote is stale")
        return item["payload"]

    def _parse_margin_reference_time(self, value):
        text = str(value or "").strip()
        china = dt.timezone(dt.timedelta(hours=8))
        for pattern in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                return dt.datetime.strptime(text, pattern).replace(tzinfo=china).astimezone(dt.timezone.utc)
            except ValueError:
                pass
        now = _now().astimezone(china)
        for pattern in ("%m-%d %H:%M:%S.%f", "%m-%d %H:%M:%S", "%m-%d %H:%M"):
            for year in (now.year, now.year - 1):
                try:
                    parsed = dt.datetime.strptime("%d-%s" % (year, text), "%Y-" + pattern)
                except ValueError:
                    continue
                candidate = parsed.replace(tzinfo=china)
                if candidate <= now + dt.timedelta(days=1):
                    return candidate.astimezone(dt.timezone.utc)
        raise ValueError("invalid source update time")

    def _load_margin_reference(self):
        path = str((self._config or {}).get("margin_reference_file") or "").strip()
        if not path:
            return {"error": "LOCAL_MARGIN_REFERENCE_DISABLED", "records": {}}
        path = os.path.abspath(path)
        checked_at = time.monotonic()
        if self._margin_reference_cache is not None and checked_at - self._margin_reference_last_check_at < 5.0:
            return self._margin_reference_cache
        self._margin_reference_last_check_at = checked_at
        try:
            stat = os.stat(path)
            metadata_path = path + ".meta.json"
            if self._config.get("margin_reference_require_signature", True):
                metadata_stat = os.stat(metadata_path)
                metadata_key = (int(metadata_stat.st_mtime_ns), int(metadata_stat.st_size))
            else:
                metadata_key = None
            cache_key = (path, int(stat.st_mtime_ns), int(stat.st_size), metadata_key)
            if cache_key == self._margin_reference_cache_key and self._margin_reference_cache is not None:
                return self._margin_reference_cache
            with open(path, "rb") as stream:
                encoded = stream.read()
            digest = hashlib.sha256(encoded).hexdigest()
            metadata = None
            if self._config.get("margin_reference_require_signature", True):
                metadata = _load_json(metadata_path)
                required_metadata = {
                    "schema_version", "csv_sha256", "source", "source_version",
                    "refreshed_at", "key_id", "signature",
                }
                if not isinstance(metadata, dict) or set(metadata) != required_metadata:
                    raise ValueError("margin reference metadata fields are invalid")
                if metadata.get("schema_version") != self._config["margin_reference_schema_version"] or metadata.get("csv_sha256") != digest:
                    raise ValueError("margin reference metadata does not bind the CSV")
                if not hmac.compare_digest(self._signature(metadata), str(metadata.get("signature") or "")):
                    raise ValueError("margin reference signature is invalid")
                refreshed_at = _parse_time(metadata.get("refreshed_at"))
            else:
                refreshed_at = dt.datetime.fromtimestamp(stat.st_mtime, dt.timezone.utc)
            text = encoded.decode("utf-8-sig")
            reader = csv.DictReader(io.StringIO(text))
            if not isinstance(reader.fieldnames, list) or not MARGIN_REFERENCE_FIELDS.issubset(set(reader.fieldnames)):
                raise ValueError("required 9qihuo columns are missing")
            records = {}
            duplicates = set()
            for row in reader:
                exchange = MARGIN_REFERENCE_EXCHANGES.get(str(row.get("交易所名称") or "").strip())
                instrument = str(row.get("合约代码") or "").strip().lower()
                if not exchange or not instrument:
                    continue
                key = (exchange, instrument)
                if key in records:
                    duplicates.add(key)
                    continue
                try:
                    long_percent = float(row.get("保证金-买"))
                    short_percent = float(row.get("保证金-卖"))
                    margin_per_lot = float(row.get("保证金-每手"))
                except (TypeError, ValueError):
                    continue
                if not (
                    math.isfinite(long_percent) and math.isfinite(short_percent) and
                    0 < long_percent <= 100 and 0 < short_percent <= 100 and
                    math.isfinite(margin_per_lot) and margin_per_lot > 0
                ):
                    continue
                try:
                    updated_at = self._parse_margin_reference_time(row.get("手续费更新时间"))
                except (TypeError, ValueError):
                    updated_at = None
                records[key] = {
                    "long_margin_ratio": long_percent / 100.0,
                    "short_margin_ratio": short_percent / 100.0,
                    "margin_per_lot": margin_per_lot,
                    "source_updated_at": updated_at,
                }
            for key in duplicates:
                records.pop(key, None)
            result = {
                "error": None,
                "records": records,
                "file_mtime": dt.datetime.fromtimestamp(stat.st_mtime, dt.timezone.utc),
                "refreshed_at": refreshed_at,
                "source": (metadata or {}).get("source") or "UNSIGNED_LOCAL_9QIHUO_CSV",
                "sha256": digest,
            }
        except (OSError, UnicodeError, ValueError, csv.Error) as exc:
            result = {"error": "LOCAL_MARGIN_REFERENCE_INVALID:%s" % type(exc).__name__, "records": {}}
            cache_key = (path, None, None, None)
        self._margin_reference_cache_key = cache_key
        self._margin_reference_cache = result
        return result

    def _local_margin_terms(self, exchange, instrument):
        reference = self._load_margin_reference()
        error = reference.get("error")
        record = reference.get("records", {}).get((str(exchange).upper(), str(instrument).lower()))
        if not record and not error:
            error = "LOCAL_MARGIN_CONTRACT_NOT_FOUND_OR_DUPLICATE"
        if not record:
            return None, None, {"error": error}
        now = _now()
        maximum_age = dt.timedelta(hours=int(self._config["margin_reference_refresh_max_age_hours"]))
        future_tolerance = dt.timedelta(minutes=10)
        for value, stale_error in (
            (reference.get("file_mtime"), "LOCAL_MARGIN_FILE_STALE"),
            (reference.get("refreshed_at"), "LOCAL_MARGIN_REFRESH_STALE"),
        ):
            if not isinstance(value, dt.datetime) or value > now + future_tolerance or now - value > maximum_age:
                return None, None, {"error": stale_error}
        source_updated_at = record.get("source_updated_at")
        source_age_hours = None
        source_warning = None
        if not isinstance(source_updated_at, dt.datetime):
            source_warning = "LOCAL_MARGIN_SOURCE_TIME_MISSING"
        elif source_updated_at > now + future_tolerance:
            source_warning = "LOCAL_MARGIN_SOURCE_TIME_IN_FUTURE"
        else:
            source_age_hours = max(0.0, (now - source_updated_at).total_seconds() / 3600.0)
            if source_age_hours > float(self._config["margin_reference_source_warn_age_hours"]):
                source_warning = "LOCAL_MARGIN_SOURCE_UPDATE_OLD"
        raw_ratio = max(record["long_margin_ratio"], record["short_margin_ratio"])
        raw_per_lot = record["margin_per_lot"]
        multiplier = float(self._config["margin_reference_safety_multiplier"])
        effective_ratio = min(1.0, raw_ratio * multiplier)
        effective_per_lot = raw_per_lot * multiplier
        return effective_ratio, effective_per_lot, {
            "error": None,
            "source": reference.get("source") or "LOCAL_9QIHUO_FUTURES_COMM_INFO",
            "source_updated_at": _iso(source_updated_at) if isinstance(source_updated_at, dt.datetime) else None,
            "source_age_hours": source_age_hours,
            "source_warning": source_warning,
            "raw_margin_ratio": raw_ratio,
            "raw_margin_per_lot": raw_per_lot,
            "safety_multiplier": multiplier,
            "sha256": reference.get("sha256"),
        }

    def _normalize_tick(self, tick):
        exchange = str(_pick(tick, ["exchange", "ExchangeID"], "")).upper()
        instrument = str(_pick(tick, ["instrument_id", "InstrumentID"], ""))
        if not exchange or not instrument:
            raise RuntimeError("tick identity is missing")
        instrument_data = None
        raw_instrument_data = None
        try:
            instrument_data = self.get_instrument_data(exchange, instrument)
        except Exception:
            pass
        if PYTHONGO_INFINI is not None:
            try:
                raw_instrument_data = PYTHONGO_INFINI.get_instrument(exchange, instrument)
            except Exception:
                pass

        def instrument_pick(names, default=None):
            for source in (raw_instrument_data, instrument_data):
                value = _pick(source, names, None)
                if value is not None:
                    return value
            return default

        long_margin = instrument_pick(["long_margin_ratio", "LongMarginRatio"], None)
        short_margin = instrument_pick(["short_margin_ratio", "ShortMarginRatio"], None)
        margins = [
            float(value) for value in (long_margin, short_margin)
            if (
                isinstance(value, (int, float)) and not isinstance(value, bool) and
                math.isfinite(float(value)) and 0 < float(value) <= 1
            )
        ]
        margin_ratio = max(margins) if margins else None
        margin_per_lot = None
        margin_meta = {
            "error": None,
            "source": "INFINITRADER_CONTRACT" if margin_ratio is not None else None,
            "source_updated_at": None,
            "source_age_hours": None,
            "source_warning": None,
            "raw_margin_ratio": margin_ratio,
            "raw_margin_per_lot": None,
            "safety_multiplier": 1.0,
            "sha256": None,
        }
        if margin_ratio is None:
            margin_ratio, margin_per_lot, margin_meta = self._local_margin_terms(exchange, instrument)
        payload = {
            "exchange": exchange,
            "instrument_id": instrument,
            "last_price": _pick(tick, ["last_price", "LastPrice"]),
            "bid_price1": _pick(tick, ["bid_price1", "BidPrice1"]),
            "ask_price1": _pick(tick, ["ask_price1", "AskPrice1"]),
            "upper_limit_price": _pick(tick, ["upper_limit_price", "UpperLimitPrice"]),
            "lower_limit_price": _pick(tick, ["lower_limit_price", "LowerLimitPrice"]),
            "price_tick": instrument_pick(["price_tick", "PriceTick"]),
            "volume_multiple": instrument_pick(["volume_multiple", "VolumeMultiple", "size"]),
            "margin_ratio": margin_ratio,
            "margin_per_lot": margin_per_lot,
            "margin_ratio_source": margin_meta.get("source"),
            "margin_ratio_source_updated_at": margin_meta.get("source_updated_at"),
            "margin_ratio_source_age_hours": margin_meta.get("source_age_hours"),
            "margin_ratio_source_warning": margin_meta.get("source_warning"),
            "margin_ratio_raw": margin_meta.get("raw_margin_ratio"),
            "margin_per_lot_raw": margin_meta.get("raw_margin_per_lot"),
            "margin_ratio_safety_multiplier": margin_meta.get("safety_multiplier"),
            "margin_ratio_reference_sha256": margin_meta.get("sha256"),
            "margin_ratio_reference_error": margin_meta.get("error"),
            "trading_day": _pick(tick, ["trading_day", "TradingDay"]),
            "update_time": _pick(tick, ["datetime", "update_time", "UpdateTime"]),
        }
        return _json_value(payload)

    def _account(self):
        value = self.get_account_fund_data(self._config["investor_id"])
        return {
            "trading_day": _pick(value, ["trading_day", "TradingDay"], dt.datetime.now().strftime("%Y%m%d")),
            "balance": _pick(value, ["balance", "Balance"]),
            "dynamic_rights": _pick(value, ["dynamic_rights", "DynamicRights"]),
            "available": _pick(value, ["available", "Available"]),
            "close_profit": _pick(value, ["close_profit", "CloseProfit"], 0),
            "position_profit": _pick(value, ["position_profit", "PositionProfit"], 0),
            "commission": _pick(value, ["commission", "Commission"]),
            "margin": _pick(value, ["margin", "Margin"]),
            "frozen_margin": _pick(value, ["frozen_margin", "FrozenMargin"]),
            "risk": _pick(value, ["risk", "Risk"]),
        }

    def _normalize_position(self, value, hedgeflag):
        long_side = getattr(value, "long", None) or _as_dict(value).get("long")
        short_side = getattr(value, "short", None) or _as_dict(value).get("short")
        identity = long_side or short_side or value

        def side(item):
            return {
                "position": int(_pick(item, ["position", "Position"], 0) or 0),
                "frozen_closing": int(_pick(item, ["frozen_closing", "FrozenClosing"], 0) or 0),
                "td_close_available": int(_pick(item, ["td_close_available", "TdCloseAvailable"], 0) or 0),
                "yd_close_available": int(_pick(item, ["yd_close_available", "YdCloseAvailable"], 0) or 0),
                "td_frozen_closing": int(_pick(item, ["td_frozen_closing", "TdFrozenClosing"], 0) or 0),
                "yd_frozen_closing": int(_pick(item, ["yd_frozen_closing", "YdFrozenClosing"], 0) or 0),
                "used_margin": _pick(item, ["used_margin", "UsedMargin"]),
            }

        long_value = side(long_side)
        short_value = side(short_side)
        verified_hedgeflags = {
            str(mapping.get("hedgeflag"))
            for mapping in ((self._profile or {}).get("mappings") or {}).values()
            if isinstance(mapping, dict) and mapping.get("hedgeflag") is not None
        }
        normalized_hedgeflag = "SPECULATION" if self._profile_status == "VALID" and str(hedgeflag) in verified_hedgeflags else "UNKNOWN"
        return {
            "exchange": str(_pick(identity, ["exchange", "ExchangeID"], "")).upper(),
            "instrument_id": str(_pick(identity, ["instrument_id", "InstrumentID"], "")),
            "hedgeflag": normalized_hedgeflag,
            "raw_hedgeflag": str(hedgeflag),
            "position": long_value["position"] + short_value["position"],
            "long": long_value,
            "short": short_value,
        }

    def _positions(self, simple=True):
        raw = self.get_all_position(simple=simple)
        investor_values = (raw or {}).get(self._config["investor_id"], {}) if isinstance(raw, dict) else {}
        result = []
        for instrument, hedge_values in investor_values.items():
            if not isinstance(hedge_values, dict):
                continue
            for hedgeflag, position in hedge_values.items():
                normalized = self._normalize_position(position, hedgeflag)
                if normalized["instrument_id"]:
                    result.append(normalized)
        return result

    def _target_position(self, exchange, instrument_id, hedgeflag):
        try:
            value = self.get_position(instrument_id, investor=self._config["investor_id"], hedgeflag=hedgeflag, simple=True)
            return self._normalize_position(value, hedgeflag)
        except Exception as exc:
            raise RuntimeError("cannot obtain fresh target position: %s" % exc)

    def _final_auto_dynamic_risk(self, command, policy, quote, account):
        equity = account.get("dynamic_rights") or account.get("balance")
        if isinstance(equity, bool) or not isinstance(equity, (int, float)) or equity <= 0:
            raise RuntimeError("account equity is unavailable at limited-auto final check")
        if float(policy.get("baseline_equity", 0)) - float(equity) > float(policy.get("max_account_drawdown", 0)):
            raise RuntimeError("limited-auto account drawdown exceeded at final check")
        mapping = self._mapping(command)
        position = self._target_position(command["exchange"], command["instrument_id"], mapping["hedgeflag"])
        current_volume = int(position.get("position") or 0)
        order_volume = int(command["volume"])
        projected_volume = current_volume + order_volume if command["semantic_action"].startswith("OPEN_") else max(0, current_volume - order_volume)
        projected_notional = projected_volume * float(command["limit_price"]) * float(quote["volume_multiple"])
        if projected_notional > float(policy.get("max_instrument_position_notional", 0)):
            raise RuntimeError("limited-auto instrument position limit exceeded at final check")
        active = sum(
            1 for item in self._orders.values()
            if item.get("normalized_status") in {
                "QUEUED", "WORKING", "PARTIALLY_FILLED", "CANCEL_REQUESTED",
            }
        )
        if active >= int(policy.get("max_concurrent_orders", 0)):
            raise RuntimeError("limited-auto concurrent-order limit exceeded at final check")

    def _final_risk(self, command):
        self._profile_status, self._profile = self._load_profile()
        if not isinstance(command, dict) or set(command) != COMMAND_FIELDS:
            raise RuntimeError("execute command fields do not match the v1 schema")
        if command["account_alias"] != self._config["account_alias"] or command["adapter_instance"] != self._config["adapter_instance"] or command["account_type"] != "FUTURES":
            raise RuntimeError("account partition mismatch")
        if command["execution_mode"] != self._config["pythongo_mode"]:
            raise RuntimeError("mode mismatch")
        if command["hedge_flag"] != "SPECULATION":
            raise RuntimeError("unsupported semantic hedge flag")
        if self._local_halted():
            raise RuntimeError("adapter is locally halted")
        authorization = self._local_authorized(command["execution_mode"], command)
        if authorization is None:
            raise RuntimeError("execution mode is not locally authorized")
        volume = command["volume"]
        price = command["limit_price"]
        if isinstance(volume, bool) or not isinstance(volume, int) or volume <= 0 or volume > self._config["adapter_max_order_volume"]:
            raise RuntimeError("invalid or excessive volume")
        if isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(float(price)) or price <= 0:
            raise RuntimeError("invalid price")
        quote = self._quote(command["exchange"], command["instrument_id"])
        for field in ("price_tick", "volume_multiple", "upper_limit_price", "lower_limit_price", "last_price"):
            if not isinstance(quote.get(field), (int, float)) or quote[field] <= 0:
                raise RuntimeError("quote field missing: " + field)
        if abs(round(price / quote["price_tick"]) * quote["price_tick"] - price) > 1e-8:
            raise RuntimeError("price is not tick aligned")
        if not quote["lower_limit_price"] <= price <= quote["upper_limit_price"]:
            raise RuntimeError("price is outside daily limits")
        if abs(price - quote["last_price"]) / quote["last_price"] > self._config["adapter_max_price_deviation_pct"]:
            raise RuntimeError("price deviation exceeded")
        notional = price * volume * quote["volume_multiple"]
        if abs(float(command["order_notional"]) - float(notional)) > 1e-6:
            raise RuntimeError("signed order notional does not match the fresh contract multiplier")
        if notional > self._config["adapter_max_order_notional"]:
            raise RuntimeError("adapter notional limit exceeded")
        account = self._account()
        if not isinstance(account.get("risk"), (int, float)) or account["risk"] > self._config["adapter_max_risk_ratio"]:
            raise RuntimeError("account risk ratio unavailable or excessive")
        if not isinstance(account.get("margin"), (int, float)) or account["margin"] > self._config["adapter_max_total_margin"]:
            raise RuntimeError("account margin unavailable or excessive")
        if command["offset"] == "OPEN":
            margin_per_lot = quote.get("margin_per_lot")
            ratio = quote.get("margin_ratio")
            if isinstance(margin_per_lot, (int, float)) and not isinstance(margin_per_lot, bool) and math.isfinite(float(margin_per_lot)) and margin_per_lot > 0:
                margin = volume * margin_per_lot
            elif isinstance(ratio, (int, float)) and not isinstance(ratio, bool) and math.isfinite(float(ratio)) and ratio > 0:
                margin = notional * ratio
            else:
                raise RuntimeError("margin data unavailable")
            if margin > self._config["adapter_max_margin_per_order"] or account["margin"] + margin > self._config["adapter_max_total_margin"]:
                raise RuntimeError("margin limit exceeded")
            if not isinstance(account.get("available"), (int, float)) or account["available"] < margin:
                raise RuntimeError("available funds insufficient")
        else:
            if self._profile_status != "VALID":
                raise RuntimeError("verified Profile is required for close-position final risk")
            mapping = self._mapping(command)
            position = self._target_position(command["exchange"], command["instrument_id"], mapping["hedgeflag"])
            side = position["long"] if command["direction"] == "SELL" else position["short"]
            available = side["td_close_available"] if command["offset"] == "CLOSE_TODAY" else side["yd_close_available"]
            if volume > available:
                raise RuntimeError("close volume exceeds fresh available position")
        if command["execution_mode"] == "LIMITED_AUTO":
            policy = authorization.get("auto_policy")
            if not isinstance(policy, dict):
                raise RuntimeError("limited-auto policy is unavailable at final check")
            self._final_auto_dynamic_risk(command, policy, quote, account)
        return quote, account

    def _execute_order(self, command, message_id):
        journal_path = self._journal_path(command.get("client_order_key", ""))
        if os.path.exists(journal_path):
            existing = _load_json(journal_path)
            if existing.get("state") == "PRE_SUBMIT":
                self._pause_auto_locally(command, "existing PRE_SUBMIT journal")
                self._ack(command, "SUBMIT_UNKNOWN", message_id, {"reason": "existing PRE_SUBMIT journal"})
            else:
                self._ack(command, existing.get("state", "ADAPTER_REJECTED"), message_id, {"duplicate": True, **(existing.get("details") or {})})
            return
        if command.get("execution_mode") == "OBSERVE_ONLY":
            self._final_risk(command)
            self._write_journal(command, "OBSERVE_ONLY_ACKNOWLEDGED")
            self._ack(command, "OBSERVE_ONLY_ACKNOWLEDGED", message_id)
            return
        self._final_risk(command)
        mapping = self._mapping(command)
        self._write_journal(command, "PRE_SUBMIT")
        try:
            order_id = self.make_order_req(
                exchange=command["exchange"],
                instrument_id=command["instrument_id"],
                volume=command["volume"],
                price=command["limit_price"],
                order_direction=mapping["order_direction"],
                offset=mapping["offset"],
                order_type=mapping["order_type"],
                investor=self._config["investor_id"],
                hedgeflag=mapping["hedgeflag"],
                market=mapping["market"],
                memo=command["memo_token"],
            )
        except Exception as exc:
            self._pause_auto_locally(command, "native order submission returned an uncertain result")
            self._ack(command, "SUBMIT_UNKNOWN", message_id, {"error": self._redact(exc)})
            return
        if isinstance(order_id, bool):
            self._pause_auto_locally(command, "unexpected boolean native order id")
            self._ack(command, "SUBMIT_UNKNOWN", message_id, {"reason": "unexpected boolean native order id"})
            return
        try:
            parsed_order_id = int(order_id) if order_id is not None else None
        except Exception:
            self._pause_auto_locally(command, "non-integer native order id")
            self._ack(command, "SUBMIT_UNKNOWN", message_id, {"reason": "non-integer native order id"})
            return
        if parsed_order_id == -1:
            details = {"pythongo_order_id": parsed_order_id}
            self._write_journal(command, "FAILED_BEFORE_SEND", details)
            self._ack(command, "FAILED_BEFORE_SEND", message_id, details)
            return
        if parsed_order_id is None or parsed_order_id < 0:
            self._pause_auto_locally(command, "unexpected native order id")
            self._ack(command, "SUBMIT_UNKNOWN", message_id, {"pythongo_order_id": parsed_order_id, "reason": "unexpected native order id"})
            return
        details = {"pythongo_order_id": parsed_order_id}
        try:
            self._write_journal(command, "SEND_RETURNED", details)
            self._ack(command, "SEND_RETURNED", message_id, details)
        except Exception as exc:
            self._pause_auto_locally(command, "post-submit journal or acknowledgement persistence failed")
            self._ack(command, "SUBMIT_UNKNOWN", message_id, {"pythongo_order_id": parsed_order_id, "error": self._redact(exc)})

    def _execute_cancel(self, command, message_id):
        if command.get("account_alias") != self._config["account_alias"] or not isinstance(command.get("pythongo_order_id"), int):
            raise RuntimeError("invalid exact cancel command")
        if self._local_halted() and not self._config["allow_cancel_while_halted"]:
            raise RuntimeError("cancel while halted is disabled")
        try:
            result = self.cancel_order(command["pythongo_order_id"])
            parsed = int(result) if result is not None and not isinstance(result, bool) else None
            status = "CANCEL_REQUEST_SENT" if parsed == 0 else "CANCEL_RECONCILIATION_REQUIRED"
            details = {"cancel_return": _json_value(result)}
        except Exception as exc:
            status = "CANCEL_RECONCILIATION_REQUIRED"
            details = {"error": self._redact(exc)}
        self._ack(command, status, message_id, details, "control_acks")

    def _emit_account(self):
        payload = {"type": "ACCOUNT_SNAPSHOT", "account_alias": self._config["account_alias"], "snapshot_id": "acct_" + uuid.uuid4().hex, "captured_at": _iso(), "account": self._account()}
        self._write_event("ACCOUNT_SNAPSHOT", payload)

    def _emit_positions(self, simple=True):
        payload = {"type": "POSITION_SNAPSHOT", "account_alias": self._config["account_alias"], "snapshot_id": "pos_" + uuid.uuid4().hex, "snapshot_kind": "SIMPLE" if simple else "FULL", "captured_at": _iso(), "positions": self._positions(simple=simple)}
        self._write_event("POSITION_SNAPSHOT", payload)

    def _emit_quotes(self, instruments):
        quotes = []
        for item in instruments:
            key = (item["exchange"], item["instrument_id"])
            cached = self._latest_ticks.get(key)
            if cached:
                quotes.append(cached["payload"])
        if quotes:
            payload = {"type": "QUOTE_SNAPSHOT", "account_alias": self._config["account_alias"], "snapshot_id": "quote_" + uuid.uuid4().hex, "captured_at": _iso(), "quotes": quotes}
            self._write_event("QUOTE_SNAPSHOT", payload)

    def _emit_kline(self, instruments, settings):
        if MarketCenter is None or KLineStyle is None:
            raise RuntimeError("MarketCenter is unavailable")
        interval = str(settings.get("interval") or "M1")
        count = min(500, max(1, int(settings.get("count") or 100)))
        style = getattr(KLineStyle, interval, None)
        if style is None:
            raise RuntimeError("unsupported K-line interval")
        center = getattr(self, "market_center", None) or MarketCenter()
        for item in instruments:
            bars = center.get_kline_data(exchange=item["exchange"], instrument_id=item["instrument_id"], style=style, count=-count)
            payload = {"type": "KLINE_SNAPSHOT", "account_alias": self._config["account_alias"], "snapshot_id": "kline_" + uuid.uuid4().hex, "captured_at": _iso(), "exchange": item["exchange"], "instrument_id": item["instrument_id"], "interval": interval, "bars": _json_value(bars)}
            self._write_event("KLINE_SNAPSHOT", payload)

    def _request_sync(self, command, message_id):
        scopes = command.get("scopes") or []
        instruments = command.get("instruments") or []
        warnings = []
        if "ACCOUNT" in scopes:
            self._emit_account()
        if "POSITION" in scopes:
            self._emit_positions(simple=True)
            self._emit_positions(simple=False)
        if "QUOTE" in scopes:
            for item in instruments:
                key = (item["exchange"], item["instrument_id"])
                self._requested_instruments.add(key)
                try:
                    self.sub_market_data(exchange=item["exchange"], instrument_id=item["instrument_id"])
                except Exception as exc:
                    warnings.append("subscribe failed: %s" % exc)
            self._emit_quotes(instruments)
            if not any((item["exchange"], item["instrument_id"]) in self._latest_ticks for item in instruments):
                warnings.append("quotes will be emitted after the next tick")
        if "KLINE" in scopes:
            try:
                self._emit_kline(instruments, command.get("kline") or {})
            except Exception as exc:
                warnings.append("K-line sync failed: %s" % exc)
        if "ORDER" in scopes or "TRADE" in scopes:
            warnings.append("order/trade sync is callback-and-journal based; public restart replay API is not assumed")
        self._ack(command, "SYNC_COMPLETED", message_id, {"warnings": warnings}, "control_acks")

    def _process_file(self, path):
        if os.path.getsize(path) > self._config["max_message_bytes"]:
            raise RuntimeError("message too large")
        message = _load_json(path)
        self._verify_envelope(message, {"TRADE_INTENT", "CANCEL_ORDER", "REQUEST_SYNC", "P0_TEST_ORDER", "P0_VALIDATION_LEG"})
        command = message["payload"]
        kind = command.get("type")
        if kind == "EXECUTE_ORDER":
            self._execute_order(command, message["message_id"])
        elif kind == "CANCEL_ORDER":
            self._execute_cancel(command, message["message_id"])
        elif kind == "REQUEST_SYNC":
            self._request_sync(command, message["message_id"])
        elif kind == "P0_TEST_ORDER":
            self._execute_p0_test_order(command, message["message_id"])
        elif kind == "P0_VALIDATION_LEG":
            self._execute_p0_validation_leg(command, message["message_id"])
        else:
            raise RuntimeError("unknown command type")

    def _claim_path(self, path):
        claimed = path + ".processing-" + uuid.uuid4().hex
        try:
            os.replace(path, claimed)
        except FileNotFoundError:
            return None
        return claimed

    def poll_commands(self):
        if not self._config:
            return
        if not self._poll_lock.acquire(blocking=False):
            return
        try:
            paths = []
            for folder in ("control", "commands"):
                directory = os.path.join(self._partition, folder)
                for name in sorted(name for name in os.listdir(directory) if name.endswith(".json")):
                    paths.append(os.path.join(directory, name))
                    if len(paths) >= self._config["max_batch"]:
                        break
                if len(paths) >= self._config["max_batch"]:
                    break
            for path in paths:
                if self._config["command_dispatch_mode"] == "TICK_DISPATCH" and os.path.dirname(path).endswith("commands"):
                    claimed = self._claim_path(path)
                    if claimed is not None:
                        with self._pending_lock:
                            self._pending_paths.append(claimed)
                    continue
                self._process_path(path)
        finally:
            self._last_scan_at = time.time()
            self._poll_lock.release()

    def _process_path(self, path):
        if path.endswith(".json"):
            path = self._claim_path(path)
            if path is None:
                return
        try:
            self._process_file(path)
            self._archive(path)
        except Exception as exc:
            if isinstance(exc, FileNotFoundError) and not os.path.exists(path):
                return
            try:
                message = _load_json(path)
                command = message.get("payload") or {}
                folder = "command_acks" if command.get("type") in ("EXECUTE_ORDER", "P0_TEST_ORDER", "P0_VALIDATION_LEG") else "control_acks"
                status = "ADAPTER_REJECTED"
                if command.get("type") == "EXECUTE_ORDER":
                    journal = self._journal_path(command.get("client_order_key", ""))
                    try:
                        state = _load_json(journal).get("state") if os.path.exists(journal) else None
                    except Exception:
                        state = None
                    if state == "PRE_SUBMIT":
                        status = "SUBMIT_UNKNOWN"
                    elif state in ("SEND_RETURNED", "FAILED_BEFORE_SEND", "OBSERVE_ONLY_ACKNOWLEDGED"):
                        status = state
                    if command.get("execution_mode") == "LIMITED_AUTO":
                        self._pause_auto_locally(command, "adapter rejected or could not classify an automatic command")
                elif command.get("type") == "P0_VALIDATION_LEG":
                    journal = self._journal_path("p0leg_" + command.get("validation_id", ""))
                    try:
                        state = _load_json(journal).get("state") if os.path.exists(journal) else None
                    except Exception:
                        state = None
                    if state == "PRE_SUBMIT":
                        status = "SUBMIT_UNKNOWN"
                    elif state in ("P0_LEG_SEND_RETURNED", "FAILED_BEFORE_SEND"):
                        status = state
                self._ack(command, status, message.get("message_id"), {"error": self._redact(exc)}, folder)
            except Exception:
                pass
            self._dead_letter(path)

    def _drain_tick_dispatch(self):
        with self._pending_lock:
            paths = self._pending_paths[:1]
            self._pending_paths = self._pending_paths[1:]
        for path in paths:
            if os.path.exists(path):
                self._process_path(path)

    def heartbeat(self):
        if not self._config:
            return
        payload = {
            "type": "HEARTBEAT",
            "account_alias": self._config["account_alias"],
            "adapter_instance": self._config["adapter_instance"],
            "status": self._status,
            "mode": self._config["pythongo_mode"],
            "profile_status": self._profile_status,
            "local_halt": self._local_halted(),
            "limited_auto_protocol": 1,
            "limited_auto_local_pause": bool(self._active_auto_pause()),
            "margin_policy_generation": self._config["margin_reference_policy_generation"],
            "margin_policy_hash": self._config["margin_reference_policy_hash"],
            "last_command_scan_at": dt.datetime.fromtimestamp(self._last_scan_at, dt.timezone.utc).isoformat() if self._last_scan_at else None,
            "occurred_at": _iso(),
        }
        self._write_event("HEARTBEAT", payload)
        self._last_heartbeat_at = time.time()
        try:
            self.output("[WorkBuddy-PythonGO][HEARTBEAT] account=%s adapter=%s status=%s mode=%s profile=%s halt=%s" % (self._config["account_alias"], self._config["adapter_instance"], self._status, self._config["pythongo_mode"], self._profile_status, payload["local_halt"]))
        except Exception:
            pass

    def _background_loop(self, stop_event):
        heartbeat_deadline = time.monotonic() + self._config["heartbeat_seconds"]
        while not stop_event.wait(1.0):
            try:
                self.poll_commands()
            except Exception as exc:
                try:
                    self.output("[WorkBuddy-PythonGO][POLL-ERROR] %s" % self._redact(exc))
                except Exception:
                    pass
            now = time.monotonic()
            if now >= heartbeat_deadline:
                try:
                    self.heartbeat()
                except Exception as exc:
                    try:
                        self.output("[WorkBuddy-PythonGO][HEARTBEAT-ERROR] %s" % self._redact(exc))
                    except Exception:
                        pass
                heartbeat_deadline = now + self._config["heartbeat_seconds"]

    def _start_background_loop(self):
        prior_stop = self._background_stop
        prior_stop.set()
        prior_thread = self._background_thread
        if prior_thread is not None and prior_thread.is_alive():
            prior_thread.join(timeout=2.0)
        stop_event = threading.Event()
        thread = threading.Thread(
            target=self._background_loop,
            args=(stop_event,),
            name="workbuddy-" + self._config["adapter_instance"],
            daemon=True,
        )
        self._background_stop = stop_event
        self._background_thread = thread
        thread.start()
        if not thread.is_alive():
            raise RuntimeError("adapter background loop did not start")

    def _stop_background_loop(self):
        self._background_stop.set()
        thread = self._background_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self._background_thread = None

    def on_init(self):
        super().on_init()
        try:
            self._load_config()
            self._require_native_base()
            self._status = "INITIALIZED"
            self.heartbeat()
        except Exception as exc:
            self._status = "CONFIG_ERROR"
            try:
                self.output("[WorkBuddy-PythonGO][ERROR] %s" % self._redact(exc))
            except Exception:
                pass

    def on_start(self):
        super().on_start()
        try:
            if self._config is None:
                self._load_config()
            self._require_native_base()
            if self._config["pythongo_mode"] != "OBSERVE_ONLY" and self._profile_status != "VALID":
                raise RuntimeError("non-observe mode requires a valid bound Profile")
            self._status = "READY"
            self._start_background_loop()
            self.poll_commands()
            self.heartbeat()
        except Exception as exc:
            self._status = "HALTED"
            try:
                self.output("[WorkBuddy-PythonGO][ERROR] %s" % self._redact(exc))
            except Exception:
                pass
            if self._config:
                self.heartbeat()

    def on_stop(self):
        self._status = "STOPPED"
        if self._config:
            try:
                self.heartbeat()
            except Exception:
                pass
        self._stop_background_loop()
        super().on_stop()

    def on_tick(self, tick):
        super().on_tick(tick)
        try:
            payload = self._normalize_tick(tick)
            key = (payload["exchange"], payload["instrument_id"])
            self._latest_ticks[key] = {"payload": payload, "received_epoch": time.time()}
            if key in self._requested_instruments:
                self._emit_quotes([{"exchange": key[0], "instrument_id": key[1]}])
            if self._config and self._config["command_dispatch_mode"] == "TICK_DISPATCH":
                if time.time() - self._last_scan_at >= 1:
                    self.poll_commands()
                self._drain_tick_dispatch()
            if self._config and time.time() - self._last_heartbeat_at >= self._config["heartbeat_seconds"]:
                self.heartbeat()
        except Exception as exc:
            try:
                self.output("[WorkBuddy-PythonGO][TICK-ERROR] %s" % self._redact(exc))
            except Exception:
                pass

    def on_contract_status(self, status):
        super().on_contract_status(status)
        if self._config:
            payload = {"type": "ERROR_EVENT", "account_alias": self._config["account_alias"], "category": "CONTRACT_STATUS", "details": _json_value(status), "occurred_at": _iso()}
            self._write_event("ERROR_EVENT", payload)

    def _normalized_order_status(self, raw):
        profile_mapping = ((self._profile or {}).get("capabilities") or {}).get("order_status_map") or {}
        return profile_mapping.get(str(raw), "UNKNOWN_BROKER_STATUS")

    def _order_payload(self, order, event_type="ORDER_EVENT"):
        memo = _pick(order, ["memo", "Memo"], "")
        return {
            "type": event_type,
            "account_alias": self._config["account_alias"],
            "pythongo_order_id": _pick(order, ["order_id", "OrderID"]),
            "order_sys_id": _pick(order, ["order_sys_id", "OrderSysID"]),
            "exchange": str(_pick(order, ["exchange", "ExchangeID"], "")).upper(),
            "instrument_id": str(_pick(order, ["instrument_id", "InstrumentID"], "")),
            "price": _pick(order, ["price", "Price"]),
            "total_volume": _pick(order, ["total_volume", "TotalVolume"]),
            "traded_volume": _pick(order, ["traded_volume", "TradedVolume"], 0),
            "cancel_volume": _pick(order, ["cancel_volume", "CancelVolume"], 0),
            "direction": _pick(order, ["direction", "Direction"]),
            "offset": _pick(order, ["offset", "Offset"]),
            "hedgeflag": _pick(order, ["hedgeflag", "HedgeFlag"]),
            "memo": str(memo or ""),
            "raw_status": _pick(order, ["status", "OrderStatus"], "已撤销" if event_type == "CANCEL_EVENT" else "未知"),
            "normalized_status": "CANCELLED" if event_type == "CANCEL_EVENT" else self._normalized_order_status(_pick(order, ["status", "OrderStatus"], "未知")),
            "trading_day": str(_pick(order, ["trading_day", "TradingDay"], dt.datetime.now().strftime("%Y%m%d"))),
            "occurred_at": _iso(),
        }

    def on_order(self, order):
        super().on_order(order)
        if self._config:
            payload = self._order_payload(order)
            self._orders[str(payload.get("pythongo_order_id"))] = payload
            self._write_event("ORDER_EVENT", payload)

    def on_trade(self, trade, log=True):
        super().on_trade(trade, log=log)
        if self._config:
            trade_id = str(_pick(trade, ["trade_id", "TradeID"], "") or "")
            if not trade_id:
                material = _canonical(_json_value(trade))
                trade_id = "derived_" + hashlib.sha256(material).hexdigest()[:24]
            payload = {
                "type": "TRADE_EVENT", "account_alias": self._config["account_alias"],
                "trade_id": trade_id,
                "pythongo_order_id": _pick(trade, ["order_id", "OrderID"]),
                "order_sys_id": _pick(trade, ["order_sys_id", "OrderSysID"]),
                "exchange": str(_pick(trade, ["exchange", "ExchangeID"], "")).upper(),
                "instrument_id": str(_pick(trade, ["instrument_id", "InstrumentID"], "")),
                "volume": int(_pick(trade, ["volume", "Volume"], 0) or 0),
                "price": float(_pick(trade, ["price", "Price"], 0) or 0),
                "direction": _pick(trade, ["direction", "Direction"]),
                "offset": _pick(trade, ["offset", "Offset"]),
                "hedgeflag": _pick(trade, ["hedgeflag", "HedgeFlag"]),
                "memo": str(_pick(trade, ["memo", "Memo"], "") or ""),
                "trading_day": str(_pick(trade, ["trading_day", "TradingDay"], dt.datetime.now().strftime("%Y%m%d"))),
                "traded_at": str(_pick(trade, ["trade_time", "TradeTime"], _iso())),
            }
            self._trades[payload["trade_id"]] = payload
            self._write_event("TRADE_EVENT", payload)

    def on_cancel(self, order):
        super().on_cancel(order)
        if self._config:
            self._write_event("CANCEL_EVENT", self._order_payload(order, "CANCEL_EVENT"))

    def on_error(self, error):
        super().on_error(error)
        if self._config:
            raw = _as_dict(error)
            order_id = raw.get("orderID")
            category = "CANCEL_ERROR" if str(raw.get("errCode")) == "0004" else ("ORDER_ERROR" if order_id is not None else "RUNTIME_ERROR")
            payload = {"type": "ERROR_EVENT", "account_alias": self._config["account_alias"], "category": category, "err_code": str(raw.get("errCode") or ""), "err_msg_redacted": self._redact(raw.get("errMsg") or ""), "pythongo_order_id": order_id, "occurred_at": _iso()}
            self._write_event("ERROR_EVENT", payload)
