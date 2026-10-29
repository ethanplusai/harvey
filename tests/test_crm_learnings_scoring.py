"""Tasks, notes, curated learnings and the explainable lead score.

Ported from the LeadForge sales OS: follow-up tasks with due dates (and a
"reply today" task for every interested reply), private notes, a gated
learnings store the Writer and Handler apply, and a deterministic score
that says where every point came from.
"""

import asyncio
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from pydantic import ValidationError

from harvey import crm
from harvey.agents.handler import CLOSING_STAGES, SPEED_TO_LEAD_SOURCE, Handler
from harvey.agents.writer import Writer
from harvey.brain import Brain
from harvey.config import ScoringConfig
from harvey.models.company import Company
from harvey.models.prospect import Prospect
from harvey.paths import PROJECT_ROOT
from harvey.scoring import DEFAULT_WEIGHTS, score_prospect
from harvey.state import StateManager
from tests.test_dashboard_signals import client  # noqa: F401  (fixture)


@pytest_asyncio.fixture
async def state(tmp_path):
    s = StateManager(str(tmp_path / "crm.db"))
    await s.init_db()
    return s


async def add_person(state, first="Ana", company_id=""):
    return await state.add_prospect(Prospect(
        first_name=first, last_name="Doe", title="Owner", company="Acme",
        company_id=company_id, industry="Roofing", email=f"{first.lower()}@example.com",
        email_status="verified"))


# ── Validation rules (harvey.crm) ──


def test_due_dates_accept_words_and_iso_and_refuse_anything_else():
    assert crm.parse_due_date("") == ""
    assert crm.parse_due_date("today") == date.today().isoformat()
    assert crm.parse_due_date("Tomorrow") == (date.today() + timedelta(days=1)).isoformat()
    assert crm.parse_due_date("2026-11-03") == "2026-11-03"
    with pytest.raises(crm.CrmInputError):
        crm.parse_due_date("next week")


def test_task_buckets():
    today = date(2026, 10, 30)
    assert crm.task_bucket("2026-10-29", False, today) == "overdue"
    assert crm.task_bucket("2026-10-30", False, today) == "today"
    assert crm.task_bucket("2026-11-02", False, today) == "upcoming"
    assert crm.task_bucket("", False, today) == "undated"
    assert crm.task_bucket("2026-10-01", True, today) == "done"


def test_learning_gate_refuses_low_confidence_and_unknown_categories():
    with pytest.raises(crm.CrmInputError, match="below"):
        crm.validate_learning("sales-learning", "A hunch", 0.5)
    with pytest.raises(crm.CrmInputError, match="Category"):
        crm.validate_learning("gossip", "Something", 0.9)
    with pytest.raises(crm.CrmInputError):
        crm.validate_learning("sales-learning", "   ", 0.9)
    with pytest.raises(crm.CrmInputError):
        crm.validate_learning("sales-learning", "x" * (crm.MAX_LEARNING_CHARS + 1), 0.9)
    assert crm.validate_learning(" Industry-Insight ", "Dentists  reply\nmornings", "0.8") == (
        "industry-insight", "Dentists reply mornings", 0.8)


# ── Storage ──


@pytest.mark.asyncio
async def test_tasks_list_open_first_by_due_date_and_carry_who(state):
    pid = await add_person(state)
    later = await state.add_task("Send the case study", due_date="2026-12-01", prospect_id=pid)
    soon = await state.add_task("Call back", due_date="2026-11-01", prospect_id=pid)
    undated = await state.add_task("Tidy the list")
    done = await state.add_task("Old one", due_date="2026-10-01")
    await state.set_task_done(done)

    rows = await state.get_tasks()
    assert [r["id"] for r in rows] == [soon, later, undated]
    assert rows[0]["first_name"] == "Ana" and rows[0]["company_name"] == "Acme"
    assert [r["id"] for r in await state.get_tasks(include_done=True)][-1] == done
    assert [r["id"] for r in await state.get_tasks(prospect_id=pid)] == [soon, later]
    assert await state.delete_task(undated) and not await state.delete_task(undated)


@pytest.mark.asyncio
async def test_notes_on_a_contact_include_their_companys_notes(state):
    cid = await state.add_company(Company(name="Acme", domain="acme.example"))
    pid = await add_person(state, company_id=cid)
    other = await add_person(state, "Ben", company_id=cid)
    await state.add_note("Prefers mornings", prospect_id=pid)
    await state.add_note("Budget resets in April", company_id=cid)
    await state.add_note("About Ben only", prospect_id=other)

    contents = {n["content"] for n in await state.get_notes(prospect_id=pid)}
    assert contents == {"Prefers mornings", "Budget resets in April"}


@pytest.mark.asyncio
async def test_learnings_retire_restore_and_search(state):
    a = await state.add_learning("sales-learning", "Trade, never discount", 0.9)
    b = await state.add_learning("industry-insight", "Roofers read email at 6am", 0.7)
    await state.set_learning_active(b, False)
    assert [r["id"] for r in await state.get_learnings()] == [a]
    assert {r["id"] for r in await state.get_learnings(active_only=False)} == {a, b}
    assert [r["id"] for r in await state.get_learnings(active_only=False, query="roofers")] == [b]
    # LIKE wildcards in a search are literal.
    assert await state.get_learnings(active_only=False, query="%") == []


# ── The score ──


ICP = SimpleNamespace(titles=["Owner"], industries=["Roofing"], geography=["Denver, CO"])


def test_default_weights_keep_the_original_heuristic():
    star = Prospect(title="Owner", seniority="c_suite", industry="Roofing",
                    email="a@example.com", email_status="verified")
    assert score_prospect(star, ICP).total == 100        # 40+25+20+10+8 capped
    weak = Prospect(title="Assistant", seniority="manager", industry="Bakery",
                    email="b@example.com", email_status="risky")
    assert score_prospect(weak, ICP).total == 48         # 40+5+3
    hiring = Prospect(title="Assistant", personalization_notes="Signal: hiring a roofer")
    assert score_prospect(hiring, ICP).total == 50       # 40+10


def test_breakdown_explains_every_point_and_optional_parts_need_weight():
    person = Prospect(title="Owner", seniority="vp", email="c@example.com")
    company = Company(industry="Roofing", location="Denver, Colorado",
                      tech_stack=["WordPress", "HubSpot"])
    off = score_prospect(person, ICP, company=company)
    parts = {p.name: p for p in off.parts}
    assert parts["industry"].points == 10                 # taken from the company
    assert parts["geography"].points == 0 and parts["tech_fit"].points == 0
    assert off.total == round(sum(p.points for p in off.parts))

    on = score_prospect(person, ICP, {"geography": 10, "tech_fit": 10, "base": 0}, company)
    parts = {p.name: p for p in on.parts}
    assert parts["geography"].points == 10 and "Denver" in parts["geography"].reason
    assert parts["tech_fit"].points == 4                  # 2 of 5 tools
    assert on.as_dict()["total"] == on.total


def test_scoring_config_refuses_unknown_parts_and_bad_numbers():
    assert ScoringConfig(weights={"geography": 15}).weights == {"geography": 15}
    with pytest.raises(ValidationError, match="unknown part"):
        ScoringConfig(weights={"vibes": 5})
    with pytest.raises(ValidationError):
        ScoringConfig(weights={"title": 500})
    with pytest.raises(ValidationError):
        ScoringConfig(weights={"title": True})
    assert set(DEFAULT_WEIGHTS) >= {"base", "geography", "tech_fit"}


# ── Skills reach the right agents ──


def test_new_skills_exist_and_are_loaded():
    for name in ("sales_strategy", "closing_strategy", "reengagement"):
        assert (PROJECT_ROOT / "skills" / f"{name}.md").is_file()
    brain = Brain.__new__(Brain)
    brain.load_skill = lambda name: f"<<{name}>>"
    handler = brain.load_skills_for_agent("handler")
    assert "<<reengagement>>" in handler and "<<sales_strategy>>" in handler
    assert "<<closing_strategy>>" not in handler          # added per stage instead
    assert "<<sales_strategy>>" in brain.load_skills_for_agent("writer")


def _handler(state, brain=None):
    h = Handler.__new__(Handler)
    h.state = state
    h.brain = brain or SimpleNamespace(load_skill=lambda name: f"<<{name}>>")
    return h


@pytest.mark.asyncio
async def test_closing_playbook_only_for_late_stages(state):
    h = _handler(state)
    for stage in CLOSING_STAGES:
        assert "<<closing_strategy>>" in h._stage_playbook(stage)
    assert h._stage_playbook("initial_outreach") == ""
    assert h._stage_playbook("engaged") == ""


@pytest.mark.asyncio
async def test_interested_reply_adds_one_task_due_today(state):
    pid = await add_person(state)
    prospect = await state.get_prospect(pid)
    h = _handler(state)
    await h._speed_to_lead_task(prospect)
    await h._speed_to_lead_task(prospect)              # a second reply doesn't double it
    [task] = await state.get_tasks()
    assert task["due_date"] == date.today().isoformat()
    assert task["source"] == SPEED_TO_LEAD_SOURCE and task["prospect_id"] == pid
    await state.set_task_done(task["id"])
    await h._speed_to_lead_task(prospect)              # done → a new reply gets a new one
    assert len(await state.get_tasks()) == 1


# ── Learnings reach the prompts ──


class RecordingBrain:
    def __init__(self, reply):
        self.reply, self.prompts = reply, []

    def load_skills_for_agent(self, name):
        return ""

    def load_prompt(self, name, **kw):
        return "prompt"

    async def think_json(self, prompt, **kw):
        self.prompts.append(prompt)
        return self.reply


def _writer(brain, state):
    config = SimpleNamespace(
        channels=SimpleNamespace(email=SimpleNamespace(provider="gmail", require_approval=True)),
        product=SimpleNamespace(name="P", description="D", key_benefits=[], pricing=""),
        persona=SimpleNamespace(name="S", company="C", role="R", tone="t"),
    )
    w = Writer(brain, state, config)
    w.skills = ""
    return w


@pytest.mark.asyncio
async def test_writer_applies_active_learnings_only(state):
    await state.add_learning("outreach-improvement", "Lead with their booking page", 0.8)
    retired = await state.add_learning("sales-learning", "Mention the free audit", 0.9)
    await state.set_learning_active(retired, False)
    pid = await add_person(state)
    brain = RecordingBrain({"subject": "hi", "body": "Hi Ana"})
    await _writer(brain, state)._write_personal_email(await state.get_prospect(pid))
    prompt = brain.prompts[0]
    assert "VALIDATED LEARNINGS" in prompt
    assert "[outreach-improvement] Lead with their booking page" in prompt
    assert "free audit" not in prompt


@pytest.mark.asyncio
async def test_no_learnings_adds_nothing(state):
    assert await _handler(state)._learnings_block() == ""


# ── Dashboard API ──


def test_dashboard_tasks_round_trip(client):
    assert client.post("/api/tasks", json={"description": "  "}).status_code == 400
    assert client.post("/api/tasks", json={"description": "x", "due_date": "soon"}).status_code == 400
    assert client.post("/api/tasks", json={"description": "x", "prospect_id": "nope"}).status_code == 404
    r = client.post("/api/tasks", json={"description": "Call back", "due_date": "today"}).json()
    [task] = client.get("/api/tasks").json()
    assert task["id"] == r["id"] and task["bucket"] == "today"
    assert client.post(f"/api/tasks/{r['id']}/done").json()["success"]
    assert client.get("/api/tasks").json() == []
    assert client.get("/api/tasks?include_done=true").json()[0]["bucket"] == "done"
    assert client.post(f"/api/tasks/{r['id']}/explode").status_code == 400


def test_dashboard_learnings_enforce_the_gate(client):
    low = client.post("/api/learnings", json={"category": "sales-learning",
                                              "content": "maybe", "confidence": 0.4})
    assert low.status_code == 400 and "below" in low.json()["message"]
    ok = client.post("/api/learnings", json={"category": "sales-learning",
                                             "content": "Trade, never discount", "confidence": 0.9})
    lid = ok.json()["id"]
    data = client.get("/api/learnings").json()
    assert [r["id"] for r in data["learnings"]] == [lid]
    assert data["min_confidence"] == crm.MIN_CONFIDENCE
    assert client.post(f"/api/learnings/{lid}/retire").json()["success"]
    assert client.get("/api/learnings").json()["learnings"] == []


def test_dashboard_notes_need_an_owner(client):
    asyncio.run(StateManager(str(client.db_path)).init_db())
    assert client.post("/api/notes", json={"content": "hi"}).status_code == 400
    r = client.post("/api/notes", json={"content": "Line one\nLine two", "company_id": "c1"}).json()
    [note] = client.get("/api/notes?company_id=c1").json()
    assert note["id"] == r["id"] and note["content"] == "Line one\nLine two"
