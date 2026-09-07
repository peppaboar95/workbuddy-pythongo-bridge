import math
from decimal import Decimal, InvalidOperation

from .close_split import ALL_ACTIONS, split_order
from .errors import BridgeError, ValidationError
from .modes import RUN_MODES
from .util import hash_json, normalize_instrument, parse_time, utc_now


REQUEST_FIELDS = {
    "account_alias", "instrument", "action", "sizing", "price_policy",
    "close_policy", "hedge_flag", "execution_mode", "source",
}


def _strict_object(value, allowed, required, name):
    if not isinstance(value, dict):
        raise ValidationError("%s must be an object" % name)
    unknown = set(value) - set(allowed)
    missing = set(required) - set(value)
    if unknown or missing:
        raise ValidationError("%s has invalid fields" % name, {"unknown": sorted(unknown), "missing": sorted(missing)})


def validate_trade_request(request):
    _strict_object(request, REQUEST_FIELDS, REQUEST_FIELDS - {"close_policy"}, "trade_request")
    normalized = dict(request)
    if not isinstance(normalized["account_alias"], str) or not normalized["account_alias"].strip():
        raise ValidationError("account_alias must be non-empty")
    _strict_object(normalized["instrument"], {"exchange", "instrument_id"}, {"exchange", "instrument_id"}, "instrument")
    try:
        exchange, instrument_id = normalize_instrument(
            normalized["instrument"]["exchange"], normalized["instrument"]["instrument_id"]
        )
    except ValueError as exc:
        raise ValidationError(str(exc))
    normalized["instrument"] = {"exchange": exchange, "instrument_id": instrument_id}
    if normalized["action"] not in ALL_ACTIONS:
        raise ValidationError("unsupported futures action")
    _strict_object(normalized["sizing"], {"type", "value"}, {"type", "value"}, "sizing")
    if normalized["sizing"]["type"] != "FIXED_VOLUME":
        raise ValidationError("only FIXED_VOLUME is supported in v1")
    volume = normalized["sizing"]["value"]
    if isinstance(volume, bool) or not isinstance(volume, int) or volume <= 0:
        raise ValidationError("sizing.value must be a positive integer")
    _strict_object(
        normalized["price_policy"],
        {"type", "limit_price", "max_deviation_pct"},
        {"type", "limit_price"},
        "price_policy",
    )
    if normalized["price_policy"]["type"] != "FIXED_LIMIT":
        raise ValidationError("only FIXED_LIMIT is supported in v1")
    price = normalized["price_policy"]["limit_price"]
    if isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(float(price)) or price <= 0:
        raise ValidationError("limit_price must be a positive finite number")
    deviation = normalized["price_policy"].get("max_deviation_pct", 0.02)
    if isinstance(deviation, bool) or not isinstance(deviation, (int, float)) or not 0 <= float(deviation) <= 1:
        raise ValidationError("max_deviation_pct must be between 0 and 1")
    normalized["price_policy"]["max_deviation_pct"] = float(deviation)
    if normalized.get("close_policy") is not None and normalized["close_policy"] not in {"TODAY_FIRST", "YESTERDAY_FIRST", "EXPLICIT_ONLY"}:
        raise ValidationError("invalid close_policy")
    if normalized["hedge_flag"] != "SPECULATION":
        raise ValidationError("only SPECULATION is supported in v1")
    if normalized["execution_mode"] not in RUN_MODES:
        raise ValidationError("invalid execution_mode")
    _strict_object(normalized["source"], {"type", "signal_id", "rule_set_id", "rule_version"}, {"type", "signal_id"}, "source")
    if not all(isinstance(normalized["source"].get(name), str) and normalized["source"][name].strip() for name in ("type", "signal_id")):
        raise ValidationError("source.type and source.signal_id must be non-empty")
    for optional in ("rule_set_id", "rule_version"):
        if optional in normalized["source"] and (not isinstance(normalized["source"][optional], str) or not normalized["source"][optional].strip()):
            raise ValidationError("source.%s must be non-empty when supplied" % optional)
    return normalized


def age_seconds(timestamp):
    return max(0.0, (utc_now() - parse_time(timestamp)).total_seconds())


def _tick_aligned(price, tick):
    try:
        price_value = Decimal(str(price))
        tick_value = Decimal(str(tick))
        if tick_value <= 0:
            return False
        return price_value % tick_value == 0
    except (InvalidOperation, ValueError):
        return False


def build_preview(request, account_config, account_snapshot, position, quote, active_orders, daily_counts):
    request = validate_trade_request(request)
    exchange = request["instrument"]["exchange"]
    instrument_id = request["instrument"]["instrument_id"]
    limits = account_config.risk_limits
    reasons = []
    if not account_config.instrument_allowed(exchange, instrument_id):
        reasons.append("INSTRUMENT_NOT_ALLOWED")
    if not account_snapshot:
        reasons.append("ACCOUNT_SNAPSHOT_MISSING")
    if not position:
        reasons.append("POSITION_SNAPSHOT_MISSING")
    if not quote:
        reasons.append("QUOTE_MISSING")
    if account_snapshot and age_seconds(account_snapshot["captured_at"]) > limits.max_snapshot_age_seconds:
        reasons.append("ACCOUNT_SNAPSHOT_STALE")
    if position and age_seconds(position["captured_at"]) > limits.max_snapshot_age_seconds:
        reasons.append("POSITION_STALE")
    if quote and age_seconds(quote["captured_at"]) > limits.max_quote_age_seconds:
        reasons.append("QUOTE_STALE")
    price = float(request["price_policy"]["limit_price"])
    volume = int(request["sizing"]["value"])
    if volume > limits.max_order_volume:
        reasons.append("MAX_ORDER_VOLUME")
    quote_payload = (quote or {}).get("payload", {})
    tick = quote_payload.get("price_tick")
    multiplier = quote_payload.get("volume_multiple")
    if not isinstance(tick, (int, float)) or isinstance(tick, bool) or tick <= 0:
        reasons.append("PRICE_TICK_MISSING")
    elif not _tick_aligned(price, tick):
        reasons.append("PRICE_TICK_MISALIGNED")
    if not isinstance(multiplier, (int, float)) or isinstance(multiplier, bool) or multiplier <= 0:
        reasons.append("VOLUME_MULTIPLE_MISSING")
        multiplier = 0
    lower = quote_payload.get("lower_limit_price")
    upper = quote_payload.get("upper_limit_price")
    if not isinstance(lower, (int, float)) or not isinstance(upper, (int, float)) or lower <= 0 or upper <= 0:
        reasons.append("DAILY_LIMIT_MISSING")
    elif not float(lower) <= price <= float(upper):
        reasons.append("PRICE_OUTSIDE_DAILY_LIMIT")
    last_price = quote_payload.get("last_price")
    max_deviation = request["price_policy"]["max_deviation_pct"]
    if not isinstance(last_price, (int, float)) or last_price <= 0:
        reasons.append("REFERENCE_PRICE_MISSING")
    elif abs(price - float(last_price)) / float(last_price) > max_deviation:
        reasons.append("PRICE_DEVIATION_EXCEEDED")
    notional = price * volume * float(multiplier)
    if notional > limits.max_order_notional:
        reasons.append("MAX_ORDER_NOTIONAL")
    account_payload = (account_snapshot or {}).get("payload", {})
    risk_ratio = account_payload.get("risk")
    if not isinstance(risk_ratio, (int, float)) or isinstance(risk_ratio, bool):
        reasons.append("RISK_RATIO_MISSING")
    elif risk_ratio > limits.max_risk_ratio:
        reasons.append("MAX_RISK_RATIO")
    current_margin = account_payload.get("margin")
    if not isinstance(current_margin, (int, float)) or isinstance(current_margin, bool):
        reasons.append("MARGIN_DATA_MISSING")
    elif current_margin > limits.max_total_margin:
        reasons.append("MAX_TOTAL_MARGIN")
    margin_ratio = quote_payload.get("margin_ratio")
    margin_per_lot = quote_payload.get("margin_per_lot")
    margin_estimate = None
    if request["action"].startswith("OPEN_"):
        if (
            isinstance(margin_per_lot, (int, float)) and
            not isinstance(margin_per_lot, bool) and
            math.isfinite(float(margin_per_lot)) and margin_per_lot > 0
        ):
            margin_estimate = volume * float(margin_per_lot)
        elif (
            isinstance(margin_ratio, (int, float)) and
            not isinstance(margin_ratio, bool) and
            math.isfinite(float(margin_ratio)) and margin_ratio > 0
        ):
            margin_estimate = notional * float(margin_ratio)
        else:
            reasons.append("MARGIN_RATIO_MISSING")
        if margin_estimate is not None:
            if margin_estimate > limits.max_margin_per_order:
                reasons.append("MAX_MARGIN_PER_ORDER")
            available = account_payload.get("available")
            if not isinstance(available, (int, float)) or available < margin_estimate:
                reasons.append("AVAILABLE_FUNDS_INSUFFICIENT")
            if isinstance(current_margin, (int, float)) and current_margin + margin_estimate > limits.max_total_margin:
                reasons.append("MAX_TOTAL_MARGIN")
    if daily_counts.get("orders", 0) >= limits.max_daily_orders:
        reasons.append("MAX_DAILY_ORDERS")
    if daily_counts.get("cancels", 0) >= limits.max_daily_cancels:
        reasons.append("MAX_DAILY_CANCELS")
    pnl = float(account_payload.get("close_profit") or 0) + float(account_payload.get("position_profit") or 0)
    if pnl < -limits.max_daily_loss:
        reasons.append("MAX_DAILY_LOSS")
    current_total = int((position or {}).get("payload", {}).get("position") or 0)
    all_position_volume = int(daily_counts.get("total_position_volume") or current_total)
    if request["action"].startswith("OPEN_"):
        if current_total + volume > limits.max_position_volume_per_instrument:
            reasons.append("MAX_INSTRUMENT_POSITION")
        if all_position_volume + volume > limits.max_total_position_volume:
            reasons.append("MAX_TOTAL_POSITION")
    close_policy = request.get("close_policy") or account_config.close_policy
    try:
        children = split_order(request["action"], volume, (position or {}).get("payload"), close_policy)
    except BridgeError as exc:
        children = []
        reasons.append(exc.code)
    if active_orders:
        reasons.append("ACTIVE_ORDER_CONFLICT")
    unique_reasons = sorted(set(reasons))
    position_payload = (position or {}).get("payload", {})
    position_risk = {
        "position": position_payload.get("position"),
        "long": {name: (position_payload.get("long") or {}).get(name) for name in (
            "position", "frozen_closing", "td_close_available", "yd_close_available",
            "td_frozen_closing", "yd_frozen_closing",
        )},
        "short": {name: (position_payload.get("short") or {}).get(name) for name in (
            "position", "frozen_closing", "td_close_available", "yd_close_available",
            "td_frozen_closing", "yd_frozen_closing",
        )},
    }
    material = {
        "account_alias": account_config.alias,
        "instrument": request["instrument"],
        "action": request["action"],
        "execution_mode": request["execution_mode"],
        "resolved_price": price,
        "children": children,
        "available": account_payload.get("available"),
        "margin": account_payload.get("margin"),
        "frozen_margin": account_payload.get("frozen_margin"),
        "risk": account_payload.get("risk"),
        "pnl": pnl,
        "target_position": position_risk,
        "quote_guard": {
            "price_tick": tick,
            "volume_multiple": multiplier,
            "margin_ratio": margin_ratio,
            "margin_per_lot": margin_per_lot,
            "margin_ratio_source": quote_payload.get("margin_ratio_source"),
            "margin_ratio_source_updated_at": quote_payload.get("margin_ratio_source_updated_at"),
            "margin_ratio_source_age_hours": quote_payload.get("margin_ratio_source_age_hours"),
            "margin_ratio_source_warning": quote_payload.get("margin_ratio_source_warning"),
            "margin_ratio_raw": quote_payload.get("margin_ratio_raw"),
            "margin_per_lot_raw": quote_payload.get("margin_per_lot_raw"),
            "margin_ratio_safety_multiplier": quote_payload.get("margin_ratio_safety_multiplier"),
            "margin_ratio_reference_sha256": quote_payload.get("margin_ratio_reference_sha256"),
            "margin_ratio_reference_error": quote_payload.get("margin_ratio_reference_error"),
            "tick_aligned": bool(isinstance(tick, (int, float)) and tick > 0 and _tick_aligned(price, tick)),
            "within_daily_limit": bool(isinstance(lower, (int, float)) and isinstance(upper, (int, float)) and lower <= price <= upper),
            "within_max_deviation": bool(isinstance(last_price, (int, float)) and last_price > 0 and abs(price - float(last_price)) / float(last_price) <= max_deviation),
        },
        "active_order_ids": sorted(str(item.get("pythongo_order_id")) for item in active_orders),
        "daily_counts": daily_counts,
        "risk_reasons": unique_reasons,
    }
    return {
        "account_alias": account_config.alias,
        "instrument": request["instrument"],
        "action": request["action"],
        "execution_mode": request["execution_mode"],
        "requested_volume": volume,
        "resolved_limit_price": price,
        "close_policy": close_policy,
        "children": children,
        "notional": notional,
        "margin_estimate": margin_estimate,
        "risk": {"allowed": not unique_reasons, "reasons": unique_reasons},
        "decision_material": material,
        "decision_fingerprint": hash_json(material),
    }
