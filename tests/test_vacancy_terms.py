"""tools/vacantes_remotas.py searches what the config says, or what the brain
derives from the candidate's profile — never a hard-coded developer list."""

import json

import pytest

from harvey import vacancy_terms as vt

PROFILE = {
    "product": {"name": "Ana Ejemplo", "description": "Video editor and motion designer."},
    "job_search": {
        "enabled": True,
        "role_sought": "a remote junior video editing role",
        "country": "Ecuador",
        "profile_path": "perfil.yaml",
        "projects": [{"name": "short film", "sectors": ["media"], "pitch": "Edited a short."}],
    },
}

CATEGORIES = {"remotive_categories": ["software-development", "design", "marketing"],
              "jobicy_industries": ["design-multimedia", "engineering"]}

BRAIN_REPLY = {
    "search_terms": ["video editor", "motion graphics", "editor de video"],
    "title_terms": ["video", "editor", "motion designer", "motion graphics", "animator"],
    "exclude_title_terms": ["wordpress"],
    "remotive_categories": ["design", "made-up-category"],
    "jobicy_industries": ["design-multimedia"],
}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    state = {"raw": json.loads(json.dumps(PROFILE)), "prompts": [], "reply": BRAIN_REPLY}
    monkeypatch.setattr(vt, "_raw_config", lambda: state["raw"])
    monkeypatch.setattr(vt, "CACHE_FILE", tmp_path / "vacancy_terms.json")

    def ask(prompt):
        state["prompts"].append(prompt)
        return state["reply"]
    state["ask"] = ask
    return state


def run(state, **kw):
    return vt.vacancy_terms(lambda: CATEGORIES, ask=state["ask"], **kw)


def test_brain_derives_terms_from_the_profile(setup):
    r = run(setup)
    assert r["search_terms"] == ["video editor", "motion graphics", "editor de video"]
    assert r["sources"]["title_terms"] == "brain"
    assert r["warning"] == ""
    prompt = setup["prompts"][0]
    assert "video editing role" in prompt and "short film" in prompt
    # Paths and places don't describe the candidate.
    assert "perfil.yaml" not in prompt and "Ecuador" not in prompt


def test_categories_must_be_ones_the_portal_lists(setup):
    r = run(setup)
    assert r["remotive_categories"] == ["design"]
    assert r["jobicy_industries"] == ["design-multimedia"]


def test_a_field_unknown_to_the_config_model_still_reaches_the_brain(setup):
    setup["raw"]["job_search"]["tools_i_use"] = ["DaVinci Resolve", "Blender"]
    run(setup)
    assert "DaVinci Resolve" in setup["prompts"][0]


def test_cached_until_the_profile_changes(setup):
    run(setup)
    r = run(setup)
    assert len(setup["prompts"]) == 1
    assert r["sources"]["search_terms"] == "cache"

    setup["raw"]["job_search"]["role_sought"] = "a remote 3D artist role"
    run(setup)
    assert len(setup["prompts"]) == 2


def test_refresh_asks_again(setup):
    run(setup)
    run(setup, refresh=True)
    assert len(setup["prompts"]) == 2


def test_config_values_win_and_are_not_asked_for(setup):
    setup["raw"]["job_search"]["vacancy_search"] = {
        "search_terms": ["unreal engine"], "remotive_categories": ["design"]}
    r = run(setup)
    assert r["search_terms"] == ["unreal engine"]
    assert r["sources"]["search_terms"] == "config"
    assert r["title_terms"] == BRAIN_REPLY["title_terms"]
    prompt = setup["prompts"][0]
    assert '"search_terms"' not in prompt and '"remotive_categories"' not in prompt


def test_editing_vacancy_search_alone_does_not_rederive_the_rest(setup):
    setup["raw"]["job_search"]["vacancy_search"] = {"search_terms": ["a"]}
    run(setup)
    setup["raw"]["job_search"]["vacancy_search"] = {"search_terms": ["b"]}
    run(setup)
    assert len(setup["prompts"]) == 1


def test_everything_in_config_needs_no_brain(setup):
    setup["raw"]["job_search"]["vacancy_search"] = {f: ["x"] for f in vt.FIELDS}
    r = run(setup)
    assert setup["prompts"] == []
    assert set(r["sources"].values()) == {"config"}


def test_brain_failure_falls_back_to_defaults_with_a_warning(setup):
    def boom(prompt):
        raise RuntimeError("no login")
    r = vt.vacancy_terms(lambda: CATEGORIES, ask=boom)
    assert r["search_terms"] == vt.DEFAULTS["search_terms"]
    assert r["sources"]["search_terms"] == "default"
    assert "no login" in r["warning"]
    assert not vt.CACHE_FILE.exists()


def test_unusable_reply_is_not_cached(setup):
    setup["reply"] = {"search_terms": [], "title_terms": "not a list?"}
    r = run(setup)
    assert r["sources"]["search_terms"] == "default"
    assert r["warning"]
    assert not vt.CACHE_FILE.exists()


def test_no_portal_categories_means_none_are_asked_for(setup):
    r = vt.vacancy_terms(lambda: {}, ask=setup["ask"])
    assert '"remotive_categories"' not in setup["prompts"][0]
    assert r["remotive_categories"] == vt.DEFAULTS["remotive_categories"]
    assert r["sources"]["remotive_categories"] == "default"


@pytest.mark.parametrize("title,hit", [
    ("Motion Graphics Designer", True),
    ("Motion-Graphics Artist", True),
    ("Junior Video Editor", True),
    ("Editorial Assistant", False),     # whole words only
    ("WordPress Developer", False),
])
def test_title_regex(title, hit):
    rx = vt.title_regex(["motion graphics", "video", "editor"])
    assert bool(rx.search(title)) is hit


def test_empty_title_terms_match_nothing():
    assert vt.title_regex([]) is None
