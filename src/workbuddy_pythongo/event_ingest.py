from .errors import BridgeError
from .util import iso_now, json_text


EVENT_TYPES = {
    "HEARTBEAT", "ACCOUNT_SNAPSHOT", "QUOTE_SNAPSHOT", "KLINE_SNAPSHOT",
    "POSITION_SNAPSHOT", "ORDER_EVENT", "TRADE_EVENT", "CANCEL_EVENT",
    "ERROR_EVENT", "SIGNAL_EVENT",
}
ACK_TYPES = {"COMMAND_ACK"}
TERMINAL_ORDER_STATES = {
    "FILLED", "PARTIALLY_FILLED_CANCELLED", "CANCELLED", "REJECTED",
    "FAILED_BEFORE_SEND", "SUBMIT_UNKNOWN", "OBSERVED",
}


class EventIngester:
    def __init__(self, config, database, file_queue):
        self.config = config
        self.database = database
        self.file_queue = file_queue

    def scan_once(self, limit_per_folder=100):
        result = {}
        for account in self.config.accounts.values():
            if not account.enabled:
                continue
            adapter = account.adapter_instance
            result[account.alias] = {
                "command_acks": self.file_queue.consume(
                    adapter, "command_acks", self._handler(account.alias), ACK_TYPES, limit_per_folder
                ),
                "control_acks": self.file_queue.consume(
                    adapter, "control_acks", self._handler(account.alias), ACK_TYPES, limit_per_folder
                ),
                "events": self.file_queue.consume(
                    adapter, "events", self._handler(account.alias), EVENT_TYPES, limit_per_folder
                ),
            }
        return result

    def _handler(self, expected_alias):
        def handle(envelope):
            payload = envelope["payload"]
            if not isinstance(payload, dict):
                raise BridgeError("MESSAGE_SCHEMA_INVALID", "event payload must be an object")
            if payload.get("account_alias") != expected_alias:
                raise BridgeError("ACCOUNT_BINDING_MISMATCH", "event arrived in the wrong account partition")
            with self.database.transaction(immediate=True) as connection:
                inserted = connection.execute(
                    "INSERT OR IGNORE INTO inbound_events(message_id,account_alias,event_type,received_at,payload_json) VALUES(?,?,?,?,?)",
                    (envelope["message_id"], expected_alias, envelope["message_type"], iso_now(), json_text(payload)),
                ).rowcount
                if not inserted:
                    return
                self._ingest(connection, envelope["message_id"], envelope["message_type"], payload)
        return handle

    def _ingest(self, connection, event_id, message_type, payload):
        if message_type == "HEARTBEAT":
            self._heartbeat(connection, payload)
        elif message_type == "ACCOUNT_SNAPSHOT":
            self._account(connection, payload)
        elif message_type == "QUOTE_SNAPSHOT":
            self._quotes(connection, payload)
        elif message_type == "KLINE_SNAPSHOT":
            self._klines(connection, payload)
        elif message_type == "POSITION_SNAPSHOT":
            self._positions(connection, payload)
        elif message_type == "COMMAND_ACK":
            self._command_ack(connection, event_id, payload)
        elif message_type in {"ORDER_EVENT", "CANCEL_EVENT"}:
            self._order_event(connection, event_id, payload)
        elif message_type == "TRADE_EVENT":
            self._trade_event(connection, payload)
        elif message_type == "SIGNAL_EVENT":
            signal_id = payload.get("signal_id")
            if not isinstance(signal_id, str) or not signal_id:
                raise BridgeError("MESSAGE_SCHEMA_INVALID", "signal_id is required")
            connection.execute(
                "INSERT OR IGNORE INTO market_signals(signal_id,source_type,received_at,payload_json) VALUES(?,?,?,?)",
                (signal_id, str(payload.get("source_type") or "PYTHONGO"), iso_now(), json_text(payload)),
            )
        elif message_type == "ERROR_EVENT":
            connection.execute(
                "INSERT INTO audit_log(occurred_at,actor,action,account_alias,object_id,details_json) VALUES(?,?,?,?,?,?)",
                (payload.get("occurred_at") or iso_now(), "adapter", "PYTHONGO_ERROR", payload["account_alias"], str(payload.get("pythongo_order_id") or ""), json_text(payload)),
            )

    @staticmethod
    def _require(payload, names):
        missing = [name for name in names if name not in payload]
        if missing:
            raise BridgeError("MESSAGE_SCHEMA_INVALID", "event fields are missing", {"missing": missing})

    def _heartbeat(self, connection, payload):
        self._require(payload, {"adapter_instance", "account_alias", "status", "occurred_at"})
        account = self.config.account(payload["account_alias"])
        if payload["adapter_instance"] != account.adapter_instance:
            raise BridgeError("ACCOUNT_BINDING_MISMATCH", "heartbeat adapter_instance mismatch")
        connection.execute(
            """INSERT INTO heartbeats(adapter_instance,account_alias,status,mode,profile_status,occurred_at,received_at,payload_json)
               VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(adapter_instance) DO UPDATE SET
               account_alias=excluded.account_alias,status=excluded.status,mode=excluded.mode,
               profile_status=excluded.profile_status,occurred_at=excluded.occurred_at,
               received_at=excluded.received_at,payload_json=excluded.payload_json""",
            (
                payload["adapter_instance"], payload["account_alias"], payload["status"],
                payload.get("mode"), payload.get("profile_status"), payload["occurred_at"],
                iso_now(), json_text(payload),
            ),
        )

    def _account(self, connection, payload):
        self._require(payload, {"snapshot_id", "account_alias", "captured_at", "account"})
        connection.execute(
            "INSERT OR IGNORE INTO account_snapshots(snapshot_id,account_alias,captured_at,received_at,payload_json) VALUES(?,?,?,?,?)",
            (payload["snapshot_id"], payload["account_alias"], payload["captured_at"], iso_now(), json_text(payload["account"])),
        )

    def _quotes(self, connection, payload):
        self._require(payload, {"snapshot_id", "account_alias", "captured_at", "quotes"})
        if not isinstance(payload["quotes"], list):
            raise BridgeError("MESSAGE_SCHEMA_INVALID", "quotes must be an array")
        for quote in payload["quotes"]:
            self._require(quote, {"exchange", "instrument_id"})
            connection.execute(
                "INSERT OR IGNORE INTO quote_snapshots(account_alias,snapshot_id,exchange,instrument_id,captured_at,received_at,payload_json) VALUES(?,?,?,?,?,?,?)",
                (
                    payload["account_alias"], payload["snapshot_id"], quote["exchange"],
                    quote["instrument_id"], payload["captured_at"], iso_now(), json_text(quote),
                ),
            )

    def _klines(self, connection, payload):
        self._require(payload, {"snapshot_id", "account_alias", "captured_at", "exchange", "instrument_id", "interval", "bars"})
        connection.execute(
            "INSERT OR IGNORE INTO kline_snapshots(account_alias,snapshot_id,exchange,instrument_id,interval,captured_at,received_at,payload_json) VALUES(?,?,?,?,?,?,?,?)",
            (
                payload["account_alias"], payload["snapshot_id"], payload["exchange"],
                payload["instrument_id"], payload["interval"], payload["captured_at"],
                iso_now(), json_text(payload),
            ),
        )

    def _positions(self, connection, payload):
        self._require(payload, {"snapshot_id", "account_alias", "snapshot_kind", "captured_at", "positions"})
        if payload["snapshot_kind"] not in {"SIMPLE", "FULL"} or not isinstance(payload["positions"], list):
            raise BridgeError("MESSAGE_SCHEMA_INVALID", "invalid position snapshot")
        connection.execute(
            "INSERT OR IGNORE INTO position_snapshot_runs(account_alias,snapshot_id,snapshot_kind,captured_at,received_at) VALUES(?,?,?,?,?)",
            (payload["account_alias"], payload["snapshot_id"], payload["snapshot_kind"], payload["captured_at"], iso_now()),
        )
        for position in payload["positions"]:
            self._require(position, {"exchange", "instrument_id", "hedgeflag"})
            connection.execute(
                "INSERT OR IGNORE INTO position_snapshots(account_alias,snapshot_id,exchange,instrument_id,hedgeflag,captured_at,payload_json) VALUES(?,?,?,?,?,?,?)",
                (
                    payload["account_alias"], payload["snapshot_id"], position["exchange"],
                    position["instrument_id"], str(position["hedgeflag"]), payload["captured_at"],
                    json_text(position),
                ),
            )

    def _lookup_child(self, connection, payload):
        key = payload.get("client_order_key")
        memo = payload.get("memo") or payload.get("memo_token")
        row = None
        if key:
            row = connection.execute("SELECT * FROM child_orders WHERE client_order_key=?", (key,)).fetchone()
        if not row and memo:
            row = connection.execute("SELECT * FROM child_orders WHERE memo_token=?", (memo,)).fetchone()
        return row

    def _command_ack(self, connection, event_id, payload):
        self._require(payload, {"message_id", "account_alias", "status", "occurred_at"})
        command = connection.execute("SELECT * FROM adapter_commands WHERE message_id=?", (payload["message_id"],)).fetchone()
        connection.execute(
            "INSERT OR IGNORE INTO adapter_command_acks(event_id,message_id,intent_id,child_order_id,account_alias,status,occurred_at,payload_json) VALUES(?,?,?,?,?,?,?,?)",
            (
                event_id, payload["message_id"], payload.get("intent_id"), payload.get("child_order_id"),
                payload["account_alias"], payload["status"], payload["occurred_at"], json_text(payload),
            ),
        )
        if command:
            connection.execute("UPDATE adapter_commands SET status=? WHERE message_id=?", (payload["status"], payload["message_id"]))
        child_id = payload.get("child_order_id") or (command["child_order_id"] if command else None)
        if child_id:
            mapping = {
                "OBSERVE_ONLY_ACKNOWLEDGED": "OBSERVED",
                "SEND_RETURNED": "SEND_RETURNED",
                "FAILED_BEFORE_SEND": "FAILED_BEFORE_SEND",
                "SUBMIT_UNKNOWN": "SUBMIT_UNKNOWN",
                "ADAPTER_REJECTED": "REJECTED",
            }
            child_status = mapping.get(payload["status"])
            if child_status:
                details = payload.get("details") or {}
                connection.execute(
                    "UPDATE child_orders SET status=?,pythongo_order_id=COALESCE(?,pythongo_order_id),updated_at=? WHERE child_order_id=?",
                    (child_status, details.get("pythongo_order_id"), iso_now(), child_id),
                )
                if child_status in TERMINAL_ORDER_STATES:
                    self._advance_sequence(connection, child_id, child_status)
                self._aggregate_intent(connection, child_id)

    def _order_event(self, connection, event_id, payload):
        self._require(payload, {"account_alias", "occurred_at", "trading_day", "exchange", "instrument_id", "normalized_status"})
        child = self._lookup_child(connection, payload)
        child_id = child["child_order_id"] if child else None
        intent_id = child["intent_id"] if child else None
        order_id = payload.get("pythongo_order_id")
        connection.execute(
            "INSERT OR IGNORE INTO order_events(event_id,account_alias,pythongo_order_id,occurred_at,payload_json) VALUES(?,?,?,?,?)",
            (event_id, payload["account_alias"], order_id, payload["occurred_at"], json_text(payload)),
        )
        if order_id is not None:
            connection.execute(
                """INSERT INTO orders(account_alias,trading_day,pythongo_order_id,order_sys_id,client_order_key,intent_id,child_order_id,
                   exchange,instrument_id,status,requested_volume,filled_volume,cancel_volume,limit_price,updated_at,payload_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(account_alias,trading_day,pythongo_order_id) DO UPDATE SET
                   order_sys_id=excluded.order_sys_id,client_order_key=excluded.client_order_key,intent_id=excluded.intent_id,
                   child_order_id=excluded.child_order_id,status=excluded.status,requested_volume=excluded.requested_volume,
                   filled_volume=excluded.filled_volume,cancel_volume=excluded.cancel_volume,limit_price=excluded.limit_price,
                   updated_at=excluded.updated_at,payload_json=excluded.payload_json""",
                (
                    payload["account_alias"], payload["trading_day"], order_id, payload.get("order_sys_id"),
                    child["client_order_key"] if child else payload.get("client_order_key"), intent_id, child_id,
                    payload["exchange"], payload["instrument_id"], payload["normalized_status"],
                    payload.get("total_volume"), payload.get("traded_volume"), payload.get("cancel_volume"),
                    payload.get("price"), iso_now(), json_text(payload),
                ),
            )
        if child_id:
            status = payload["normalized_status"]
            connection.execute(
                "UPDATE child_orders SET status=?,pythongo_order_id=COALESCE(?,pythongo_order_id),order_sys_id=COALESCE(?,order_sys_id),updated_at=? WHERE child_order_id=?",
                (status, order_id, payload.get("order_sys_id"), iso_now(), child_id),
            )
            if status in TERMINAL_ORDER_STATES:
                self._advance_sequence(connection, child_id, status)
            self._aggregate_intent(connection, child_id)
        elif order_id is not None:
            connection.execute(
                "INSERT INTO audit_log(occurred_at,actor,action,account_alias,object_id,details_json) VALUES(?,?,?,?,?,?)",
                (iso_now(), "adapter", "UNOWNED_ORDER_DETECTED", payload["account_alias"], str(order_id), json_text(payload)),
            )

    def _trade_event(self, connection, payload):
        self._require(payload, {"account_alias", "trading_day", "trade_id", "exchange", "instrument_id", "volume", "price", "traded_at"})
        child = self._lookup_child(connection, payload)
        connection.execute(
            "INSERT OR IGNORE INTO trades(account_alias,trading_day,trade_id,pythongo_order_id,intent_id,child_order_id,exchange,instrument_id,volume,price,traded_at,payload_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                payload["account_alias"], payload["trading_day"], payload["trade_id"], payload.get("pythongo_order_id"),
                child["intent_id"] if child else None, child["child_order_id"] if child else None,
                payload["exchange"], payload["instrument_id"], int(payload["volume"]), float(payload["price"]),
                payload["traded_at"], json_text(payload),
            ),
        )

    def _advance_sequence(self, connection, child_id, status):
        child = connection.execute("SELECT * FROM child_orders WHERE child_order_id=?", (child_id,)).fetchone()
        if not child:
            return
        next_child = connection.execute(
            "SELECT * FROM child_orders WHERE intent_id=? AND child_no>? ORDER BY child_no LIMIT 1",
            (child["intent_id"], child["child_no"]),
        ).fetchone()
        if not next_child:
            return
        if status in {"FILLED", "OBSERVED"}:
            connection.execute(
                "UPDATE adapter_commands SET status='PENDING_DELIVERY' WHERE child_order_id=? AND status='BLOCKED_SEQUENCE'",
                (next_child["child_order_id"],),
            )
        else:
            connection.execute(
                "UPDATE child_orders SET status='SEQUENCE_ABORTED',updated_at=? WHERE intent_id=? AND child_no>? AND status='BLOCKED_SEQUENCE'",
                (iso_now(), child["intent_id"], child["child_no"]),
            )
            connection.execute(
                "UPDATE adapter_commands SET status='SEQUENCE_ABORTED' WHERE intent_id=? AND status='BLOCKED_SEQUENCE'",
                (child["intent_id"],),
            )

    def _aggregate_intent(self, connection, child_id):
        child = connection.execute("SELECT intent_id FROM child_orders WHERE child_order_id=?", (child_id,)).fetchone()
        if not child:
            return
        rows = connection.execute("SELECT status FROM child_orders WHERE intent_id=? ORDER BY child_no", (child["intent_id"],)).fetchall()
        states = [row["status"] for row in rows]
        if any(state == "SUBMIT_UNKNOWN" for state in states):
            status = "SUBMIT_UNKNOWN"
        elif all(state in {"FILLED", "OBSERVED"} for state in states):
            status = "OBSERVED" if all(state == "OBSERVED" for state in states) else "FILLED"
        elif any(state in {"REJECTED", "FAILED_BEFORE_SEND", "SEQUENCE_ABORTED"} for state in states):
            status = "FAILED"
        elif any(state in {"WORKING", "PARTIALLY_FILLED", "SEND_RETURNED"} for state in states):
            status = "ACTIVE"
        else:
            status = "QUEUED"
        connection.execute("UPDATE trade_intents SET status=?,updated_at=? WHERE intent_id=?", (status, iso_now(), child["intent_id"]))

    def reconcile_terminal_evidence(self):
        with self.database.transaction(immediate=True) as connection:
            rows = connection.execute("SELECT child_order_id FROM child_orders").fetchall()
            for row in rows:
                self._aggregate_intent(connection, row["child_order_id"])
