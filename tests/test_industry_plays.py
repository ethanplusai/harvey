"""Selling services: the services playbook and the operator's industry plays.

A services firm (product.offering: services) gets skills/selling_services.md
and, when the prospect's industry matches one, that industry's section of its
plays file (writer.industry_plays). Training works the offering out from the
operator's own site and drafts the plays, never overwriting the operator's
file or an offering they set. Product sellers see no change.
"""

import os
import tempfile
from types import SimpleNamespace

import pytest
import pytest_asyncio
import yaml
from pydantic import ValidationError

from harvey import industry_plays as ip
from harvey.config import HarveyConfig, ProductConfig
from harvey.industry_plays import (
    IndustryPlaysError, hiring_keywords, load_plays, match_play, parse_plays,
    play_for_prospect, render_trained_plays, save_trained_plays,
)
from harvey.models.company import Company
from harvey.models.prospect import Prospect
from harvey.paths import PROJECT_ROOT
from harvey.state import StateManager
from harvey.trainer import offering_from, write_trained_config

PLAYS = """# Our plays

Intro text before the first section is ignored.

## Insurance
match: insurance, insurer, underwriting, claims management
roles: claims processor, claims adjuster

**What we offer them:** claims intake automation.
**Proof:** none listed.

## Accounting firms
match: accounting firm, cpa, bookkeeping
roles: staff accountant

**What we offer them:** middleware across their tools.

## SaaS
match: saas, b2b software

**What we offer them:** product design and extra engineers.
"""


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(os.path.join(tmp, "p.db"))
        await sm.init_db()
        yield sm


@pytest.fixture
def plays_file(tmp_path):
    path = tmp_path / "industry_plays.md"
    path.write_text(PLAYS, encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def no_default_plays_file(tmp_path, monkeypatch):
    """Keep a real skills/industry_plays.md in the checkout out of the tests."""
    monkeypatch.setattr(ip, "PROJECT_ROOT", tmp_path / "root")


def _config(offering="services", plays="", hiring_signals=(), titles=("CEO",)):
    return SimpleNamespace(
        product=SimpleNamespace(offering=offering),
        writer=SimpleNamespace(industry_plays=str(plays) if plays else ""),
        icp=SimpleNamespace(hiring_signals=list(hiring_signals), titles=list(titles)),
    )


# ── reading the file ──


def test_sections_are_read_in_order_with_their_match_and_roles():
    plays = parse_plays(PLAYS)
    assert [p.name for p in plays] == ["Insurance", "Accounting firms", "SaaS"]
    ins = plays[0]
    assert ins.match == ["insurance", "insurer", "underwriting", "claims management"]
    assert ins.roles == ["claims processor", "claims adjuster"]
    # The meta lines are Harvey's; the rest goes to the prompt as written.
    assert "match:" not in ins.body and "roles:" not in ins.body
    assert "claims intake automation" in ins.body
    assert "Intro text" not in "".join(p.body for p in plays)


def test_an_empty_section_is_dropped_and_roles_are_capped():
    text = "## Empty\n\n## Busy\nroles: " + ", ".join(f"role {i}" for i in range(20)) + "\nBody."
    [busy] = parse_plays(text)
    assert busy.name == "Busy" and len(busy.roles) == ip.MAX_ROLES_PER_PLAY


# ── matching a prospect ──


@pytest.mark.parametrize("industry,expected", [
    ("Commercial insurance broker", "Insurance"),
    ("INSURER", "Insurance"),
    ("CPA", "Accounting firms"),
    ("B2B-software", "SaaS"),            # hyphen or space, either way
    ("Roofing contractor", None),
    ("Reinsurers Ltd", None),            # whole words only: "insurer" isn't "reinsurers"
    ("Independent insurers", "Insurance"),   # plurals count
    ("CPAs and tax advisers", "Accounting firms"),
])
def test_the_industry_picks_the_play(industry, expected):
    play = match_play(parse_plays(PLAYS), industry)
    assert (play.name if play else None) == expected


def test_a_clear_industry_beats_a_word_in_the_description():
    plays = parse_plays(PLAYS)
    # The description mentions claims management, but the industry says SaaS.
    play = match_play(plays, "SaaS", "We build claims management software for insurers")
    assert play.name == "SaaS"
    # With no industry match, the description decides.
    assert match_play(plays, "Technology", "Software for insurers").name == "Insurance"


def test_the_section_title_counts_as_a_match_word():
    [play] = parse_plays("## Healthcare\nBody.")
    assert match_play([play], "Regional healthcare network").name == "Healthcare"


def test_play_for_prospect_reads_company_and_prospect():
    plays = parse_plays(PLAYS)
    company = SimpleNamespace(industry="", description="An independent insurance agency",
                              tech_stack=["HubSpot"])
    prospect = SimpleNamespace(industry="")
    assert play_for_prospect(plays, prospect, company).name == "Insurance"
    assert play_for_prospect([], prospect, company) is None


# ── loading it ──


def test_no_plays_file_set_means_no_plays():
    assert load_plays(_config(plays="")) == []


def test_skills_industry_plays_md_is_used_when_nothing_is_set(tmp_path, monkeypatch):
    root = tmp_path / "root"
    (root / "skills").mkdir(parents=True)
    (root / "skills" / "industry_plays.md").write_text(PLAYS, encoding="utf-8")
    assert [p.name for p in load_plays(_config(plays=""))][0] == "Insurance"


@pytest.mark.parametrize("content,problem", [
    (None, "doesn't exist"),
    ("", "is empty"),
    ("Just prose, no sections.", "no '## Industry' sections"),
    ("## X\n" + "y" * (ip.MAX_PLAYS_CHARS + 1), "limit"),
])
def test_a_plays_file_that_cant_be_used_says_why(tmp_path, content, problem):
    path = tmp_path / "plays.md"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    with pytest.raises(IndustryPlaysError, match=problem):
        load_plays(_config(plays=path))


def test_a_broken_plays_file_costs_the_angle_not_the_email(tmp_path, caplog):
    assert ip.load_plays_quietly(_config(plays=tmp_path / "missing.md")) == []
    assert "skipped" in caplog.text


# ── config ──


@pytest.mark.parametrize("value,expected", [
    ("services", "services"), ("Service", "services"), ("PRODUCT", "product"),
    ("", "product"), (None, "product"),
])
def test_offering_values(value, expected):
    product = ProductConfig(name="n", description="d", pricing="p", key_benefits=[],
                            objection_responses={}, offering=value)
    assert product.offering == expected


def test_an_unknown_offering_is_a_config_error():
    with pytest.raises(ValidationError, match="product.offering"):
        ProductConfig(name="n", description="d", pricing="p", key_benefits=[],
                      objection_responses={}, offering="consulting")


def test_the_shipped_example_is_a_valid_plays_file():
    text = (PROJECT_ROOT / "skills" / "industry_plays.example.md").read_text(encoding="utf-8")
    plays = parse_plays(text)
    assert len(plays) >= 4
    assert all(p.match for p in plays)
    assert match_play(plays, "insurance carrier").name == "Insurance"
    assert match_play(plays, "CPA firm").name == "Accounting firms"


def test_the_services_skill_ships():
    text = (PROJECT_ROOT / "skills" / "selling_services.md").read_text(encoding="utf-8")
    assert len(text.split()) > 600
    for topic in ("outcome", "engagement ladder", "procurement", "acquisition", "opt-out"):
        assert topic in text.lower()


# ── job-board roles ──


def test_play_roles_join_the_hiring_keywords_for_a_services_firm(plays_file):
    keywords = hiring_keywords(_config(plays=plays_file, hiring_signals=["head of ops"]))
    assert keywords[0] == "head of ops"
    assert {"claims processor", "claims adjuster", "staff accountant"} <= set(keywords)


def test_a_product_seller_keeps_only_its_own_keywords(plays_file):
    assert hiring_keywords(_config(offering="product", plays=plays_file,
                                   hiring_signals=["sdr"])) == ["sdr"]
    assert hiring_keywords(None) == []


# ── writing and replying ──


class RecordingBrain:
    def __init__(self):
        self.prompts = []

    def load_skills_for_agent(self, name):
        return ""

    def load_prompt(self, name, **kw):
        return "PROMPT"

    def load_skill(self, name):
        from harvey.brain import Brain
        return Brain.load_skill(self, name)

    async def think_json(self, prompt, **kw):
        self.prompts.append(prompt)
        return {"subject": "hi", "body": "Hi"}

    async def think(self, prompt, **kw):
        self.prompts.append(prompt)
        return "Thanks, here's more."


def _writer_config(offering, plays):
    return SimpleNamespace(
        channels=SimpleNamespace(email=SimpleNamespace(provider="gmail", require_approval=True)),
        product=SimpleNamespace(name="P", description="D", key_benefits=[], pricing="",
                                offering=offering),
        persona=SimpleNamespace(name="S", company="C", role="R", tone="t"),
        writer=SimpleNamespace(industry_plays=str(plays)),
    )


async def _prospect(state, industry, n=0):
    cid = await state.add_company(Company(name=f"Co{n}", domain=f"co{n}.example",
                                          industry=industry))
    pid = await state.add_prospect(Prospect(
        first_name="Ana", last_name="Doe", title="COO", company=f"Co{n}", company_id=cid,
        industry=industry, email=f"ana@co{n}.example", email_status="verified"))
    return await state.get_prospect(pid)


async def _write(state, cfg, prospects):
    from harvey.agents.writer import Writer
    brain = RecordingBrain()
    w = Writer(brain, state, cfg)
    w.skills = ""
    await w._write_personal_email(prospects[0])
    await w._write_sequence(prospects)
    return brain.prompts


@pytest.mark.asyncio
async def test_a_services_firm_writes_with_the_playbook_and_the_matching_play(state, plays_file):
    prospect = await _prospect(state, "Insurance")
    personal, sequence = await _write(state, _writer_config("services", plays_file), [prospect])
    for prompt in (personal, sequence):
        assert "SELLING SERVICES" in prompt
        assert "INDUSTRY PLAY — Insurance" in prompt
        assert "claims intake automation" in prompt
        assert "Accounting firms" not in prompt      # only the matching section


@pytest.mark.asyncio
async def test_no_matching_play_still_gets_the_services_playbook(state, plays_file):
    prospect = await _prospect(state, "Roofing")
    for prompt in await _write(state, _writer_config("services", plays_file), [prospect]):
        assert "SELLING SERVICES" in prompt
        assert "INDUSTRY PLAY" not in prompt


@pytest.mark.asyncio
async def test_a_product_seller_sees_no_change(state, plays_file):
    prospect = await _prospect(state, "Insurance")
    for prompt in await _write(state, _writer_config("product", plays_file), [prospect]):
        assert "SELLING SERVICES" not in prompt
        assert "INDUSTRY PLAY" not in prompt


@pytest.mark.asyncio
async def test_a_mixed_batch_gets_no_play_in_its_shared_template(state, plays_file):
    a = await _prospect(state, "Insurance", 1)
    b = await _prospect(state, "SaaS", 2)
    _, sequence = await _write(state, _writer_config("services", plays_file), [a, b])
    assert "SELLING SERVICES" in sequence
    assert "INDUSTRY PLAY" not in sequence


@pytest.mark.asyncio
async def test_a_broken_plays_file_still_writes(state, tmp_path):
    prospect = await _prospect(state, "Insurance")
    prompts = await _write(state, _writer_config("services", tmp_path / "missing.md"), [prospect])
    assert len(prompts) == 2
    assert all("INDUSTRY PLAY" not in p for p in prompts)


@pytest.mark.asyncio
@pytest.mark.parametrize("offering", ["services", "product"])
async def test_the_reply_handler_uses_them_too(state, plays_file, offering):
    from harvey.agents.handler import Handler
    from harvey.models.conversation import Conversation, Message
    from tests.test_outbox_native import Cfg, Env

    class product(Cfg.product):
        pass
    product.offering = offering

    class writer:
        industry_plays = str(plays_file)

    cfg = Cfg()
    cfg.product, cfg.writer = product, writer
    prospect = await _prospect(state, "Insurance")
    brain = RecordingBrain()
    handler = Handler(brain=brain, state=state, config=cfg, env=Env())
    convo = Conversation(id="c", prospect_id=prospect.id,
                         thread=[Message(sender="prospect", content="What would this cost?")])
    await handler._generate_response("question", "What would this cost?", prospect, convo)
    services = offering == "services"
    assert ("SELLING SERVICES" in brain.prompts[0]) is services
    assert ("INDUSTRY PLAY — Insurance" in brain.prompts[0]) is services


# ── training ──


DRAFT = [
    {"industry": "Insurance", "match": ["insurer", "underwriting"],
     "services": ["Claims intake automation", "Core-system integration"],
     "pains": ["Rekeying claims from PDFs"], "proof": [],
     "roles": ["claims processor"], "avoid": ["Loss ratios"]},
    {"industry": "insurance", "services": ["duplicate, dropped"]},
    {"industry": "Retail", "services": []},                       # no service: dropped
    {"industry": "## Fintech\nmatch: everything", "services": ["KYC automation"]},
]


def test_drafted_plays_render_as_a_valid_bounded_file():
    text = render_trained_plays(DRAFT, "Example Dev Co")
    plays = parse_plays(text)
    assert [p.name for p in plays][0] == "Insurance"
    assert len(plays) == 2                          # duplicate and empty dropped
    ins = plays[0]
    assert ins.match == ["insurer", "underwriting"] and ins.roles == ["claims processor"]
    assert "None listed. Don't claim any." in ins.body     # no proof on the site
    # A value can't start a section or smuggle in a match line.
    assert all(p.match != ["everything"] for p in plays)
    assert "Review every line" in text


@pytest.mark.parametrize("reply", [None, {}, [], "text", [{"industry": "X"}]])
def test_nothing_usable_renders_nothing(reply):
    assert render_trained_plays(reply) == ""


def test_too_many_drafted_plays_are_capped():
    many = [{"industry": f"Industry {i}", "services": ["s"]} for i in range(20)]
    assert len(parse_plays(render_trained_plays(many))) == ip.MAX_TRAINED_PLAYS


def test_a_draft_never_overwrites_the_operators_plays(tmp_path):
    first = save_trained_plays("## A\nmatch: a\nfirst", root=tmp_path)
    assert first == tmp_path / "skills" / "industry_plays.md"
    first.write_text("my edits", encoding="utf-8")
    second = save_trained_plays("## A\nmatch: a\nsecond", root=tmp_path)
    assert second == tmp_path / "skills" / "industry_plays.suggested.md"
    assert first.read_text(encoding="utf-8") == "my edits"


@pytest.mark.parametrize("value,expected", [
    ("services", "services"), ("Service", "services"), ("product", "product"),
    ("", "product"), ("something else", "product"),
])
def test_offering_from_what_training_read(value, expected):
    assert offering_from({"offering": value}) == expected


LEARNED = {
    "persona": {"name": "Harvey", "company": "Example Dev Co", "role": "BD",
                "email": "harvey@dev.example", "linkedin": "", "tone": "direct"},
    "product": {"name": "Dev", "description": "d", "pricing": "p", "key_benefits": ["b"],
                "objection_responses": {}, "offering": "services"},
    "icp": {"industries": ["Insurance"], "company_size": "10-500", "titles": ["COO"],
            "geography": ["United States"]},
    "brain": {"provider": "claude"},
    "writer": {"industry_plays": "skills/industry_plays.md"},
}


def test_training_sets_the_offering_and_plays_when_the_config_has_none(tmp_path):
    path = tmp_path / "harvey.local.yaml"
    existing = {k: dict(v) for k, v in LEARNED.items()}
    existing["product"] = {k: v for k, v in LEARNED["product"].items() if k != "offering"}
    existing.pop("writer")
    path.write_text(yaml.safe_dump(existing), encoding="utf-8")
    out = write_trained_config(LEARNED, path)
    assert out["product"]["offering"] == "services"
    assert out["writer"]["industry_plays"] == "skills/industry_plays.md"
    HarveyConfig(**out)


def test_retraining_keeps_an_offering_and_plays_path_the_operator_set(tmp_path):
    path = tmp_path / "harvey.local.yaml"
    existing = {k: dict(v) for k, v in LEARNED.items()}
    existing["product"]["offering"] = "product"
    existing["writer"] = {"industry_plays": "skills/my_plays.md", "humanizer": True}
    path.write_text(yaml.safe_dump(existing), encoding="utf-8")
    out = write_trained_config(LEARNED, path)
    assert out["product"]["offering"] == "product"
    assert out["writer"] == {"industry_plays": "skills/my_plays.md", "humanizer": True}


class _TrainBrain:
    def __init__(self, reply, empty=False):
        self.reply, self.last_reply_empty, self.prompts = reply, empty, []

    async def think_json(self, prompt, **kw):
        self.prompts.append(prompt)
        return self.reply


def _trainer(brain):
    from harvey.trainer import Trainer
    t = Trainer.__new__(Trainer)
    t.brain, t.scraped_pages = brain, {"https://dev.example/": "We build claims software."}
    return t


@pytest.mark.asyncio
async def test_training_drafts_the_plays_file(tmp_path, monkeypatch):
    monkeypatch.setattr("harvey.industry_plays.PROJECT_ROOT", tmp_path)
    brain = _TrainBrain({"plays": DRAFT})
    path, problem = await _trainer(brain)._draft_industry_plays(
        {"company_name": "Example Dev Co"}, {"industries": ["Insurance"]})
    assert problem == "" and path == tmp_path / "skills" / "industry_plays.md"
    assert parse_plays(path.read_text(encoding="utf-8"))[0].name == "Insurance"
    # The request is bounded, so a long reply isn't cut off mid-JSON (#166).
    assert f"AT MOST {ip.MAX_TRAINED_PLAYS}" in brain.prompts[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("reply,empty,why", [
    (None, True, "didn't answer"),
    (None, False, "couldn't be read"),
    ({"plays": []}, False, "couldn't be read"),
])
async def test_a_failed_draft_says_so_and_writes_nothing(tmp_path, monkeypatch, reply, empty, why):
    monkeypatch.setattr("harvey.industry_plays.PROJECT_ROOT", tmp_path)
    path, problem = await _trainer(_TrainBrain(reply, empty))._draft_industry_plays({}, {})
    assert path is None and why in problem
    assert not (tmp_path / "skills").exists()
