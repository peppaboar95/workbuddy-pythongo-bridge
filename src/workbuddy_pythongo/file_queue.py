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
                    name.endswith(".json") and os.path.isfile(os.path.join(directory, name))
                    for name in os.listdir(directory)
                )
        return result
