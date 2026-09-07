RUN_MODES = (
    "OBSERVE_ONLY",
    "SIM_SIGNAL",
    "MANUAL_LIVE",
    "LIMITED_AUTO",
)

LEGACY_MODE_MIGRATIONS = {
    "READ_ONLY": "OBSERVE_ONLY",
    "DRY_RUN": "OBSERVE_ONLY",
    "PAPER": "SIM_SIGNAL",
    "LIVE_APPROVAL": "MANUAL_LIVE",
    "LIVE_LIMITED_AUTO": "LIMITED_AUTO",
    "HALTED": "OBSERVE_ONLY",
}


def normalize_mode(value, allow_legacy=False):
    if value in RUN_MODES:
        return value
    if allow_legacy and value in LEGACY_MODE_MIGRATIONS:
        return LEGACY_MODE_MIGRATIONS[value]
    raise ValueError("invalid run mode")

