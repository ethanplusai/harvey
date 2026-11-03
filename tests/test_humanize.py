"""The Humanizer pass rewrites drafts but never lets a bad rewrite through."""

import pytest

from sellbird.config import SellbirdConfig
from sellbird.humanize import humanize, humanizer_enabled, load_skill


class FakeBrain:
    """Returns ``reply`` for every call, or the next item of ``replies``."""

    def __init__(self, reply=None, replies=None):
        self.reply = reply
        self.replies = list(replies or [])
        self.prompts = []

    async def think_json(self, prompt, **kwargs):
        self.prompts.append(prompt)
        reply = self.replies.pop(0) if self.replies else self.reply
        if isinstance(reply, Exception):
            raise reply
        return reply


@pytest.fixture
def skill(tmp_path):
    path = tmp_path / "SKILL.md"
    path.write_text("---\nname: humanizer\n---\n\n# Humanizer\nCut the tells.\n")
    return path


DRAFT = {"subject": "missed calls", "body": "Hi {{first_name}}, it's not about calls. It's about jobs. Who answers after five?"}


def test_load_skill_strips_frontmatter(skill):
    assert load_skill(skill) == "# Humanizer\nCut the tells."


def test_missing_skill_is_a_no_op(tmp_path):
    assert load_skill(tmp_path / "nope.md") == ""


@pytest.mark.asyncio
async def test_no_skill_makes_no_call(tmp_path):
    brain = FakeBrain([])
    assert await humanize(brain, [DRAFT], "writer", path=tmp_path / "nope.md", enabled=True) == [DRAFT]
    assert brain.prompts == []


@pytest.mark.asyncio
async def test_good_rewrite_is_used(skill):
    rewrite = {"subject": "after five", "body": "Hi {{first_name}}, who picks up the phone after five?"}
    brain = FakeBrain(rewrite)
    out = await humanize(brain, [DRAFT], "writer", path=skill, enabled=True)
    assert out == [rewrite]
    assert "Cut the tells." in brain.prompts[0]


@pytest.mark.parametrize("bad", [
    {"subject": "after five", "body": "Hi there, who picks up after five?"},           # lost merge var
    {"subject": "after five", "body": "Hi {{first_name}} — who picks up after five?"},  # new em dash
    {"subject": "", "body": "Hi {{first_name}}, who picks up after five?"},            # dropped subject
    {"subject": "after five", "body": "Hi {{first_name}}, " + "word " * 40},          # much longer
    {"subject": "after five", "body": "As an AI, I rewrote it: Hi {{first_name}}, who picks up?"},  # meta-text
    "not a dict",
])
@pytest.mark.asyncio
async def test_bad_rewrite_keeps_original(skill, bad):
    out = await humanize(FakeBrain(bad), [DRAFT], "writer", path=skill, enabled=True)
    assert out == [DRAFT]


@pytest.mark.asyncio
async def test_wrong_shape_or_failure_keeps_all(skill):
    assert await humanize(FakeBrain([]), [DRAFT], "writer", path=skill, enabled=True) == [DRAFT]
    assert await humanize(FakeBrain([DRAFT, DRAFT]), [DRAFT], "writer", path=skill, enabled=True) == [DRAFT]
    assert await humanize(FakeBrain(None), [DRAFT], "writer", path=skill, enabled=True) == [DRAFT]
    assert await humanize(FakeBrain(RuntimeError("down")), [DRAFT], "writer", path=skill, enabled=True) == [DRAFT]


@pytest.mark.asyncio
async def test_reply_without_subject(skill):
    reply = {"subject": "", "body": "Sure, Thursday works. What time suits you?"}
    out = await humanize(FakeBrain(reply), [{"subject": "", "body": "Sure! Thursday works for me. What time suits you best?"}], "handler", path=skill, enabled=True)
    assert out == [reply]


@pytest.mark.asyncio
async def test_installed_skill_alone_does_not_turn_it_on(skill):
    # Installing the skill for Claude Code must not change what Sellbird sends.
    brain = FakeBrain({"subject": "after five", "body": "Hi {{first_name}}, who picks up?"})
    assert await humanize(brain, [DRAFT], "writer", path=skill) == [DRAFT]
    assert brain.prompts == []


def _config(**extra):
    base = {
        "persona": {
            "name": "H", "company": "A", "role": "B",
            "email": "h@a.com", "linkedin": "", "tone": "casual",
        },
        "product": {
            "name": "P", "description": "D", "pricing": "$1",
            "key_benefits": ["x"], "objection_responses": {},
        },
        "icp": {
            "industries": ["SaaS"], "company_size": "10-50",
            "titles": ["CEO"], "geography": ["US"],
        },
    }
    return SellbirdConfig(**{**base, **extra})


def test_humanizer_is_off_by_default():
    assert humanizer_enabled(_config()) is False
    assert humanizer_enabled(None) is False


def test_humanizer_opt_in_from_config():
    assert humanizer_enabled(_config(writer={"humanizer": True})) is True


@pytest.mark.asyncio
async def test_each_draft_is_rewritten_on_its_own(skill):
    # One call per draft: a model can't hand one prospect's rewrite (with a
    # real name and company in it) to another prospect by reordering.
    alice = {"subject": "phones at beta", "body": "Hi Alice, who answers the phones at Beta Plumbing after five?"}
    bob = {"subject": "phones at acme", "body": "Hi Bob, who answers the phones at Acme Roofing after five?"}
    alice2 = {"subject": "beta after five", "body": "Hi Alice, who picks up at Beta Plumbing after five?"}
    bob2 = {"subject": "acme after five", "body": "Hi Bob, who picks up at Acme Roofing after five?"}
    brain = FakeBrain(replies=[alice2, bob2])
    out = await humanize(brain, [alice, bob], "writer", path=skill, enabled=True)
    assert out == [alice2, bob2]
    assert len(brain.prompts) == 2
    assert "Alice" in brain.prompts[0] and "Bob" not in brain.prompts[0]
    assert "Bob" in brain.prompts[1] and "Alice" not in brain.prompts[1]


@pytest.mark.asyncio
async def test_rewrite_may_not_grow_past_the_draft(skill):
    # A 39-word break-up email can't come back over the writer's 40-word cap.
    draft = {"subject": "closing the loop", "body": " ".join(["word"] * 39)}
    longer = {"subject": "closing the loop", "body": " ".join(["word"] * 45)}
    assert await humanize(FakeBrain(longer), [draft], "writer", path=skill, enabled=True) == [draft]
