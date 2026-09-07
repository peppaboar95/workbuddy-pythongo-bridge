from .util import iso_now, json_text, new_id


ACTIVE_CHILD_STATES = {
    "QUEUED", "BLOCKED_SEQUENCE", "PENDING_DELIVERY", "DELIVERED",
    "SEND_RETURNED", "WORKING", "PARTIALLY_FILLED", "CANCEL_REQUESTED",
    "SUBMIT_UNKNOWN",
}


class Reconciler:
    """Conservative restart/sync reconciliation over normalized local evidence."""

    def __init__(self, config, database):
        self.config = config
        self.database = database

    def require_on_startup(self):
        now = iso_now()
        required = []
        with self.database.transaction(immediate=True) as connection:
            for account in self.config.accounts.values():
                placeholders = ",".join("?" for _ in ACTIVE_CHILD_STATES)
                row = connection.execute(
                    "SELECT COUNT(*) AS n FROM child_orders WHERE intent_id IN (SELECT intent_id FROM trade_intents WHERE account_alias=?) AND status IN (%s)" % placeholders,
                    [account.alias] + sorted(ACTIVE_CHILD_STATES),
                ).fetchone()
                if row["n"]:
                    key = "reconciliation_required:%s" % account.alias
                    connection.execute(
                        "INSERT INTO system_state(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value='true',updated_at=excluded.updated_at",
                        (key, "true", now),
                    )
                    exists = connection.execute(
                        "SELECT 1 FROM reconciliation_runs WHERE account_alias=? AND status IN ('REQUESTED','WAITING_FOR_SNAPSHOTS','MANUAL_REVIEW') LIMIT 1",
                        (account.alias,),
                    ).fetchone()
                    if not exists:
                        connection.execute(
                            "INSERT INTO reconciliation_runs(run_id,account_alias,status,started_at,result_json) VALUES(?,?,?,?,?)",
                            (new_id("recon"), account.alias, "REQUESTED", now, json_text({"reason": "worker restart with non-terminal child orders"})),
                        )
                    required.append(account.alias)
        return required

    def scan_once(self):
        completed = []
        with self.database.transaction(immediate=True) as connection:
            runs = connection.execute(
                "SELECT * FROM reconciliation_runs WHERE status IN ('REQUESTED','WAITING_FOR_SNAPSHOTS') ORDER BY started_at"
            ).fetchall()
            for run in runs:
                alias = run["account_alias"]
                account = connection.execute(
                    "SELECT captured_at FROM account_snapshots WHERE account_alias=? AND captured_at>=? ORDER BY captured_at DESC LIMIT 1",
                    (alias, run["started_at"]),
                ).fetchone()
                positions = connection.execute(
                    "SELECT captured_at FROM position_snapshot_runs WHERE account_alias=? AND captured_at>=? ORDER BY captured_at DESC LIMIT 1",
                    (alias, run["started_at"]),
                ).fetchone()
                if not account or not positions:
                    connection.execute("UPDATE reconciliation_runs SET status='WAITING_FOR_SNAPSHOTS' WHERE run_id=?", (run["run_id"],))
                    continue
                placeholders = ",".join("?" for _ in ACTIVE_CHILD_STATES)
                active = connection.execute(
                    "SELECT child_order_id,status,pythongo_order_id FROM child_orders WHERE intent_id IN (SELECT intent_id FROM trade_intents WHERE account_alias=?) AND status IN (%s) ORDER BY child_order_id" % placeholders,
                    [alias] + sorted(ACTIVE_CHILD_STATES),
                ).fetchall()
                unknown = [dict(row) for row in active if row["status"] == "SUBMIT_UNKNOWN"]
                result = {
                    "fresh_account_snapshot": account["captured_at"],
                    "fresh_position_snapshot": positions["captured_at"],
                    "active_child_orders": [dict(row) for row in active],
                    "submit_unknown": unknown,
                    "order_trade_restart_replay_assumed": False,
                }
                if active:
                    status = "MANUAL_REVIEW"
                else:
                    status = "COMPLETED"
                    key = "reconciliation_required:%s" % alias
                    connection.execute(
                        "INSERT INTO system_state(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value='false',updated_at=excluded.updated_at",
                        (key, "false", iso_now()),
                    )
                    completed.append(run["run_id"])
                connection.execute(
                    "UPDATE reconciliation_runs SET status=?,completed_at=?,result_json=? WHERE run_id=?",
                    (status, iso_now(), json_text(result), run["run_id"]),
                )
                connection.execute(
                    "INSERT INTO audit_log(occurred_at,actor,action,account_alias,object_id,details_json) VALUES(?,?,?,?,?,?)",
                    (iso_now(), "worker", "RECONCILIATION_%s" % status, alias, run["run_id"], json_text(result)),
                )
        return completed
