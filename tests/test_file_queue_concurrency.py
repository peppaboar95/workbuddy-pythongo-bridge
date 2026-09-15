import concurrent.futures
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

from workbuddy_pythongo.bootstrap import initialize
from workbuddy_pythongo.errors import BridgeError
from workbuddy_pythongo.file_queue import FileQueue
from workbuddy_pythongo.security import KeyRing, make_envelope
from workbuddy_pythongo.util import iso_now
from workbuddy_pythongo.worker import build_runtime


class FileQueueConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.keyring = KeyRing({"test": b"x" * 32}, "test")
        self.queue = FileQueue(self.temporary.name, self.keyring)
        self.partition = Path(self.queue.ensure_partition("test_adapter"))

    def write(self, folder="events"):
        envelope = make_envelope(self.keyring, "HEARTBEAT", {"test": True}, 60, "test-adapter")
        return Path(self.queue.write("test_adapter", folder, envelope)), envelope

    def test_concurrent_threads_and_queue_instances_do_not_handle_or_archive_twice(self):
        for same_queue in (True, False):
            with self.subTest(same_queue=same_queue):
                path, envelope = self.write()
                contender = self.queue if same_queue else FileQueue(self.temporary.name, self.keyring)
                entered = threading.Event()
                release = threading.Event()
                handled = []

                def owner_handler(message):
                    handled.append(message["message_id"])
                    entered.set()
                    if not release.wait(10):
                        raise RuntimeError("test consumer was not released")

                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    owner = pool.submit(self.queue.consume, "test_adapter", "events", owner_handler)
                    try:
                        self.assertTrue(entered.wait(5))
                        result = contender.consume("test_adapter", "events", lambda _message: self.fail("Duplicate handler"))
                        self.assertEqual(result, {"processed": 0, "dead_lettered": 0})
                        self.assertTrue(path.exists())
                    finally:
                        release.set()
                    self.assertEqual(owner.result(timeout=5), {"processed": 1, "dead_lettered": 0})
                self.assertEqual(handled, [envelope["message_id"]])
                self.assertFalse(path.exists())
                self.assertEqual(len(list((self.partition / "archive").glob(path.name + ".processed*"))), 1)
                self.assertEqual(self.queue.depths("test_adapter")["dead_letter"], 0)

    def child_consume(self, *, crash=False):
        code = """
import json, os, sys
from workbuddy_pythongo.file_queue import FileQueue
from workbuddy_pythongo.security import KeyRing
queue = FileQueue(sys.argv[1], KeyRing({'test': b'x' * 32}, 'test'))
handled = []
def handler(message):
    if sys.argv[2] == 'crash':
        os._exit(7)
    handled.append(message['message_id'])
result = queue.consume('test_adapter', 'events', handler)
print(json.dumps({'result': result, 'handled': handled}))
"""
        return subprocess.run(
            [sys.executable, "-c", code, self.temporary.name, "crash" if crash else "normal"],
            capture_output=True, text=True, encoding="utf-8", timeout=15,
        )

    def test_another_process_skips_busy_folder_and_can_consume_after_release(self):
        path, _envelope = self.write()

        def handler(_message):
            child = self.child_consume()
            self.assertEqual(child.returncode, 0, child.stderr)
            data = json.loads(child.stdout)
            self.assertEqual(data, {"result": {"processed": 0, "dead_lettered": 0}, "handled": []})

        self.assertEqual(self.queue.consume("test_adapter", "events", handler)["processed"], 1)
        self.assertFalse(path.exists())
        next_path, next_envelope = self.write()
        child = self.child_consume()
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(json.loads(child.stdout), {
            "result": {"processed": 1, "dead_lettered": 0}, "handled": [next_envelope["message_id"]],
        })
        self.assertFalse(next_path.exists())

    def test_crashed_consumer_releases_lock_and_leaves_message_for_retry(self):
        path, envelope = self.write()
        child = self.child_consume(crash=True)
        self.assertEqual(child.returncode, 7, child.stderr)
        self.assertTrue(path.exists())
        handled = []
        result = self.queue.consume("test_adapter", "events", lambda message: handled.append(message["message_id"]))
        self.assertEqual(result, {"processed": 1, "dead_lettered": 0})
        self.assertEqual(handled, [envelope["message_id"]])

    def test_archive_failure_is_reported_and_ingester_retry_does_not_duplicate_event(self):
        initialized = initialize(self.temporary.name)
        _config, database, core = build_runtime(initialized["config"])
        now = iso_now()
        payload = {
            "snapshot_id": "TEST-SNAPSHOT", "account_alias": "main_futures", "captured_at": now,
            "account": {"equity": 100000},
        }
        envelope = make_envelope(core.keyring, "ACCOUNT_SNAPSHOT", payload, 60, "test-adapter")
        path = Path(core.file_queue.write("pythongo_futures_01", "events", envelope))
        with mock.patch("workbuddy_pythongo.file_queue.os.replace", side_effect=PermissionError("TEST ARCHIVE FAILURE")):
            with self.assertRaises(PermissionError):
                core.ingester.scan_once()
        self.assertTrue(path.exists())
        self.assertEqual(core.ingester.scan_once()["main_futures"]["events"]["processed"], 1)
        with database.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM inbound_events").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM account_snapshots").fetchone()[0], 1)
        self.assertFalse(path.exists())

    def test_invalid_and_rejected_messages_are_quarantined(self):
        invalid, _envelope = self.write()
        invalid.write_text("invalid json", encoding="utf-8")
        rejected, _envelope = self.write()

        def reject(_message):
            raise BridgeError("ACCOUNT_BINDING_MISMATCH", "test rejection")

        self.assertEqual(self.queue.consume("test_adapter", "events", reject), {"processed": 0, "dead_lettered": 2})
        self.assertTrue((self.partition / "dead_letter" / (invalid.name + ".invalid")).exists())
        self.assertTrue((self.partition / "dead_letter" / (rejected.name + ".rejected")).exists())
        self.assertEqual(self.queue.depths("test_adapter")["dead_letter"], 2)

    def test_unrelated_lock_io_errors_are_reported(self):
        path, _envelope = self.write()
        with mock.patch("workbuddy_pythongo.file_queue.open", side_effect=PermissionError("TEST LOCK FILE FAILURE")):
            with self.assertRaises(PermissionError):
                self.queue.consume("test_adapter", "events", lambda _message: self.fail("Unexpected handler"))
        self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
