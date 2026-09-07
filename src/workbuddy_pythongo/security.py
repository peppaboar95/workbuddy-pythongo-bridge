import base64
import hashlib
import hmac
import json

from .errors import BridgeError
from .util import canonical_json, new_id, parse_time, utc_now


class KeyRing:
    def __init__(self, keys, active_key_id):
        self.keys = dict(keys)
        self.active_key_id = active_key_id
        if active_key_id not in self.keys:
            raise BridgeError("CONFIG_ERROR", "active message key is missing")
        if any(len(value) < 32 for value in self.keys.values()):
            raise BridgeError("CONFIG_ERROR", "message keys must be at least 256 bits")

    @classmethod
    def load(cls, path):
        try:
            with open(path, "r", encoding="utf-8") as stream:
                raw = json.load(stream)
            if not isinstance(raw, dict) or set(raw) != {"active_key_id", "keys"}:
                raise ValueError("invalid keyring fields")
            keys = {
                key_id: base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
                for key_id, value in raw["keys"].items()
            }
            return cls(keys, raw["active_key_id"])
        except BridgeError:
            raise
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise BridgeError("CONFIG_ERROR", "cannot load message keys: %s" % exc)

    def sign(self, message):
        unsigned = dict(message)
        unsigned.pop("signature", None)
        key_id = unsigned.get("key_id", self.active_key_id)
        key = self.keys.get(key_id)
        if not key:
            raise BridgeError("SIGNATURE_INVALID", "unknown key_id")
        digest = hmac.new(key, canonical_json(unsigned), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

    def verify(self, message):
        supplied = message.get("signature") if isinstance(message, dict) else None
        if not isinstance(supplied, str) or not hmac.compare_digest(self.sign(message), supplied):
            raise BridgeError("SIGNATURE_INVALID", "message signature does not match")
        return True

    def fingerprint_investor(self, investor_id):
        if not isinstance(investor_id, str) or not investor_id.strip():
            raise BridgeError("INVALID_REQUEST", "investor_id must be non-empty")
        key = self.keys[self.active_key_id]
        material = b"pythongo-investor-v1\0" + investor_id.strip().encode("utf-8")
        return "hmac-sha256:v1:" + hmac.new(key, material, hashlib.sha256).hexdigest()


def make_envelope(keyring, message_type, payload, ttl_seconds, sender, correlation_id=None):
    import datetime as dt

    issued = utc_now()
    envelope = {
        "protocol_version": "1.0",
        "message_id": new_id("msg"),
        "correlation_id": correlation_id or new_id("corr"),
        "message_type": message_type,
        "issued_at": issued.isoformat(timespec="milliseconds"),
        "expires_at": (issued + dt.timedelta(seconds=int(ttl_seconds))).isoformat(timespec="milliseconds"),
        "sender": sender,
        "key_id": keyring.active_key_id,
        "payload": payload,
    }
    envelope["signature"] = keyring.sign(envelope)
    return envelope


def validate_envelope(envelope, keyring, expected_types=None, max_clock_skew_seconds=5):
    required = {
        "protocol_version", "message_id", "correlation_id", "message_type",
        "issued_at", "expires_at", "sender", "key_id", "payload", "signature",
    }
    if not isinstance(envelope, dict) or set(envelope) != required:
        raise BridgeError("MESSAGE_SCHEMA_INVALID", "invalid envelope fields")
    if envelope["protocol_version"] != "1.0":
        raise BridgeError("MESSAGE_SCHEMA_INVALID", "unsupported protocol version")
    if expected_types and envelope["message_type"] not in set(expected_types):
        raise BridgeError("MESSAGE_SCHEMA_INVALID", "unexpected message type")
    for name in ("message_id", "correlation_id", "message_type", "sender", "key_id"):
        if not isinstance(envelope[name], str) or not envelope[name]:
            raise BridgeError("MESSAGE_SCHEMA_INVALID", "%s must be non-empty" % name)
    keyring.verify(envelope)
    try:
        issued = parse_time(envelope["issued_at"])
        expires = parse_time(envelope["expires_at"])
    except ValueError as exc:
        raise BridgeError("MESSAGE_SCHEMA_INVALID", str(exc))
    now = utc_now()
    if expires <= now:
        raise BridgeError("MESSAGE_EXPIRED", "message has expired")
    if issued.timestamp() > now.timestamp() + max_clock_skew_seconds:
        raise BridgeError("CLOCK_SKEW", "message issued_at is in the future")
    if expires <= issued:
        raise BridgeError("MESSAGE_SCHEMA_INVALID", "expires_at must follow issued_at")
    return envelope

