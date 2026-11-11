"""Designing the first-email offer: suggestions, a preview, saving it."""

from types import SimpleNamespace

import pytest
import yaml

from sellbird.offer import save_offer, suggest_offers


def _config():
    return SimpleNamespace(
        product=SimpleNamespace(name="Brightline", description="Dental websites", pricing="$6,500",
                                key_benefits=["Online booking"], offer=SimpleNamespace(entry="", entry_ideas=[])),
        icp=SimpleNamespace(titles=["Practice owner"], industries=["Dental"], company_size="1-20"))


class Brain:
    def __init__(self, answer):
        self.answer, self.prompts = answer, []

    async def think_json(self, prompt, **kw):
        self.prompts.append(prompt)
        return self.answer


@pytest.mark.asyncio
async def test_suggestions_come_with_their_finding_and_why():
    brain = Brain([{"offer": "a homepage mockup with booking", "finding": "no online booking", "why": "they see it"},
                   {"offer": ""}, "junk"])
    ideas = await suggest_offers(brain, _config())
    assert ideas == [{"offer": "a homepage mockup with booking", "finding": "no online booking", "why": "they see it"}]
    assert "never a discount" in brain.prompts[0] and "Dental websites" in brain.prompts[0]


@pytest.mark.asyncio
async def test_suggestions_survive_a_wrapped_answer():
    ideas = await suggest_offers(Brain({"offers": [{"offer": "a ranking report"}]}), _config())
    assert [i["offer"] for i in ideas] == ["a ranking report"]


def test_saving_writes_the_offer_and_keeps_everything_else(tmp_path):
    f = tmp_path / "sellbird.local.yaml"
    f.write_text("persona:\n  name: Maya\nproduct:\n  name: Brightline\n  offer:\n    goal: book_call\n")
    save_offer(f, "  a written plan   for page one ", ["a plan", "", "a mockup"])
    data = yaml.safe_load(f.read_text())
    assert data["product"]["offer"] == {"goal": "book_call", "entry": "a written plan for page one",
                                        "entry_ideas": ["a plan", "a mockup"]}
    assert data["persona"]["name"] == "Maya"
    assert (tmp_path / "sellbird.local.yaml.bak").read_text().startswith("persona:")
