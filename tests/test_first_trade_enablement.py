import contextlib
import datetime as dt
import io
import json
import pathlib
import tempfile
import unittest
from unittest import mock

from workbuddy_pythongo.config import load_config
from workbuddy_pythongo.bootstrap import initialize
from workbuddy_pythongo.console import (
    bind_investor, enable_first_trade, get_first_trade_enablement,
    get_non_observe_mode_blockers, sign_profile,
)
from workbuddy_pythongo.desktop import (
    _offer_first_trade_enablement, run_first_trade_enablement, select_start_mode, start_desktop, status_desktop,
)
from workbuddy_pythongo.errors import BridgeError
from workbuddy_pythongo.util import iso_now, utc_now
from workbuddy_pythongo.worker import build_runtime


class FirstTradeEnablementTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        initialized = initialize(self.temporary.name)
        self.config_path = initialized["config"]
        self.ready = pathlib.Path(initialized["ready_dir"])
        bind_investor(self.config_path, "main_futures", "TEST-ACCOUNT-001", "BIND-ACCOUNT")
        self.config, self.database, self.core = build_runtime(self.config_path)
        self.account = self.config.account("main_futures")
        self.local_halt = pathlib.Path(
            self.config.data_dir, "pythongo_runtime", self.account.adapter_instance, "local_halt.json",
        )
        self.worker_probe = mock.patch("workbuddy_pythongo.desktop.probe_worker", return_value={"state": "RUNNING"})
        self.worker_probe.start()
        self.addCleanup(self.worker_probe.stop)

    def state(self):
        with self.database.connect() as connection:
            return dict(connection.execute(
                "SELECT key,value FROM system_state WHERE key IN ('mode','halted','trade_protection_kind')"
            ).fetchall())

    def prepare_verified_profile_and_adapter(self):
        profile_path = self.ready / "pythongo_profile.json"
        adapter = json.loads((self.ready / "pythongo_adapter.json").read_text(encoding="utf-8"))
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        profile.update({
            "profile_id": "TEST-PROFILE", "infinitrader_build": "TEST-INFINITRADER",
            "pythongo_build": "TEST-PYTHONGO", "broker_build": "TEST-BROKER",
        })
        profile["capabilities"].update({
            "dispatch_mode": adapter["command_dispatch_mode"], "memo_max_bytes": 32,
            "explicit_close_yesterday": False, "order_trade_replay_after_restart": False,
            "order_status_map": {"3": "WORKING"},
        })
        profile["mappings"] = {"FUTURES:OPEN_LONG:LIMIT:GFD": {
            "order_direction": "0", "offset": "0", "order_type": "0", "hedgeflag": "1", "market": False,
        }}
        profile_path.write_text(json.dumps(profile), encoding="utf-8")
        sign_profile(self.config_path, "main_futures", "VERIFIED-PROFILE")
        with self.database.transaction() as connection:
            generation = connection.execute(
                "SELECT value FROM system_state WHERE key=?", ("margin_policy_generation:main_futures",),
            ).fetchone()["value"]
            policy_hash = connection.execute(
                "SELECT value FROM system_state WHERE key=?", ("margin_policy_hash:main_futures",),
            ).fetchone()["value"]
            payload = {"margin_policy_generation": int(generation), "margin_policy_hash": policy_hash, "local_halt": True}
            now = iso_now()
            connection.execute(
                "INSERT OR REPLACE INTO heartbeats VALUES(?,?,?,?,?,?,?,?)",
                (self.account.adapter_instance, "main_futures", "READY", "OBSERVE_ONLY", "VALID", now, now, json.dumps(payload)),
            )

    def test_unverified_profile_explains_steps_without_asking_to_unlock(self):
        before = self.local_halt.read_bytes()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = run_first_trade_enablement(self.config_path, input_func=lambda _prompt: self.fail("Unexpected confirmation"))
        self.assertEqual(result, 2)
        self.assertIn("P0现场验证和Profile签名", output.getvalue())
        self.assertIn("Adapter当前为OFFLINE", output.getvalue())
        self.assertEqual(self.state()["trade_protection_kind"], "SETUP_LOCK")
        self.assertEqual(self.local_halt.read_bytes(), before)

    def test_cancellation_and_wrong_confirmation_preserve_protection(self):
        self.prepare_verified_profile_and_adapter()
        self.assertTrue(get_first_trade_enablement(self.config_path)["can_enable"])
        before = self.local_halt.read_bytes()
        with self.assertRaises(BridgeError) as error:
            enable_first_trade(self.config_path, "CLEAR-HALT")
        self.assertEqual(error.exception.code, "CONFIRMATION_REQUIRED")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(run_first_trade_enablement(self.config_path, input_func=lambda _prompt: ""), 0)
        self.assertEqual(self.state()["trade_protection_kind"], "SETUP_LOCK")
        self.assertEqual(self.local_halt.read_bytes(), before)

    def test_confirmed_enablement_clears_only_setup_lock_and_keeps_observe_mode(self):
        self.prepare_verified_profile_and_adapter()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = run_first_trade_enablement(self.config_path, input_func=lambda _prompt: "ENABLE-FIRST-TRADE")
        self.assertEqual(result, 0)
        self.assertEqual(self.state(), {"halted": "false", "mode": "OBSERVE_ONLY", "trade_protection_kind": "NONE"})
        self.assertFalse(json.loads(self.local_halt.read_text(encoding="utf-8"))["payload"]["halted"])
        self.assertIn("LIMITED_AUTO还需要", output.getvalue())
        with self.database.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM auto_permits").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM audit_log WHERE action='CLEAR_HALT'").fetchone()[0], 1)

    def test_review_and_incident_protections_cannot_use_first_enablement(self):
        self.prepare_verified_profile_and_adapter()
        before = self.local_halt.read_bytes()
        for kind in ("ACCOUNT_CHANGE", "POLICY_REVIEW", "INCIDENT_HALT", "SETUP_LOCK"):
            with self.subTest(kind=kind):
                with self.database.transaction() as connection:
                    connection.execute("UPDATE system_state SET value=? WHERE key='trade_protection_kind'", (kind,))
                    connection.execute("UPDATE system_state SET value='true' WHERE key='halted'")
                with self.assertRaises(BridgeError) as error:
                    enable_first_trade(self.config_path, "ENABLE-FIRST-TRADE")
                self.assertEqual(error.exception.code, "FIRST_TRADE_NOT_READY")
                self.assertEqual(self.state()["trade_protection_kind"], kind)
                self.assertEqual(self.state()["halted"], "true")
                self.assertEqual(self.local_halt.read_bytes(), before)

    def test_stale_heartbeat_and_unloaded_profile_block_enablement(self):
        self.prepare_verified_profile_and_adapter()
        before = self.local_halt.read_bytes()
        for column, value in (
            ("received_at", (utc_now() - dt.timedelta(seconds=60)).isoformat()),
            ("profile_status", "UNVERIFIED"), ("mode", "LIMITED_AUTO"),
        ):
            with self.subTest(column=column):
                self.prepare_verified_profile_and_adapter()
                with self.database.transaction() as connection:
                    connection.execute("UPDATE heartbeats SET " + column + "=?", (value,))
                self.assertFalse(get_first_trade_enablement(self.config_path)["can_enable"])
                with self.assertRaises(BridgeError):
                    enable_first_trade(self.config_path, "ENABLE-FIRST-TRADE")
                self.assertEqual(self.state()["trade_protection_kind"], "SETUP_LOCK")
                self.assertEqual(self.local_halt.read_bytes(), before)

    def test_incident_during_confirmation_is_rechecked_and_preserved(self):
        self.prepare_verified_profile_and_adapter()
        incident_files = []

        def confirm(_prompt):
            self.core.halt_trading("TEST INCIDENT")
            incident_files.append(self.local_halt.read_bytes())
            return "ENABLE-FIRST-TRADE"

        with contextlib.redirect_stdout(io.StringIO()):
            result = run_first_trade_enablement(self.config_path, input_func=confirm)
        self.assertEqual(result, 2)
        self.assertEqual(self.state()["trade_protection_kind"], "INCIDENT_HALT")
        self.assertEqual(self.state()["halted"], "true")
        self.assertEqual(self.local_halt.read_bytes(), incident_files[0])

    def test_tampered_signature_and_unsynced_policy_block_enablement(self):
        self.prepare_verified_profile_and_adapter()
        profile_path = self.ready / "pythongo_profile.json"
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        profile["capabilities"]["memo_max_bytes"] += 1
        profile_path.write_text(json.dumps(profile), encoding="utf-8")
        self.assertIn("PROFILE_INVALID", {item["code"] for item in get_first_trade_enablement(self.config_path)["blockers"]})
        with self.assertRaises(BridgeError):
            enable_first_trade(self.config_path, "ENABLE-FIRST-TRADE")
        self.prepare_verified_profile_and_adapter()
        with self.database.transaction() as connection:
            payload = json.loads(connection.execute("SELECT payload_json FROM heartbeats").fetchone()[0])
            payload["margin_policy_hash"] = "OLD-POLICY"
            connection.execute("UPDATE heartbeats SET payload_json=?", (json.dumps(payload),))
        self.assertIn("ADAPTER_NOT_READY", {item["code"] for item in get_first_trade_enablement(self.config_path)["blockers"]})
        with self.assertRaises(BridgeError):
            enable_first_trade(self.config_path, "ENABLE-FIRST-TRADE")
        self.assertEqual(self.state()["trade_protection_kind"], "SETUP_LOCK")

    def test_unknown_order_result_requires_reconciliation(self):
        self.prepare_verified_profile_and_adapter()
        now = iso_now()
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO trade_intents(intent_id,preview_id,account_alias,status,action,exchange,instrument_id,"
                "requested_volume,execution_mode,created_at,updated_at,payload_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                ("TEST-INTENT", "TEST-PREVIEW", "main_futures", "SUBMIT_UNKNOWN", "OPEN_LONG", "TEST", "TEST", 1,
                 "OBSERVE_ONLY", now, now, "{}"),
            )
        self.assertIn("RECONCILIATION_REQUIRED", {
            item["code"] for item in get_first_trade_enablement(self.config_path)["blockers"]
        })
        with self.assertRaises(BridgeError):
            enable_first_trade(self.config_path, "ENABLE-FIRST-TRADE")
        self.assertEqual(self.state()["trade_protection_kind"], "SETUP_LOCK")

    def test_running_worker_status_still_offers_first_enablement(self):
        self.prepare_verified_profile_and_adapter()
        health = self.core.pythongo_health()
        probe = {"state": "RUNNING", "response": {"ok": True, "data": health}}
        with mock.patch("workbuddy_pythongo.desktop.probe_worker", return_value=probe), \
                mock.patch("workbuddy_pythongo.desktop._offer_first_trade_enablement") as offer, \
                mock.patch("workbuddy_pythongo.desktop.worker_main") as worker, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(start_desktop(self.config_path), 0)
        offer.assert_called_once_with(load_config(self.config_path).path, health)
        worker.assert_not_called()

    def test_local_file_failure_rolls_back_first_enablement(self):
        self.prepare_verified_profile_and_adapter()
        before = self.local_halt.read_bytes()
        with mock.patch("workbuddy_pythongo.console._sync_adapter_mode", side_effect=OSError("TEST FAILURE")):
            with self.assertRaises(OSError):
                enable_first_trade(self.config_path, "ENABLE-FIRST-TRADE")
        self.assertEqual(self.state()["trade_protection_kind"], "SETUP_LOCK")
        self.assertEqual(self.local_halt.read_bytes(), before)

    def test_stopped_menu_redirects_t_to_observe_start_instead_of_enablement(self):
        blockers = get_non_observe_mode_blockers(self.config_path)
        availability = {"OBSERVE_ONLY": [], "LIMITED_AUTO": blockers}
        answers = iter(["t", "4", "t", "1"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            selected = select_start_mode(availability, input_func=lambda _prompt: next(answers))
        self.assertEqual(selected, ("OBSERVE_ONLY", None))
        self.assertIn("先选择1启动观察模式", output.getvalue())
        self.assertIn("另开状态入口", output.getvalue())
        self.assertNotIn("输入T进入", output.getvalue())
        self.assertNotIn("T. ", output.getvalue())

    def test_stopped_start_only_runs_selected_worker_and_never_opens_enablement(self):
        with mock.patch("workbuddy_pythongo.desktop.probe_worker", return_value={"state": "STOPPED"}), \
                mock.patch("workbuddy_pythongo.desktop.select_start_mode", return_value=("OBSERVE_ONLY", None)), \
                mock.patch("workbuddy_pythongo.desktop.run_first_trade_enablement") as wizard, \
                mock.patch("workbuddy_pythongo.desktop.set_mode", return_value={}) as set_mode, \
                mock.patch("workbuddy_pythongo.desktop.worker_main", return_value=0) as worker, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(start_desktop(self.config_path), 0)
        wizard.assert_not_called()
        set_mode.assert_called_once_with(load_config(self.config_path).path, "OBSERVE_ONLY", None)
        worker.assert_called_once()
        self.assertEqual(self.state()["trade_protection_kind"], "SETUP_LOCK")

    def test_direct_wizard_requires_running_worker_even_with_fresh_stored_heartbeat(self):
        self.prepare_verified_profile_and_adapter()
        with mock.patch("workbuddy_pythongo.desktop.probe_worker", return_value={"state": "STOPPED"}), \
                mock.patch("workbuddy_pythongo.desktop.get_first_trade_enablement") as preflight, \
                contextlib.redirect_stdout(io.StringIO()):
            result = run_first_trade_enablement(self.config_path, input_func=lambda _prompt: self.fail("Unexpected confirmation"))
        self.assertEqual(result, 2)
        preflight.assert_not_called()
        self.assertEqual(self.state()["trade_protection_kind"], "SETUP_LOCK")

    def test_running_status_lists_unmet_conditions_without_offering_confirmation(self):
        self.prepare_verified_profile_and_adapter()
        with self.database.transaction() as connection:
            connection.execute("UPDATE heartbeats SET status='STOPPED'")
        output = io.StringIO()
        with contextlib.redirect_stdout(output), \
                mock.patch("workbuddy_pythongo.desktop.run_first_trade_enablement") as wizard:
            _offer_first_trade_enablement(self.config_path, self.core.pythongo_health(),
                                          input_func=lambda _prompt: self.fail("Unexpected confirmation"))
        self.assertIn("Adapter当前为STOPPED", output.getvalue())
        self.assertNotIn("保证金策略未同步", output.getvalue())
        self.assertNotIn("T. ", output.getvalue())
        wizard.assert_not_called()

    def test_ready_running_status_offers_explicit_confirmation(self):
        self.prepare_verified_profile_and_adapter()

        def answer(_prompt):
            return "t"

        output = io.StringIO()
        with contextlib.redirect_stdout(output), \
                mock.patch("workbuddy_pythongo.desktop.run_first_trade_enablement") as wizard:
            _offer_first_trade_enablement(self.config_path, self.core.pythongo_health(), input_func=answer)
        self.assertIn("T. 确认首次启用交易", output.getvalue())
        self.assertIn("前置检查已通过", output.getvalue())
        wizard.assert_called_once_with(self.config_path, answer)

    def test_stopped_status_does_not_offer_t_from_doctor_cached_health(self):
        self.prepare_verified_profile_and_adapter()
        health = self.core.pythongo_health()
        output = io.StringIO()
        with mock.patch("workbuddy_pythongo.desktop.probe_worker", return_value={"state": "STOPPED"}), \
                mock.patch("workbuddy_pythongo.desktop.run_doctor", return_value={"health": health}), \
                mock.patch("workbuddy_pythongo.desktop.print_doctor_human"), \
                mock.patch("workbuddy_pythongo.desktop.get_first_trade_enablement") as preflight, \
                contextlib.redirect_stdout(output):
            self.assertEqual(status_desktop(self.config_path), 1)
        preflight.assert_not_called()
        self.assertNotIn("T. ", output.getvalue())
        self.assertIn("先在启动入口选择1", output.getvalue())
