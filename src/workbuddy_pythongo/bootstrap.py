import argparse
import base64
import json
import os
import secrets
import sys
from importlib import resources

from .margin_policy import bind_margin_policy
from .util import atomic_write_bytes, atomic_write_json


DEFAULT_ALIAS = "main_futures"
DEFAULT_ADAPTER = "pythongo_futures_01"
DEFAULT_KEY_ID = "bridge-local-01"


def _write_new(path, data, force=False):
    if os.path.exists(path) and not force:
        return False
    atomic_write_bytes(path, data)
    return True


def _secret_bytes(length=32):
    return secrets.token_bytes(length)


def _risk_limits():
    return {
        "max_order_volume": 5,
        "max_order_notional": 500000.0,
        "max_position_volume_per_instrument": 20,
        "max_total_position_volume": 100,
        "max_margin_per_order": 50000.0,
        "max_total_margin": 200000.0,
        "max_risk_ratio": 0.8,
        "max_daily_orders": 50,
        "max_daily_cancels": 100,
        "max_daily_loss": 10000.0,
        "max_snapshot_age_seconds": 90,
        "max_quote_age_seconds": 30,
        "preview_ttl_seconds": 120,
        "command_ttl_seconds": 60,
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


def _bridge_config(root):
    return {
        "data_dir": "../data",
        "host": "127.0.0.1",
        "port": 17662,
        "default_mode": "OBSERVE_ONLY",
        "max_message_bytes": 65536,
        "key_file": "../data/secrets/message_keys.json",
        "worker_token_file": "../data/secrets/worker.token",
        "accounts": [{
            "alias": DEFAULT_ALIAS,
            "account_type": "FUTURES",
            "adapter_instance": DEFAULT_ADAPTER,
            "enabled": True,
            "investor_fingerprint": "REPLACE_AFTER_P0_BINDING",
            "instrument_allowlist": [],
            "close_policy": "TODAY_FIRST",
            "risk_limits": _risk_limits(),
        }],
    }


def _adapter_config(root):
    data_dir = os.path.abspath(os.path.join(root, "data"))
    ready = os.path.abspath(os.path.join(root, "pythongo_ready", DEFAULT_ADAPTER))
    limits = _risk_limits()
    adapter = {
        "account_alias": DEFAULT_ALIAS,
        "account_type": "FUTURES",
        "adapter_instance": DEFAULT_ADAPTER,
        "investor_id": "REPLACE_ON_INFINITRADER_HOST",
        "investor_fingerprint": "REPLACE_AFTER_P0_BINDING",
        "data_dir": data_dir,
        "key_file": os.path.join(data_dir, "secrets", "message_keys.json"),
        "mapping_profile": os.path.join(ready, "pythongo_profile.json"),
        "pythongo_mode": "OBSERVE_ONLY",
        "strategy_name": "WorkBuddyPythonGO",
        "expected_profile_id": "REPLACE_AFTER_P0_PROFILE_ID",
        "expected_infinitrader_build": "REPLACE_AFTER_P0_INFINITRADER_BUILD",
        "expected_pythongo_build": "REPLACE_AFTER_P0_PYTHONGO_BUILD",
        "expected_broker_build": "REPLACE_AFTER_P0_BROKER_BUILD",
        "max_batch": 20,
        "max_message_bytes": 65536,
        "adapter_max_order_volume": limits["max_order_volume"],
        "adapter_max_order_notional": limits["max_order_notional"],
        "adapter_max_margin_per_order": limits["max_margin_per_order"],
        "adapter_max_total_margin": limits["max_total_margin"],
        "adapter_max_risk_ratio": limits["max_risk_ratio"],
        "adapter_max_auto_session_notional": limits["max_auto_session_notional"],
        "adapter_max_auto_orders": limits["max_auto_orders"],
        "adapter_min_auto_order_interval_seconds": limits["min_auto_order_interval_seconds"],
        "adapter_max_auto_concurrent_orders": limits["max_auto_concurrent_orders"],
        "adapter_max_auto_instrument_position_notional": limits["max_auto_instrument_position_notional"],
        "adapter_max_auto_account_drawdown": limits["max_auto_account_drawdown"],
        "max_snapshot_age_seconds": limits["max_snapshot_age_seconds"],
        "max_quote_age_seconds": limits["max_quote_age_seconds"],
        "adapter_max_price_deviation_pct": 0.02,
        "allow_cancel_while_halted": True,
        "command_dispatch_mode": "TICK_DISPATCH",
        "heartbeat_seconds": 5,
        "margin_reference_file": os.path.join(data_dir, "reference", "保证金手续费.csv"),
        "margin_reference_schema_version": 2,
        "margin_reference_refresh_max_age_hours": 36,
        "margin_reference_source_warn_age_hours": 168,
        "margin_reference_safety_multiplier": 1.25,
        "margin_reference_require_signature": True,
    }
    return bind_margin_policy(adapter, 1)


def _profile():
    return {
        "protocol_version": "1.0",
        "profile_id": "REPLACE_AFTER_P0_PROFILE_ID",
        "infinitrader_build": "REPLACE_AFTER_P0_INFINITRADER_BUILD",
        "pythongo_build": "REPLACE_AFTER_P0_PYTHONGO_BUILD",
        "broker_build": "REPLACE_AFTER_P0_BROKER_BUILD",
        "account_type": "FUTURES",
        "strategy_name": "WorkBuddyPythonGO",
        "adapter_binding": {
            "account_alias": DEFAULT_ALIAS,
            "account_type": "FUTURES",
            "adapter_instance": DEFAULT_ADAPTER,
            "investor_fingerprint": "REPLACE_AFTER_P0_BINDING",
            "strategy_name": "WorkBuddyPythonGO",
        },
        "verified": False,
        "capabilities": {
            "dispatch_mode": "TICK_DISPATCH",
            "memo_max_bytes": 0,
            "explicit_close_yesterday": False,
            "order_trade_replay_after_restart": False,
            "order_status_map": {},
        },
        "mappings": {},
        "key_id": DEFAULT_KEY_ID,
        "signature": "",
    }


def _mcp_example(root):
    config = os.path.abspath(os.path.join(root, "config", "bridge.json"))
    return {
        "mcpServers": {
            "workbuddy-pythongo": {
                "command": sys.executable,
                "args": ["-m", "workbuddy_pythongo.mcp_server", "--config", config],
            }
        }
    }


def initialize(root=".", force=False):
    root = os.path.abspath(root)
    config_dir = os.path.join(root, "config")
    data_dir = os.path.join(root, "data")
    secret_dir = os.path.join(data_dir, "secrets")
    reference_dir = os.path.join(data_dir, "reference")
    ready_dir = os.path.join(root, "pythongo_ready", DEFAULT_ADAPTER)
    runtime_dir = os.path.join(data_dir, "pythongo_runtime", DEFAULT_ADAPTER)
    queue_dir = os.path.join(data_dir, "queue", DEFAULT_ADAPTER)
    for path in (config_dir, secret_dir, reference_dir, ready_dir):
        os.makedirs(path, exist_ok=True)
    for folder in ("commands", "command_acks", "events", "control", "control_acks", "archive", "dead_letter"):
        os.makedirs(os.path.join(queue_dir, folder), exist_ok=True)
    for folder in ("execution_journal", "heartbeat"):
        os.makedirs(os.path.join(runtime_dir, folder), exist_ok=True)

    created = []
    key_path = os.path.join(secret_dir, "message_keys.json")
    if force or not os.path.exists(key_path):
        key = base64.urlsafe_b64encode(_secret_bytes()).decode("ascii").rstrip("=")
        atomic_write_json(key_path, {"active_key_id": DEFAULT_KEY_ID, "keys": {DEFAULT_KEY_ID: key}})
        created.append(key_path)
    token_path = os.path.join(secret_dir, "worker.token")
    if _write_new(token_path, secrets.token_urlsafe(48).encode("ascii") + b"\n", force):
        created.append(token_path)
    for path in (key_path, token_path):
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    adapter_config_path = os.path.join(ready_dir, "pythongo_adapter.json")
    outputs = [
        (os.path.join(config_dir, "bridge.json"), _bridge_config(root)),
        (adapter_config_path, _adapter_config(root)),
        (os.path.join(ready_dir, "pythongo_profile.json"), _profile()),
    ]
    for path, value in outputs:
        if force or not os.path.exists(path):
            atomic_write_json(path, value)
            created.append(path)
    mcp_path = os.path.join(root, "workbuddy.mcp.example.json")
    mcp_existed = os.path.exists(mcp_path)
    atomic_write_json(mcp_path, _mcp_example(root))
    if not mcp_existed:
        created.append(mcp_path)

    locator_path = os.path.join(ready_dir, "pythongo_adapter.path")
    locator_existed = os.path.exists(locator_path)
    atomic_write_bytes(locator_path, (os.path.abspath(adapter_config_path) + "\n").encode("utf-8"))
    if not locator_existed:
        created.append(locator_path)

    adapter_path = os.path.join(ready_dir, "WorkBuddyPythonGOAdapter.py")
    asset = resources.files("workbuddy_pythongo.assets").joinpath("pythongo_embedded_adapter.py").read_bytes()
    adapter_existed = os.path.exists(adapter_path)
    adapter_current = b""
    if adapter_existed:
        with open(adapter_path, "rb") as stream:
            adapter_current = stream.read()
    if force or adapter_current != asset:
        atomic_write_bytes(adapter_path, asset)
    if not adapter_existed:
        created.append(adapter_path)
    return {
        "root": root,
        "created": created,
        "config": os.path.join(config_dir, "bridge.json"),
        "ready_dir": ready_dir,
        "deployment_files": [adapter_path, locator_path],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Initialize a fail-closed WorkBuddy-PythonGO bridge")
    parser.add_argument("--root", default=".", help="deployment root")
    parser.add_argument("--force", action="store_true", help="replace generated config and secrets")
    args = parser.parse_args(argv)
    result = initialize(args.root, args.force)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
