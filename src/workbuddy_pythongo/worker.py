import argparse
import datetime as dt
import hmac
import ipaddress
import json
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__
from .config import load_config
from .core import BridgeCore
from .db import Database
from .errors import BridgeError
from .event_ingest import EventIngester
from .file_queue import FileQueue
from .margin_reference import ensure_margin_policy_state
from .reconciliation import Reconciler
from .security import KeyRing
from .util import iso_now, json_text, new_id, strict_json_loads, utc_now


def build_runtime(config_path=None, require_margin_policy=True):
    config = load_config(config_path)
    if not ipaddress.ip_address(config.host).is_loopback:
        raise BridgeError("CONFIG_ERROR", "worker must bind to a loopback address")
    database = Database(os.path.join(config.data_dir, "state", "bridge.db"))
    database.initialize(config.default_mode)
    if require_margin_policy:
        ensure_margin_policy_state(config, database)
    keyring = KeyRing.load(config.key_file)
    queue = FileQueue(config.data_dir, keyring, config.max_message_bytes)
    for account in config.accounts.values():
        queue.ensure_partition(account.adapter_instance)
        os.makedirs(os.path.join(config.data_dir, "pythongo_runtime", account.adapter_instance, "execution_journal"), exist_ok=True)
        os.makedirs(os.path.join(config.data_dir, "pythongo_runtime", account.adapter_instance, "heartbeat"), exist_ok=True)
    ingester = EventIngester(config, database, queue)
    ingester.reconcile_terminal_evidence()
    reconciler = Reconciler(config, database)
    core = BridgeCore(config, database, keyring, queue, ingester)
    core.reconciler = reconciler
    return config, database, core


def revoke_startup_authorizations(config, database, core, phase="startup"):
    now = iso_now()
    with database.transaction(immediate=True) as connection:
        connection.execute("UPDATE approvals SET used_at=? WHERE used_at IS NULL", (now,))
        connection.execute("DELETE FROM system_state WHERE key LIKE 'manual_session:%'")
        connection.execute("UPDATE system_state SET value=?,updated_at=? WHERE key LIKE 'live_until:%'", (now, now))
        connection.execute("DELETE FROM system_state WHERE key LIKE 'auto_license:%'")
        for account in config.accounts.values():
            if not account.enabled:
                continue
            core._next_auto_generation(connection, account.alias)
            connection.execute(
                """UPDATE auto_permits SET status='REVOKED',revoked_at=?,revoked_reason=?
                   WHERE account_alias=? AND status IN ('ACTIVE','PAUSED')""",
                (now, "worker process %s" % phase, account.alias),
            )
        connection.execute(
            "INSERT INTO audit_log(occurred_at,actor,action,details_json) VALUES(?,?,?,?)",
            (now, "worker", "REVOKE_AUTHORIZATIONS_ON_%s" % phase.upper(), json_text({"reason": "worker process %s" % phase})),
        )
    for account in config.accounts.values():
        if account.enabled:
            core._write_local_authorization(account, [], utc_now() + dt.timedelta(seconds=1), "worker-%s" % phase)


def refresh_simulation_lease(core, lease_seconds=5):
    with core.database.connect() as connection:
        mode_row = connection.execute("SELECT value FROM system_state WHERE key='mode'").fetchone()
        halt_row = connection.execute("SELECT value FROM system_state WHERE key='halted'").fetchone()
        recon_row = connection.execute("SELECT 1 FROM system_state WHERE key LIKE 'reconciliation_required:%' AND value='true' LIMIT 1").fetchone()
        mode = mode_row["value"] if mode_row else core.config.default_mode
        halted = bool(halt_row and halt_row["value"] == "true")
    if mode != "SIM_SIGNAL" or halted or bool(recon_row):
        return False
    expires = utc_now() + dt.timedelta(seconds=int(lease_seconds))
    for account in core.config.accounts.values():
        if account.enabled:
            core._write_local_authorization(account, ["SIM_SIGNAL"], expires, "worker-simulation-lease")
    return True


class RuntimeLoop(threading.Thread):
    def __init__(self, core, active_interval=0.1, idle_interval=0.2, maintenance_interval=1.0):
        super().__init__(name="pythongo-queue-loop", daemon=True)
        self.core = core
        self.active_interval = float(active_interval)
        self.idle_interval = float(idle_interval)
        self.maintenance_interval = float(maintenance_interval)
        self.stop_event = threading.Event()
        self.failed_closed = False
        self.last_lease_at = 0.0
        self.last_maintenance_at = 0.0

    @staticmethod
    def _ingest_activity(result):
        return any(
            int(summary.get("processed", 0)) or int(summary.get("dead_lettered", 0))
            for account in result.values()
            for summary in account.values()
        )

    def run(self):
        while not self.stop_event.is_set():
            try:
                now = time.monotonic()
                if now - self.last_lease_at >= 1.0:
                    refresh_simulation_lease(self.core)
                    self.last_lease_at = now
                ingested = self.core.ingester.scan_once()
                delivered = self.core.dispatch_pending_commands()
                reconciled = []
                if now - self.last_maintenance_at >= self.maintenance_interval:
                    reconciled = self.core.reconciler.scan_once()
                    self.last_maintenance_at = now
                activity = self._ingest_activity(ingested) or bool(delivered) or bool(reconciled)
            except Exception as exc:
                print("queue loop error: %s" % exc, file=sys.stderr, flush=True)
                if not self.failed_closed:
                    self.failed_closed = True
                    reason = "Worker queue loop failure: %s" % type(exc).__name__
                    try:
                        self.core.halt_trading(reason)
                    except Exception:
                        for account in self.core.config.accounts.values():
                            if account.enabled:
                                try:
                                    self.core._write_local_halt(account, True, reason, "worker-fail-closed")
                                except Exception:
                                    pass
                activity = False
            self.stop_event.wait(self.active_interval if activity else self.idle_interval)


def make_handler(core, database, token, max_bytes):
    class Handler(BaseHTTPRequestHandler):
        server_version = "WorkBuddyPythonGO/" + __version__

        def log_message(self, format_string, *args):
            return

        def _send(self, status, value):
            encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(encoded)

        def do_POST(self):
            if self.path != "/rpc":
                self._send(404, {"ok": False})
                return
            supplied = self.headers.get("Authorization", "")
            if not hmac.compare_digest(supplied, "Bearer " + token):
                self._send(401, {"ok": False, "error": {"code": "UNAUTHORIZED", "message": "invalid local token", "details": {}}})
                return
            request_id = new_id("req")
            method = "<invalid>"
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > max_bytes:
                    raise BridgeError("INVALID_REQUEST", "invalid request size")
                if "application/json" not in self.headers.get("Content-Type", ""):
                    raise BridgeError("INVALID_REQUEST", "Content-Type must be application/json")
                body = strict_json_loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(body, dict) or set(body) - {"method", "params", "request_id"} or "method" not in body:
                    raise BridgeError("INVALID_REQUEST", "invalid RPC envelope")
                supplied_id = body.get("request_id")
                if supplied_id is not None and (not isinstance(supplied_id, str) or not 1 <= len(supplied_id) <= 128):
                    raise BridgeError("INVALID_REQUEST", "request_id must contain 1-128 characters")
                if not isinstance(body["method"], str) or not 1 <= len(body["method"]) <= 100:
                    raise BridgeError("INVALID_REQUEST", "method must contain 1-100 characters")
                if "params" in body and not isinstance(body["params"], dict):
                    raise BridgeError("INVALID_REQUEST", "params must be an object")
                request_id = supplied_id or request_id
                method = body["method"]
                data = core.call(method, body.get("params") or {})
                result = {"ok": True, "request_id": request_id, "as_of": iso_now(), "data": data, "warnings": [], "error": None}
                with database.transaction() as connection:
                    connection.execute(
                        "INSERT OR REPLACE INTO mcp_requests(request_id,method,requested_at,ok,error_code) VALUES(?,?,?,?,?)",
                        (request_id, method, result["as_of"], 1, None),
                    )
                self._send(200, result)
            except BridgeError as exc:
                result = {"ok": False, "request_id": request_id, "as_of": iso_now(), "data": None, "warnings": [], "error": {"code": exc.code, "message": exc.message, "details": exc.details}}
                try:
                    with database.transaction() as connection:
                        connection.execute(
                            "INSERT OR REPLACE INTO mcp_requests(request_id,method,requested_at,ok,error_code) VALUES(?,?,?,?,?)",
                            (request_id, method, result["as_of"], 0, exc.code),
                        )
                except Exception:
                    pass
                self._send(200, result)
            except Exception:
                self._send(500, {"ok": False, "request_id": request_id, "as_of": iso_now(), "data": None, "warnings": [], "error": {"code": "INTERNAL_ERROR", "message": "worker failed to process request", "details": {}}})

    return Handler


def _install_signal_handlers():
    if threading.current_thread() is not threading.main_thread():
        return {}
    previous = {}

    def interrupt(signum, frame):
        raise KeyboardInterrupt

    for name in ("SIGINT", "SIGBREAK"):
        value = getattr(signal, name, None)
        if value is not None:
            previous[value] = signal.getsignal(value)
            signal.signal(value, interrupt)
    return previous


def main(argv=None):
    parser = argparse.ArgumentParser(description="WorkBuddy-PythonGO bridge worker")
    parser.add_argument("--config", default=None)
    parser.add_argument("--confirm-mode", default=None, help="must equal the persisted non-observe mode")
    args = parser.parse_args(argv)
    config, database, core = build_runtime(args.config)
    with database.connect() as connection:
        row = connection.execute("SELECT value FROM system_state WHERE key='mode'").fetchone()
        mode = row["value"] if row else config.default_mode
    if mode != "OBSERVE_ONLY" and args.confirm_mode != mode:
        raise SystemExit("non-observe Worker startup requires --confirm-mode %s" % mode)
    reconciliation_accounts = core.reconciler.require_on_startup()
    revoke_startup_authorizations(config, database, core)
    for alias in reconciliation_accounts:
        core.request_sync(alias, ["ACCOUNT", "POSITION", "ORDER", "TRADE"])
    refresh_simulation_lease(core)
    try:
        with open(config.worker_token_file, "r", encoding="ascii") as stream:
            token = stream.read().strip()
    except OSError as exc:
        raise SystemExit("cannot read worker token: %s" % exc)
    if len(token) < 32:
        raise SystemExit("worker token is too short")
    loop = RuntimeLoop(core)
    loop.start()
    server = ThreadingHTTPServer((config.host, config.port), make_handler(core, database, token, config.max_message_bytes))
    server.daemon_threads = True
    previous = _install_signal_handlers()
    print("worker listening on http://%s:%d" % (config.host, config.port), file=sys.stderr, flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        loop.stop_event.set()
        server.server_close()
        loop.join(timeout=2)
        try:
            revoke_startup_authorizations(config, database, core, "shutdown")
        except Exception:
            for account in config.accounts.values():
                if account.enabled:
                    try:
                        core._write_local_authorization(account, [], utc_now() + dt.timedelta(seconds=1), "worker-shutdown")
                    except Exception:
                        pass
        for value, handler in previous.items():
            signal.signal(value, handler)
    return 0


if __name__ == "__main__":
    sys.exit(main())
