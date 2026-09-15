"""Read-only synchronization after a fresh Adapter connection, in either boot order."""

import sys

from .util import parse_time


class AdapterConnectionSync:
    def __init__(self, core, retry_interval=5.0):
        self.core = core
        self.retry_interval = float(retry_interval)
        self.states = {}

    @staticmethod
    def _transition(alias, state, phase, message):
        if state.get("phase") != phase:
            state["phase"] = phase
            print("[连接] %s：%s" % (alias, message), file=sys.stderr, flush=True)

    def _completed(self, alias, state):
        with self.core.database.connect() as connection:
            command = connection.execute(
                "SELECT status,created_at FROM adapter_commands WHERE message_id=?", (state["message_id"],),
            ).fetchone()
            account = connection.execute(
                "SELECT captured_at FROM account_snapshots WHERE account_alias=? ORDER BY captured_at DESC LIMIT 1",
                (alias,),
            ).fetchone()
            positions = connection.execute(
                "SELECT captured_at FROM position_snapshot_runs WHERE account_alias=? ORDER BY captured_at DESC LIMIT 1",
                (alias,),
            ).fetchone()
        completed = bool(
            command and command["status"] == "SYNC_COMPLETED" and account and positions
            and parse_time(account["captured_at"]) >= parse_time(command["created_at"])
            and parse_time(positions["captured_at"]) >= parse_time(command["created_at"])
        )
        return completed, command["status"] if command else None

    def scan_once(self, now):
        issued = 0
        for health in self.core.pythongo_health()["accounts"]:
            alias = health["account_alias"]
            state = self.states.setdefault(alias, {})
            if not health["connected"]:
                state.pop("message_id", None)
                self._transition(alias, state, "WAITING", "等待无限易中的 Adapter 上线；两种启动顺序均可。")
                continue
            if not health["mode_match"] or not health["margin_policy_match"]:
                state.pop("message_id", None)
                self._transition(alias, state, "CONFIG_MISMATCH", "模式或保证金策略不一致，请完整重启无限易加载当前配置。")
                continue
            session = health.get("adapter_session_id")
            if state.get("session_id") != session:
                state.pop("message_id", None)
                state["session_id"] = session
            if state.get("message_id"):
                completed, status = self._completed(alias, state)
                if completed:
                    self._transition(alias, state, "READY", "已连接，账户和持仓已自动同步。")
                    continue
                # Do not flood the control queue while a valid request is pending.
                ttl = self.core.config.account(alias).risk_limits.command_ttl_seconds
                retry_after = self.retry_interval if status not in {"PENDING_DELIVERY", "DELIVERED"} else max(self.retry_interval, ttl + 1)
                if now - state["sent_at"] < retry_after:
                    continue
            result = self.core.request_sync(alias, ["ACCOUNT", "POSITION", "ORDER", "TRADE"])
            state["message_id"] = result["message_id"]
            state["sent_at"] = now
            self._transition(alias, state, "SYNCING", "Adapter 已上线，正在自动同步账户和持仓。")
            issued += 1
        return issued
