"""writer.email_rules: the operator's own shape for the emails Harvey drafts.

The Writer's prompts end with a built-in shape (email 1 under 75 words, an
observation and a question, no pitch). An operator whose first email has to
say more (who they are, what the product is, a link, their own opt-out
sentence) had no way past those last lines: their skills came earlier in
the prompt and lost. With email_rules set, the operator's file replaces the
shape; the FACTS rule, the untrusted-text rule and the output format stay.
"""

import logging
from types import SimpleNamespace

import pytest
import pytest_asyncio
from pydantic import ValidationError

import harvey.config as config_module
from harvey.agents.writer import BUILT_IN_FOOTER, Writer
from harvey.config import (
    MAX_EMAIL_RULES_CHARS,
    ComplianceConfig,
    EmailRulesError,
    WriterConfig,
    load_email_rules,
)
from harvey.models.prospect import Prospect
from harvey.state import StateManager
from harvey.untrusted import UNTRUSTED_RULE

RULES = "Open with who you are. Say what the product costs. Link to example.com/join."
SEQ = [{"step": n, "subject": f"s{n}", "body": f"b{n} {{{{company}}}}", "delay_days": 0 if n == 1 else 3}
       for n in (1, 2, 3)]


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


def config(provider="gmail", email_rules="", compliance=None):
    return SimpleNamespace(
        channels=SimpleNamespace(email=SimpleNamespace(provider=provider, require_approval=True)),
        product=SimpleNamespace(name="P", description="D", key_benefits=[], pricing=""),
        persona=SimpleNamespace(name="S", company="C", role="R", tone="t"),
        writer=SimpleNamespace(humanizer=False, email_rules=email_rules),
        compliance=compliance or ComplianceConfig(),
    )


def writer(brain, state, rules="", **kw):
    w = Writer(brain, state, config(**kw))
    w.skills = ""          # run() loads these; the tests call the prompt builders directly
    w.email_rules = rules
    return w


@pytest_asyncio.fixture
async def state(tmp_path):
    s = StateManager(str(tmp_path / "w.db"))
    await s.init_db()
    return s


async def prospect(state, first="Ana"):
    pid = await state.add_prospect(Prospect(
        first_name=first, last_name="Doe", title="Owner", company=f"{first} Co",
        industry="Roofing", email=f"{first.lower()}@x.test", email_status="verified"))
    return await state.get_prospect(pid)


async def personal_prompt(state, rules="", **kw):
    brain = RecordingBrain({"subject": "hi", "body": "Hi"})
    await writer(brain, state, rules, **kw)._write_personal_email(await prospect(state))
    return brain.prompts[0]


async def sequence_prompt(state, rules="", **kw):
    brain = RecordingBrain(SEQ)
    await writer(brain, state, rules, provider="instantly", **kw)._write_sequence(
        [await prospect(state)])
    return brain.prompts[0]


# ── The setting and its file ──

def test_off_by_default_and_blank_means_off():
    assert WriterConfig().email_rules == ""
    assert WriterConfig(email_rules=None).email_rules == ""
    assert WriterConfig(email_rules="  skills/email_rules.md ").email_rules == "skills/email_rules.md"
    assert load_email_rules(config()) == ""
    assert load_email_rules(SimpleNamespace()) == ""


def test_a_relative_path_is_read_from_the_harvey_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "PROJECT_ROOT", tmp_path)
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "email_rules.md").write_text(f"\n{RULES}\n", encoding="utf-8")
    assert load_email_rules(config(email_rules="skills/email_rules.md")) == RULES


def test_an_absolute_path_is_read_as_is(tmp_path):
    path = tmp_path / "rules.md"
    path.write_text(RULES, encoding="utf-8")
    assert load_email_rules(config(email_rules=str(path))) == RULES


@pytest.mark.parametrize("content, problem", [
    (None, "doesn't exist"),
    ("  \n", "is empty"),
    ("x" * (MAX_EMAIL_RULES_CHARS + 1), "the limit is"),
])
def test_a_file_that_cant_be_used_is_an_error(tmp_path, content, problem):
    path = tmp_path / "rules.md"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    with pytest.raises(EmailRulesError, match=problem):
        load_email_rules(config(email_rules=str(path)))


def test_a_folder_is_an_error_not_a_crash(tmp_path):
    with pytest.raises(EmailRulesError, match="can't be read"):
        load_email_rules(config(email_rules=str(tmp_path)))


def test_the_setting_must_be_text():
    with pytest.raises(ValidationError):
        WriterConfig(email_rules=["a.md"])


# ── The prompts ──

@pytest.mark.asyncio
async def test_rules_replace_the_built_in_shape_of_the_first_email(state):
    prompt = await personal_prompt(state, RULES)
    assert "OPERATOR'S EMAIL RULES" in prompt and RULES in prompt
    for built_in in ("Under 75 words", "No pitch, no product name", "lowercase, 2-5 words"):
        assert built_in not in prompt
    # The rules come after everything they override, right before the task.
    assert prompt.index(RULES) < prompt.index("Write ONE cold email")


@pytest.mark.asyncio
async def test_rules_never_lift_the_facts_rule_or_the_untrusted_rule(state):
    prompt = await personal_prompt(state, RULES)
    assert "never override the FACTS rule" in prompt
    assert "Every claim about the prospect in your email must come from these" in prompt
    assert UNTRUSTED_RULE in prompt
    assert "no merge variables" in prompt
    assert 'Return ONLY JSON: {"subject": "...", "body": "..."}' in prompt


@pytest.mark.asyncio
async def test_rules_replace_the_built_in_shape_of_the_sequence(state):
    prompt = await sequence_prompt(state, RULES)
    assert RULES in prompt
    for built_in in ("Under 75 words", "Under 40 words", "Break-up email", "No pitch",
                     "Subject lines: lowercase", "Never be pushy"):
        assert built_in not in prompt
    # Three emails, their timing and the output format don't change.
    assert "email 2 a follow-up 3 days later" in prompt
    assert '{"step": 3, "subject": "...", "body": "...", "delay_days": 4}' in prompt
    assert "Use {{first_name}}, {{company}}, {{title}} as merge variables" in prompt


@pytest.mark.asyncio
async def test_without_rules_the_prompts_keep_the_built_in_shape(state):
    personal = await personal_prompt(state)
    sequence = await sequence_prompt(state)
    assert "OPERATOR'S EMAIL RULES" not in personal + sequence
    assert "Under 75 words. One specific observation" in personal
    assert "Email 3: Break-up email 4 days after that. Under 40 words." in sequence
    assert BUILT_IN_FOOTER in personal and BUILT_IN_FOOTER in sequence


@pytest.mark.asyncio
async def test_with_an_opt_out_line_set_the_draft_gets_no_second_one(state):
    compliance = ComplianceConfig(opt_out_line='Reply "no thanks" and I won\'t write again.')
    for prompt in (await personal_prompt(state, RULES, compliance=compliance),
                   await sequence_prompt(state, RULES, compliance=compliance)):
        assert 'Harvey adds this opt-out line under the email when it sends: "Reply "no thanks"' in prompt
        assert "Don't write another way to opt out, even if the operator's rules give one." in prompt
        assert BUILT_IN_FOOTER not in prompt


@pytest.mark.asyncio
async def test_with_no_opt_out_line_the_operators_own_sentence_is_written(state):
    compliance = ComplianceConfig(opt_out_line="")
    prompt = await personal_prompt(state, RULES, compliance=compliance)
    assert "Harvey adds no opt-out line." in prompt
    assert "write it into the body as they say" in prompt
    assert "opt-out line under the email" not in prompt


@pytest.mark.asyncio
async def test_the_postal_address_is_only_mentioned_when_harvey_adds_one(state):
    without = await personal_prompt(state, RULES, compliance=ComplianceConfig())
    with_address = await personal_prompt(
        state, RULES, compliance=ComplianceConfig(postal_address="1 Example St, Springfield"))
    assert "postal address" not in without.lower()
    assert "No postal address: Harvey adds it under the email when it sends." in with_address


# ── A run ──

@pytest.mark.asyncio
async def test_a_run_with_unusable_rules_writes_nothing(state, tmp_path, caplog):
    await prospect(state)
    brain = RecordingBrain(SEQ)
    w = Writer(brain, state, config(email_rules=str(tmp_path / "gone.md")))
    with caplog.at_level(logging.WARNING, logger="harvey.writer"):
        await w.run()
    assert brain.prompts == []
    assert await state.get_campaigns_by_status("draft") == []
    assert "writer.email_rules" in caplog.text and "doesn't exist" in caplog.text


@pytest.mark.asyncio
async def test_a_run_puts_the_rules_in_every_prompt(state, tmp_path):
    path = tmp_path / "rules.md"
    path.write_text(RULES, encoding="utf-8")
    await prospect(state)
    brain = RecordingBrain(SEQ)
    await Writer(brain, state, config(provider="instantly", email_rules=str(path))).run()
    assert brain.prompts and all(RULES in p for p in brain.prompts)
