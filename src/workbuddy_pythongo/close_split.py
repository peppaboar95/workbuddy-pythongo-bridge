from .errors import BridgeError


OPEN_ACTIONS = {"OPEN_LONG", "OPEN_SHORT"}
CLOSE_ACTIONS = {
    "CLOSE_LONG", "CLOSE_SHORT",
    "CLOSE_TODAY_LONG", "CLOSE_TODAY_SHORT",
    "CLOSE_YESTERDAY_LONG", "CLOSE_YESTERDAY_SHORT",
}
ALL_ACTIONS = OPEN_ACTIONS | CLOSE_ACTIONS


def _side(action):
    return "long" if action.endswith("LONG") else "short"


def _direction(action):
    if action in {"OPEN_LONG", "CLOSE_SHORT", "CLOSE_TODAY_SHORT", "CLOSE_YESTERDAY_SHORT"}:
        return "BUY"
    return "SELL"


def split_order(action, volume, position, close_policy):
    if action not in ALL_ACTIONS:
        raise BridgeError("INVALID_REQUEST", "unsupported futures action")
    if isinstance(volume, bool) or not isinstance(volume, int) or volume <= 0:
        raise BridgeError("INVALID_REQUEST", "volume must be a positive integer")
    if action in OPEN_ACTIONS:
        return [{
            "action": action,
            "direction": _direction(action),
            "offset": "OPEN",
            "volume": volume,
        }]
    position = position or {}
    side = position.get(_side(action)) or {}
    td_available = int(side.get("td_close_available") or 0)
    yd_available = int(side.get("yd_close_available") or 0)
    if action.startswith("CLOSE_TODAY_"):
        if volume > td_available:
            raise BridgeError("POSITION_INSUFFICIENT", "today close volume exceeds available position")
        return [{"action": action, "direction": _direction(action), "offset": "CLOSE_TODAY", "volume": volume}]
    if action.startswith("CLOSE_YESTERDAY_"):
        if volume > yd_available:
            raise BridgeError("POSITION_INSUFFICIENT", "yesterday close volume exceeds available position")
        return [{"action": action, "direction": _direction(action), "offset": "CLOSE_YESTERDAY", "volume": volume}]
    if volume > td_available + yd_available:
        raise BridgeError("POSITION_INSUFFICIENT", "close volume exceeds available position")
    if close_policy == "EXPLICIT_ONLY":
        raise BridgeError("EXPLICIT_OFFSET_REQUIRED", "generic close is disabled for this account")
    order = (
        (("CLOSE_TODAY", td_available), ("CLOSE_YESTERDAY", yd_available))
        if close_policy == "TODAY_FIRST"
        else (("CLOSE_YESTERDAY", yd_available), ("CLOSE_TODAY", td_available))
    )
    remaining = volume
    children = []
    for offset, available in order:
        take = min(remaining, available)
        if take:
            suffix = "LONG" if action == "CLOSE_LONG" else "SHORT"
            children.append({
                "action": "%s_%s" % (offset, suffix),
                "direction": _direction(action),
                "offset": offset,
                "volume": take,
            })
            remaining -= take
        if remaining == 0:
            break
    return children

