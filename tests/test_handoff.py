"""Said yes to your offer: Sellbird hands the person to another system's MCP
tool once, and the reply it drafts carries what that system answered."""

import sys
import textwrap
from types import SimpleNamespace

import pytest
import pytest_asyncio

from sellbird.config import HandoffConfig
from sellbird.handoff import call_tool, configured, fill, values_for
from sellbird.models.conversation import Conversation, Message
from sellbird.models.prospect import Prospect
from sellbird.state import StateManager
from tests.test_booking import OfferCfg, PromptBrain
from tests.test_outbox_native import Env

SERVER = textwrap.dedent('''
    from mcp.server.mcpserver import MCPServer
    server = MCPServer("roofish")

    @server.tool()
    def create_site(company: str, email: str, website: str = "") -> str:
        """Start a site build."""
        if not company:
            raise ValueError("company is required")
        return f"Build started for {company} ({website}). Preview: https://preview.example/{company.lower().replace(' ', '-')}"

    server.run()
''')


@pytest.fixture
def stdio_handoff(tmp_path):
    script = tmp_path / "server.py"
    script.write_text(SERVER)
    return HandoffConfig(mcp_command=[sys.executable, str(script)], tool="create_site",
                         arguments={"company": "{company}", "email": "{email}", "website": "{website}"})


def test_arguments_fill_from_the_prospect_and_unknown_fields_go_blank():
    values = {"company": "Summit Roofing", "email": "mike@summit.example", "website": "summit.example"}
    assert fill({"company": "{company}", "note": "{first_name} at {company} {nope}",
                 "tags": ["{company}"], "n": 3}, values) == {
        "company": "Summit Roofing", "note": "at Summit Roofing", "tags": ["Summit Roofing"], "n": 3}


def test_nothing_is_configured_without_a_server_and_a_tool():
    assert not configured(SimpleNamespace(handoff=HandoffConfig()))
    assert not configured(SimpleNamespace(handoff=HandoffConfig(tool="x")))
    assert configured(SimpleNamespace(handoff=HandoffConfig(tool="x", mcp_url="https://a.example/mcp")))


@pytest.mark.asyncio
async def test_a_real_mcp_server_gets_the_call_and_its_answer_comes_back(stdio_handoff):
    prospect = Prospect(first_name="Mike", last_name="Ross", company="Summit Roofing",
                        email="mike@summit.example", title="Owner")
    company = SimpleNamespace(website="summit.example", domain="")
    args = fill(stdio_handoff.arguments, values_for(prospect, company, "Sure, send it", "a new site"))
    outcome = await call_tool(stdio_handoff, args)
    assert outcome["ok"], outcome
    assert "Build started for Summit Roofing (summit.example)" in outcome["result"]
    assert "https://preview.example/summit-roofing" in outcome["result"]


@pytest.mark.asyncio
async def test_a_tool_error_is_reported_not_raised(stdio_handoff):
    outcome = await call_tool(stdio_handoff, {"company": "", "email": "x@y.example"})
    assert outcome["ok"] is False and outcome["error"]


@pytest.mark.asyncio
async def test_a_missing_token_is_named():
    h = HandoffConfig(mcp_url="https://roofo.example/mcp", token_env="ROOFO_MCP_TOKEN", tool="t")
    outcome = await call_tool(h, {}, env={})
    assert outcome == {"ok": False, "error": "ROOFO_MCP_TOKEN isn't set in .env"}


# ── The reply handler ─────────────────────────────────────────────────────

class YesCfg(OfferCfg):
    class product(OfferCfg.product):
        class offer(OfferCfg.product.offer):
            entry = "your new site, free until you launch it"


@pytest_asyncio.fixture
async def state(tmp_path):
    s = StateManager(str(tmp_path / "handoff.db"))
    await s.init_db()
    return s


async def _setup(state, handoff=None):
    from sellbird.agents.handler import Handler
    cfg = YesCfg()
    cfg.handoff = handoff or HandoffConfig()
    brain = PromptBrain(text_reply="Great. Here it is.")
    handler = Handler(brain=brain, state=state, config=cfg, env=Env())
    pid = await state.add_prospect(Prospect(first_name="Mike", last_name="Ross", title="Owner",
                                            company="Summit Roofing", email="mike@summit.example",
                                            email_status="verified"))
    return handler, brain, await state.get_prospect(pid)


@pytest.mark.asyncio
async def test_the_classifier_knows_the_offer_it_can_say_yes_to(state):
    handler, brain, prospect = await _setup(state)
    brain.text_reply = "accepted_offer"
    assert await handler._classify_intent("Sure, send it over", prospect) == "accepted_offer"
    assert '"accepted_offer"' in brain.prompts[0]
    assert "your new site, free until you launch it" in brain.prompts[0]


@pytest.mark.asyncio
async def test_a_yes_is_handed_off_once_and_the_reply_carries_the_answer(state, stdio_handoff):
    handler, brain, prospect = await _setup(state, stdio_handoff)
    first = await handler._hand_off_once(prospect, "Sure, send it")
    again = await handler._hand_off_once(prospect, "Sure, send it")
    assert first["ok"] and again == first   # stored: a retry never builds twice
    convo = Conversation(id="c", prospect_id=prospect.id, stage="engaged",
                         thread=[Message(sender="prospect", content="Sure, send it")])
    await handler._generate_response("accepted_offer", "Sure, send it", prospect, convo, handoff=first)
    prompt = brain.prompts[-1]
    assert "They said yes to your offer (your new site, free until you launch it)" in prompt
    assert "https://preview.example/summit-roofing" in prompt
    assert "https://cal.example/sellbird/15" not in prompt   # no meeting ask on top


@pytest.mark.asyncio
async def test_without_a_handoff_the_reply_promises_no_date(state):
    handler, brain, prospect = await _setup(state)
    assert await handler._hand_off_once(prospect, "yes") is None
    convo = Conversation(id="c", prospect_id=prospect.id, stage="engaged",
                         thread=[Message(sender="prospect", content="yes")])
    await handler._generate_response("accepted_offer", "yes", prospect, convo)
    assert "Don't promise a date or a time." in brain.prompts[-1]
