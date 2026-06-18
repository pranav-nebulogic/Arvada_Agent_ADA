"""Request-type registry: normalization, routing, deep-links — pure logic."""
from agent import request_types as rt
from agent.nodes.escalate import _route_department
from agent.state import AgentState


def _state(**kw) -> AgentState:
    base = dict(city_id="arvada-co", session_id="s1", query="q")
    base.update(kw)
    return AgentState(**base)


def test_exact_code_matches():
    assert rt.normalize("fence") == "fence"
    assert rt.normalize("right_of_way") == "right_of_way"
    assert rt.normalize("pz_variance") == "pz_variance"
    assert rt.normalize("class_p_plumbing") == "class_p_plumbing"


def test_natural_language_aliases():
    assert rt.normalize("privacy fence") == "fence"
    assert rt.normalize("right of way") == "right_of_way"
    assert rt.normalize("block party") == "block_party"
    assert rt.normalize("plumbing license") == "class_p_plumbing"
    assert rt.normalize("variance") == "pz_variance"
    assert rt.normalize("new home") == "new_single_family"
    assert rt.normalize("solar panels") == "solar"
    assert rt.normalize("airbnb") == "short_term_rental"
    assert rt.normalize("become a licensed contractor") == "contractor_license_municipal"


def test_case_and_separator_insensitive():
    assert rt.normalize("  Right-Of-Way ") == "right_of_way"
    assert rt.normalize("BLOCK   PARTY") == "block_party"


def test_unknown_returns_none():
    assert rt.normalize(None) is None
    assert rt.normalize("") is None
    assert rt.normalize("buy a hotdog") is None


def test_contact_bucket_only_returns_verified_buckets():
    # Every bucket must be one we have a verified phone number for.
    buckets = {rt.contact_bucket(k) for k in rt.REGISTRY}
    assert buckets <= {"building", "planning", "general"}


def test_contact_bucket_routing():
    assert rt.contact_bucket("fence") == "building"            # building permit
    assert rt.contact_bucket("right_of_way") == "general"      # engineering -> general (no verified #)
    assert rt.contact_bucket("site_disturbance") == "general"  # stormwater -> general
    assert rt.contact_bucket("pz_variance") == "planning"      # planning project
    assert rt.contact_bucket("class_a") == "building"          # contractor licensing -> Building
    assert rt.contact_bucket("special_event") == "planning"
    assert rt.contact_bucket("block_party") == "general"       # city manager -> general
    assert rt.contact_bucket(None) == "general"


def test_apply_url_citizen_paths():
    assert rt.apply_url("fence") == "/permits/apply/fence"
    assert rt.apply_url("pz_variance") == "/planning/apply/pz_variance"
    assert rt.apply_url("special_event") == "/events/apply/special_event"
    # Citizen contractor licensing has no apply-by-code route yet -> browse page.
    assert rt.apply_url("class_a") == "/contractors"
    assert rt.apply_url("nope") is None


def test_apply_url_is_surface_aware():
    # Staff land in the agent workbench intake, not the citizen apply flow.
    assert rt.apply_url("fence", "agent") == "/agent/intake"
    assert rt.apply_url("class_a", "agent") == "/agent/intake"
    assert rt.apply_url("fence", "citizen") == "/permits/apply/fence"


def test_surface_for():
    assert rt.surface_for("staff") == "agent"
    assert rt.surface_for("homeowner") == "citizen"
    assert rt.surface_for("contractor") == "citizen"
    assert rt.surface_for(None) == "citizen"


def test_apply_url_with_base():
    assert rt.apply_url("fence", "citizen", "https://x.test/") == "https://x.test/permits/apply/fence"
    assert rt.apply_url("fence", "agent", "https://x.test") == "https://x.test/agent/intake"


def test_registry_keys_unique():
    keys = [t.key for t in rt._TYPES]
    assert len(keys) == len(set(keys))


def test_escalate_routes_via_request_type():
    assert _route_department(_state(entities={"request_type": "right_of_way"})) == "general"
    assert _route_department(_state(entities={"request_type": "fence"})) == "building"
    assert _route_department(_state(entities={"request_type": "pz_variance"})) == "planning"
