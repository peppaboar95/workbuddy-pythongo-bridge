import argparse
import json
import os
import sys

from .config import load_config
from .errors import BridgeError
from .modes import RUN_MODES, normalize_mode
from .security import KeyRing
from .util import atomic_write_json, iso_now, json_text
from .worker import build_runtime


PROFILE_FIELDS = {
    "protocol_version", "profile_id", "infinitrader_build", "pythongo_build",
    "broker_build", "account_type", "strategy_name", "adapter_binding",
    "verified", "capabilities", "mappings", "key_id", "signature",
}
PROFILE_CAPABILITIES = {
    "dispatch_mode", "memo_max_bytes", "explicit_close_yesterday",
    "order_trade_replay_after_restart", "order_status_map",
}
MAPPING_FIELDS = {"order_direction", "offset", "order_type", "hedgeflag", "market"}
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


def _read_json(path):
    with open(path, "r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise BridgeError("CONFIG_ERROR", "%s must contain a JSON object" % path)
    return value


def _deployment_root(config):
    return os.path.abspath(os.path.join(os.path.dirname(config.path), os.pardir))


def _adapter_path(config, account):
    return os.path.join(_deployment_root(config), "pythongo_ready", account.adapter_instance, "pythongo_adapter.json")


def _sync_adapter_mode(config, mode):
    changed = []
    for account in config.accounts.values():
        path = _adapter_path(config, account)
        raw = _read_json(path)
        raw["pythongo_mode"] = mode
        raw.setdefault("command_scan_active_ms", 100)
        raw.setdefault("command_scan_idle_ms", 200)
        raw["pre_subscribe_instruments"] = [
            {"exchange": exchange, "instrument_id": instrument_id}
            for exchange, instrument_id in account.instrument_allowlist
        ]
        atomic_write_json(path, raw)
        changed.append(path)
    return changed


def _remove_authorizations(config):
    removed = []
    for account in config.accounts.values():
        path = os.path.join(config.data_dir, "pythongo_runtime", account.adapter_instance, "local_authorization.json")
        try:
            os.unlink(path)
            removed.append(path)
        except FileNotFoundError:
            pass
    return removed


def _require_verified_profiles(config):
    keyring = KeyRing.load(config.key_file)
    for account in config.accounts.values():
        if not account.enabled:
            continue
        adapter = _read_json(_adapter_path(config, account))
        profile = _read_json(adapter.get("mapping_profile"))
        if profile.get("verified") is not True or not profile.get("signature"):
            raise BridgeError("PROFILE_INVALID", "non-observe mode requires a verified Profile for %s" % account.alias)
        keyring.verify(profile)
        _validate_profile(profile, adapter, account, keyring)
        expected = {
            "expected_profile_id": "profile_id",
            "expected_infinitrader_build": "infinitrader_build",
            "expected_pythongo_build": "pythongo_build",
            "expected_broker_build": "broker_build",
        }
        for adapter_name, profile_name in expected.items():
            if adapter.get(adapter_name) != profile.get(profile_name):
                raise BridgeError("PROFILE_INVALID", "Adapter expected build binding is stale for %s" % account.alias)


def get_non_observe_mode_blockers(config_path):
    """Return the fail-closed gates that currently prevent a trading mode.

    This is a read-only preflight for interactive launchers.  ``set_mode`` keeps
    enforcing the same gates independently so a state change between preflight
    and the actual mode switch cannot bypass them.
    """
    config, database, _ = build_runtime(config_path)
    blockers = []
    try:
        _require_verified_profiles(config)
    except Exception as exc:
        blockers.append({
            "code": "PROFILE_INVALID",
            "message": str(exc),
        })
    with database.connect() as connection:
        halted = connection.execute("SELECT value FROM system_state WHERE key='halted'").fetchone()
        halt_reason = connection.execute("SELECT value FROM system_state WHERE key='halt_reason'").fetchone()
        protection_kind = connection.execute(
            "SELECT value FROM system_state WHERE key='trade_protection_kind'"
        ).fetchone()
        protection_reason = connection.execute(
            "SELECT value FROM system_state WHERE key='trade_protection_reason'"
        ).fetchone()
    if halted and halted["value"] == "true":
        blockers.append({
            "code": "TRADING_HALTED",
            "message": (halt_reason["value"] if halt_reason else "") or "local halt is active",
        })
    elif protection_kind and protection_kind["value"] != "NONE":
        blockers.append({
            "code": "TRADE_PROTECTION_ACTIVE",
            "message": (protection_reason["value"] if protection_reason else "") or protection_kind["value"],
        })
    return blockers


def set_mode(config_path, mode, confirm=None):
    mode = normalize_mode(mode)
    if mode != "OBSERVE_ONLY" and confirm != mode:
        raise BridgeError("CONFIRMATION_REQUIRED", "--confirm must exactly equal %s" % mode)
    config, database, core = build_runtime(config_path)
    if mode != "OBSERVE_ONLY":
        _require_verified_profiles(config)
    _remove_authorizations(config)
    now = iso_now()
    with database.transaction(immediate=True) as connection:
        halted = connection.execute("SELECT value FROM system_state WHERE key='halted'").fetchone()
        protection_kind = connection.execute(
            "SELECT value FROM system_state WHERE key='trade_protection_kind'"
        ).fetchone()
        if mode != "OBSERVE_ONLY" and halted and halted["value"] == "true":
            raise BridgeError("TRADING_HALTED", "clear the local halt before selecting a non-observe mode")
        if mode != "OBSERVE_ONLY" and protection_kind and protection_kind["value"] != "NONE":
            raise BridgeError(
                "TRADE_PROTECTION_ACTIVE",
                "clear the local trade protection before selecting a non-observe mode",
                {"kind": protection_kind["value"]},
            )
        connection.execute(
            "INSERT INTO system_state(key,value,updated_at) VALUES('mode',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
            (mode, now),
        )
        connection.execute("UPDATE approvals SET used_at=? WHERE used_at IS NULL", (now,))
        connection.execute("DELETE FROM system_state WHERE key LIKE 'manual_session:%'")
        connection.execute("UPDATE system_state SET value=?,updated_at=? WHERE key LIKE 'live_until:%'", (now, now))
        for account in config.accounts.values():
            if account.enabled:
                core._next_auto_generation(connection, account.alias)
                connection.execute(
                    """UPDATE auto_permits SET status='REVOKED',revoked_at=?,revoked_reason='mode changed'
                       WHERE account_alias=? AND status IN ('ACTIVE','PAUSED')""",
                    (now, account.alias),
                )
        connection.execute(
            "INSERT INTO audit_log(occurred_at,actor,action,details_json) VALUES(?,?,?,?)",
            (now, "local-console", "SET_MODE", json_text({"mode": mode})),
        )
    changed = _sync_adapter_mode(config, mode)
    return {"mode": mode, "adapter_configs": changed, "authorization_revoked": True}


def clear_halt(config_path, confirm):
    if confirm != "CLEAR-HALT":
        raise BridgeError("CONFIRMATION_REQUIRED", "--confirm must exactly equal CLEAR-HALT")
    config, database, core = build_runtime(config_path)
    now = iso_now()
    with database.transaction(immediate=True) as connection:
        connection.execute("UPDATE system_state SET value='false',updated_at=? WHERE key='halted'", (now,))
        connection.execute("UPDATE system_state SET value='',updated_at=? WHERE key='halt_reason'", (now,))
        connection.execute("UPDATE system_state SET value='NONE',updated_at=? WHERE key='trade_protection_kind'", (now,))
        connection.execute("UPDATE system_state SET value='',updated_at=? WHERE key='trade_protection_reason'", (now,))
        connection.execute("UPDATE system_state SET value='OBSERVE_ONLY',updated_at=? WHERE key='mode'", (now,))
        connection.execute("UPDATE approvals SET used_at=? WHERE used_at IS NULL", (now,))
        connection.execute("DELETE FROM system_state WHERE key LIKE 'manual_session:%'")
        connection.execute("UPDATE system_state SET value=?,updated_at=? WHERE key LIKE 'live_until:%'", (now, now))
        for account in config.accounts.values():
            if account.enabled:
                core._next_auto_generation(connection, account.alias)
                connection.execute(
                    """UPDATE auto_permits SET status='REVOKED',revoked_at=?,revoked_reason='halt cleared'
                       WHERE account_alias=? AND status IN ('ACTIVE','PAUSED')""",
                    (now, account.alias),
                )
        connection.execute(
            "INSERT INTO audit_log(occurred_at,actor,action,details_json) VALUES(?,?,?,?)",
            (now, "local-console", "CLEAR_HALT", json_text({"forced_mode": "OBSERVE_ONLY"})),
        )
    _remove_authorizations(config)
    _sync_adapter_mode(config, "OBSERVE_ONLY")
    for account in config.accounts.values():
        core._write_local_halt(account, False, "cleared by local console", "local-console")
    return {"halted": False, "mode": "OBSERVE_ONLY"}


def p0_test_order(config_path, alias, exchange, instrument_id, confirm, isolated_notional_cap=None):
    if confirm != "AUTHORIZE-P0-ONE-LOT":
        raise BridgeError("CONFIRMATION_REQUIRED", "--confirm must exactly equal AUTHORIZE-P0-ONE-LOT")
    _, _, core = build_runtime(config_path)
    return core.queue_p0_test_order(alias, exchange, instrument_id, isolated_notional_cap)


def p0_validation_leg(config_path, alias, exchange, instrument_id, action, confirm):
    if confirm != "AUTHORIZE-P0-TRADE-LEG":
        raise BridgeError("CONFIRMATION_REQUIRED", "--confirm must exactly equal AUTHORIZE-P0-TRADE-LEG")
    _, _, core = build_runtime(config_path)
    return core.queue_p0_validation_leg(alias, exchange, instrument_id, action)


def bind_investor(config_path, alias, investor_id, confirm):
    if confirm != "BIND-ACCOUNT":
        raise BridgeError("CONFIRMATION_REQUIRED", "--confirm must exactly equal BIND-ACCOUNT")
    if not investor_id or not investor_id.strip():
        raise BridgeError("INVALID_REQUEST", "investor_id must be non-empty")
    normalized_investor_id = investor_id.strip()
    config = load_config(config_path)
    account = config.account(alias)
    keyring = KeyRing.load(config.key_file)
    fingerprint = keyring.fingerprint_investor(normalized_investor_id)
    adapter_path = _adapter_path(config, account)
    adapter = _read_json(adapter_path)
    same_binding = (
        account.investor_fingerprint == fingerprint
        and adapter.get("investor_fingerprint") == fingerprint
        and keyring.fingerprint_investor(adapter.get("investor_id", "")) == fingerprint
    )
    initial_binding = "REPLACE" in account.investor_fingerprint.upper()
    _, database, core = build_runtime(config.path)
    with database.connect() as connection:
        existing_halted_row = connection.execute(
            "SELECT value FROM system_state WHERE key='halted'"
        ).fetchone()
    existing_halted = bool(existing_halted_row and existing_halted_row["value"] == "true")
    initial_setup = initial_binding and not existing_halted
    if same_binding:
        with database.connect() as connection:
            halted = connection.execute("SELECT value FROM system_state WHERE key='halted'").fetchone()
            protection_kind = connection.execute(
                "SELECT value FROM system_state WHERE key='trade_protection_kind'"
            ).fetchone()
        return {
            "account_alias": alias,
            "investor_fingerprint_prefix": fingerprint[:24] + "...",
            "binding_changed": False,
            "profile_reset_to_unverified": False,
            "halted": bool(halted and halted["value"] == "true"),
            "trade_protection_kind": protection_kind["value"] if protection_kind else "NONE",
            "next_step": "binding already matches; no Profile or halt state was changed",
        }
    if initial_setup:
        try:
            core._write_local_halt(account, True, "initial setup: trading is not enabled", "setup")
        except OSError as exc:
            raise BridgeError("HALT_FILE_FAILED", "cannot initialize the local trade lock", {"account": alias}) from exc
        now = iso_now()
        with database.transaction(immediate=True) as connection:
            connection.execute("UPDATE system_state SET value='false',updated_at=? WHERE key='halted'", (now,))
            connection.execute("UPDATE system_state SET value='',updated_at=? WHERE key='halt_reason'", (now,))
            connection.execute(
                "UPDATE system_state SET value='SETUP_LOCK',updated_at=? WHERE key='trade_protection_kind'",
                (now,),
            )
            connection.execute(
                "UPDATE system_state SET value='initial setup: trading is not enabled',updated_at=? "
                "WHERE key='trade_protection_reason'",
                (now,),
            )
            connection.execute("UPDATE system_state SET value='OBSERVE_ONLY',updated_at=? WHERE key='mode'", (now,))
            connection.execute(
                "INSERT INTO audit_log(occurred_at,actor,action,account_alias,details_json) VALUES(?,?,?,?,?)",
                (now, "local-console", "INITIAL_ACCOUNT_BINDING", alias, json_text({"trade_protection_kind": "SETUP_LOCK"})),
            )
        halt = {"halted": False, "trade_protection_kind": "SETUP_LOCK"}
    else:
        halt = core._activate_trade_protection("account binding changed by local console", "ACCOUNT_CHANGE")
        if halt.get("adapter_file_failures"):
            raise BridgeError("HALT_FILE_FAILED", "cannot bind account because a local halt file could not be written", {"accounts": [item["account_alias"] for item in halt["adapter_file_failures"]]})
    bridge_raw = _read_json(config.path)
    found = False
    for item in bridge_raw["accounts"]:
        if item["alias"] == alias:
            item["investor_fingerprint"] = fingerprint
            found = True
    if not found:
        raise BridgeError("ACCOUNT_NOT_FOUND", "account alias is not present in bridge.json")
    adapter["investor_id"] = normalized_investor_id
    adapter["investor_fingerprint"] = fingerprint
    profile_path = adapter["mapping_profile"]
    profile = _read_json(profile_path)
    profile["adapter_binding"]["investor_fingerprint"] = fingerprint
    profile["verified"] = False
    profile["signature"] = ""
    atomic_write_json(profile_path, profile)
    atomic_write_json(adapter_path, adapter)
    atomic_write_json(config.path, bridge_raw)
    return {
        "account_alias": alias,
        "investor_fingerprint_prefix": fingerprint[:24] + "...",
        "binding_changed": True,
        "profile_reset_to_unverified": True,
        "halted": bool(halt.get("halted")),
        "trade_protection_kind": halt.get("trade_protection_kind", "ACCOUNT_CHANGE"),
        "next_step": (
            "queries are available in OBSERVE_ONLY; enable trading later with P0 and Profile signing"
            if initial_setup else
            "review the account change, complete P0 if required, sign the Profile, then clear halt locally"
        ),
    }


def _validate_profile(profile, adapter, account, keyring):
    if set(profile) != PROFILE_FIELDS or profile.get("protocol_version") != "1.0":
        raise BridgeError("PROFILE_INVALID", "Profile fields or protocol version are invalid")
    for name in ("profile_id", "infinitrader_build", "pythongo_build", "broker_build"):
        value = profile.get(name)
        if not isinstance(value, str) or not value.strip() or "REPLACE" in value.upper():
            raise BridgeError("PROFILE_INVALID", "%s still contains a placeholder" % name)
    binding = {
        "account_alias": account.alias,
        "account_type": account.account_type,
        "adapter_instance": account.adapter_instance,
        "investor_fingerprint": account.investor_fingerprint,
        "strategy_name": adapter.get("strategy_name"),
    }
    if profile.get("adapter_binding") != binding:
        raise BridgeError("PROFILE_INVALID", "Profile adapter_binding does not match bridge and Adapter config")
    if profile.get("account_type") != "FUTURES" or profile.get("strategy_name") != adapter.get("strategy_name"):
        raise BridgeError("PROFILE_INVALID", "Profile strategy or account type does not match")
    if keyring.fingerprint_investor(adapter.get("investor_id")) != account.investor_fingerprint:
        raise BridgeError("PROFILE_INVALID", "investor_id does not match the approved fingerprint")
    capabilities = profile.get("capabilities")
    if not isinstance(capabilities, dict) or set(capabilities) != PROFILE_CAPABILITIES:
        raise BridgeError("PROFILE_INVALID", "Profile capabilities must contain the exact v1 fields")
    if capabilities["dispatch_mode"] != adapter.get("command_dispatch_mode"):
        raise BridgeError("PROFILE_INVALID", "dispatch mode does not match Adapter config")
    if isinstance(capabilities["memo_max_bytes"], bool) or not isinstance(capabilities["memo_max_bytes"], int) or capabilities["memo_max_bytes"] < 16:
        raise BridgeError("PROFILE_INVALID", "memo_max_bytes must be at least 16 for the v1 correlation token")
    for name in ("explicit_close_yesterday", "order_trade_replay_after_restart"):
        if not isinstance(capabilities[name], bool):
            raise BridgeError("PROFILE_INVALID", "%s must be a boolean" % name)
    if not isinstance(capabilities["order_status_map"], dict) or not capabilities["order_status_map"]:
        raise BridgeError("PROFILE_INVALID", "P0 order_status_map evidence is required")
    for raw_status, normalized in capabilities["order_status_map"].items():
        if not isinstance(raw_status, str) or not raw_status or normalized not in NORMALIZED_ORDER_STATES:
            raise BridgeError("PROFILE_INVALID", "order_status_map contains an unsupported normalized state")
    mappings = profile.get("mappings")
    if not isinstance(mappings, dict) or not mappings:
        raise BridgeError("PROFILE_INVALID", "at least one verified P0 order mapping is required")
    for key, value in mappings.items():
        parts = key.split(":") if isinstance(key, str) else []
        if len(parts) != 4 or parts[0] != "FUTURES" or parts[1] not in PROFILE_ACTIONS or parts[2:] != ["LIMIT", "GFD"] or not isinstance(value, dict) or set(value) != MAPPING_FIELDS:
            raise BridgeError("PROFILE_INVALID", "invalid mapping entry: %s" % key)
        if value["market"] is not False:
            raise BridgeError("PROFILE_INVALID", "v1 only permits mappings with market=false")
        for name in MAPPING_FIELDS - {"market"}:
            if not isinstance(value[name], (str, int)) or isinstance(value[name], bool) or (isinstance(value[name], str) and not value[name]):
                raise BridgeError("PROFILE_INVALID", "mapping %s.%s has an invalid type" % (key, name))
    if not capabilities["explicit_close_yesterday"] and any(
        key.split(":")[1].startswith("CLOSE_YESTERDAY_") for key in mappings
    ):
        raise BridgeError("PROFILE_INVALID", "CloseYesterday mappings contradict explicit_close_yesterday=false")
    return True


def sign_profile(config_path, alias, confirm):
    if confirm != "VERIFIED-PROFILE":
        raise BridgeError("CONFIRMATION_REQUIRED", "--confirm must exactly equal VERIFIED-PROFILE")
    config = load_config(config_path)
    account = config.account(alias)
    keyring = KeyRing.load(config.key_file)
    adapter_path = _adapter_path(config, account)
    adapter = _read_json(adapter_path)
    profile_path = adapter.get("mapping_profile")
    profile = _read_json(profile_path)
    _validate_profile(profile, adapter, account, keyring)
    profile["verified"] = True
    profile["key_id"] = keyring.active_key_id
    profile["signature"] = keyring.sign(profile)
    adapter["expected_profile_id"] = profile["profile_id"]
    adapter["expected_infinitrader_build"] = profile["infinitrader_build"]
    adapter["expected_pythongo_build"] = profile["pythongo_build"]
    adapter["expected_broker_build"] = profile["broker_build"]
    atomic_write_json(profile_path, profile)
    atomic_write_json(adapter_path, adapter)
    return {"account_alias": alias, "profile_id": profile["profile_id"], "verified": True, "signature_written": True}


def _print(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Local safety console for WorkBuddy-PythonGO")
    parser.add_argument("--config", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status")
    status.add_argument("--audit-limit", type=int, default=0)
    mode = sub.add_parser("set-mode")
    mode.add_argument("mode", choices=RUN_MODES)
    mode.add_argument("--confirm")
    halt = sub.add_parser("halt")
    halt.add_argument("--reason", required=True)
    clear = sub.add_parser("clear-halt")
    clear.add_argument("--confirm", required=True)
    bind = sub.add_parser("bind-investor")
    bind.add_argument("account_alias")
    bind.add_argument("--investor-id")
    bind.add_argument("--confirm", required=True)
    sign = sub.add_parser("sign-profile")
    sign.add_argument("account_alias")
    sign.add_argument("--confirm", required=True)
    approve = sub.add_parser("approve-preview")
    approve.add_argument("preview_id")
    approve.add_argument("--reason", required=True)
    approve.add_argument("--live-minutes", type=int, default=10)
    audit = sub.add_parser("audit")
    audit.add_argument("--limit", type=int, default=50)
    p0_order = sub.add_parser("p0-test-order")
    p0_order.add_argument("account_alias")
    p0_order.add_argument("exchange")
    p0_order.add_argument("instrument_id")
    p0_order.add_argument("--confirm", required=True)
    p0_order.add_argument("--isolated-notional-cap", type=float)
    p0_leg = sub.add_parser("p0-validation-leg")
    p0_leg.add_argument("account_alias")
    p0_leg.add_argument("exchange")
    p0_leg.add_argument("instrument_id")
    p0_leg.add_argument("action", choices=("OPEN_LONG", "CLOSE_TODAY_LONG", "OPEN_SHORT", "CLOSE_TODAY_SHORT"))
    p0_leg.add_argument("--confirm", required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == "set-mode":
            result = set_mode(args.config, args.mode, args.confirm)
        elif args.command == "clear-halt":
            result = clear_halt(args.config, args.confirm)
        elif args.command == "bind-investor":
            investor_id = args.investor_id or input("Investor ID（非密码，明文显示）: ")
            result = bind_investor(args.config, args.account_alias, investor_id, args.confirm)
        elif args.command == "p0-test-order":
            result = p0_test_order(
                args.config, args.account_alias, args.exchange, args.instrument_id,
                args.confirm, args.isolated_notional_cap,
            )
        elif args.command == "p0-validation-leg":
            result = p0_validation_leg(
                args.config, args.account_alias, args.exchange, args.instrument_id,
                args.action, args.confirm,
            )
        elif args.command == "sign-profile":
            result = sign_profile(args.config, args.account_alias, args.confirm)
        else:
            _, _, core = build_runtime(args.config)
            if args.command == "status":
                result = core.pythongo_health()
                if args.audit_limit:
                    result["audit"] = core.get_audit_events(limit=args.audit_limit)
            elif args.command == "halt":
                result = core.halt_trading(args.reason)
            elif args.command == "approve-preview":
                result = core.authorize_manual_trade(args.preview_id, args.reason, "AUTHORIZE-MANUAL-TRADE", args.live_minutes)
            elif args.command == "audit":
                result = core.get_audit_events(limit=args.limit)
            else:
                raise BridgeError("INVALID_REQUEST", "unknown console command")
        _print(result)
        return 0
    except BridgeError as exc:
        print(json.dumps({"ok": False, "error": {"code": exc.code, "message": exc.message, "details": exc.details}}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
