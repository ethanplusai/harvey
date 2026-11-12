"""writer.require_tip: no draft for a prospect with nothing to build a tip on.

A first email is meant to name one concrete thing the sender would change.
With no finding about the company, the model could only say it had no advice,
which is a poor cold email. With the option on, such a prospect is parked as
"no_tip" (not left "new", which would make the heartbeat retry it every cycle)
and no model call is made. Off by default.
"""

import logging
from types import SimpleNamespace

import pytest

from sellbird.agents.writer import NO_TIP_STATUS, Writer
from sellbird.config import WriterConfig
from sellbird.models.company import Company
from sellbird.models.prospect import Prospect
from sellbird.signals import seed_signal_catalog
from tests.test_writer_retry import GOOD, FakeBrain, state, status  # noqa: F401  (state is a fixture)


def config(require_tip=None, site_facts=None):
    writer = {}
    if require_tip is not None:
        writer["require_tip"] = require_tip
    if site_facts is not None:
        writer["site_facts"] = site_facts
    return SimpleNamespace(
        channels=SimpleNamespace(email=SimpleNamespace(provider="instantly", require_approval=False)),
        product=SimpleNamespace(name="P", description="D", key_benefits=[], pricing=""),
        persona=SimpleNamespace(name="S", company="C", role="R", tone="t"),
        writer=SimpleNamespace(humanizer=False, email_rules="", **writer),
    )


async def add(state, n, notes="", company_id=""):
    return await state.add_prospect(Prospect(
        first_name=f"P{n}", last_name="Doe", title="Owner", company=f"Co{n}",
        industry="Bakery", email=f"p{n}@co{n}.example", email_status="verified",
        personalization_notes=notes, company_id=company_id,
    ))


async def company(state, n, schema_missing=False, signals=None):
    await seed_signal_catalog(state)
    cid = await state.add_company(Company(name=f"Co{n}", domain=f"co{n}.example"))
    if schema_missing:
        await state.add_observation(
            company_id=cid, signal_code="NO_SCHEMA_MARKUP", collector="profile", value_num=1)
    if signals:
        await state.update_company_signals(cid, new_signals=signals)
    return cid


def test_the_option_is_off_by_default():
    assert WriterConfig().require_tip is False
    assert WriterConfig(require_tip=True).require_tip is True


@pytest.mark.asyncio
async def test_off_it_writes_for_a_prospect_with_nothing_to_go_on(state):
    pid = await add(state, 1)
    await Writer(FakeBrain(reply=GOOD), state, config(), None).run()
    assert await status(state, pid) == "queued"


@pytest.mark.asyncio
async def test_on_a_prospect_with_nothing_concrete_is_parked_without_a_model_call(state, caplog):
    pid = await add(state, 1)
    brain = FakeBrain(reply=GOOD)
    caplog.set_level(logging.INFO, logger="sellbird.writer")
    await Writer(brain, state, config(require_tip=True), None).run()
    assert brain.calls == 0
    assert await status(state, pid) == NO_TIP_STATUS == "no_tip"
    assert "Not writing for 1 prospect(s)" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("basis", ["notes", "site_finding", "signal"])
async def test_on_a_prospect_with_something_concrete_is_written_for(state, basis):
    if basis == "notes":
        pid = await add(state, 1, notes="Opened a second shop last spring.")
    elif basis == "site_finding":
        pid = await add(state, 1, company_id=await company(state, 1, schema_missing=True))
    else:
        pid = await add(state, 1, company_id=await company(
            state, 1, signals=[{"type": "hiring", "detail": "Hiring a web developer"}]))
    await Writer(FakeBrain(reply=GOOD), state, config(require_tip=True), None).run()
    assert await status(state, pid) == "queued"


@pytest.mark.asyncio
async def test_only_the_prospect_without_a_basis_is_parked(state):
    bare = await add(state, 1)
    grounded = await add(state, 2, notes="Sponsors a local team.")
    await Writer(FakeBrain(reply=GOOD), state, config(require_tip=True), None).run()
    assert await status(state, bare) == "no_tip"
    assert await status(state, grounded) == "queued"


@pytest.mark.asyncio
async def test_a_finding_does_not_count_when_site_facts_is_off(state):
    pid = await add(state, 1, company_id=await company(state, 1, schema_missing=True))
    brain = FakeBrain(reply=GOOD)
    await Writer(brain, state, config(require_tip=True, site_facts=False), None).run()
    assert brain.calls == 0
    assert await status(state, pid) == "no_tip"


# ── Seeing and undoing a no_tip skip ──

@pytest.mark.asyncio
async def test_skipped_prospects_can_be_put_back_in_line(state):
    from tests.test_writer_retry import add
    pid = await add(state, 7)
    await state.update_prospect_status(pid, NO_TIP_STATUS)
    assert await state.retry_no_tip() == 1
    assert await status(state, pid) == "new"
    assert await state.retry_no_tip() == 0


def test_retry_writes_no_tip_only_touches_skipped_prospects(state, capsys, monkeypatch):
    import argparse
    import asyncio
    from sellbird import cli
    from tests.test_writer_retry import add

    async def seed():
        skipped = await add(state, 8)
        failed = await add(state, 9)
        await state.update_prospect_status(skipped, NO_TIP_STATUS)
        await state.update_prospect_status(failed, "write_failed")
        return skipped, failed

    skipped, failed = asyncio.run(seed())
    monkeypatch.setattr("sellbird.state.StateManager", lambda *a, **k: state)
    cli.cmd_retry_writes(argparse.Namespace(no_tip=True))
    assert "1 prospect(s) are back in line" in capsys.readouterr().out
    assert asyncio.run(status(state, skipped)) == "new"
    assert asyncio.run(status(state, failed)) == "write_failed"


def test_the_dashboard_has_a_label_for_no_tip():
    from pathlib import Path
    app = (Path(__file__).resolve().parents[1] / "sellbird" / "web" / "app.js").read_text(encoding="utf-8")
    assert f"  {NO_TIP_STATUS}:" in app.split("const STATUS = {", 1)[1].split("};", 1)[0]
