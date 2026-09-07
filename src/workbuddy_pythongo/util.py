import datetime as dt
import hashlib
import json
import os
import secrets
import tempfile
import uuid


UTC = dt.timezone.utc


def utc_now():
    return dt.datetime.now(UTC)


def iso_now():
    return utc_now().isoformat(timespec="milliseconds")


def parse_time(value):
    if not isinstance(value, str) or not value:
        raise ValueError("timestamp must be a non-empty string")
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(UTC)


def canonical_json(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def strict_json_loads(value):
    def reject(constant):
        raise ValueError("non-finite JSON number is forbidden: %s" % constant)

    return json.loads(value, parse_constant=reject)


def json_text(value):
    return canonical_json(value).decode("utf-8")


def hash_json(value):
    return hashlib.sha256(canonical_json(value)).hexdigest()


def new_id(prefix):
    return "%s_%s" % (prefix, uuid.uuid4().hex)


def new_client_order_key():
    return "wb_" + secrets.token_urlsafe(18).replace("-", "A").replace("_", "B")


def memo_token(intent_id, child_no):
    digest = hashlib.sha256((str(intent_id) + ":" + str(child_no)).encode("utf-8")).hexdigest()
    return "WB%s%02d" % (digest[:12].upper(), int(child_no))


def atomic_write_bytes(path, data):
    path = os.path.abspath(path)
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def atomic_write_json(path, value):
    atomic_write_bytes(path, canonical_json(value))


def normalize_instrument(exchange, instrument_id):
    if not isinstance(exchange, str) or not exchange.strip():
        raise ValueError("exchange must be a non-empty string")
    if not isinstance(instrument_id, str) or not instrument_id.strip():
        raise ValueError("instrument_id must be a non-empty string")
    return exchange.strip().upper(), instrument_id.strip()


def safe_relative_name(value):
    return (
        isinstance(value, str)
        and bool(value)
        and value not in {".", ".."}
        and all(c.isalnum() or c in "_.-" for c in value)
    )
