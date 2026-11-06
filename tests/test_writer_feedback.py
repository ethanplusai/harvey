"""The dashboard's Feedback buttons reach the Writer.

Campaign feedback is guidance for every email written next; feedback on a
contact is used when Sellbird writes to that contact. Before this, the notes
were stored and nothing read them.
"""

from types import SimpleNamespace

import pytest
import pytest_asyncio

from sellbird.agents.writer import Writer
from sellbird.models.campaign import Campaign
from sellbird.models.prospect import Prospect
from sellbird.state import StateManager

SEQ = [{"step": 1, "subject": "quick one", "body": "Hi {{first_name}}, who answers at {{company}}?",
        "delay_days": 0}]


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


def config(provider):
    return SimpleNamespace(
        channels=SimpleNamespace(email=SimpleNamespace(provider=provider, require_approval=True)),
        product=SimpleNamespace(name="P", description="D", key_benefits=[], pricing=""),
        persona=SimpleNamespace(name="S", company="C", role="R", tone="t"),
        humanizer=SimpleNamespace(enabled=False),
    )


def writer(brain, state, provider):
    w = Writer(brain, state, config(provider))
    w.skills = ""   # run() loads these; the tests call the prompt builders directly
    return w


@pytest_asyncio.fixture
async def state(tmp_path):
    s = StateManager(str(tmp_path / "w.db"))
    await s.init_db()
    return s


async def prospect(state, first):
    return await state.add_prospect(Prospect(
        first_name=first, last_name="Doe", title="Owner", company=f"{first} Co",
        industry="Roofing", email=f"{first.lower()}@x.test", email_status="verified"))


@pytest.mark.asyncio
async def test_sequence_prompt_gets_campaign_notes_but_never_one_contacts(state):
    pid = await prospect(state, "Ana")
    other = await prospect(state, "Ben")
    cid = await state.add_campaign(Campaign(id="", name="Denver roofers"))
    await state.add_feedback("campaign", cid, "Too long.\nDrop the question about reviews.")
    await state.add_feedback("contact", pid, "She is the owner's daughter; keep it warm.")
    await state.add_feedback("contact", other, "Not relevant to Ana.")

    brain = RecordingBrain(SEQ)
    await writer(brain, state, "instantly")._write_sequence([await state.get_prospect(pid)])
    [prompt] = brain.prompts
    assert "OWNER FEEDBACK" in prompt
    assert "on campaign 'Denver roofers': Too long. Drop the question about reviews." in prompt
    # The template goes to the whole batch: a note about Ana must not reach it.
    assert "owner's daughter" not in prompt and "on Ana Doe" not in prompt


@pytest.mark.asyncio
async def test_personal_email_prompt_carries_that_contacts_feedback(state):
    pid = await prospect(state, "Cara")
    await state.add_feedback("contact", pid, "Met her at the expo in May.")
    brain = RecordingBrain({"subject": "hi", "body": "Hi Cara"})
    await writer(brain, state, "gmail")._write_personal_email(await state.get_prospect(pid))
    assert "on Cara Doe: Met her at the expo in May." in brain.prompts[0]


@pytest.mark.asyncio
async def test_no_feedback_adds_nothing(state):
    pid = await prospect(state, "Dee")
    brain = RecordingBrain({"subject": "hi", "body": "Hi Dee"})
    await writer(brain, state, "gmail")._write_personal_email(await state.get_prospect(pid))
    assert "OWNER FEEDBACK" not in brain.prompts[0]


@pytest.mark.asyncio
async def test_personal_prompt_skips_other_contacts_notes(state):
    pid = await prospect(state, "Eve")
    other = await prospect(state, "Fay")
    await state.add_feedback("contact", other, "Fay only.")
    brain = RecordingBrain({"subject": "hi", "body": "Hi Eve"})
    await writer(brain, state, "gmail")._write_personal_email(await state.get_prospect(pid))
    assert "Fay only" not in brain.prompts[0]
