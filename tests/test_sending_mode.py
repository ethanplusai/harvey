"""Approval or autopilot: which one you're in, what it means, and switching.

Approval (the default): every email waits in the Outbox for you. Autopilot
(channels.email.require_approval: false): Harvey sends on its own, except
what gate.autopilot_review holds. The dashboard shows the mode and can
switch it. Turning autopilot on takes a typed confirmation. Turning it off
works at once: nothing autopilot approved goes out after that.
"""

import asyncio
import shutil
import sqlite3
from pathlib import Path

import pytest
import pytest_asyncio

from harvey.config import ConfigError, load_config, refresh_sending_mode, set_require_approval
from harvey.paths import PROJECT_ROOT
from harvey.state import MIGRATIONS, StateManager
from tests.test_dashboard_signals import client  # noqa: F401  (fixture)


@pytest.fixture
def yaml_file(tmp_path):
    path = tmp_path / "harvey.yaml"
    shutil.copy(PROJECT_ROOT / "harvey.yaml", path)
    return path


# ── the setting in the file ──


def test_switching_changes_one_line_and_keeps_your_comments(yaml_file):
    before = yaml_file.read_text().splitlines()
    set_require_approval(False, yaml_file)
    after = yaml_file.read_text().splitlines()
    changed = [(a, b) for a, b in zip(before, after) if a != b]
    assert len(before) == len(after) and len(changed) == 1
    old, new = changed[0]
    assert "require_approval: true" in old and "require_approval: false" in new
    assert old.split("#", 1)[1] == new.split("#", 1)[1]          # the comment stays
    assert load_config(str(yaml_file)).channels.email.require_approval is False
    set_require_approval(True, yaml_file)
    assert yaml_file.read_text().splitlines() == before


def test_a_file_without_the_line_gets_it_under_email(tmp_path, yaml_file):
    text = "\n".join(l for l in yaml_file.read_text().splitlines() if "require_approval:" not in l)
    path = tmp_path / "trained.yaml"
    path.write_text(text + "\n")
    set_require_approval(False, path)
    assert load_config(str(path)).channels.email.require_approval is False
    assert path.read_text().count("require_approval:") == 1


def test_a_file_it_cannot_change_safely_is_left_alone(tmp_path):
    path = tmp_path / "odd.yaml"
    path.write_text("channels: {email: {provider: gmail}}\n")
    with pytest.raises(ConfigError):
        set_require_approval(False, path)
    assert path.read_text() == "channels: {email: {provider: gmail}}\n"


def test_the_running_loop_picks_up_a_change(yaml_file):
    config = load_config(str(yaml_file))
    assert refresh_sending_mode(config, yaml_file) is False
    set_require_approval(False, yaml_file)
    assert refresh_sending_mode(config, yaml_file) is True
    assert config.channels.email.require_approval is False


# ── the outbox remembers who approved ──


@pytest_asyncio.fixture
async def state(tmp_path):
    s = StateManager(str(tmp_path / "m.db"))
    await s.init_db()
    return s


_n = iter(range(10**6))


async def _row(state, status):
    from tests.test_outbox_native import seed_prospect, _now_iso
    email = f"p{next(_n)}@acme.com"
    pid = await seed_prospect(state, email=email, status="replied")
    return await state.add_outbox_item(prospect_id=pid, to_email=email,
                                       subject="s", body="Fine body. Question?", kind="reply",
                                       send_at=_now_iso(), status=status,
                                       approved_by="autopilot" if status == "approved" else "")


@pytest.mark.asyncio
async def test_autopilot_rows_are_marked_as_such(state):
    auto = await _row(state, "approved")
    waiting = await _row(state, "pending_review")
    assert (await state.get_outbox_item(auto))["approved_by"] == "autopilot"
    assert (await state.get_outbox_item(waiting))["approved_by"] == ""
    await state.approve_outbox(waiting)
    assert (await state.get_outbox_item(waiting))["approved_by"] == "you"
    assert await state.requeue_autopilot_approved() == 1
    item = await state.get_outbox_item(auto)
    assert item["status"] == "pending_review" and "autopilot" in item["error"]
    assert (await state.get_outbox_item(waiting))["status"] == "approved"   # yours stays


@pytest.mark.asyncio
async def test_rows_approved_before_the_upgrade_count_as_approved(tmp_path):
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    for script in MIGRATIONS[:12]:
        conn.executescript(script)
    conn.execute("INSERT INTO outbox (id, prospect_id, kind, step, to_email, subject, body, status, send_at) "
                 "VALUES ('o1', 'p', 'sequence', 2, 'a@x.test', 's', 'b', 'approved', '2026-11-01T09:00:00')")
    conn.execute("PRAGMA user_version = 12")
    conn.commit()
    conn.close()
    s = StateManager(str(db))
    await s.init_db()
    assert (await s.get_outbox_item("o1"))["approved_by"] == "earlier"


# ── sending respects the mode ──


@pytest.mark.asyncio
async def test_with_approval_back_on_autopilot_rows_wait_and_yours_go(state):
    from tests.test_outbox_native import FakeProvider, make_sender
    auto = await _row(state, "approved")
    mine = await _row(state, "pending_review")
    await state.approve_outbox(mine)
    old = await _row(state, "pending_review")
    with sqlite3.connect(state.db_path) as db:
        db.execute("UPDATE outbox SET status = 'approved', approved_by = 'earlier' WHERE id = ?", (old,))
    provider = FakeProvider()
    await make_sender(state, provider, require_approval=True)._drain_due()
    assert len(provider.sent) == 2                                  # yours and the pre-upgrade one
    item = await state.get_outbox_item(auto)
    assert item["status"] == "pending_review" and "autopilot" in item["error"]


# ── the dashboard ──


@pytest.fixture
def dash_yaml(client, yaml_file, monkeypatch):
    import harvey.dashboard as dash
    monkeypatch.setattr(dash, "_resolve_config_file", lambda: yaml_file)
    return yaml_file


def test_the_dashboard_shows_the_mode(client, dash_yaml):
    asyncio.run(StateManager(str(client.db_path)).init_db())
    data = client.get("/api/sending-mode").json()
    assert data["mode"] == "approval" and data["file"] == "harvey.yaml"
    assert "provider" in data and "your_domains" in data and data["held"] == 0


def test_turning_autopilot_on_needs_the_word(client, dash_yaml):
    asyncio.run(StateManager(str(client.db_path)).init_db())
    for body in ({"mode": "autopilot"}, {"mode": "autopilot", "confirm": "yes"}, {"mode": "fast"}, [1], None):
        assert client.post("/api/sending-mode", json=body).status_code == 400
    assert load_config(str(dash_yaml)).channels.email.require_approval is True
    r = client.post("/api/sending-mode", json={"mode": "autopilot", "confirm": "autopilot"})
    assert r.status_code == 200 and r.json()["mode"] == "autopilot"
    assert load_config(str(dash_yaml)).channels.email.require_approval is False


def test_turning_it_off_is_one_click_and_holds_what_autopilot_queued(client, dash_yaml):
    st = StateManager(str(client.db_path))
    asyncio.run(st.init_db())
    set_require_approval(False, dash_yaml)
    asyncio.run(_row(st, "approved"))
    r = client.post("/api/sending-mode", json={"mode": "approval"})
    assert r.status_code == 200 and r.json() == {"success": True, "mode": "approval", "requeued": 1}
    assert load_config(str(dash_yaml)).channels.email.require_approval is True


@pytest.mark.asyncio
async def test_each_autopilot_path_marks_what_it_queues(state, monkeypatch):
    """Writer first emails, sender staging and handler replies: approved on
    autopilot means approved_by 'autopilot'; with approval on, nothing."""
    from tests.test_outbox_native import (Cfg, FakeProvider, InboundMessage, make_handler,
                                          make_sender, seed_campaign, seed_prospect)
    monkeypatch.setattr(Cfg.channels.email, "require_approval", False)
    pid = await seed_prospect(state, status="new")
    campaign = await seed_campaign(state, [pid])
    await make_sender(state, FakeProvider(), require_approval=False)._stage_campaign_native(campaign)
    replier = await seed_prospect(state, email="r@acme.com", status="contacted")
    provider = FakeProvider()
    provider.inbound = [InboundMessage(provider_id="m1", from_email="r@acme.com",
                                       subject="Re: hi", body="What does it cost?")]
    await make_handler(state, provider)._run_native()
    rows = await state.get_outbox()
    assert rows and {r["approved_by"] for r in rows} == {"autopilot"}
    assert {r["prospect_id"] for r in rows} == {pid, replier}


@pytest.mark.asyncio
@pytest.mark.parametrize("require_approval,status,approved_by", [
    (False, "approved", "autopilot"), (True, "pending_review", ""),
])
async def test_the_writers_first_emails_are_marked_too(state, require_approval, status, approved_by):
    from tests.test_writer_feedback import RecordingBrain, prospect, writer
    pid = await prospect(state, "Ana")
    w = writer(RecordingBrain({"subject": "hi", "body": "Hi Ana, who answers the phones?"}), state, "gmail")
    w.config.channels.email.require_approval = require_approval
    await w._personalize_first_emails("c1", [await state.get_prospect(pid)])
    [row] = await state.get_outbox()
    assert (row["status"], row["approved_by"]) == (status, approved_by)


def _render(func, mode, armed=False):
    import json
    import re
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    source = (PROJECT_ROOT / "harvey" / "web" / "app.js").read_text(encoding="utf-8")
    parts = [re.search(rf"^function {name}\(.*?^}}\n", source, re.S | re.M).group(0)
             for name in ("escHtml", "sendingModeBanner", "sendingModeCard")]
    i18n = (PROJECT_ROOT / "harvey" / "web" / "i18n.js").read_text(encoding="utf-8")  # t()
    script = (i18n + "\nconst icon = () => '';\n" + "\n".join(parts) +
              f"\nlet _modeBusy = false, _autopilotArmed = {json.dumps(armed)};"
              f"\nprocess.stdout.write({func}({json.dumps(mode)}));")
    return subprocess.run([node, "-e", script], capture_output=True, text=True, encoding="utf-8",
                          check=True, timeout=30).stdout


MODE = {"mode": "approval", "provider": "gmail", "file": "harvey.local.yaml",
        "your_domains": ["acme.com", "<b>x</b>"], "booking_page": "cal.com/sam/15", "held": 0}


def test_the_outbox_says_which_mode_and_offers_the_way_out():
    approval = _render("sendingModeBanner", MODE)
    assert "Approval mode" in approval and "waits here until you approve it" in approval
    auto = _render("sendingModeBanner", {**MODE, "mode": "autopilot", "held": 2})
    assert "Autopilot is on" in auto and "(2 now)" in auto and "Turn off autopilot" in auto
    instantly = _render("sendingModeBanner", {**MODE, "provider": "instantly"})
    assert "campaigns aren’t sent to Instantly" in instantly


def test_settings_explains_both_and_asks_for_the_word():
    card = _render("sendingModeCard", MODE)
    assert "Sending mode: Approval" in card and "<b>Autopilot</b>: Harvey sends on its own" in card
    assert "acme.com, &lt;b&gt;x&lt;/b&gt;" in card and "harvey.local.yaml" in card
    assert "Turn on autopilot…" in card and 'id="autopilot-go"' not in card
    armed = _render("sendingModeCard", MODE, armed=True)
    assert 'id="autopilot-confirm"' in armed and 'id="autopilot-go" disabled' in armed
    auto = _render("sendingModeCard", {**MODE, "mode": "autopilot"})
    assert "Turn off autopilot" in auto and "works at once" in auto


# ── review of the switch: no autopilot email after "off" ──


@pytest.mark.asyncio
async def test_off_mid_cycle_stops_what_this_cycle_queues_and_sends(state, yaml_file):
    """The running loop loaded autopilot. The switch goes off mid-cycle:
    the drain and the next row queued both see it, without waiting a cycle."""
    from tests.test_outbox_native import FakeProvider
    from harvey.agents.sender import Sender
    set_require_approval(False, yaml_file)
    config = load_config(str(yaml_file))                 # what `harvey run` holds
    assert config.channels.email.require_approval is False
    row = await _row(state, "approved")                  # queued on autopilot
    set_require_approval(True, yaml_file)                # the click, mid-cycle
    from tests.test_outbox_native import Env
    sender = Sender(brain=None, state=state, config=config, env=Env())
    sender.provider, sender.send_pacing = FakeProvider(), False
    await sender._drain_due()
    assert sender.provider.sent == []
    assert (await state.get_outbox_item(row))["status"] == "pending_review"
    assert config.channels.email.require_approval is True


def test_a_config_not_loaded_from_a_file_is_never_refreshed(monkeypatch):
    from tests.test_outbox_native import Cfg
    monkeypatch.setattr(Cfg.channels.email, "require_approval", False)
    cfg = Cfg()                     # the repo's harvey.yaml says true: must not be read
    assert refresh_sending_mode(cfg) is False
    assert cfg.channels.email.require_approval is False


@pytest.mark.asyncio
async def test_off_also_brings_back_rows_from_before_the_upgrade(state):
    row = await _row(state, "approved")
    with sqlite3.connect(state.db_path) as db:
        db.execute("UPDATE outbox SET approved_by = 'earlier' WHERE id = ?", (row,))
    assert await state.requeue_autopilot_approved() == 1


@pytest.mark.asyncio
async def test_the_loop_noticing_off_brings_autopilot_rows_back(state, yaml_file):
    from harvey.sending_mode import sync_sending_mode
    set_require_approval(False, yaml_file)
    config = load_config(str(yaml_file))
    row = await _row(state, "approved")
    set_require_approval(True, yaml_file)                 # edited by hand, say
    assert await sync_sending_mode(config, state) is True
    assert (await state.get_outbox_item(row))["status"] == "pending_review"


def test_the_writer_keeps_a_symlinked_config_a_symlink(tmp_path, yaml_file):
    real = tmp_path / "real.yaml"
    real.write_bytes(yaml_file.read_bytes())
    link = tmp_path / "harvey.local.yaml"
    link.symlink_to(real)
    set_require_approval(False, link)
    assert link.is_symlink()
    assert load_config(str(real)).channels.email.require_approval is False


def test_a_file_that_isnt_utf8_is_refused_not_mangled(tmp_path, yaml_file):
    path = tmp_path / "latin.yaml"
    path.write_bytes(yaml_file.read_bytes().replace(b"# native", "# café native".encode("latin-1")))
    before = path.read_bytes()
    with pytest.raises(ConfigError):
        set_require_approval(False, path)
    assert path.read_bytes() == before


def test_a_byte_order_mark_is_kept(tmp_path, yaml_file):
    path = tmp_path / "bom.yaml"
    path.write_bytes(b"\xef\xbb\xbf" + yaml_file.read_bytes())
    set_require_approval(False, path)
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")


@pytest.mark.asyncio
async def test_a_hand_edit_back_to_approval_noticed_by_the_sender_still_holds_everything(state, yaml_file):
    """Third review: the sender noticed the change first, so the loop never
    requeued and a pre-upgrade row went out."""
    from tests.test_outbox_native import Env, FakeProvider
    from harvey.agents.sender import Sender
    set_require_approval(False, yaml_file)
    config = load_config(str(yaml_file))
    row = await _row(state, "approved")
    with sqlite3.connect(state.db_path) as db:
        db.execute("UPDATE outbox SET approved_by = 'earlier' WHERE id = ?", (row,))
    yaml_file.write_text(yaml_file.read_text().replace("require_approval: false", "require_approval: true"))
    sender = Sender(brain=None, state=state, config=config, env=Env())
    sender.provider, sender.send_pacing = FakeProvider(), False
    await sender._drain_due()
    assert sender.provider.sent == []
    assert (await state.get_outbox_item(row))["status"] == "pending_review"


def test_a_change_that_keeps_the_old_timestamp_is_still_seen(yaml_file):
    import os
    set_require_approval(False, yaml_file)
    config = load_config(str(yaml_file))
    refresh_sending_mode(config)                                  # cached
    before = os.stat(yaml_file)
    yaml_file.write_text(yaml_file.read_text().replace("require_approval: false", "require_approval: true"))
    os.utime(yaml_file, ns=(before.st_atime_ns, before.st_mtime_ns))   # cp -p, a restore
    assert refresh_sending_mode(config) is True and config.channels.email.require_approval is True


@pytest.mark.parametrize("written,expected", [('"true"', True), ("'false'", False), ("no", False), ("on", True)])
def test_quoted_and_spelled_values_count_as_they_do_at_startup(yaml_file, written, expected):
    yaml_file.write_text(yaml_file.read_text().replace("require_approval: true", f"require_approval: {written}"))
    assert load_config(str(yaml_file)).channels.email.require_approval is expected
    config = load_config(str(yaml_file))
    config.channels.email.require_approval = not expected
    assert refresh_sending_mode(config) is True and config.channels.email.require_approval is expected
