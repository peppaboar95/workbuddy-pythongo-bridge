import datetime as dt

from .errors import BridgeError
from .util import hash_json, iso_now, json_text, parse_time, utc_now


class SignalEngine:
    """Persist versioned, deterministic signals with TTL and cooldown guards."""

    def __init__(self, database):
        self.database = database

    @staticmethod
    def moving_average_cross(bars, fast, slow):
        if isinstance(fast, bool) or isinstance(slow, bool) or not isinstance(fast, int) or not isinstance(slow, int) or fast < 1 or slow <= fast:
            raise BridgeError("INVALID_RULE", "moving-average windows must satisfy 1 <= fast < slow")
        if not isinstance(bars, list) or len(bars) < slow + 1:
            return None
        closes = []
        for bar in bars[-(slow + 1):]:
            value = bar.get("close") if isinstance(bar, dict) else None
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise BridgeError("INVALID_RULE_INPUT", "each bar must have a numeric close")
            closes.append(float(value))
        previous_fast = sum(closes[-fast - 1:-1]) / fast
        previous_slow = sum(closes[-slow - 1:-1]) / slow
        current_fast = sum(closes[-fast:]) / fast
        current_slow = sum(closes[-slow:]) / slow
        if previous_fast <= previous_slow and current_fast > current_slow:
            return "CROSS_UP"
        if previous_fast >= previous_slow and current_fast < current_slow:
            return "CROSS_DOWN"
        return None

    def persist(self, account_alias, exchange, instrument_id, rule_set_id, rule_version, signal_type, evidence, ttl_seconds=300, cooldown_seconds=60):
        if not all(isinstance(value, str) and value.strip() for value in (account_alias, exchange, instrument_id, rule_set_id, rule_version, signal_type)):
            raise BridgeError("INVALID_SIGNAL", "signal identity fields must be non-empty")
        if isinstance(ttl_seconds, bool) or isinstance(cooldown_seconds, bool) or not isinstance(ttl_seconds, int) or not isinstance(cooldown_seconds, int) or ttl_seconds < 1 or cooldown_seconds < 0:
            raise BridgeError("INVALID_SIGNAL", "invalid TTL or cooldown")
        now = utc_now()
        material = {
            "account_alias": account_alias,
            "exchange": exchange.upper(),
            "instrument_id": instrument_id,
            "rule_set_id": rule_set_id,
            "rule_version": rule_version,
            "signal_type": signal_type,
            "evidence": evidence,
        }
        signal_id = "sig_" + hash_json(material)[:32]
        payload = dict(material)
        payload.update({
            "signal_id": signal_id,
            "source_type": "DETERMINISTIC_RULE",
            "created_at": iso_now(),
            "expires_at": (now + dt.timedelta(seconds=ttl_seconds)).isoformat(timespec="milliseconds"),
        })
        with self.database.transaction(immediate=True) as connection:
            duplicate = connection.execute("SELECT payload_json FROM market_signals WHERE signal_id=?", (signal_id,)).fetchone()
            if duplicate:
                return {"created": False, "reason": "DUPLICATE", "signal_id": signal_id}
            recent = connection.execute(
                "SELECT payload_json FROM market_signals WHERE source_type='DETERMINISTIC_RULE' ORDER BY received_at DESC LIMIT 500"
            ).fetchall()
            for row in recent:
                import json
                prior = json.loads(row["payload_json"])
                if (
                    prior.get("account_alias") == account_alias
                    and prior.get("exchange") == exchange.upper()
                    and prior.get("instrument_id") == instrument_id
                    and prior.get("rule_set_id") == rule_set_id
                    and prior.get("signal_type") == signal_type
                ):
                    try:
                        age = (now - parse_time(prior["created_at"])).total_seconds()
                    except Exception:
                        age = 0
                    if age < cooldown_seconds:
                        return {"created": False, "reason": "COOLDOWN", "signal_id": prior.get("signal_id")}
                    break
            connection.execute(
                "INSERT INTO market_signals(signal_id,source_type,received_at,payload_json) VALUES(?,?,?,?)",
                (signal_id, "DETERMINISTIC_RULE", iso_now(), json_text(payload)),
            )
        return {"created": True, "signal_id": signal_id, "expires_at": payload["expires_at"]}
