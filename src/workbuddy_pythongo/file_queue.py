import contextlib
import errno
import json
import os
import time

from .errors import BridgeError
from .security import validate_envelope
from .util import atomic_write_bytes, canonical_json


QUEUE_FOLDERS = (
    "commands", "command_acks", "events", "control", "control_acks",
    "archive", "dead_letter",
)


class FileQueue:
    def __init__(self, data_dir, keyring, max_message_bytes=65536):
        self.root = os.path.join(os.path.abspath(data_dir), "queue")
        self.keyring = keyring
        self.max_message_bytes = int(max_message_bytes)

    def ensure_partition(self, adapter_instance):
        base = os.path.join(self.root, adapter_instance)
        for folder in QUEUE_FOLDERS:
            os.makedirs(os.path.join(base, folder), exist_ok=True)
        return base

    def write(self, adapter_instance, folder, envelope):
        if folder not in QUEUE_FOLDERS:
            raise ValueError("invalid queue folder")
        encoded = canonical_json(envelope)
        if len(encoded) > self.max_message_bytes:
            raise BridgeError("MESSAGE_TOO_LARGE", "queue message exceeds size limit")
        safe_id = "".join(c for c in envelope["message_id"] if c.isalnum() or c in "_-")
        name = "%020d_%s.json" % (time.time_ns(), safe_id)
        path = os.path.join(self.ensure_partition(adapter_instance), folder, name)
        atomic_write_bytes(path, encoded)
        return path

    def _move(self, source, adapter_instance, destination, suffix=""):
        target_dir = os.path.join(self.ensure_partition(adapter_instance), destination)
        name = os.path.basename(source) + suffix
        target = os.path.join(target_dir, name)
        if os.path.exists(target):
            target += ".%d" % time.time_ns()
        os.replace(source, target)
        return target

    def consume(self, adapter_instance, folder, handler, expected_types=None, limit=100):
        if folder not in QUEUE_FOLDERS:
            raise ValueError("invalid queue folder")
        with self._consumer_lock(adapter_instance, folder) as acquired:
            if not acquired:
                return {"processed": 0, "dead_lettered": 0}
            return self._consume_locked(adapter_instance, folder, handler, expected_types, limit)

    @contextlib.contextmanager
    def _consumer_lock(self, adapter_instance, folder):
        partition = self.ensure_partition(adapter_instance)
        # Hold a per-folder OS lock from listing through handler commit and
        # archive. Concurrent scans retry on their next pass; closing the file
        # also releases ownership if the consumer process exits unexpectedly.
        with open(os.path.join(partition, ".consume-%s.lock" % folder), "a+b") as lock_file:
            if os.name == "nt":
                import msvcrt

                def lock():
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)

                def unlock():
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                def lock():
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

                def unlock():
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.seek(0)
            try:
                lock()
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise
                yield False
                return
            try:
                yield True
            finally:
                unlock()

    def _consume_locked(self, adapter_instance, folder, handler, expected_types, limit):
        directory = os.path.join(self.ensure_partition(adapter_instance), folder)
        processed = 0
        dead = 0
        for name in sorted(os.listdir(directory)):
            if processed + dead >= limit:
                break
            if not name.endswith(".json"):
                continue
            path = os.path.join(directory, name)
            try:
                if os.path.getsize(path) > self.max_message_bytes:
                    raise BridgeError("MESSAGE_TOO_LARGE", "queue file exceeds size limit")
                with open(path, "rb") as stream:
                    envelope = json.loads(stream.read().decode("utf-8"))
                validate_envelope(envelope, self.keyring, expected_types)
            except Exception:
                self._move(path, adapter_instance, "dead_letter", ".invalid")
                dead += 1
                continue
            try:
                handler(envelope)
                self._move(path, adapter_instance, "archive", ".processed")
                processed += 1
            except BridgeError:
                self._move(path, adapter_instance, "dead_letter", ".rejected")
                dead += 1
            except Exception:
                raise
        return {"processed": processed, "dead_lettered": dead}

    def depths(self, adapter_instance):
        base = self.ensure_partition(adapter_instance)
        result = {}
        for folder in QUEUE_FOLDERS:
            if folder == "archive":
                continue
            directory = os.path.join(base, folder)
            if folder == "dead_letter":
                result[folder] = sum(
                    os.path.isfile(os.path.join(directory, name))
                    for name in os.listdir(directory)
                )
            else:
                result[folder] = sum(
                    (name.endswith(".json") or ".json.processing-" in name)
                    and os.path.isfile(os.path.join(directory, name))
                    for name in os.listdir(directory)
                )
        return result
