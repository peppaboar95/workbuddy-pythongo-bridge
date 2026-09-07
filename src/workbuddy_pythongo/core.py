import datetime as dt
import json
import math
import os

from .errors import BridgeError, ValidationError
from .futures import age_seconds, build_preview, validate_trade_request
from .security import make_envelope
from .util import (
    atomic_write_json, hash_json, iso_now, json_text, memo_token,
    new_client_order_key, new_id, normalize_instrument, parse_time, utc_now,
)


ACTIVE_ORDER_STATES = {
    "QUEUED", "REPORTED", "WORKING", "PARTIALLY_FILLED", "CANCEL_REQUESTED",
    "SEND_RETURNED", "UNKNOWN_BROKER_STATUS",
}
AUTO_ACTIONS = {
    "OPEN_LONG", "OPEN_SHORT", "CLOSE_LONG", "CLOSE_SHORT",
    "CLOSE_TODAY_LONG", "CLOSE_TODAY_SHORT",
    "CLOSE_YESTERDAY_LONG", "CLOSE_YESTERDAY_SHORT",
}
AUTO_FAILURE_STATES = {
    "ADAPTER_REJECTED", "BROKER_REJECTED", "FAILED", "SUBMIT_UNKNOWN",
    "SEQUENCE_ABORTED",
}
AUTO_PAUSE_REASONS = {
    "AUTO_PROFILE_INVALID", "AUTO_ADAPTER_NOT_READY",
    "AUTO_ADAPTER_MODE_MISMATCH", "AUTO_ADAPTER_LOCALLY_HALTED",
    "AUTO_ADAPTER_LOCALLY_PAUSED", "AUTO_ADAPTER_PROTOCOL_MISMATCH",
    "AUTO_RECONCILIATION_REQUIRED", "AUTO_DEAD_LETTER_PRESENT",
    "AUTO_QUEUE_BACKLOG", "AUTO_SUBMIT_UNKNOWN_PRESENT",
    "AUTO_ACCOUNT_SNAPSHOT_STALE", "AUTO_POSITION_SNAPSHOT_STALE",
    "AUTO_ACCOUNT_DRAWDOWN_EXCEEDED", "AUTO_CONSECUTIVE_FAILURE_LIMIT",
}
CHINA_TZ = dt.timezone(dt.timedelta(hours=8), name="Asia/Shanghai")
P0_ISOLATED_NOTIONAL_CAPS = {("SHFE", "au2610"): 900000.0}
P0_ISOLATED_MARGIN_GUARD_RATIO = 0.20
P0_VALIDATION_NOTIONAL_CAP = 1000000.0
P0_VALIDATION_LEG_MAPPINGS = {
    "OPEN_LONG": ("BUY", "0"),
    "CLOSE_TODAY_LONG": ("SELL", "3"),
    "OPEN_SHORT": ("SELL", "0"),
    "CLOSE_TODAY_SHORT": ("BUY", "3"),
}


def _payload(row):
    return json.loads(row["payload_json"]) if row else None


def _text(value, name, maximum=200):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValidationError("%s must contain 1-%d characters" % (name, maximum))
    return value.strip()


def _number(value, name, minimum=None, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValidationError("%s must be a finite number" % name)
    result = float(value)
    if minimum is not None and result < minimum:
        raise ValidationError("%s is below its minimum" % name)
    if maximum is not None and result > maximum:
        raise ValidationError("%s is above its maximum" % name)
    return result


def _statuses(value, name="status"):
    if value is None:
        return None
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, list) or not 1 <= len(values) <= 50:
        raise ValidationError("%s must be a string or an array of 1-50 strings" % name)
    return [_text(item, name, 64) for item in values]


class BridgeCore:
    def __init__(self, config, database, keyring, file_queue, ingester):
        self.config = config
        self.database = database
        self.keyring = keyring
        self.file_queue = file_queue
        self.ingester = ingester

    def call(self, method, params=None):
        methods = {
            "pythongo_health": self.pythongo_health,
            "list_adapters": self.list_adapters,
            "list_account_aliases": self.list_account_aliases,
            "query_instruments": self.query_instruments,
            "get_quote_snapshot": self.get_quote_snapshot,
            "get_kline_snapshot": self.get_kline_snapshot,
            "get_pending_signals": self.get_pending_signals,
            "get_account_snapshot": self.get_account_snapshot,
            "get_positions": self.get_positions,
            "get_orders": self.get_orders,
            "get_trades": self.get_trades,
            "get_trade_intent": self.get_trade_intent,
            "list_trade_intents": self.list_trade_intents,
            "get_risk_limits": self.get_risk_limits,
            "get_reconciliation_status": self.get_reconciliation_status,
            "get_audit_events": self.get_audit_events,
            "get_manual_authorization_status": self.get_manual_authorization_status,
            "request_sync": self.request_sync,
            "preview_trade": self.preview_trade,
            "authorize_manual_trade": self.authorize_manual_trade,
            "authorize_manual_session": self.authorize_manual_session,
            "revoke_manual_session": self.revoke_manual_session,
            "check_limited_auto_readiness": self.check_limited_auto_readiness,
            "authorize_limited_auto": self.authorize_limited_auto,
            "get_limited_auto_status": self.get_limited_auto_status,
            "resume_limited_auto": self.resume_limited_auto,
            "revoke_limited_auto": self.revoke_limited_auto,
            "submit_trade_intent": self.submit_trade_intent,
            "cancel_order": self.cancel_order,
            "halt_trading": self.halt_trading,
            "request_reconciliation": self.request_reconciliation,
        }
        function = methods.get(method)
        if not function:
            raise BridgeError("METHOD_NOT_FOUND", "unknown bridge method")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise ValidationError("params must be an object")
        try:
            return function(**params)
        except TypeError as exc:
            raise ValidationError("invalid method parameters: %s" % exc)

    @staticmethod
    def _state(connection, key, default=None):
        row = connection.execute("SELECT value FROM system_state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    @staticmethod
    def _set_state(connection, key, value, now=None):
        connection.execute(
            "INSERT INTO system_state(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
            (key, str(value), now or iso_now()),
        )

    def _mode(self, connection):
        return self._state(connection, "mode", self.config.default_mode)

    def _halted(self, connection):
        return self._state(connection, "halted", "false") == "true"

    def _runtime_file(self, account, name):
        return os.path.join(self.config.data_dir, "pythongo_runtime", account.adapter_instance, name)

    def _write_local_authorization(
        self, account, allowed_modes, expires, actor, authorization_id=None,
        authorization_type="MODE_LEASE", auto_permit=None,
    ):
        ttl = max(1, int((expires - utc_now()).total_seconds()))
        envelope = make_envelope(
            self.keyring,
            "LOCAL_AUTHORIZATION",
            dict({
                "account_alias": account.alias,
                "allowed_modes": list(allowed_modes),
                "live_until": expires.isoformat(timespec="milliseconds"),
                "actor": actor,
                "authorization_id": authorization_id,
                "authorization_type": authorization_type,
            }, **({"auto_permit": auto_permit} if auto_permit is not None else {})),
            ttl,
            "pythongo-bridge-worker",
        )
        atomic_write_json(self._runtime_file(account, "local_authorization.json"), envelope)

    def _write_local_halt(self, account, halted, reason, actor):
        envelope = make_envelope(
            self.keyring,
            "LOCAL_HALT",
            {"halted": bool(halted), "reason": reason, "actor": actor, "changed_at": iso_now()},
            10 * 365 * 24 * 3600,
            "pythongo-bridge-worker",
        )
        atomic_write_json(self._runtime_file(account, "local_halt.json"), envelope)

    def pythongo_health(self):
        with self.database.connect() as connection:
            mode = self._mode(connection)
            halted = self._halted(connection)
            accounts = []
            for account in self.config.accounts.values():
                if not account.enabled:
                    continue
                heartbeat = connection.execute(
                    "SELECT * FROM heartbeats WHERE adapter_instance=?", (account.adapter_instance,)
                ).fetchone()
                payload = _payload(heartbeat)
                heartbeat_age = age_seconds(heartbeat["received_at"]) if heartbeat else None
                profile_status = heartbeat["profile_status"] if heartbeat else "UNKNOWN"
                mode_match = bool(heartbeat and heartbeat["mode"] == mode)
                ready = bool(
                    heartbeat and heartbeat["status"] == "READY" and heartbeat_age <= 15
                    and mode_match and not bool((payload or {}).get("local_halt"))
                )
                if mode != "OBSERVE_ONLY" and profile_status != "VALID":
                    ready = False
                accounts.append({
                    "account_alias": account.alias,
                    "adapter_instance": account.adapter_instance,
                    "adapter_status": heartbeat["status"] if heartbeat else "OFFLINE",
                    "heartbeat_age_seconds": heartbeat_age,
                    "adapter_mode": heartbeat["mode"] if heartbeat else None,
                    "mode_match": mode_match,
                    "profile_status": profile_status,
                    "local_halt": bool((payload or {}).get("local_halt")),
                    "ready": ready,
                    "queue_depths": self.file_queue.depths(account.adapter_instance),
                })
            unresolved = connection.execute(
                "SELECT COUNT(*) AS n FROM trade_intents WHERE status='SUBMIT_UNKNOWN'"
            ).fetchone()["n"]
            return {
                "worker": "READY",
                "mode": mode,
                "halted": halted,
                "halt_reason": self._state(connection, "halt_reason", ""),
                "accounts": accounts,
                "unresolved_submit_unknown": unresolved,
                "ready": bool(accounts) and all(item["ready"] for item in accounts) and not halted,
            }

    def list_adapters(self):
        return self.pythongo_health()["accounts"]

    def list_account_aliases(self):
        return [
            {"account_alias": item.alias, "account_type": item.account_type, "enabled": item.enabled}
            for item in self.config.accounts.values() if item.enabled
        ]

    def query_instruments(self, filter=None, cursor=0, limit=100):
        if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
            raise ValidationError("cursor must be a non-negative integer")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValidationError("limit must be between 1 and 500")
        if filter is not None and not isinstance(filter, str):
            raise ValidationError("filter must be a string")
        needle = (filter or "").lower()
        values = set()
        for account in self.config.accounts.values():
            values.update(account.instrument_allowlist)
        with self.database.connect() as connection:
            for row in connection.execute("SELECT DISTINCT exchange,instrument_id FROM quote_snapshots"):
                values.add((row["exchange"], row["instrument_id"]))
        ordered = [
            {"exchange": exchange, "instrument_id": instrument}
            for exchange, instrument in sorted(values)
            if not needle or needle in (exchange + ":" + instrument).lower()
        ]
        page = ordered[cursor:cursor + limit]
        return {"items": page, "next_cursor": cursor + len(page) if cursor + len(page) < len(ordered) else None}

    def _latest_account(self, connection, alias):
        row = connection.execute(
            "SELECT * FROM account_snapshots WHERE account_alias=? ORDER BY captured_at DESC LIMIT 1", (alias,)
        ).fetchone()
        return None if not row else {"snapshot_id": row["snapshot_id"], "captured_at": row["captured_at"], "received_at": row["received_at"], "payload": _payload(row)}

    def _latest_quote(self, connection, alias, exchange, instrument_id):
        row = connection.execute(
            "SELECT * FROM quote_snapshots WHERE account_alias=? AND exchange=? AND instrument_id=? ORDER BY captured_at DESC LIMIT 1",
            (alias, exchange, instrument_id),
        ).fetchone()
        return None if not row else {"snapshot_id": row["snapshot_id"], "captured_at": row["captured_at"], "received_at": row["received_at"], "payload": _payload(row)}

    def _latest_position_run(self, connection, alias, kind="SIMPLE"):
        run = connection.execute(
            "SELECT * FROM position_snapshot_runs WHERE account_alias=? AND snapshot_kind=? ORDER BY captured_at DESC LIMIT 1",
            (alias, kind),
        ).fetchone()
        if not run:
            return None, []
        rows = connection.execute(
            "SELECT * FROM position_snapshots WHERE account_alias=? AND snapshot_id=? ORDER BY exchange,instrument_id,hedgeflag",
            (alias, run["snapshot_id"]),
        ).fetchall()
        return run, [{"captured_at": row["captured_at"], "payload": _payload(row)} for row in rows]

    def get_account_snapshot(self, account_alias):
        self.config.account(account_alias)
        with self.database.connect() as connection:
            value = self._latest_account(connection, account_alias)
        if not value:
            raise BridgeError("ACCOUNT_SNAPSHOT_MISSING", "no account snapshot is available")
        value["age_seconds"] = age_seconds(value["captured_at"])
        return value

    def get_quote_snapshot(self, account_alias, instruments):
        self.config.account(account_alias)
        if not isinstance(instruments, list) or not 1 <= len(instruments) <= 100:
            raise ValidationError("instruments must contain 1-100 entries")
        result = []
        with self.database.connect() as connection:
            for item in instruments:
                if not isinstance(item, dict) or set(item) != {"exchange", "instrument_id"}:
                    raise ValidationError("invalid instrument")
                try:
                    exchange, instrument = normalize_instrument(item["exchange"], item["instrument_id"])
                except ValueError as exc:
                    raise ValidationError(str(exc))
                value = self._latest_quote(connection, account_alias, exchange, instrument)
                if value:
                    value["age_seconds"] = age_seconds(value["captured_at"])
                result.append({"exchange": exchange, "instrument_id": instrument, "snapshot": value})
        return result

    def get_kline_snapshot(self, account_alias, exchange, instrument_id, interval, count=100):
        self.config.account(account_alias)
        try:
            exchange, instrument_id = normalize_instrument(exchange, instrument_id)
        except ValueError as exc:
            raise ValidationError(str(exc))
        interval = _text(interval, "interval", 32)
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 500:
            raise ValidationError("count must be between 1 and 500")
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM kline_snapshots WHERE account_alias=? AND exchange=? AND instrument_id=? AND interval=? ORDER BY captured_at DESC LIMIT 1",
                (account_alias, exchange, instrument_id, interval),
            ).fetchone()
        if not row:
            raise BridgeError("KLINE_NOT_FOUND", "no K-line snapshot is available")
        value = _payload(row)
        value["bars"] = value.get("bars", [])[-count:]
        value["age_seconds"] = age_seconds(row["captured_at"])
        return value

    def get_pending_signals(self, filters=None, cursor=0, limit=100):
        if filters not in (None, {}):
            raise ValidationError("filters does not define any v1 fields")
        if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
            raise ValidationError("cursor must be a non-negative integer")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValidationError("limit must be between 1 and 500")
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT rowid,* FROM market_signals WHERE rowid>? ORDER BY rowid LIMIT ?", (cursor, limit)
            ).fetchall()
        return {"items": [{"seq": row["rowid"], **_payload(row)} for row in rows], "next_cursor": rows[-1]["rowid"] if len(rows) == limit else None}

    def get_positions(self, account_alias, exchange=None, instrument_id=None, snapshot_kind="SIMPLE"):
        self.config.account(account_alias)
        if snapshot_kind not in {"SIMPLE", "FULL"}:
            raise ValidationError("snapshot_kind must be SIMPLE or FULL")
        with self.database.connect() as connection:
            run, rows = self._latest_position_run(connection, account_alias, snapshot_kind)
        if not run:
            raise BridgeError("POSITION_SNAPSHOT_MISSING", "no coherent position snapshot is available")
        items = [item["payload"] for item in rows]
        if exchange is not None:
            exchange = _text(exchange, "exchange", 32).upper()
            items = [item for item in items if item.get("exchange") == exchange]
        if instrument_id is not None:
            instrument_id = _text(instrument_id, "instrument_id", 80)
            items = [item for item in items if item.get("instrument_id") == instrument_id]
        return {"snapshot_id": run["snapshot_id"], "snapshot_kind": snapshot_kind, "captured_at": run["captured_at"], "age_seconds": age_seconds(run["captured_at"]), "items": items}

    def get_orders(self, account_alias, status=None, trading_day=None):
        self.config.account(account_alias)
        query = "SELECT * FROM orders WHERE account_alias=?"
        values = [account_alias]
        if trading_day is not None:
            trading_day = _text(trading_day, "trading_day", 16)
            query += " AND trading_day=?"
            values.append(trading_day)
        statuses = _statuses(status)
        if statuses:
            query += " AND status IN (%s)" % ",".join("?" for _ in statuses)
            values.extend(statuses)
        query += " ORDER BY updated_at DESC LIMIT 500"
        with self.database.connect() as connection:
            rows = connection.execute(query, values).fetchall()
        return [_payload(row) for row in rows]

    def get_trades(self, account_alias, trading_day=None):
        self.config.account(account_alias)
        query = "SELECT * FROM trades WHERE account_alias=?"
        values = [account_alias]
        if trading_day is not None:
            trading_day = _text(trading_day, "trading_day", 16)
            query += " AND trading_day=?"
            values.append(trading_day)
        query += " ORDER BY traded_at DESC LIMIT 500"
        with self.database.connect() as connection:
            rows = connection.execute(query, values).fetchall()
        return [_payload(row) for row in rows]

    def get_trade_intent(self, intent_id):
        intent_id = _text(intent_id, "intent_id", 128)
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM trade_intents WHERE intent_id=?", (intent_id,)).fetchone()
            if not row:
                raise BridgeError("INTENT_NOT_FOUND", "trade intent was not found")
            children = connection.execute("SELECT * FROM child_orders WHERE intent_id=? ORDER BY child_no", (intent_id,)).fetchall()
            trades = connection.execute("SELECT * FROM trades WHERE intent_id=? ORDER BY traded_at", (intent_id,)).fetchall()
        return {"intent": dict(row), "request": _payload(row), "children": [dict(item) for item in children], "trades": [_payload(item) for item in trades]}

    def list_trade_intents(self, status=None, after_seq=0, limit=100):
        if isinstance(after_seq, bool) or not isinstance(after_seq, int) or after_seq < 0:
            raise ValidationError("after_seq must be a non-negative integer")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValidationError("limit must be between 1 and 500")
        query = "SELECT * FROM trade_intents WHERE seq>?"
        values = [after_seq]
        statuses = _statuses(status)
        if statuses:
            query += " AND status IN (%s)" % ",".join("?" for _ in statuses)
            values.extend(statuses)
        query += " ORDER BY seq LIMIT ?"
        values.append(limit)
        with self.database.connect() as connection:
            rows = connection.execute(query, values).fetchall()
        return {"items": [dict(row) for row in rows], "next_seq": rows[-1]["seq"] if len(rows) == limit else None}

    def get_risk_limits(self, account_alias):
        account = self.config.account(account_alias)
        return dict(account.risk_limits.__dict__)

    def get_reconciliation_status(self, account_alias):
        self.config.account(account_alias)
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM reconciliation_runs WHERE account_alias=? ORDER BY started_at DESC LIMIT 1", (account_alias,)
            ).fetchone()
            required = self._state(connection, "reconciliation_required:%s" % account_alias, "false") == "true"
        return {"required": required, "latest_run": dict(row) if row else None}

    def get_audit_events(self, correlation_id=None, cursor=0, limit=100):
        if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
            raise ValidationError("cursor must be a non-negative integer")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValidationError("limit must be between 1 and 500")
        with self.database.connect() as connection:
            rows = connection.execute("SELECT * FROM audit_log WHERE seq>? ORDER BY seq LIMIT ?", (cursor, limit)).fetchall()
        items = [dict(row) for row in rows]
        if correlation_id:
            items = [item for item in items if correlation_id in item["details_json"]]
        return {"items": items, "next_cursor": rows[-1]["seq"] if len(rows) == limit else None}

    def _context(self, connection, account, exchange, instrument_id):
        account_snapshot = self._latest_account(connection, account.alias)
        quote = self._latest_quote(connection, account.alias, exchange, instrument_id)
        run, positions = self._latest_position_run(connection, account.alias, "SIMPLE")
        target = None
        total_position = 0
        for item in positions:
            payload = item["payload"]
            total_position += int(payload.get("position") or 0)
            if payload.get("exchange") == exchange and payload.get("instrument_id") == instrument_id and payload.get("hedgeflag") == "SPECULATION":
                target = {"captured_at": item["captured_at"], "payload": payload}
        if run and target is None:
            empty_side = {
                "position": 0, "frozen_closing": 0,
                "td_close_available": 0, "yd_close_available": 0,
                "td_frozen_closing": 0, "yd_frozen_closing": 0,
            }
            target = {
                "captured_at": run["captured_at"],
                "payload": {
                    "exchange": exchange, "instrument_id": instrument_id, "hedgeflag": "SPECULATION",
                    "position": 0, "long": dict(empty_side), "short": dict(empty_side),
                },
            }
        active_rows = connection.execute(
            "SELECT * FROM orders WHERE account_alias=? AND exchange=? AND instrument_id=? AND status IN (%s)" % ",".join("?" for _ in ACTIVE_ORDER_STATES),
            [account.alias, exchange, instrument_id] + sorted(ACTIVE_ORDER_STATES),
        ).fetchall()
        active = [dict(row) for row in active_rows]
        trading_day = ((account_snapshot["payload"] or {}).get("trading_day") if account_snapshot else None) or utc_now().strftime("%Y%m%d")
        orders = connection.execute("SELECT COUNT(*) AS n FROM orders WHERE account_alias=? AND trading_day=?", (account.alias, trading_day)).fetchone()["n"]
        cancels = connection.execute("SELECT COUNT(*) AS n FROM orders WHERE account_alias=? AND trading_day=? AND status IN ('CANCELLED','PARTIALLY_FILLED_CANCELLED')", (account.alias, trading_day)).fetchone()["n"]
        return account_snapshot, target, quote, active, {"orders": orders, "cancels": cancels, "total_position_volume": total_position, "trading_day": trading_day}

    def _apply_system_gates(self, connection, request, result, account):
        mode = self._mode(connection)
        heartbeat = connection.execute("SELECT * FROM heartbeats WHERE adapter_instance=?", (account.adapter_instance,)).fetchone()
        adapter_gate = {
            "worker_mode": mode,
            "requested_mode": request["execution_mode"],
            "halted": self._halted(connection),
            "reconciliation_required": self._state(connection, "reconciliation_required:%s" % account.alias, "false") == "true",
            "heartbeat_status": heartbeat["status"] if heartbeat else "OFFLINE",
            "heartbeat_age_seconds": age_seconds(heartbeat["received_at"]) if heartbeat else None,
            "adapter_mode": heartbeat["mode"] if heartbeat else None,
            "profile_status": heartbeat["profile_status"] if heartbeat else "UNKNOWN",
            "limited_auto_protocol": (_payload(heartbeat) or {}).get("limited_auto_protocol") if heartbeat else None,
            "limited_auto_local_pause": bool((_payload(heartbeat) or {}).get("limited_auto_local_pause")) if heartbeat else False,
        }
        reasons = list(result["risk"]["reasons"])
        if request["execution_mode"] != mode:
            reasons.append("MODE_MISMATCH")
        if adapter_gate["halted"]:
            reasons.append("TRADING_HALTED")
        if adapter_gate["reconciliation_required"]:
            reasons.append("RECONCILIATION_REQUIRED")
        if not heartbeat or heartbeat["status"] != "READY" or adapter_gate["heartbeat_age_seconds"] > 15:
            reasons.append("ADAPTER_STALE")
        elif heartbeat["mode"] != mode:
            reasons.append("MODE_MISMATCH")
        if mode != "OBSERVE_ONLY" and adapter_gate["profile_status"] != "VALID":
            reasons.append("PROFILE_INVALID")
        if mode == "LIMITED_AUTO":
            permit = self._active_auto_permit_row(connection, account.alias)
            if not permit:
                reasons.append("AUTO_PERMIT_REQUIRED")
                result["limited_auto"] = {"active": False, "permit_id": None}
            else:
                auto_reasons, _, summary = self._limited_auto_policy_reasons(
                    connection, permit, request, result,
                )
                reasons.extend(auto_reasons)
                result["limited_auto"] = dict(summary or {}, active=not auto_reasons)
        result["risk"]["reasons"] = sorted(set(reasons))
        result["risk"]["allowed"] = not result["risk"]["reasons"]
        result["adapter_gate"] = adapter_gate
        result["decision_material"]["adapter_gate"] = {
            "worker_mode": adapter_gate["worker_mode"],
            "requested_mode": adapter_gate["requested_mode"],
            "halted": adapter_gate["halted"],
            "reconciliation_required": adapter_gate["reconciliation_required"],
            "heartbeat_status": adapter_gate["heartbeat_status"],
            "heartbeat_fresh": bool(adapter_gate["heartbeat_age_seconds"] is not None and adapter_gate["heartbeat_age_seconds"] <= 15),
            "adapter_mode": adapter_gate["adapter_mode"],
            "profile_status": adapter_gate["profile_status"],
            "limited_auto_protocol": adapter_gate["limited_auto_protocol"],
            "limited_auto_local_pause": adapter_gate["limited_auto_local_pause"],
        }
        result["decision_material"]["risk_reasons"] = result["risk"]["reasons"]
        result["decision_fingerprint"] = hash_json(result["decision_material"])
        return result

    @staticmethod
    def _clock_minutes(value):
        if not isinstance(value, str):
            raise ValidationError("trading-window times must use HH:MM")
        try:
            parsed = dt.datetime.strptime(value, "%H:%M").time()
        except ValueError as exc:
            raise ValidationError("trading-window times must use HH:MM") from exc
        return parsed.hour * 60 + parsed.minute

    def _next_auto_generation(self, connection, account_alias):
        key = "auto_generation:%s" % account_alias
        try:
            generation = int(self._state(connection, key, "0")) + 1
        except (TypeError, ValueError):
            generation = 1
        self._set_state(connection, key, generation)
        return generation

    def _active_auto_permit_row(self, connection, account_alias):
        row = connection.execute(
            """SELECT * FROM auto_permits WHERE account_alias=? AND status='ACTIVE'
               ORDER BY created_at DESC LIMIT 1""",
            (account_alias,),
        ).fetchone()
        if not row:
            return None
        try:
            current_generation = int(self._state(
                connection, "auto_generation:%s" % account_alias, "0"
            ))
            if (
                int(row["generation"]) != current_generation or
                parse_time(row["starts_at"]) > utc_now() or
                parse_time(row["expires_at"]) <= utc_now()
            ):
                return None
        except (TypeError, ValueError):
            return None
        return row

    @staticmethod
    def _auto_schedule_active(policy, now):
        local_now = now.astimezone(CHINA_TZ)
        minute = local_now.hour * 60 + local_now.minute
        for window in policy.get("trading_windows", []):
            try:
                start_hour, start_minute = [int(item) for item in window["start"].split(":")]
                end_hour, end_minute = [int(item) for item in window["end"].split(":")]
                start = start_hour * 60 + start_minute
                end = end_hour * 60 + end_minute
            except (AttributeError, KeyError, TypeError, ValueError):
                return False
            if start < end and start <= minute < end:
                return True
            if start > end and (minute >= start or minute < end):
                return True
        return False

    def _auto_usage_snapshot(self, connection, permit_id):
        row = connection.execute(
            """SELECT COUNT(*) AS order_count,COALESCE(SUM(notional),0) AS notional,
                      MAX(reserved_at) AS last_reserved_at
               FROM auto_permit_usage WHERE permit_id=?""",
            (permit_id,),
        ).fetchone()
        return {
            "order_count": int(row["order_count"] or 0),
            "notional": float(row["notional"] or 0),
            "last_reserved_at": row["last_reserved_at"],
        }

    def _limited_auto_health_reasons(self, connection, account):
        reasons = []
        limits = account.risk_limits
        if self._mode(connection) != "LIMITED_AUTO":
            reasons.append("AUTO_MODE_NOT_ENABLED")
        if self._halted(connection):
            reasons.append("AUTO_TRADING_HALTED")
        if self._state(connection, "reconciliation_required:%s" % account.alias, "false") == "true":
            reasons.append("AUTO_RECONCILIATION_REQUIRED")
        heartbeat = connection.execute(
            "SELECT * FROM heartbeats WHERE adapter_instance=?", (account.adapter_instance,)
        ).fetchone()
        heartbeat_payload = _payload(heartbeat) or {}
        if (
            not heartbeat or heartbeat["status"] != "READY" or
            age_seconds(heartbeat["received_at"]) > limits.auto_heartbeat_max_age_seconds
        ):
            reasons.append("AUTO_ADAPTER_NOT_READY")
        else:
            if heartbeat["mode"] != "LIMITED_AUTO":
                reasons.append("AUTO_ADAPTER_MODE_MISMATCH")
            if heartbeat["profile_status"] != "VALID":
                reasons.append("AUTO_PROFILE_INVALID")
            if heartbeat_payload.get("local_halt") is True:
                reasons.append("AUTO_ADAPTER_LOCALLY_HALTED")
            if heartbeat_payload.get("limited_auto_local_pause") is True:
                reasons.append("AUTO_ADAPTER_LOCALLY_PAUSED")
            if heartbeat_payload.get("limited_auto_protocol") != 1:
                reasons.append("AUTO_ADAPTER_PROTOCOL_MISMATCH")
        account_snapshot = self._latest_account(connection, account.alias)
        if (
            not account_snapshot or
            age_seconds(account_snapshot["captured_at"]) > limits.max_snapshot_age_seconds
        ):
            reasons.append("AUTO_ACCOUNT_SNAPSHOT_STALE")
        position_run, _ = self._latest_position_run(connection, account.alias, "SIMPLE")
        if (
            not position_run or
            age_seconds(position_run["captured_at"]) > limits.max_snapshot_age_seconds
        ):
            reasons.append("AUTO_POSITION_SNAPSHOT_STALE")
        depths = self.file_queue.depths(account.adapter_instance)
        if int(depths.get("dead_letter", 0) or 0) > 0:
            reasons.append("AUTO_DEAD_LETTER_PRESENT")
        if int(depths.get("commands", 0) or 0) > limits.auto_max_queue_depth:
            reasons.append("AUTO_QUEUE_BACKLOG")
        unresolved = connection.execute(
            "SELECT COUNT(*) AS n FROM trade_intents WHERE account_alias=? AND status='SUBMIT_UNKNOWN'",
            (account.alias,),
        ).fetchone()["n"]
        if unresolved:
            reasons.append("AUTO_SUBMIT_UNKNOWN_PRESENT")
        return sorted(set(reasons))

    def _auto_consecutive_failures(self, connection, permit_id):
        rows = connection.execute(
            """SELECT i.status,MAX(u.reserved_at) AS last_reserved
               FROM auto_permit_usage u JOIN trade_intents i ON i.intent_id=u.intent_id
               WHERE u.permit_id=? GROUP BY i.intent_id,i.status
               ORDER BY last_reserved DESC LIMIT 100""",
            (permit_id,),
        ).fetchall()
        count = 0
        for row in rows:
            if row["status"] in AUTO_FAILURE_STATES:
                count += 1
            else:
                break
        return count

    def _limited_auto_policy_reasons(
        self, connection, permit_row, request, current, include_health=True,
    ):
        reasons = []
        account = self.config.account(current["account_alias"])
        limits = account.risk_limits
        try:
            policy = json.loads(permit_row["policy_json"])
        except (TypeError, ValueError):
            return ["AUTO_POLICY_INVALID"], None, None
        if hash_json(policy) != permit_row["policy_hash"]:
            reasons.append("AUTO_POLICY_HASH_MISMATCH")
        now = utc_now()
        try:
            if parse_time(permit_row["starts_at"]) > now or parse_time(permit_row["expires_at"]) <= now:
                reasons.append("AUTO_PERMIT_EXPIRED")
        except (TypeError, ValueError):
            reasons.append("AUTO_POLICY_INVALID")
        if not self._auto_schedule_active(policy, now):
            reasons.append("AUTO_OUTSIDE_TRADING_WINDOW")
        instrument = "%s:%s" % (
            request["instrument"]["exchange"], request["instrument"]["instrument_id"]
        )
        action = request["action"]
        source = request.get("source") or {}
        if instrument not in policy.get("allowed_instruments", []):
            reasons.append("AUTO_INSTRUMENT_NOT_ALLOWED")
        if action not in policy.get("allowed_actions", []):
            reasons.append("AUTO_ACTION_NOT_ALLOWED")
        if request.get("sizing", {}).get("type") not in policy.get("allowed_sizing_types", []):
            reasons.append("AUTO_SIZING_NOT_ALLOWED")
        if any(source.get(field) != policy.get("source", {}).get(field) for field in ("type", "rule_set_id", "rule_version")):
            reasons.append("AUTO_STRATEGY_BINDING_MISMATCH")
        volume = int(current["requested_volume"])
        notional = float(current["notional"])
        child_count = len(current.get("children", []))
        if volume > int(policy.get("max_order_volume", 0)):
            reasons.append("AUTO_ORDER_VOLUME_EXCEEDED")
        if notional > float(policy.get("max_order_notional", 0)):
            reasons.append("AUTO_ORDER_NOTIONAL_EXCEEDED")
        if (
            int(policy.get("max_order_volume", 0)) > limits.max_order_volume or
            float(policy.get("max_order_notional", 0)) > limits.max_order_notional or
            float(policy.get("max_session_notional", 0)) > limits.max_auto_session_notional or
            int(policy.get("max_orders", 0)) > limits.max_auto_orders or
            int(policy.get("min_order_interval_seconds", 0)) < limits.min_auto_order_interval_seconds or
            int(policy.get("max_concurrent_orders", 0)) > limits.max_auto_concurrent_orders or
            float(policy.get("max_instrument_position_notional", 0)) > limits.max_auto_instrument_position_notional or
            float(policy.get("max_account_drawdown", 0)) > limits.max_auto_account_drawdown
        ):
            reasons.append("AUTO_POLICY_EXCEEDS_CURRENT_CONFIG")
        usage = self._auto_usage_snapshot(connection, permit_row["permit_id"])
        if usage["order_count"] + child_count > int(policy.get("max_orders", 0)):
            reasons.append("AUTO_ORDER_COUNT_EXCEEDED")
        if usage["notional"] + notional > float(policy.get("max_session_notional", 0)):
            reasons.append("AUTO_SESSION_NOTIONAL_EXCEEDED")
        if usage.get("last_reserved_at"):
            elapsed = (now - parse_time(usage["last_reserved_at"])).total_seconds()
            if elapsed < int(policy.get("min_order_interval_seconds", 0)):
                reasons.append("AUTO_ORDER_RATE_EXCEEDED")
        active_count = connection.execute(
            "SELECT COUNT(*) AS n FROM orders WHERE account_alias=? AND status IN (%s)" %
            ",".join("?" for _ in ACTIVE_ORDER_STATES),
            [account.alias] + sorted(ACTIVE_ORDER_STATES),
        ).fetchone()["n"]
        if int(active_count) + child_count > int(policy.get("max_concurrent_orders", 0)):
            reasons.append("AUTO_CONCURRENT_ORDER_LIMIT")
        multiplier = float(current.get("decision_material", {}).get("quote_guard", {}).get("volume_multiple") or 0)
        current_volume = int(current.get("decision_material", {}).get("target_position", {}).get("position") or 0)
        if action.startswith("OPEN_"):
            projected_volume = current_volume + volume
        else:
            projected_volume = max(0, current_volume - volume)
        if projected_volume * float(current["resolved_limit_price"]) * multiplier > float(policy.get("max_instrument_position_notional", 0)):
            reasons.append("AUTO_INSTRUMENT_POSITION_LIMIT")
        account_snapshot = self._latest_account(connection, account.alias)
        funds = (account_snapshot or {}).get("payload") or {}
        current_equity = float(funds.get("dynamic_rights") or funds.get("balance") or 0)
        baseline = float(policy.get("baseline_equity", 0) or 0)
        if baseline <= 0 or current_equity <= 0:
            reasons.append("AUTO_ACCOUNT_EQUITY_UNAVAILABLE")
        elif baseline - current_equity > float(policy.get("max_account_drawdown", 0)):
            reasons.append("AUTO_ACCOUNT_DRAWDOWN_EXCEEDED")
        consecutive = self._auto_consecutive_failures(connection, permit_row["permit_id"])
        if consecutive >= int(policy.get("max_consecutive_failures", 1)):
            reasons.append("AUTO_CONSECUTIVE_FAILURE_LIMIT")
        if include_health:
            reasons.extend(self._limited_auto_health_reasons(connection, account))
        summary = {
            "permit_id": permit_row["permit_id"],
            "policy_hash": permit_row["policy_hash"],
            "generation": permit_row["generation"],
            "expires_at": permit_row["expires_at"],
            "usage": usage,
            "remaining_orders": max(0, int(policy.get("max_orders", 0)) - usage["order_count"]),
            "remaining_notional": max(0.0, float(policy.get("max_session_notional", 0)) - usage["notional"]),
            "consecutive_failures": consecutive,
        }
        return sorted(set(reasons)), policy, summary

    def _limited_auto_status(self, connection, account, include_health=True):
        row = connection.execute(
            "SELECT * FROM auto_permits WHERE account_alias=? ORDER BY created_at DESC LIMIT 1",
            (account.alias,),
        ).fetchone()
        if not row:
            return {
                "configured": False, "active": False, "status": "NONE", "permit_id": None,
                "health_reasons": self._limited_auto_health_reasons(connection, account) if include_health else [],
            }
        try:
            policy = json.loads(row["policy_json"])
            remaining = max(0, int((parse_time(row["expires_at"]) - utc_now()).total_seconds()))
            generation = int(self._state(connection, "auto_generation:%s" % account.alias, "0"))
        except (TypeError, ValueError):
            return {"configured": True, "active": False, "status": "INVALID", "permit_id": row["permit_id"], "health_reasons": ["AUTO_POLICY_INVALID"]}
        usage = self._auto_usage_snapshot(connection, row["permit_id"])
        active = bool(
            row["status"] == "ACTIVE" and remaining > 0 and int(row["generation"]) == generation and
            self._mode(connection) == "LIMITED_AUTO" and not self._halted(connection)
        )
        return {
            "configured": True, "active": active,
            "status": row["status"] if remaining else "EXPIRED",
            "permit_id": row["permit_id"], "policy_hash": row["policy_hash"],
            "generation": row["generation"], "starts_at": row["starts_at"],
            "expires_at": row["expires_at"], "remaining_seconds": remaining,
            "pause_reason": row["pause_reason"], "revoked_reason": row["revoked_reason"],
            "policy": policy, "usage": usage,
            "remaining_orders": max(0, int(policy["max_orders"]) - usage["order_count"]),
            "remaining_notional": max(0.0, float(policy["max_session_notional"]) - usage["notional"]),
            "health_reasons": self._limited_auto_health_reasons(connection, account) if include_health else [],
        }

    def _write_auto_permit_authorization(self, account, permit_row):
        policy = json.loads(permit_row["policy_json"])
        self._write_local_authorization(
            account, ["LIMITED_AUTO"], parse_time(permit_row["expires_at"]), "mcp",
            permit_row["permit_id"], "LIMITED_AUTO_PERMIT",
            {
                "permit_id": permit_row["permit_id"], "policy_hash": permit_row["policy_hash"],
                "generation": permit_row["generation"], "policy": policy,
            },
        )

    def check_limited_auto_readiness(self, account_alias):
        account = self.config.account(account_alias)
        try:
            self.ingester.scan_once()
            self.ingester.reconcile_terminal_evidence()
            scan_error = None
        except Exception as exc:
            scan_error = type(exc).__name__
        run_id = new_id("recon")
        started = iso_now()
        with self.database.transaction(immediate=True) as connection:
            reasons = self._limited_auto_health_reasons(connection, account)
            if scan_error:
                reasons.append("AUTO_RECONCILIATION_SCAN_FAILED")
            reasons = sorted(set(reasons))
            result = {
                "account_alias": account.alias, "ready": not reasons,
                "reasons": reasons, "scan_error_type": scan_error,
            }
            connection.execute(
                """INSERT INTO reconciliation_runs(run_id,account_alias,status,started_at,completed_at,result_json)
                   VALUES(?,?,?,?,?,?)""",
                (run_id, account.alias, "SUCCESS" if not reasons else "FAILED", started, iso_now(), json_text(result)),
            )
        self._pause_active_auto_permit(account.alias, reasons, actor="worker")
        return dict(result, reconciliation_run_id=run_id)

    def authorize_limited_auto(
        self, account_alias, instruments, actions, source_type, rule_set_id,
        rule_version, max_order_notional, max_order_volume,
        max_session_notional, max_orders, max_concurrent_orders,
        max_instrument_position_notional, max_account_drawdown, reason, confirm,
        minutes=480, min_order_interval_seconds=1,
        max_consecutive_failures=3, trading_windows=None,
    ):
        if confirm != "AUTHORIZE-LIMITED-AUTO-P1":
            raise BridgeError("LOCAL_CONFIRMATION_REQUIRED", "confirm must be AUTHORIZE-LIMITED-AUTO-P1")
        reason = _text(reason, "reason", 500)
        account = self.config.account(account_alias)
        limits = account.risk_limits
        if not isinstance(instruments, list) or not instruments or len(instruments) > 100:
            raise ValidationError("instruments must contain 1-100 entries")
        normalized = []
        for item in instruments:
            if not isinstance(item, dict) or set(item) != {"exchange", "instrument_id"}:
                raise ValidationError("each instrument must contain exactly exchange and instrument_id")
            try:
                exchange, instrument_id = normalize_instrument(item["exchange"], item["instrument_id"])
            except ValueError as exc:
                raise ValidationError(str(exc))
            if not account.instrument_allowed(exchange, instrument_id):
                raise BridgeError("INSTRUMENT_NOT_ALLOWED", "permit instruments exceed the account allowlist")
            normalized.append("%s:%s" % (exchange, instrument_id))
        if len(set(normalized)) != len(normalized):
            raise ValidationError("instruments must not contain duplicates")
        if (
            not isinstance(actions, list) or not actions or len(set(actions)) != len(actions) or
            any(action not in AUTO_ACTIONS for action in actions)
        ):
            raise ValidationError("actions must be a non-empty unique supported futures action array")
        source = {
            "type": _text(source_type, "source_type", 100),
            "rule_set_id": _text(rule_set_id, "rule_set_id", 100),
            "rule_version": _text(rule_version, "rule_version", 100),
        }
        integers = {
            "minutes": minutes, "max_order_volume": max_order_volume,
            "max_orders": max_orders, "max_concurrent_orders": max_concurrent_orders,
            "min_order_interval_seconds": min_order_interval_seconds,
            "max_consecutive_failures": max_consecutive_failures,
        }
        if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in integers.values()):
            raise ValidationError("limited-auto integer limits must be positive integers")
        if minutes > limits.max_auto_authorization_minutes:
            raise ValidationError("minutes exceeds max_auto_authorization_minutes")
        if max_order_volume > limits.max_order_volume or max_orders > limits.max_auto_orders:
            raise ValidationError("order limits exceed the account automatic hard limits")
        if max_concurrent_orders > limits.max_auto_concurrent_orders:
            raise ValidationError("max_concurrent_orders exceeds the configured hard limit")
        if min_order_interval_seconds < limits.min_auto_order_interval_seconds:
            raise ValidationError("min_order_interval_seconds is below the configured hard minimum")
        if max_consecutive_failures > 100:
            raise ValidationError("max_consecutive_failures must not exceed 100")
        numbers = {
            "max_order_notional": _number(max_order_notional, "max_order_notional", 0.000001),
            "max_session_notional": _number(max_session_notional, "max_session_notional", 0.000001),
            "max_instrument_position_notional": _number(max_instrument_position_notional, "max_instrument_position_notional", 0.000001),
            "max_account_drawdown": _number(max_account_drawdown, "max_account_drawdown", 0.000001),
        }
        if numbers["max_order_notional"] > limits.max_order_notional:
            raise ValidationError("max_order_notional exceeds the account hard limit")
        if numbers["max_session_notional"] > limits.max_auto_session_notional or numbers["max_session_notional"] < numbers["max_order_notional"]:
            raise ValidationError("max_session_notional is outside the configured automatic limits")
        if numbers["max_instrument_position_notional"] > limits.max_auto_instrument_position_notional:
            raise ValidationError("max_instrument_position_notional exceeds the configured hard limit")
        if numbers["max_account_drawdown"] > limits.max_auto_account_drawdown:
            raise ValidationError("max_account_drawdown exceeds the configured hard limit")
        if not isinstance(trading_windows, list) or not trading_windows or len(trading_windows) > 8:
            raise ValidationError("trading_windows must explicitly contain 1-8 windows")
        normalized_windows = []
        for window in trading_windows:
            if not isinstance(window, dict) or set(window) != {"start", "end"}:
                raise ValidationError("each trading window must contain exactly start and end")
            start, end = self._clock_minutes(window["start"]), self._clock_minutes(window["end"])
            if start == end:
                raise ValidationError("a trading window cannot cover a full day")
            normalized_windows.append({"start": window["start"], "end": window["end"]})
        starts = utc_now()
        expires = starts + dt.timedelta(minutes=minutes)
        with self.database.transaction(immediate=True) as connection:
            health = [item for item in self._limited_auto_health_reasons(connection, account) if item != "AUTO_ADAPTER_LOCALLY_PAUSED"]
            if health:
                raise BridgeError("AUTO_NOT_READY", "limited-auto readiness checks failed", {"reasons": health})
            snapshot = self._latest_account(connection, account.alias)
            funds = (snapshot or {}).get("payload") or {}
            baseline_equity = float(funds.get("dynamic_rights") or funds.get("balance") or 0)
            if baseline_equity <= 0:
                raise BridgeError("AUTO_ACCOUNT_EQUITY_UNAVAILABLE", "positive dynamic_rights or balance is required")
            generation = self._next_auto_generation(connection, account.alias)
            permit_id = new_id("auto_permit")
            policy = {
                "policy_version": 1, "permit_id": permit_id, "generation": generation,
                "account_alias": account.alias, "account_type": "FUTURES",
                "timezone": "Asia/Shanghai", "trading_windows": normalized_windows,
                "allowed_instruments": sorted(normalized), "allowed_actions": sorted(actions),
                "allowed_sizing_types": ["FIXED_VOLUME"], "source": source,
                **numbers, "max_order_volume": max_order_volume,
                "max_orders": max_orders, "min_order_interval_seconds": min_order_interval_seconds,
                "max_concurrent_orders": max_concurrent_orders,
                "max_consecutive_failures": max_consecutive_failures,
                "baseline_equity": baseline_equity,
                "starts_at": starts.isoformat(timespec="milliseconds"),
                "expires_at": expires.isoformat(timespec="milliseconds"),
            }
            policy_hash = hash_json(policy)
            created = iso_now()
            connection.execute(
                """UPDATE auto_permits SET status='REVOKED',revoked_at=?,
                          revoked_reason='superseded by a new permit'
                   WHERE account_alias=? AND status IN ('ACTIVE','PAUSED')""",
                (created, account.alias),
            )
            connection.execute(
                """INSERT INTO auto_permits(
                       permit_id,account_alias,status,generation,policy_hash,policy_json,
                       actor,reason,created_at,starts_at,expires_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (permit_id, account.alias, "ACTIVE", generation, policy_hash, json_text(policy),
                 "mcp", reason, created, policy["starts_at"], policy["expires_at"]),
            )
            connection.execute(
                "INSERT INTO audit_log(occurred_at,actor,action,account_alias,object_id,details_json) VALUES(?,?,?,?,?,?)",
                (created, "mcp", "AUTHORIZE_LIMITED_AUTO", account.alias, permit_id,
                 json_text({"reason": reason, "policy_hash": policy_hash, "policy": policy})),
            )
            row = connection.execute("SELECT * FROM auto_permits WHERE permit_id=?", (permit_id,)).fetchone()
        self._write_auto_permit_authorization(account, row)
        with self.database.connect() as connection:
            return self._limited_auto_status(connection, account)

    def get_limited_auto_status(self, account_alias):
        account = self.config.account(account_alias)
        with self.database.connect() as connection:
            return self._limited_auto_status(connection, account)

    def _pause_active_auto_permit(self, account_alias, reasons, actor="worker"):
        serious = sorted(set(reasons) & AUTO_PAUSE_REASONS)
        if not serious:
            return False
        account = self.config.account(account_alias)
        now = iso_now()
        with self.database.transaction(immediate=True) as connection:
            row = self._active_auto_permit_row(connection, account.alias)
            if not row:
                return False
            self._next_auto_generation(connection, account.alias)
            connection.execute(
                "UPDATE auto_permits SET status='PAUSED',paused_at=?,pause_reason=? WHERE permit_id=? AND status='ACTIVE'",
                (now, ",".join(serious), row["permit_id"]),
            )
            connection.execute(
                "INSERT INTO audit_log(occurred_at,actor,action,account_alias,object_id,details_json) VALUES(?,?,?,?,?,?)",
                (now, actor, "PAUSE_LIMITED_AUTO", account.alias, row["permit_id"], json_text({"reasons": serious})),
            )
        self._write_local_authorization(account, [], utc_now() + dt.timedelta(seconds=1), actor, new_id("auto_pause"), "PAUSED")
        return True

    def resume_limited_auto(self, account_alias, reason, confirm):
        if confirm != "RESUME-LIMITED-AUTO-P1":
            raise BridgeError("LOCAL_CONFIRMATION_REQUIRED", "confirm must be RESUME-LIMITED-AUTO-P1")
        reason = _text(reason, "reason", 500)
        account = self.config.account(account_alias)
        now = iso_now()
        with self.database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM auto_permits WHERE account_alias=? AND status='PAUSED' ORDER BY created_at DESC LIMIT 1",
                (account.alias,),
            ).fetchone()
            if not row:
                raise BridgeError("AUTO_PERMIT_NOT_FOUND", "no paused limited-auto permit exists")
            if parse_time(row["expires_at"]) <= utc_now():
                raise BridgeError("AUTO_PERMIT_EXPIRED", "the paused permit has expired")
            if "AUTO_CONSECUTIVE_FAILURE_LIMIT" in (row["pause_reason"] or ""):
                raise BridgeError("AUTO_STRATEGY_REAUTHORIZATION_REQUIRED", "consecutive failures require a new versioned permit")
            health = [item for item in self._limited_auto_health_reasons(connection, account) if item != "AUTO_ADAPTER_LOCALLY_PAUSED"]
            if health:
                raise BridgeError("AUTO_NOT_READY", "readiness checks still fail", {"reasons": health})
            policy = json.loads(row["policy_json"])
            snapshot = self._latest_account(connection, account.alias)
            funds = (snapshot or {}).get("payload") or {}
            equity = float(funds.get("dynamic_rights") or funds.get("balance") or 0)
            if equity <= 0 or float(policy["baseline_equity"]) - equity > float(policy["max_account_drawdown"]):
                raise BridgeError("AUTO_ACCOUNT_DRAWDOWN_EXCEEDED", "account drawdown guard still fails")
            generation = self._next_auto_generation(connection, account.alias)
            policy.update({"generation": generation, "resumed_at": now})
            policy_hash = hash_json(policy)
            connection.execute(
                """UPDATE auto_permits SET status='ACTIVE',generation=?,policy_hash=?,policy_json=?,
                          paused_at=NULL,pause_reason=NULL WHERE permit_id=?""",
                (generation, policy_hash, json_text(policy), row["permit_id"]),
            )
            connection.execute(
                "INSERT INTO audit_log(occurred_at,actor,action,account_alias,object_id,details_json) VALUES(?,?,?,?,?,?)",
                (now, "mcp", "RESUME_LIMITED_AUTO", account.alias, row["permit_id"], json_text({"reason": reason, "generation": generation})),
            )
            updated = connection.execute("SELECT * FROM auto_permits WHERE permit_id=?", (row["permit_id"],)).fetchone()
        self._write_auto_permit_authorization(account, updated)
        with self.database.connect() as connection:
            return self._limited_auto_status(connection, account)

    def revoke_limited_auto(self, account_alias, reason, confirm):
        if confirm != "REVOKE-LIMITED-AUTO-P1":
            raise BridgeError("LOCAL_CONFIRMATION_REQUIRED", "confirm must be REVOKE-LIMITED-AUTO-P1")
        reason = _text(reason, "reason", 500)
        account = self.config.account(account_alias)
        now = iso_now()
        with self.database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM auto_permits WHERE account_alias=? AND status IN ('ACTIVE','PAUSED') ORDER BY created_at DESC LIMIT 1",
                (account.alias,),
            ).fetchone()
            self._next_auto_generation(connection, account.alias)
            if row:
                connection.execute(
                    "UPDATE auto_permits SET status='REVOKED',revoked_at=?,revoked_reason=? WHERE permit_id=?",
                    (now, reason, row["permit_id"]),
                )
            connection.execute(
                "INSERT INTO audit_log(occurred_at,actor,action,account_alias,object_id,details_json) VALUES(?,?,?,?,?,?)",
                (now, "mcp", "REVOKE_LIMITED_AUTO", account.alias, row["permit_id"] if row else None, json_text({"reason": reason})),
            )
        self._write_local_authorization(account, [], utc_now() + dt.timedelta(seconds=1), "mcp", new_id("auto_revoke"), "REVOKED")
        return {"account_alias": account.alias, "revoked_permit_id": row["permit_id"] if row else None, "active": False}

    def _build_current(self, connection, request):
        request = validate_trade_request(request)
        account = self.config.account(request["account_alias"])
        exchange = request["instrument"]["exchange"]
        instrument = request["instrument"]["instrument_id"]
        context = self._context(connection, account, exchange, instrument)
        result = build_preview(request, account, *context)
        return self._apply_system_gates(connection, request, result, account)

    def preview_trade(self, trade_request):
        request = validate_trade_request(trade_request)
        with self.database.transaction(immediate=True) as connection:
            result = self._build_current(connection, request)
            now = utc_now()
            preview_id = new_id("preview")
            risk_id = new_id("risk")
            expires = now + dt.timedelta(seconds=self.config.account(request["account_alias"]).risk_limits.preview_ttl_seconds)
            result.update({
                "preview_id": preview_id,
                "risk_decision_id": risk_id,
                "created_at": now.isoformat(timespec="milliseconds"),
                "expires_at": expires.isoformat(timespec="milliseconds"),
                "request_hash": hash_json(request),
            })
            source = request["source"]
            connection.execute(
                "INSERT OR IGNORE INTO market_signals(signal_id,source_type,received_at,payload_json) VALUES(?,?,?,?)",
                (source["signal_id"], source["type"], result["created_at"], json_text(source)),
            )
            connection.execute(
                "INSERT INTO trade_previews(preview_id,account_alias,request_json,result_json,request_hash,decision_fingerprint,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?)",
                (preview_id, result["account_alias"], json_text(request), json_text(result), result["request_hash"], result["decision_fingerprint"], result["created_at"], result["expires_at"]),
            )
            connection.execute(
                "INSERT INTO risk_decisions(risk_decision_id,preview_id,account_alias,allowed,reasons_json,decision_fingerprint,created_at) VALUES(?,?,?,?,?,?,?)",
                (risk_id, preview_id, result["account_alias"], int(result["risk"]["allowed"]), json_text(result["risk"]["reasons"]), result["decision_fingerprint"], result["created_at"]),
            )
        if request["execution_mode"] == "LIMITED_AUTO" and not result["risk"]["allowed"]:
            self._pause_active_auto_permit(request["account_alias"], result["risk"]["reasons"], actor="worker")
        return result

    def _load_preview(self, connection, preview_id):
        row = connection.execute("SELECT * FROM trade_previews WHERE preview_id=?", (preview_id,)).fetchone()
        if not row:
            raise BridgeError("PREVIEW_NOT_FOUND", "preview was not found")
        if parse_time(row["expires_at"]) <= utc_now():
            raise BridgeError("PREVIEW_EXPIRED", "preview has expired")
        if row["consumed_intent_id"]:
            return row, json.loads(row["request_json"]), json.loads(row["result_json"]), row["consumed_intent_id"]
        return row, json.loads(row["request_json"]), json.loads(row["result_json"]), None

    def authorize_manual_trade(self, preview_id, reason, confirm, live_minutes=10, approval_ttl_seconds=30):
        if confirm != "AUTHORIZE-MANUAL-TRADE":
            raise BridgeError("LOCAL_CONFIRMATION_REQUIRED", "confirm must be AUTHORIZE-MANUAL-TRADE")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 200:
            raise ValidationError("reason must contain 1-200 characters")
        if not isinstance(live_minutes, int) or isinstance(live_minutes, bool) or not 1 <= live_minutes <= 60:
            raise ValidationError("live_minutes must be between 1 and 60")
        if not isinstance(approval_ttl_seconds, int) or isinstance(approval_ttl_seconds, bool) or not 1 <= approval_ttl_seconds <= 300:
            raise ValidationError("approval_ttl_seconds must be between 1 and 300")
        now = utc_now()
        with self.database.transaction(immediate=True) as connection:
            row, request, _, consumed = self._load_preview(connection, preview_id)
            if consumed:
                raise BridgeError("PREVIEW_ALREADY_CONSUMED", "preview has already been consumed")
            if self._mode(connection) != "MANUAL_LIVE" or request["execution_mode"] != "MANUAL_LIVE":
                raise BridgeError("LIVE_NOT_ENABLED", "Worker and preview must be in MANUAL_LIVE")
            if self._halted(connection):
                raise BridgeError("LIVE_NOT_ENABLED", "trading is halted")
            current = self._build_current(connection, request)
            if current["decision_fingerprint"] != row["decision_fingerprint"]:
                raise BridgeError("SNAPSHOT_CHANGED", "risk decision changed; create a new preview")
            if not current["risk"]["allowed"]:
                raise BridgeError("RISK_REJECTED", "current hard risk rejected the preview", {"reasons": current["risk"]["reasons"]})
            account = self.config.account(request["account_alias"])
            live_until = now + dt.timedelta(minutes=live_minutes)
            approval_expires = min(now + dt.timedelta(seconds=approval_ttl_seconds), parse_time(row["expires_at"]))
            approval_id = new_id("approval")
            self._set_state(connection, "live_until:%s" % account.alias, live_until.isoformat(timespec="milliseconds"))
            connection.execute(
                "INSERT INTO approvals(approval_id,preview_id,decision,actor,reason,created_at,expires_at) VALUES(?,?,?,?,?,?,?)",
                (approval_id, preview_id, "APPROVED", "mcp", reason.strip(), now.isoformat(timespec="milliseconds"), approval_expires.isoformat(timespec="milliseconds")),
            )
            connection.execute(
                "INSERT INTO audit_log(occurred_at,actor,action,account_alias,object_id,details_json) VALUES(?,?,?,?,?,?)",
                (iso_now(), "mcp", "AUTHORIZE_MANUAL_TRADE", account.alias, preview_id, json_text({"reason": reason, "live_until": live_until.isoformat(), "approval_expires": approval_expires.isoformat()})),
            )
        self._write_local_authorization(account, ["MANUAL_LIVE"], live_until, "mcp")
        return {"preview_id": preview_id, "approval_context": {"local_approval_id": approval_id}, "live_until": live_until.isoformat(timespec="milliseconds"), "approval_expires_at": approval_expires.isoformat(timespec="milliseconds")}

    def _active_manual_session(self, connection, alias):
        raw = self._state(connection, "manual_session:%s" % alias)
        if not raw:
            return None
        try:
            session = json.loads(raw)
            if parse_time(session["expires_at"]) <= utc_now():
                return None
            return session
        except Exception:
            return None

    def get_manual_authorization_status(self, account_alias):
        self.config.account(account_alias)
        with self.database.connect() as connection:
            session = self._active_manual_session(connection, account_alias)
            live_until = self._state(connection, "live_until:%s" % account_alias)
            mode = self._mode(connection)
            halted = self._halted(connection)
            approvals = connection.execute(
                "SELECT COUNT(*) AS n FROM approvals WHERE decision='APPROVED' AND used_at IS NULL AND expires_at>? AND preview_id IN (SELECT preview_id FROM trade_previews WHERE account_alias=?)",
                (iso_now(), account_alias),
            ).fetchone()["n"]
        live_active = bool(live_until and parse_time(live_until) > utc_now())
        return {
            "account_alias": account_alias,
            "mode": mode,
            "halted": halted,
            "timed_session": session,
            "session_remaining_seconds": max(0, int((parse_time(session["expires_at"]) - utc_now()).total_seconds())) if session else 0,
            "live_until": live_until,
            "live_active": live_active,
            "unused_one_time_approvals": approvals,
            "authorized": bool(mode == "MANUAL_LIVE" and not halted and live_active and (session or approvals)),
        }

    def authorize_manual_session(self, account_alias, minutes, reason, confirm):
        if confirm != "AUTHORIZE-TIMED-MANUAL-TRADING":
            raise BridgeError("LOCAL_CONFIRMATION_REQUIRED", "confirm must be AUTHORIZE-TIMED-MANUAL-TRADING")
        if not isinstance(minutes, int) or isinstance(minutes, bool) or not 1 <= minutes <= 60:
            raise ValidationError("minutes must be between 1 and 60")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 200:
            raise ValidationError("reason must contain 1-200 characters")
        account = self.config.account(account_alias)
        now = utc_now()
        expires = now + dt.timedelta(minutes=minutes)
        session = {"session_id": new_id("manual_session"), "account_alias": account.alias, "created_at": now.isoformat(timespec="milliseconds"), "expires_at": expires.isoformat(timespec="milliseconds"), "reason": reason.strip(), "actor": "mcp", "unlimited_order_count": True}
        with self.database.transaction(immediate=True) as connection:
            if self._mode(connection) != "MANUAL_LIVE" or self._halted(connection):
                raise BridgeError("LIVE_NOT_ENABLED", "Worker must be in MANUAL_LIVE and not halted")
            self._set_state(connection, "manual_session:%s" % account.alias, json_text(session))
            self._set_state(connection, "live_until:%s" % account.alias, expires.isoformat(timespec="milliseconds"))
            connection.execute(
                "INSERT INTO audit_log(occurred_at,actor,action,account_alias,object_id,details_json) VALUES(?,?,?,?,?,?)",
                (iso_now(), "mcp", "AUTHORIZE_MANUAL_SESSION", account.alias, session["session_id"], json_text(session)),
            )
        self._write_local_authorization(account, ["MANUAL_LIVE"], expires, "mcp")
        return session

    def revoke_manual_session(self, account_alias, reason, confirm):
        if confirm != "REVOKE-TIMED-MANUAL-TRADING":
            raise BridgeError("LOCAL_CONFIRMATION_REQUIRED", "confirm must be REVOKE-TIMED-MANUAL-TRADING")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 200:
            raise ValidationError("reason must contain 1-200 characters")
        account = self.config.account(account_alias)
        now = iso_now()
        with self.database.transaction(immediate=True) as connection:
            connection.execute("DELETE FROM system_state WHERE key=?", ("manual_session:%s" % account.alias,))
            self._set_state(connection, "live_until:%s" % account.alias, now)
            connection.execute(
                "UPDATE approvals SET expires_at=? WHERE used_at IS NULL AND expires_at>? AND preview_id IN (SELECT preview_id FROM trade_previews WHERE account_alias=?)",
                (now, now, account.alias),
            )
            connection.execute(
                "INSERT INTO audit_log(occurred_at,actor,action,account_alias,details_json) VALUES(?,?,?,?,?)",
                (now, "mcp", "REVOKE_MANUAL_SESSION", account.alias, json_text({"reason": reason.strip()})),
            )
        self._write_local_authorization(account, [], utc_now() + dt.timedelta(seconds=1), "mcp")
        return {"account_alias": account.alias, "revoked": True}

    def _check_submit_authorization(self, connection, row, request, approval_context, current):
        mode = self._mode(connection)
        if self._halted(connection):
            raise BridgeError("TRADING_HALTED", "trading is halted")
        if request["execution_mode"] != mode:
            raise BridgeError("MODE_MISMATCH", "preview mode does not match Worker mode")
        if mode in {"OBSERVE_ONLY", "SIM_SIGNAL"}:
            return None
        if mode == "LIMITED_AUTO":
            permit = self._active_auto_permit_row(connection, request["account_alias"])
            if not permit:
                raise BridgeError("AUTO_PERMIT_REQUIRED", "no active LIMITED_AUTO permit")
            reasons, policy, summary = self._limited_auto_policy_reasons(
                connection, permit, request, current,
            )
            if reasons:
                raise BridgeError(
                    "AUTO_POLICY_REJECTED", "limited-auto policy rejected this order",
                    {"reasons": reasons},
                )
            return {
                "type": "LIMITED_AUTO", "permit_id": permit["permit_id"],
                "policy_hash": permit["policy_hash"], "generation": permit["generation"],
                "policy": policy, "summary": summary,
            }
        session = self._active_manual_session(connection, request["account_alias"])
        live_until = self._state(connection, "live_until:%s" % request["account_alias"])
        if not live_until or parse_time(live_until) <= utc_now():
            raise BridgeError("LIVE_NOT_ENABLED", "manual LIVE window is not active")
        if session:
            return None
        approval_id = (approval_context or {}).get("local_approval_id") if isinstance(approval_context, dict) else None
        if not approval_id:
            raise BridgeError("MANUAL_AUTHORIZATION_REQUIRED", "a one-time approval or timed session is required")
        approval = connection.execute(
            "SELECT * FROM approvals WHERE approval_id=? AND preview_id=?", (approval_id, row["preview_id"])
        ).fetchone()
        if not approval or approval["decision"] != "APPROVED" or approval["used_at"] or parse_time(approval["expires_at"]) <= utc_now():
            raise BridgeError("MANUAL_AUTHORIZATION_REQUIRED", "approval is missing, expired, used, or bound to another preview")
        return approval_id

    def submit_trade_intent(self, preview_id, approval_context=None):
        created = iso_now()
        envelopes = []
        with self.database.connect() as preflight_connection:
            preflight_row, preflight_request, _, preflight_consumed = self._load_preview(
                preflight_connection, preview_id,
            )
            if preflight_consumed:
                return self.get_trade_intent(preflight_consumed)
            preflight = self._build_current(preflight_connection, preflight_request)
        if preflight_request["execution_mode"] == "LIMITED_AUTO" and not preflight["risk"]["allowed"]:
            self._pause_active_auto_permit(
                preflight_request["account_alias"], preflight["risk"]["reasons"], actor="worker",
            )
            raise BridgeError(
                "RISK_REJECTED", "current hard risk rejected the trade",
                {"reasons": preflight["risk"]["reasons"]},
            )
        with self.database.transaction(immediate=True) as connection:
            row, request, prior_result, consumed = self._load_preview(connection, preview_id)
            if consumed:
                return self.get_trade_intent(consumed)
            current = self._build_current(connection, request)
            if current["decision_fingerprint"] != row["decision_fingerprint"]:
                raise BridgeError("SNAPSHOT_CHANGED", "risk decision changed; create a new preview", {"previous": row["decision_fingerprint"], "current": current["decision_fingerprint"]})
            if not current["risk"]["allowed"]:
                raise BridgeError("RISK_REJECTED", "current hard risk rejected the trade", {"reasons": current["risk"]["reasons"]})
            authorization = self._check_submit_authorization(
                connection, row, request, approval_context, current,
            )
            signal_id = request["source"]["signal_id"]
            existing = connection.execute("SELECT intent_id FROM trade_intents WHERE source_signal_id=?", (signal_id,)).fetchone()
            if existing:
                return self.get_trade_intent(existing["intent_id"])
            intent_id = new_id("intent")
            intent_payload = {"intent_id": intent_id, "preview_id": preview_id, "request": request, "preview": current}
            connection.execute(
                "INSERT INTO trade_intents(intent_id,preview_id,account_alias,status,action,exchange,instrument_id,requested_volume,execution_mode,source_signal_id,created_at,updated_at,payload_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    intent_id, preview_id, request["account_alias"], "PERSISTED", request["action"],
                    request["instrument"]["exchange"], request["instrument"]["instrument_id"],
                    request["sizing"]["value"], request["execution_mode"], signal_id,
                    created, created, json_text(intent_payload),
                ),
            )
            account = self.config.account(request["account_alias"])
            for index, child in enumerate(current["children"], 1):
                child_id = new_id("child")
                client_key = new_client_order_key()
                memo = memo_token(intent_id, index)
                child_payload = {
                    **child,
                    "type": "EXECUTE_ORDER",
                    "intent_id": intent_id,
                    "child_order_id": child_id,
                    "child_no": index,
                    "client_order_key": client_key,
                    "memo_token": memo,
                    "account_alias": account.alias,
                    "account_type": "FUTURES",
                    "adapter_instance": account.adapter_instance,
                    "exchange": request["instrument"]["exchange"],
                    "instrument_id": request["instrument"]["instrument_id"],
                    "limit_price": current["resolved_limit_price"],
                    "hedge_flag": request["hedge_flag"],
                    "execution_mode": request["execution_mode"],
                    "risk_decision_id": prior_result["risk_decision_id"],
                    "source_signal_id": signal_id,
                    "semantic_action": request["action"],
                    "sizing_type": request["sizing"]["type"],
                    "order_notional": round(
                        float(current["resolved_limit_price"]) * int(child["volume"]) *
                        float(current["decision_material"]["quote_guard"]["volume_multiple"]), 6,
                    ),
                    "source": request["source"],
                    "auto_permit": ({
                        "permit_id": authorization["permit_id"],
                        "policy_hash": authorization["policy_hash"],
                        "generation": authorization["generation"],
                    } if isinstance(authorization, dict) and authorization.get("type") == "LIMITED_AUTO" else None),
                    "risk_limits": dict(account.risk_limits.__dict__),
                }
                child_status = "PENDING_DELIVERY" if index == 1 else "BLOCKED_SEQUENCE"
                connection.execute(
                    "INSERT INTO child_orders(child_order_id,intent_id,child_no,client_order_key,memo_token,action,offset,volume,limit_price,status,created_at,updated_at,payload_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (child_id, intent_id, index, client_key, memo, child["action"], child["offset"], child["volume"], current["resolved_limit_price"], child_status, created, created, json_text(child_payload)),
                )
                envelope = make_envelope(
                    self.keyring, "TRADE_INTENT", child_payload,
                    account.risk_limits.command_ttl_seconds, "pythongo-bridge-worker", intent_id,
                )
                connection.execute(
                    "INSERT INTO adapter_commands(message_id,intent_id,child_order_id,account_alias,command_type,folder,status,created_at,payload_json) VALUES(?,?,?,?,?,?,?,?,?)",
                    (envelope["message_id"], intent_id, child_id, account.alias, "TRADE_INTENT", "commands", child_status, created, json_text(envelope)),
                )
                if isinstance(authorization, dict) and authorization.get("type") == "LIMITED_AUTO":
                    connection.execute(
                        """INSERT INTO auto_permit_usage(
                               permit_id,child_order_id,intent_id,account_alias,exchange,
                               instrument_id,action,volume,notional,reserved_at
                           ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                        (
                            authorization["permit_id"], child_id, intent_id, account.alias,
                            request["instrument"]["exchange"], request["instrument"]["instrument_id"],
                            request["action"], int(child["volume"]), child_payload["order_notional"], created,
                        ),
                    )
                envelopes.append((account, envelope, child_status))
            connection.execute("UPDATE trade_previews SET consumed_intent_id=? WHERE preview_id=?", (intent_id, preview_id))
            if isinstance(authorization, str):
                connection.execute("UPDATE approvals SET used_at=? WHERE approval_id=?", (created, authorization))
            connection.execute(
                "INSERT INTO audit_log(occurred_at,actor,action,account_alias,object_id,details_json) VALUES(?,?,?,?,?,?)",
                (created, "mcp", "SUBMIT_TRADE_INTENT", account.alias, intent_id, json_text({
                    "preview_id": preview_id, "children": len(envelopes), "mode": request["execution_mode"],
                    "authorization_type": authorization.get("type") if isinstance(authorization, dict) else ("SINGLE_TRADE" if authorization else None),
                    "permit_id": authorization.get("permit_id") if isinstance(authorization, dict) else None,
                })),
            )
        self.dispatch_pending_commands(account.alias)
        return self.get_trade_intent(intent_id)

    def _queue_control(self, account, message_type, payload, intent_id=None):
        envelope = make_envelope(
            self.keyring, message_type, payload, account.risk_limits.command_ttl_seconds,
            "pythongo-bridge-worker", intent_id,
        )
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO adapter_commands(message_id,intent_id,account_alias,command_type,folder,status,created_at,payload_json) VALUES(?,?,?,?,?,?,?,?)",
                (envelope["message_id"], intent_id, account.alias, message_type, "control", "PENDING_DELIVERY", iso_now(), json_text(envelope)),
            )
        self.dispatch_pending_commands(account.alias)
        return {"message_id": envelope["message_id"], "status": "DELIVERED", "command_type": message_type}

    def request_sync(self, account_alias, scopes, instruments=None, kline=None):
        account = self.config.account(account_alias)
        allowed = {"ACCOUNT", "POSITION", "ORDER", "TRADE", "QUOTE", "KLINE"}
        if not isinstance(scopes, list) or not scopes or len(scopes) > len(allowed) or not all(isinstance(item, str) for item in scopes) or len(set(scopes)) != len(scopes) or set(scopes) - allowed:
            raise ValidationError("scopes must be a unique non-empty subset of supported values")
        instruments = [] if instruments is None else instruments
        if not isinstance(instruments, list) or len(instruments) > 100:
            raise ValidationError("instruments must be an array with at most 100 entries")
        if ("QUOTE" in scopes or "KLINE" in scopes) and not instruments:
            raise ValidationError("QUOTE and KLINE sync require instruments")
        normalized = []
        for item in instruments:
            if not isinstance(item, dict) or set(item) != {"exchange", "instrument_id"}:
                raise ValidationError("invalid instrument")
            try:
                exchange, instrument_id = normalize_instrument(item["exchange"], item["instrument_id"])
            except ValueError as exc:
                raise ValidationError(str(exc))
            normalized.append({"exchange": exchange, "instrument_id": instrument_id})
        if kline is not None:
            if not isinstance(kline, dict) or set(kline) - {"interval", "count"}:
                raise ValidationError("kline contains invalid fields")
            interval = _text(kline.get("interval"), "kline.interval", 32)
            count = kline.get("count", 100)
            if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 500:
                raise ValidationError("kline.count must be between 1 and 500")
            kline = {"interval": interval, "count": count}
        if "KLINE" in scopes and kline is None:
            raise ValidationError("KLINE sync requires kline.interval")
        payload = {"type": "REQUEST_SYNC", "account_alias": account.alias, "scopes": scopes, "instruments": normalized, "kline": kline or {}}
        return self._queue_control(account, "REQUEST_SYNC", payload)

    def queue_p0_test_order(self, account_alias, exchange, instrument_id, isolated_notional_cap=None):
        account = self.config.account(account_alias)
        try:
            exchange, instrument_id = normalize_instrument(exchange, instrument_id)
        except ValueError as exc:
            raise ValidationError(str(exc))
        if not account.instrument_allowed(exchange, instrument_id):
            raise BridgeError("INSTRUMENT_NOT_ALLOWED", "P0 instrument is outside the account allowlist")
        isolated_cap = isolated_notional_cap is not None
        effective_notional_cap = float(account.risk_limits.max_order_notional)
        if isolated_cap:
            if (
                isinstance(isolated_notional_cap, bool)
                or not isinstance(isolated_notional_cap, (int, float))
                or not math.isfinite(float(isolated_notional_cap))
                or float(isolated_notional_cap) != P0_ISOLATED_NOTIONAL_CAPS.get((exchange, instrument_id))
            ):
                raise BridgeError(
                    "P0_ISOLATED_CAP_REJECTED",
                    "isolated P0 cap is not authorized for this exact instrument and amount",
                )
            effective_notional_cap = float(isolated_notional_cap)
        with self.database.connect() as connection:
            mode = self._mode(connection)
            halted = self._halted(connection)
            heartbeat = connection.execute(
                "SELECT * FROM heartbeats WHERE adapter_instance=?", (account.adapter_instance,)
            ).fetchone()
        if mode != "OBSERVE_ONLY" or not halted:
            raise BridgeError("P0_GATE_FAILED", "P0 probe requires OBSERVE_ONLY with the durable halt enabled")
        if not heartbeat or heartbeat["status"] != "READY" or age_seconds(heartbeat["received_at"]) > 15:
            raise BridgeError("ADAPTER_NOT_READY", "fresh READY heartbeat is required for the P0 probe")
        heartbeat_payload = _payload(heartbeat) or {}
        if heartbeat["mode"] != "OBSERVE_ONLY" or not bool(heartbeat_payload.get("local_halt")):
            raise BridgeError("P0_GATE_FAILED", "Adapter must report OBSERVE_ONLY with local halt enabled")

        quote_result = self.get_quote_snapshot(
            account.alias, [{"exchange": exchange, "instrument_id": instrument_id}]
        )[0]["snapshot"]
        if not quote_result or quote_result["age_seconds"] > account.risk_limits.max_quote_age_seconds:
            raise BridgeError("QUOTE_STALE", "fresh P0 quote is required")
        quote = quote_result["payload"]
        required_quote = ("last_price", "bid_price1", "lower_limit_price", "price_tick", "volume_multiple")
        if not all(isinstance(quote.get(name), (int, float)) and not isinstance(quote.get(name), bool) and quote[name] > 0 for name in required_quote):
            raise BridgeError("QUOTE_INCOMPLETE", "P0 quote fields are incomplete")
        limit_price = float(quote["lower_limit_price"])
        if limit_price >= float(quote["bid_price1"]):
            raise BridgeError("P0_PRICE_NOT_PASSIVE", "lower-limit P0 buy price is not below the current best bid")
        if abs(round(limit_price / float(quote["price_tick"])) * float(quote["price_tick"]) - limit_price) > 1e-8:
            raise BridgeError("P0_PRICE_INVALID", "P0 lower-limit price is not tick aligned")
        notional = limit_price * float(quote["volume_multiple"])
        if account.risk_limits.max_order_volume < 1 or notional > effective_notional_cap:
            raise BridgeError("P0_RISK_REJECTED", "one-lot P0 order exceeds hard volume or notional limits")

        account_snapshot = self.get_account_snapshot(account.alias)
        if account_snapshot["age_seconds"] > account.risk_limits.max_snapshot_age_seconds:
            raise BridgeError("ACCOUNT_SNAPSHOT_STALE", "fresh account snapshot is required")
        funds = account_snapshot["payload"]
        for name in ("available", "margin", "risk"):
            if not isinstance(funds.get(name), (int, float)) or isinstance(funds.get(name), bool):
                raise BridgeError("ACCOUNT_SNAPSHOT_INCOMPLETE", "P0 account risk fields are incomplete")
        if funds["available"] <= 0 or funds["margin"] > account.risk_limits.max_total_margin or funds["risk"] > account.risk_limits.max_risk_ratio:
            raise BridgeError("P0_RISK_REJECTED", "P0 account hard risk gate rejected the probe")
        if isolated_cap:
            guarded_margin = notional * P0_ISOLATED_MARGIN_GUARD_RATIO
            if (
                funds["available"] < guarded_margin
                or funds["margin"] + guarded_margin > account.risk_limits.max_total_margin
            ):
                raise BridgeError("P0_RISK_REJECTED", "isolated P0 conservative margin gate rejected the probe")

        authorization_id = new_id("p0auth")
        command = {
            "type": "P0_TEST_ORDER",
            "authorization_id": authorization_id,
            "account_alias": account.alias,
            "account_type": account.account_type,
            "adapter_instance": account.adapter_instance,
            "exchange": exchange,
            "instrument_id": instrument_id,
            "action": "OPEN_LONG",
            "order_direction": "BUY",
            "offset": "0",
            "order_type": "GFD",
            "hedgeflag": "1",
            "market": False,
            "volume": 1,
            "limit_price": limit_price,
            "price_policy": "LOWER_LIMIT",
            "memo_token": "WBP0" + authorization_id[-12:].upper(),
            "quote_captured_at": quote_result["captured_at"],
            "notional_cap": effective_notional_cap,
            "isolated_notional_cap": isolated_cap,
            "margin_guard_ratio": P0_ISOLATED_MARGIN_GUARD_RATIO if isolated_cap else None,
        }
        ttl = min(30, account.risk_limits.command_ttl_seconds)
        authorization = make_envelope(
            self.keyring,
            "P0_AUTHORIZATION",
            {
                "authorization_id": authorization_id,
                "account_alias": account.alias,
                "adapter_instance": account.adapter_instance,
                "investor_fingerprint": account.investor_fingerprint,
                "command_hash": hash_json(command),
            },
            ttl,
            "local-console",
            authorization_id,
        )
        atomic_write_json(self._runtime_file(account, "p0_authorization.json"), authorization)
        envelope = make_envelope(
            self.keyring, "P0_TEST_ORDER", command, ttl, "local-console", authorization_id
        )
        created = iso_now()
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO adapter_commands(message_id,intent_id,account_alias,command_type,folder,status,created_at,payload_json) VALUES(?,?,?,?,?,?,?,?)",
                (envelope["message_id"], None, account.alias, "P0_TEST_ORDER", "commands", "PENDING_DELIVERY", created, json_text(envelope)),
            )
            connection.execute(
                "INSERT INTO audit_log(occurred_at,actor,action,account_alias,object_id,details_json) VALUES(?,?,?,?,?,?)",
                (created, "local-console", "AUTHORIZE_P0_TEST_ORDER", account.alias, authorization_id, json_text({
                    "exchange": exchange, "instrument_id": instrument_id, "volume": 1,
                    "price_policy": "LOWER_LIMIT", "mapping_candidate": "BUY/0/GFD/1/market=false",
                    "isolated_notional_cap": effective_notional_cap if isolated_cap else None,
                    "margin_guard_ratio": P0_ISOLATED_MARGIN_GUARD_RATIO if isolated_cap else None,
                })),
            )
        self.dispatch_pending_commands(account.alias)
        return {
            "authorization_id": authorization_id,
            "message_id": envelope["message_id"],
            "status": "DELIVERED",
            "account_alias": account.alias,
            "instrument": "%s:%s" % (exchange, instrument_id),
            "volume": 1,
            "price_policy": "LOWER_LIMIT",
            "mapping_candidate": "BUY/0/GFD/1/market=false",
            "expires_in_seconds": ttl,
            "local_halt_preserved": True,
            "isolated_notional_cap": effective_notional_cap if isolated_cap else None,
            "margin_guard_ratio": P0_ISOLATED_MARGIN_GUARD_RATIO if isolated_cap else None,
        }

    def queue_p0_validation_leg(self, account_alias, exchange, instrument_id, action):
        account = self.config.account(account_alias)
        try:
            exchange, instrument_id = normalize_instrument(exchange, instrument_id)
        except ValueError as exc:
            raise ValidationError(str(exc))
        if (exchange, instrument_id) != ("SHFE", "au2610"):
            raise BridgeError("P0_VALIDATION_REJECTED", "validation legs are bound to SHFE:au2610")
        if action not in P0_VALIDATION_LEG_MAPPINGS:
            raise BridgeError("P0_VALIDATION_REJECTED", "unsupported P0 validation action")
        with self.database.connect() as connection:
            mode = self._mode(connection)
            halted = self._halted(connection)
            heartbeat = connection.execute(
                "SELECT * FROM heartbeats WHERE adapter_instance=?", (account.adapter_instance,)
            ).fetchone()
        if mode != "OBSERVE_ONLY" or not halted:
            raise BridgeError("P0_GATE_FAILED", "P0 validation requires OBSERVE_ONLY with durable halt")
        if not heartbeat or heartbeat["status"] != "READY" or age_seconds(heartbeat["received_at"]) > 15:
            raise BridgeError("ADAPTER_NOT_READY", "fresh READY heartbeat is required")
        heartbeat_payload = _payload(heartbeat) or {}
        if heartbeat["mode"] != "OBSERVE_ONLY" or not bool(heartbeat_payload.get("local_halt")):
            raise BridgeError("P0_GATE_FAILED", "Adapter must report OBSERVE_ONLY with local halt")

        quote_result = self.get_quote_snapshot(
            account.alias, [{"exchange": exchange, "instrument_id": instrument_id}]
        )[0]["snapshot"]
        if not quote_result or quote_result["age_seconds"] > account.risk_limits.max_quote_age_seconds:
            raise BridgeError("QUOTE_STALE", "fresh quote is required for a P0 validation leg")
        quote = quote_result["payload"]
        required_quote = (
            "last_price", "bid_price1", "ask_price1", "lower_limit_price",
            "upper_limit_price", "price_tick", "volume_multiple",
        )
        if not all(
            isinstance(quote.get(name), (int, float))
            and not isinstance(quote.get(name), bool)
            and quote[name] > 0
            for name in required_quote
        ):
            raise BridgeError("QUOTE_INCOMPLETE", "P0 validation quote fields are incomplete")
        direction, offset = P0_VALIDATION_LEG_MAPPINGS[action]
        tick = float(quote["price_tick"])
        if direction == "BUY":
            raw_price = float(quote["ask_price1"]) + 5 * tick
            limit_price = min(float(quote["upper_limit_price"]), round(raw_price / tick) * tick)
            marketable = limit_price >= float(quote["ask_price1"])
        else:
            raw_price = float(quote["bid_price1"]) - 5 * tick
            limit_price = max(float(quote["lower_limit_price"]), round(raw_price / tick) * tick)
            marketable = limit_price <= float(quote["bid_price1"])
        if not marketable or abs(limit_price - float(quote["last_price"])) / float(quote["last_price"]) > 0.01:
            raise BridgeError("P0_PRICE_INVALID", "P0 validation price is not safely marketable")
        notional = limit_price * float(quote["volume_multiple"])
        if account.risk_limits.max_order_volume < 1 or notional > P0_VALIDATION_NOTIONAL_CAP:
            raise BridgeError("P0_RISK_REJECTED", "P0 validation leg exceeds its isolated limit")

        account_snapshot = self.get_account_snapshot(account.alias)
        if account_snapshot["age_seconds"] > account.risk_limits.max_snapshot_age_seconds:
            raise BridgeError("ACCOUNT_SNAPSHOT_STALE", "fresh account snapshot is required")
        funds = account_snapshot["payload"]
        for name in ("available", "margin", "risk"):
            if not isinstance(funds.get(name), (int, float)) or isinstance(funds.get(name), bool):
                raise BridgeError("ACCOUNT_SNAPSHOT_INCOMPLETE", "P0 account fields are incomplete")
        if funds["available"] <= 0 or funds["margin"] > account.risk_limits.max_total_margin or funds["risk"] > account.risk_limits.max_risk_ratio:
            raise BridgeError("P0_RISK_REJECTED", "P0 account hard risk gate rejected the leg")

        positions = self.get_positions(
            account.alias, exchange=exchange, instrument_id=instrument_id, snapshot_kind="FULL"
        )
        if positions["age_seconds"] > account.risk_limits.max_snapshot_age_seconds:
            raise BridgeError("POSITION_SNAPSHOT_STALE", "fresh FULL position snapshot is required")
        long_position = sum(int((item.get("long") or {}).get("position") or 0) for item in positions["items"])
        short_position = sum(int((item.get("short") or {}).get("position") or 0) for item in positions["items"])
        long_today = sum(int((item.get("long") or {}).get("td_close_available") or 0) for item in positions["items"])
        short_today = sum(int((item.get("short") or {}).get("td_close_available") or 0) for item in positions["items"])
        if action.startswith("OPEN_"):
            if long_position or short_position:
                raise BridgeError("P0_POSITION_GATE", "P0 open leg requires zero existing position")
            guarded_margin = notional * P0_ISOLATED_MARGIN_GUARD_RATIO
            if funds["available"] < guarded_margin or funds["margin"] + guarded_margin > account.risk_limits.max_total_margin:
                raise BridgeError("P0_RISK_REJECTED", "conservative margin gate rejected the open leg")
        elif action == "CLOSE_TODAY_LONG" and long_today < 1:
            raise BridgeError("P0_POSITION_GATE", "one lot of long today position is required")
        elif action == "CLOSE_TODAY_SHORT" and short_today < 1:
            raise BridgeError("P0_POSITION_GATE", "one lot of short today position is required")

        validation_id = new_id("p0leg")
        command = {
            "type": "P0_VALIDATION_LEG", "validation_id": validation_id,
            "account_alias": account.alias, "account_type": account.account_type,
            "adapter_instance": account.adapter_instance, "exchange": exchange,
            "instrument_id": instrument_id, "action": action,
            "order_direction": direction, "offset": offset, "order_type": "GFD",
            "hedgeflag": "1", "market": False, "volume": 1,
            "limit_price": limit_price, "price_policy": "CROSS_5_TICKS",
            "memo_token": "WBLG" + validation_id[-12:].upper(),
            "quote_captured_at": quote_result["captured_at"],
            "notional_cap": P0_VALIDATION_NOTIONAL_CAP, "margin_guard_ratio": P0_ISOLATED_MARGIN_GUARD_RATIO,
        }
        ttl = min(30, account.risk_limits.command_ttl_seconds)
        authorization = make_envelope(
            self.keyring, "P0_LEG_AUTHORIZATION",
            {
                "validation_id": validation_id, "account_alias": account.alias,
                "adapter_instance": account.adapter_instance,
                "investor_fingerprint": account.investor_fingerprint,
                "command_hash": hash_json(command),
            },
            ttl, "local-console", validation_id,
        )
        atomic_write_json(self._runtime_file(account, "p0_leg_authorization.json"), authorization)
        envelope = make_envelope(
            self.keyring, "P0_VALIDATION_LEG", command, ttl, "local-console", validation_id
        )
        created = iso_now()
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO adapter_commands(message_id,intent_id,account_alias,command_type,folder,status,created_at,payload_json) VALUES(?,?,?,?,?,?,?,?)",
                (envelope["message_id"], None, account.alias, "P0_VALIDATION_LEG", "commands", "PENDING_DELIVERY", created, json_text(envelope)),
            )
            connection.execute(
                "INSERT INTO audit_log(occurred_at,actor,action,account_alias,object_id,details_json) VALUES(?,?,?,?,?,?)",
                (created, "local-console", "AUTHORIZE_P0_VALIDATION_LEG", account.alias, validation_id, json_text({
                    "exchange": exchange, "instrument_id": instrument_id, "action": action,
                    "volume": 1, "price_policy": "CROSS_5_TICKS", "mapping": "%s/%s/GFD/1/market=false" % (direction, offset),
                })),
            )
        self.dispatch_pending_commands(account.alias)
        return {
            "validation_id": validation_id, "message_id": envelope["message_id"],
            "status": "DELIVERED", "instrument": "%s:%s" % (exchange, instrument_id),
            "action": action, "volume": 1, "price_policy": "CROSS_5_TICKS",
            "mapping_candidate": "%s/%s/GFD/1/market=false" % (direction, offset),
            "expires_in_seconds": ttl, "local_halt_preserved": True,
        }

    def cancel_order(self, account_alias, bridge_order_id, reason):
        account = self.config.account(account_alias)
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 200:
            raise ValidationError("reason must contain 1-200 characters")
        with self.database.connect() as connection:
            child = connection.execute("SELECT * FROM child_orders WHERE child_order_id=? AND intent_id IN (SELECT intent_id FROM trade_intents WHERE account_alias=?)", (bridge_order_id, account_alias)).fetchone()
            if not child:
                raise BridgeError("ORDER_NOT_FOUND", "bridge order was not found")
            if child["status"] not in ACTIVE_ORDER_STATES:
                raise BridgeError("ORDER_ALREADY_TERMINAL", "order is not active")
            prior = connection.execute("SELECT * FROM adapter_commands WHERE child_order_id=? AND command_type='CANCEL_ORDER' ORDER BY created_at DESC LIMIT 1", (bridge_order_id,)).fetchone()
            if prior:
                return {"message_id": prior["message_id"], "status": prior["status"], "duplicate": True}
        payload = {
            "type": "CANCEL_ORDER", "account_alias": account.alias,
            "child_order_id": child["child_order_id"], "intent_id": child["intent_id"],
            "client_order_key": child["client_order_key"], "pythongo_order_id": child["pythongo_order_id"],
            "reason": reason.strip(),
        }
        if payload["pythongo_order_id"] is None:
            raise BridgeError("ORDER_NOT_ACKNOWLEDGED", "PythonGO order_id is not known")
        return self._queue_control(account, "CANCEL_ORDER", payload, child["intent_id"])

    def request_reconciliation(self, account_alias, reason):
        account = self.config.account(account_alias)
        if not isinstance(reason, str) or not reason.strip():
            raise ValidationError("reason must be non-empty")
        run_id = new_id("recon")
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO reconciliation_runs(run_id,account_alias,status,started_at,result_json) VALUES(?,?,?,?,?)",
                (run_id, account.alias, "REQUESTED", iso_now(), json_text({"reason": reason.strip()})),
            )
            self._set_state(connection, "reconciliation_required:%s" % account.alias, "true")
        sync = self.request_sync(account.alias, ["ACCOUNT", "POSITION", "ORDER", "TRADE"])
        return {"run_id": run_id, "status": "REQUESTED", "sync": sync}

    def halt_trading(self, reason):
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
            raise ValidationError("reason must contain 1-500 characters")
        now = iso_now()
        with self.database.transaction(immediate=True) as connection:
            self._set_state(connection, "halted", "true", now)
            self._set_state(connection, "halt_reason", reason.strip(), now)
            connection.execute("DELETE FROM system_state WHERE key LIKE 'manual_session:%'")
            connection.execute("UPDATE system_state SET value=?,updated_at=? WHERE key LIKE 'live_until:%'", (now, now))
            connection.execute("UPDATE approvals SET expires_at=? WHERE used_at IS NULL AND expires_at>?", (now, now))
            for account in self.config.accounts.values():
                if not account.enabled:
                    continue
                self._next_auto_generation(connection, account.alias)
                connection.execute(
                    """UPDATE auto_permits SET status='REVOKED',revoked_at=?,revoked_reason=?
                       WHERE account_alias=? AND status IN ('ACTIVE','PAUSED')""",
                    (now, "halt_trading: " + reason.strip(), account.alias),
                )
            connection.execute(
                "INSERT INTO audit_log(occurred_at,actor,action,details_json) VALUES(?,?,?,?)",
                (now, "mcp", "HALT_TRADING", json_text({"reason": reason.strip()})),
            )
        failures = []
        for account in self.config.accounts.values():
            if account.enabled:
                try:
                    self._write_local_halt(account, True, reason.strip(), "mcp")
                except Exception as exc:
                    failures.append({"account_alias": account.alias, "error": str(exc)})
        return {"halted": True, "reason": reason.strip(), "adapter_file_failures": failures}

    def dispatch_pending_commands(self, account_alias=None):
        query = "SELECT * FROM adapter_commands WHERE status='PENDING_DELIVERY'"
        values = []
        if account_alias:
            query += " AND account_alias=?"
            values.append(account_alias)
        query += " ORDER BY created_at LIMIT 100"
        delivered = []
        with self.database.connect() as connection:
            rows = connection.execute(query, values).fetchall()
        for row in rows:
            account = self.config.account(row["account_alias"])
            envelope = json.loads(row["payload_json"])
            self.file_queue.write(account.adapter_instance, row["folder"], envelope)
            with self.database.transaction(immediate=True) as connection:
                connection.execute(
                    "UPDATE adapter_commands SET status='DELIVERED',delivered_at=? WHERE message_id=? AND status='PENDING_DELIVERY'",
                    (iso_now(), row["message_id"]),
                )
            delivered.append(row["message_id"])
        return delivered
