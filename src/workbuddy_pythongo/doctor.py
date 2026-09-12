import hashlib
import json
import os
from importlib import resources

from .assets.pythongo_embedded_adapter import (
    CONFIG_FIELDS, PERFORMANCE_CONFIG_DEFAULTS, PROFILE_FIELDS,
)
from .config import load_config
from .console import _validate_profile
from .security import KeyRing
from .worker import build_runtime


def _read_json(path):
    with open(path, "r", encoding="utf-8") as stream:
        return json.load(stream)


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def run_doctor(config_path=None):
    checks = []

    def add(name, ok, detail, severity="error"):
        checks.append({"name": name, "ok": bool(ok), "severity": severity, "detail": str(detail)})

    try:
        config = load_config(config_path)
        add("bridge_config", True, config.path)
    except Exception as exc:
        add("bridge_config", False, exc)
        return {"ok": False, "checks": checks}
    try:
        keyring = KeyRing.load(config.key_file)
        add("message_keyring", True, "active key is present and at least 256 bits")
    except Exception as exc:
        add("message_keyring", False, exc)
        return {"ok": False, "checks": checks}
    try:
        with open(config.worker_token_file, "r", encoding="ascii") as stream:
            token = stream.read().strip()
        add("worker_token", len(token) >= 32, "present; length=%d" % len(token))
    except OSError as exc:
        add("worker_token", False, exc)
    try:
        _, database, core = build_runtime(config.path)
        health = core.pythongo_health()
        add("sqlite", True, database.path)
    except Exception as exc:
        add("runtime", False, exc)
        return {"ok": False, "checks": checks}

    root = os.path.abspath(os.path.join(os.path.dirname(config.path), os.pardir))
    packaged = resources.files("workbuddy_pythongo.assets").joinpath("pythongo_embedded_adapter.py").read_bytes()
    with database.connect() as connection:
        row = connection.execute("SELECT value FROM system_state WHERE key='mode'").fetchone()
        worker_mode = row["value"] if row else config.default_mode
    for account in config.accounts.values():
        prefix = account.alias + ":"
        ready = os.path.join(root, "pythongo_ready", account.adapter_instance)
        adapter_py = os.path.join(ready, "WorkBuddyPythonGOAdapter.py")
        adapter_json = os.path.join(ready, "pythongo_adapter.json")
        locator_path = os.path.join(ready, "pythongo_adapter.path")
        try:
            with open(adapter_py, "rb") as stream:
                deployed_hash = _hash(stream.read())
            add(prefix + "adapter_hash", deployed_hash == _hash(packaged), "packaged Adapter hash matches ready bundle")
        except OSError as exc:
            add(prefix + "adapter_hash", False, exc)
        try:
            with open(locator_path, "r", encoding="utf-8-sig") as stream:
                locator_target = os.path.expandvars(stream.read().strip())
            if not os.path.isabs(locator_target):
                locator_target = os.path.join(os.path.dirname(locator_path), locator_target)
            locator_ok = os.path.normcase(os.path.abspath(locator_target)) == os.path.normcase(os.path.abspath(adapter_json))
            add(prefix + "adapter_config_locator", locator_ok, "%s -> %s" % (locator_path, locator_target))
        except OSError as exc:
            add(prefix + "adapter_config_locator", False, exc)
        try:
            adapter = _read_json(adapter_json)
            fields = set(adapter) if isinstance(adapter, dict) else set()
            required = CONFIG_FIELDS - set(PERFORMANCE_CONFIG_DEFAULTS)
            valid = required <= fields <= CONFIG_FIELDS
            add(prefix + "adapter_config_schema", valid, adapter_json)
            if not valid:
                continue
            identity_ok = (
                adapter["account_alias"] == account.alias
                and adapter["account_type"] == account.account_type
                and adapter["adapter_instance"] == account.adapter_instance
                and adapter["investor_fingerprint"] == account.investor_fingerprint
            )
            add(prefix + "identity_binding", identity_ok, "bridge/Adapter alias, type, instance and fingerprint")
            try:
                investor_ok = keyring.fingerprint_investor(adapter["investor_id"]) == account.investor_fingerprint
            except Exception:
                investor_ok = False
            add(prefix + "investor_binding", investor_ok, "Adapter investor matches the approved keyed fingerprint")
            add(prefix + "mode_sync", adapter["pythongo_mode"] == worker_mode, "worker=%s adapter=%s" % (worker_mode, adapter["pythongo_mode"]))
            limits = account.risk_limits
            risk_ok = (
                adapter["adapter_max_order_volume"] == limits.max_order_volume
                and adapter["adapter_max_order_notional"] == limits.max_order_notional
                and adapter["adapter_max_margin_per_order"] == limits.max_margin_per_order
                and adapter["adapter_max_total_margin"] == limits.max_total_margin
                and adapter["adapter_max_risk_ratio"] == limits.max_risk_ratio
                and adapter["max_snapshot_age_seconds"] == limits.max_snapshot_age_seconds
                and adapter["max_quote_age_seconds"] == limits.max_quote_age_seconds
                and adapter["adapter_max_auto_session_notional"] == limits.max_auto_session_notional
                and adapter["adapter_max_auto_orders"] == limits.max_auto_orders
                and adapter["adapter_min_auto_order_interval_seconds"] == limits.min_auto_order_interval_seconds
                and adapter["adapter_max_auto_concurrent_orders"] == limits.max_auto_concurrent_orders
                and adapter["adapter_max_auto_instrument_position_notional"] == limits.max_auto_instrument_position_notional
                and adapter["adapter_max_auto_account_drawdown"] == limits.max_auto_account_drawdown
            )
            add(prefix + "risk_limit_sync", risk_ok, "duplicated Worker/Adapter hard limits")
            profile = _read_json(adapter["mapping_profile"])
            schema_ok = isinstance(profile, dict) and set(profile) == PROFILE_FIELDS
            add(prefix + "profile_schema", schema_ok, adapter["mapping_profile"])
            if not schema_ok:
                continue
            verified = profile.get("verified") is True and bool(profile.get("signature"))
            if verified:
                try:
                    keyring.verify(profile)
                    signed = True
                except Exception:
                    signed = False
                add(prefix + "profile_signature", signed, "verified Profile signature")
                try:
                    _validate_profile(profile, adapter, account, keyring)
                    binding = True
                except Exception:
                    binding = False
                add(prefix + "profile_p0_binding", binding, "Profile identity, capabilities and mappings")
                builds = (
                    profile["profile_id"] == adapter["expected_profile_id"]
                    and profile["infinitrader_build"] == adapter["expected_infinitrader_build"]
                    and profile["pythongo_build"] == adapter["expected_pythongo_build"]
                    and profile["broker_build"] == adapter["expected_broker_build"]
                )
                add(prefix + "profile_build_binding", builds, "Profile and Adapter expected_* values")
            else:
                severity = "warning" if worker_mode == "OBSERVE_ONLY" else "error"
                add(prefix + "profile_signature", False, "Profile is unverified; only OBSERVE_ONLY is allowed", severity)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            add(prefix + "ready_bundle", False, exc)

    accounts = {item["account_alias"]: item for item in health["accounts"]}
    for account in config.accounts.values():
        item = accounts.get(account.alias, {})
        heartbeat_age = item.get("heartbeat_age_seconds")
        online = bool(
            item.get("adapter_status") == "READY" and
            isinstance(heartbeat_age, (int, float)) and
            heartbeat_age <= account.risk_limits.auto_heartbeat_max_age_seconds
        )
        add(
            account.alias + ":heartbeat", online,
            "adapter=%s age_seconds=%s" % (item.get("adapter_status", "UNKNOWN"), heartbeat_age),
            "warning",
        )
        add(
            account.alias + ":margin_policy_heartbeat",
            item.get("margin_policy_match") is True,
            "worker_generation=%s adapter_generation=%s" % (
                item.get("expected_margin_policy_generation"),
                item.get("margin_policy_generation"),
            ),
            "error" if online else "warning",
        )
    errors = [item for item in checks if not item["ok"] and item["severity"] == "error"]
    warnings = [item for item in checks if not item["ok"] and item["severity"] == "warning"]
    return {"ok": not errors, "errors": len(errors), "warnings": len(warnings), "checks": checks, "health": health}
