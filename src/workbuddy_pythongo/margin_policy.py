from .util import hash_json


MARGIN_POLICY_MATERIAL_FIELDS = (
    "margin_reference_file",
    "margin_reference_schema_version",
    "margin_reference_refresh_max_age_hours",
    "margin_reference_safety_multiplier",
    "margin_reference_require_signature",
)

def margin_policy_material(adapter):
    return {name: adapter[name] for name in MARGIN_POLICY_MATERIAL_FIELDS}


def margin_policy_hash(adapter):
    return hash_json(margin_policy_material(adapter))


def bind_margin_policy(adapter, generation):
    bound = dict(adapter)
    bound["margin_reference_policy_generation"] = int(generation)
    bound["margin_reference_policy_hash"] = margin_policy_hash(bound)
    return bound
