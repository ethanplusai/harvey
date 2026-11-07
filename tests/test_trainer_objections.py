
from types import SimpleNamespace

import pytest

from sellbird.trainer import Trainer


VALID = {
    "Too expensive": "Let's compare the return on investment. What budget are you working with?",
    "Already using a competitor": "What would you improve about your current solution?",
}


class FakeBrain:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0
        self.last_reply_empty = False
        self.prompts = []

    async def think_json(self, prompt, **kwargs):
        self.calls += 1
        self.prompts.append(prompt)

        result, is_empty = self.outcomes.pop(0)
        self.last_reply_empty = is_empty
        return result


def make_trainer(outcomes):
    trainer = Trainer.__new__(Trainer)
    trainer.brain = FakeBrain(outcomes)
    return trainer


@pytest.mark.asyncio
async def test_valid_objections_are_accepted():
    trainer = make_trainer([(VALID, False)])

    result = await trainer._generate_objections({}, {})

    assert result == VALID
    assert trainer.brain.calls == 1


@pytest.mark.asyncio
async def test_empty_objections_are_retried():
    trainer = make_trainer([
        ({}, False),
        (VALID, False),
    ])

    result = await trainer._generate_objections({}, {})

    assert result == VALID
    assert trainer.brain.calls == 2


@pytest.mark.asyncio
async def test_invalid_objection_pairs_are_rejected():
    trainer = make_trainer([
        ({"Too expensive": "", "": "Invalid response"}, False),
        (VALID, False),
    ])

    result = await trainer._generate_objections({}, {})

    assert result == VALID
    assert trainer.brain.calls == 2


@pytest.mark.asyncio
async def test_retries_stop_after_invalid_responses():
    trainer = make_trainer([
        ({}, False),
        ({}, False),
        ({}, False),
    ])

    result = await trainer._generate_objections({}, {})

    assert result is None
    assert trainer.brain.calls == 3


@pytest.mark.asyncio
async def test_provider_outage_does_not_trigger_retries():
    trainer = make_trainer([
        (None, True),
    ])

    result = await trainer._generate_objections({}, {})

    assert result is None
    assert trainer.brain.calls == 1


@pytest.mark.asyncio
async def test_retry_prompt_explains_required_json_format():
    trainer = make_trainer([
        ({}, False),
        (VALID, False),
    ])

    await trainer._generate_objections({}, {})

    assert len(trainer.brain.prompts) == 2
    assert trainer.brain.prompts[1] != trainer.brain.prompts[0]
