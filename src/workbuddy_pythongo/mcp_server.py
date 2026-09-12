import argparse
import json
import sys
import urllib.error
import urllib.request

from . import __version__
from .config import load_config
from .modes import RUN_MODES
from .util import new_id, strict_json_loads


def _object(properties=None, required=None):
    return {"type": "object", "properties": properties or {}, "required": required or [], "additionalProperties": False}


STRING = {"type": "string"}
ACCOUNT = {"type": "string", "minLength": 1}
INSTRUMENT = _object({"exchange": STRING, "instrument_id": STRING}, ["exchange", "instrument_id"])
AUTO_ACTION = {"type": "string", "enum": [
    "OPEN_LONG", "OPEN_SHORT", "CLOSE_LONG", "CLOSE_SHORT",
    "CLOSE_TODAY_LONG", "CLOSE_TODAY_SHORT",
    "CLOSE_YESTERDAY_LONG", "CLOSE_YESTERDAY_SHORT",
]}
TRADING_WINDOW = _object({"start": {"type": "string", "pattern": "^[0-2][0-9]:[0-5][0-9]$"}, "end": {"type": "string", "pattern": "^[0-2][0-9]:[0-5][0-9]$"}}, ["start", "end"])
TRADE_REQUEST = _object({
    "account_alias": ACCOUNT,
    "instrument": INSTRUMENT,
    "action": {"type": "string", "enum": [
        "OPEN_LONG", "OPEN_SHORT", "CLOSE_LONG", "CLOSE_SHORT",
        "CLOSE_TODAY_LONG", "CLOSE_TODAY_SHORT",
        "CLOSE_YESTERDAY_LONG", "CLOSE_YESTERDAY_SHORT",
    ]},
    "sizing": _object({"type": {"type": "string", "enum": ["FIXED_VOLUME"]}, "value": {"type": "integer", "minimum": 1}}, ["type", "value"]),
    "price_policy": _object({
        "type": {"type": "string", "enum": ["FIXED_LIMIT"]},
        "limit_price": {"type": "number", "exclusiveMinimum": 0},
        "max_deviation_pct": {"type": "number", "minimum": 0, "maximum": 1},
    }, ["type", "limit_price"]),
    "close_policy": {"type": "string", "enum": ["TODAY_FIRST", "YESTERDAY_FIRST", "EXPLICIT_ONLY"]},
    "hedge_flag": {"type": "string", "enum": ["SPECULATION"]},
    "execution_mode": {"type": "string", "enum": list(RUN_MODES)},
    "source": _object({"type": STRING, "signal_id": STRING, "rule_set_id": STRING, "rule_version": STRING}, ["type", "signal_id"]),
}, ["account_alias", "instrument", "action", "sizing", "price_policy", "hedge_flag", "execution_mode", "source"])


def _tool(name, description, schema, read_only=True, destructive=False, idempotent=None):
    return {
        "name": name,
        "description": description,
        "inputSchema": schema,
        "annotations": {
            "readOnlyHint": read_only,
            "destructiveHint": destructive,
            "idempotentHint": read_only if idempotent is None else bool(idempotent),
            "openWorldHint": False,
        },
    }


TOOLS = [
    _tool("pythongo_health", "Return Worker, Adapter, queue, mode, Profile and halt health.", _object()),
    _tool("list_adapters", "List configured Adapter instances and current health.", _object()),
    _tool("list_account_aliases", "List safe account aliases; never returns real investor IDs.", _object()),
    _tool("query_instruments", "List known allowlisted or observed futures instruments.", _object({"filter": STRING, "cursor": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 500}})),
    _tool("get_quote_snapshot", "Read latest coherent PythonGO quote snapshots. Refresh with request_sync first.", _object({"account_alias": ACCOUNT, "instruments": {"type": "array", "items": INSTRUMENT, "minItems": 1, "maxItems": 100}}, ["account_alias", "instruments"])),
    _tool("get_kline_snapshot", "Read latest cached K-line snapshot; does not call MarketCenter directly.", _object({"account_alias": ACCOUNT, "exchange": STRING, "instrument_id": STRING, "interval": STRING, "count": {"type": "integer", "minimum": 1, "maximum": 500}}, ["account_alias", "exchange", "instrument_id", "interval"])),
    _tool("get_pending_signals", "Read persisted deterministic signals.", _object({"filters": _object(), "cursor": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 500}})),
    _tool("get_account_snapshot", "Read latest normalized futures account snapshot.", _object({"account_alias": ACCOUNT}, ["account_alias"])),
    _tool("get_positions", "Read one coherent SIMPLE or FULL position snapshot.", _object({"account_alias": ACCOUNT, "exchange": STRING, "instrument_id": STRING, "snapshot_kind": {"type": "string", "enum": ["SIMPLE", "FULL"]}}, ["account_alias"])),
    _tool("get_orders", "Read normalized PythonGO orders.", _object({"account_alias": ACCOUNT, "status": {"oneOf": [STRING, {"type": "array", "items": STRING}]}, "trading_day": STRING}, ["account_alias"])),
    _tool("get_trades", "Read normalized PythonGO trades.", _object({"account_alias": ACCOUNT, "trading_day": STRING}, ["account_alias"])),
    _tool("get_trade_intent", "Read asynchronous submission, broker acknowledgement, child-order and trade status.", _object({"intent_id": STRING}, ["intent_id"])),
    _tool("list_trade_intents", "List intents in stable sequence order.", _object({"status": {"oneOf": [STRING, {"type": "array", "items": STRING}]}, "after_seq": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 500}})),
    _tool("get_risk_limits", "Read local hard-risk limits for an account alias.", _object({"account_alias": ACCOUNT}, ["account_alias"])),
    _tool("get_reconciliation_status", "Read latest reconciliation state.", _object({"account_alias": ACCOUNT}, ["account_alias"])),
    _tool("get_audit_events", "Read local audit events.", _object({"correlation_id": STRING, "cursor": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 500}})),
    _tool("get_manual_authorization_status", "Read timed MANUAL_LIVE session and one-time approval state.", _object({"account_alias": ACCOUNT}, ["account_alias"])),
    _tool("request_sync", "Request fresh Adapter snapshots. For a latency-sensitive trade use purpose TRADE and never include KLINE.", _object({
        "account_alias": ACCOUNT,
        "scopes": {"type": "array", "minItems": 1, "uniqueItems": True, "items": {"type": "string", "enum": ["ACCOUNT", "POSITION", "ORDER", "TRADE", "QUOTE", "KLINE"]}},
        "instruments": {"type": "array", "items": INSTRUMENT, "maxItems": 100},
        "kline": _object({"interval": STRING, "count": {"type": "integer", "minimum": 1, "maximum": 500}}),
        "purpose": {"type": "string", "enum": ["GENERAL", "TRADE"]},
    }, ["account_alias", "scopes"]), read_only=False, idempotent=False),
    _tool("preview_trade", "Normalize, split today/yesterday close volume and hard-risk-check a futures trade; creates no executable command.", _object({"trade_request": TRADE_REQUEST}, ["trade_request"])),
    _tool("authorize_manual_trade", "HIGH RISK: authorize one exact unexpired MANUAL_LIVE preview after explicit user confirmation.", _object({
        "preview_id": STRING, "reason": {"type": "string", "minLength": 1, "maxLength": 200},
        "confirm": {"type": "string", "enum": ["AUTHORIZE-MANUAL-TRADE"]},
        "live_minutes": {"type": "integer", "minimum": 1, "maximum": 60},
        "approval_ttl_seconds": {"type": "integer", "minimum": 1, "maximum": 300},
    }, ["preview_id", "reason", "confirm"]), read_only=False, destructive=True, idempotent=False),
    _tool("authorize_manual_session", "HIGH RISK: authorize unlimited order count for one account during a 1-60 minute MANUAL_LIVE window; every order still requires fresh sync, preview and risk checks.", _object({
        "account_alias": ACCOUNT, "minutes": {"type": "integer", "minimum": 1, "maximum": 60},
        "reason": {"type": "string", "minLength": 1, "maxLength": 200},
        "confirm": {"type": "string", "enum": ["AUTHORIZE-TIMED-MANUAL-TRADING"]},
    }, ["account_alias", "minutes", "reason", "confirm"]), read_only=False, destructive=True, idempotent=False),
    _tool("revoke_manual_session", "Immediately revoke timed MANUAL_LIVE and unused one-time approvals.", _object({
        "account_alias": ACCOUNT, "reason": {"type": "string", "minLength": 1, "maxLength": 200},
        "confirm": {"type": "string", "enum": ["REVOKE-TIMED-MANUAL-TRADING"]},
    }, ["account_alias", "reason", "confirm"]), read_only=False, idempotent=True),
    _tool("check_limited_auto_readiness", "Run fail-closed LIMITED_AUTO readiness checks and persist the result; no permit is issued.", _object({"account_alias": ACCOUNT}, ["account_alias"]), read_only=False, idempotent=False),
    _tool("authorize_limited_auto", "HIGH RISK: issue one bounded, version-bound futures LIMITED_AUTO permit. This never changes mode or clears a halt.", _object({
        "account_alias": ACCOUNT,
        "instruments": {"type": "array", "items": INSTRUMENT, "minItems": 1, "maxItems": 100, "uniqueItems": True},
        "actions": {"type": "array", "items": AUTO_ACTION, "minItems": 1, "maxItems": 8, "uniqueItems": True},
        "source_type": {"type": "string", "minLength": 1, "maxLength": 100},
        "rule_set_id": {"type": "string", "minLength": 1, "maxLength": 100},
        "rule_version": {"type": "string", "minLength": 1, "maxLength": 100},
        "max_order_notional": {"type": "number", "exclusiveMinimum": 0},
        "max_order_volume": {"type": "integer", "minimum": 1},
        "max_session_notional": {"type": "number", "exclusiveMinimum": 0},
        "max_orders": {"type": "integer", "minimum": 1},
        "max_concurrent_orders": {"type": "integer", "minimum": 1},
        "max_instrument_position_notional": {"type": "number", "exclusiveMinimum": 0},
        "max_account_drawdown": {"type": "number", "exclusiveMinimum": 0},
        "minutes": {"type": "integer", "minimum": 1, "maximum": 720},
        "min_order_interval_seconds": {"type": "integer", "minimum": 1},
        "max_consecutive_failures": {"type": "integer", "minimum": 1, "maximum": 100},
        "trading_windows": {"type": "array", "items": TRADING_WINDOW, "minItems": 1, "maxItems": 8},
        "reason": {"type": "string", "minLength": 1, "maxLength": 500},
        "confirm": {"type": "string", "enum": ["AUTHORIZE-LIMITED-AUTO-P1"]},
    }, [
        "account_alias", "instruments", "actions", "source_type", "rule_set_id",
        "rule_version", "max_order_notional", "max_order_volume", "max_session_notional",
        "max_orders", "max_concurrent_orders", "max_instrument_position_notional",
        "max_account_drawdown", "trading_windows", "reason", "confirm",
    ]), read_only=False, destructive=True, idempotent=False),
    _tool("get_limited_auto_status", "Read the latest permit, policy, usage budget, pause state and health reasons.", _object({"account_alias": ACCOUNT}, ["account_alias"])),
    _tool("resume_limited_auto", "HIGH RISK: rotate the generation and resume one paused, unexpired permit after readiness recovers.", _object({
        "account_alias": ACCOUNT, "reason": {"type": "string", "minLength": 1, "maxLength": 500},
        "confirm": {"type": "string", "enum": ["RESUME-LIMITED-AUTO-P1"]},
    }, ["account_alias", "reason", "confirm"]), read_only=False, destructive=True, idempotent=False),
    _tool("revoke_limited_auto", "Immediately revoke the current LIMITED_AUTO permit and rotate its generation.", _object({
        "account_alias": ACCOUNT, "reason": {"type": "string", "minLength": 1, "maxLength": 500},
        "confirm": {"type": "string", "enum": ["REVOKE-LIMITED-AUTO-P1"]},
    }, ["account_alias", "reason", "confirm"]), read_only=False, destructive=True, idempotent=True),
    _tool("submit_trade_intent", "Recompute risk, persist and enqueue an intent, then return asynchronous status immediately; poll get_trade_intent for broker progress.", _object({"preview_id": STRING, "approval_context": _object({"local_approval_id": STRING})}, ["preview_id"]), read_only=False, idempotent=True),
    _tool("cancel_order", "Request cancellation of one exact active PythonGO order owned by this bridge.", _object({"account_alias": ACCOUNT, "bridge_order_id": STRING, "reason": {"type": "string", "minLength": 1, "maxLength": 200}}, ["account_alias", "bridge_order_id", "reason"]), read_only=False, destructive=True, idempotent=True),
    _tool("halt_trading", "Fail closed and halt all new trading. Only the local Console can clear it.", _object({"reason": {"type": "string", "minLength": 1, "maxLength": 500}}, ["reason"]), read_only=False, destructive=True, idempotent=True),
    _tool("request_reconciliation", "Mark an account reconciliation-required and request fresh account, position, order and trade facts.", _object({"account_alias": ACCOUNT, "reason": {"type": "string", "minLength": 1, "maxLength": 500}}, ["account_alias", "reason"]), read_only=False, idempotent=False),
]


class WorkerClient:
    def __init__(self, endpoint, token, timeout=10):
        self.url = endpoint.rstrip("/") + "/rpc"
        self.token = token
        self.timeout = timeout

    def call(self, method, params):
        payload = json.dumps({"method": method, "params": params, "request_id": new_id("req")}, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        request = urllib.request.Request(self.url, data=payload, method="POST", headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.token})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (OSError, ValueError, urllib.error.HTTPError):
            return {"ok": False, "request_id": new_id("req"), "as_of": None, "data": None, "warnings": [], "error": {"code": "BRIDGE_UNAVAILABLE", "message": "cannot reach local bridge worker", "details": {}}}


def read_message(stream, max_bytes=1048576):
    while True:
        line = stream.readline(max_bytes + 1)
        if not line:
            return None
        if len(line) > max_bytes:
            raise ValueError("MCP message exceeds size limit")
        if not line.strip():
            continue
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1].strip())
            if length <= 0 or length > max_bytes:
                raise ValueError("MCP message exceeds size limit")
            while True:
                header = stream.readline()
                if header in (b"\r\n", b"\n", b""):
                    break
            return strict_json_loads(stream.read(length).decode("utf-8"))
        return strict_json_loads(line.decode("utf-8"))


def write_message(stream, message):
    stream.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")
    stream.flush()


def serve(client, input_stream=None, output_stream=None, max_bytes=1048576):
    input_stream = input_stream or sys.stdin.buffer
    output_stream = output_stream or sys.stdout.buffer
    while True:
        request_id = None
        try:
            message = read_message(input_stream, max_bytes)
            if message is None:
                return 0
            if "id" not in message:
                continue
            request_id = message["id"]
            method = message.get("method")
            if method == "initialize":
                requested = (message.get("params") or {}).get("protocolVersion")
                result = {"protocolVersion": requested or "2025-03-26", "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": "workbuddy-pythongo-bridge", "version": __version__}, "instructions": "Trade tools fail closed. Sync and preview before submit. LIMITED_AUTO additionally requires an explicit bounded permit bound to instruments, actions and a strategy version. Mode changes, Profile signing and halt recovery require the local Console."}
                write_message(output_stream, {"jsonrpc": "2.0", "id": request_id, "result": result})
            elif method == "ping":
                write_message(output_stream, {"jsonrpc": "2.0", "id": request_id, "result": {}})
            elif method == "tools/list":
                write_message(output_stream, {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}})
            elif method == "tools/call":
                params = message.get("params") or {}
                name = params.get("name")
                if not any(tool["name"] == name for tool in TOOLS):
                    raise ValueError("unknown tool")
                response = client.call(name, params.get("arguments") or {})
                text = json.dumps(response, ensure_ascii=False, separators=(",", ":"))
                write_message(output_stream, {"jsonrpc": "2.0", "id": request_id, "result": {"content": [{"type": "text", "text": text}], "isError": not response.get("ok", False)}})
            else:
                write_message(output_stream, {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Method not found"}})
        except Exception as exc:
            if request_id is not None:
                write_message(output_stream, {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": str(exc)}})


def main(argv=None):
    parser = argparse.ArgumentParser(description="stdio MCP frontend for WorkBuddy-PythonGO")
    parser.add_argument("--config", default=None)
    parser.add_argument("--endpoint", default=None)
    parser.add_argument("--token-file", default=None)
    args = parser.parse_args(argv)
    config = load_config(args.config)
    endpoint = args.endpoint or "http://%s:%d" % (config.host, config.port)
    token_file = args.token_file or config.worker_token_file
    try:
        with open(token_file, "r", encoding="ascii") as stream:
            token = stream.read().strip()
    except OSError as exc:
        print("cannot read worker token: %s" % exc, file=sys.stderr)
        return 2
    if len(token) < 32:
        print("worker token is too short", file=sys.stderr)
        return 2
    return serve(WorkerClient(endpoint, token), max_bytes=config.max_message_bytes)


if __name__ == "__main__":
    sys.exit(main())
