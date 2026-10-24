"""Dashboard API for the signal-confirmation gate, cohorts, and Today.

These cover the contract the UI depends on: Harvey proposes signals, nothing
is collected until a human confirms, and a confirmed set becomes a cohort
query rather than a static list.
"""

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import harvey.dashboard as dash
from harvey.signals import SIGNAL_CATALOG
from harvey.state import StateManager


@pytest.fixture
def client(monkeypatch):
    """A dashboard bound to a throwaway database, used the way its own UI
    uses it: same origin, with the token (tests/test_dashboard_auth.py covers
    everyone else)."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "harvey.db"
        monkeypatch.setattr(dash, "DB_PATH", db)
        monkeypatch.setattr(dash, "WEB_DIR", Path(__file__).resolve().parent.parent / "harvey" / "web")
        dash.token_store.configure(Path(tmp) / "data" / "dashboard-token")
        token = dash.token_store.get()
        with TestClient(dash.app, base_url="http://127.0.0.1:5555",
                        headers={"Authorization": f"Bearer {token}"}) as c:
            c.db_path = db
            yield c


# ── The confirmation gate ──


def test_signals_seed_as_proposed_not_confirmed(client):
    """Harvey proposes. Nothing is live until a human says so."""
    data = client.get("/api/signals").json()
    assert data["total"] == len(SIGNAL_CATALOG)
    assert data["summary"]["proposed"] == len(SIGNAL_CATALOG)
    assert data["summary"]["confirmed"] == 0


def test_signals_grouped_with_plain_language(client):
    data = client.get("/api/signals").json()
    keys = [g["key"] for g in data["groups"]]
    assert keys == ["discovery", "profile", "people", "verification"]
    for group in data["groups"]:
        assert group["blurb"]  # every category explains itself
        for sig in group["signals"]:
            assert sig["description"] and sig["cost_note"]


def test_confirm_and_reject_signals(client):
    r = client.post("/api/signals/status",
                    json={"codes": ["INCUMBENT_AGENCY", "RUNNING_GOOGLE_ADS"],
                          "status": "confirmed"})
    assert r.json()["changed"] == 2 and r.json()["unknown"] == []

    r = client.post("/api/signals/status",
                    json={"code": "BLOCKS_AI_CRAWLERS", "status": "rejected"})
    assert r.json()["changed"] == 1

    summary = client.get("/api/signals").json()["summary"]
    assert summary["confirmed"] == 2
    assert summary["rejected"] == 1


def test_reseeding_never_overrides_a_users_decision(client):
    """Every /api/signals load re-seeds so upgrades add new signals. It must
    not quietly re-enable something the user turned off."""
    client.post("/api/signals/status",
                json={"code": "RUNNING_META_ADS", "status": "rejected"})
    client.get("/api/signals")  # re-seeds
    data = client.get("/api/signals").json()
    meta = next(s for g in data["groups"] for s in g["signals"]
                if s["code"] == "RUNNING_META_ADS")
    assert meta["status"] == "rejected"


def test_confirming_before_the_catalog_loads_still_works(client):
    """A confirm can arrive before anything has read /api/signals. It must
    seed the vocabulary rather than silently changing nothing."""
    r = client.post("/api/signals/status",
                    json={"codes": ["SERP_RANK"], "status": "confirmed"}).json()
    assert r["changed"] == 1


def test_unknown_codes_are_reported_not_swallowed(client):
    r = client.post("/api/signals/status",
                    json={"codes": ["SERP_RANK", "NOT_A_SIGNAL"],
                          "status": "confirmed"}).json()
    assert r["changed"] == 1
    assert r["unknown"] == ["NOT_A_SIGNAL"]


def test_invalid_status_is_rejected(client):
    r = client.post("/api/signals/status", json={"codes": ["SERP_RANK"], "status": "on"})
    assert r.status_code == 400
    r = client.post("/api/signals/status", json={"codes": [], "status": "confirmed"})
    assert r.status_code == 400


# ── Cohorts ──


@pytest.mark.asyncio
async def _seed_observations(db_path):
    state = StateManager(str(db_path))
    await state.init_db()
    from harvey.signals import seed_signal_catalog
    await seed_signal_catalog(state)
    from harvey.models import Company

    ids = []
    for n, name in enumerate(["Alpha Roofing", "Beta Roofing", "Gamma Roofing"]):
        ids.append(await state.add_company(
            Company(name=name, domain=f"{name.split()[0].lower()}.com")))
    # Alpha: agency + ads. Beta: agency only. Gamma: ads + booking already.
    await state.add_observations([
        {"company_id": ids[0], "signal_code": "INCUMBENT_AGENCY", "value_text": "Studio3"},
        {"company_id": ids[0], "signal_code": "RUNNING_GOOGLE_ADS", "value_num": 1},
        {"company_id": ids[1], "signal_code": "INCUMBENT_AGENCY", "value_text": "Etna"},
        {"company_id": ids[2], "signal_code": "RUNNING_GOOGLE_ADS", "value_num": 1},
        {"company_id": ids[2], "signal_code": "NO_ONLINE_BOOKING", "value_num": 1},
    ])
    return ids


def test_cohort_is_a_set_intersection(client):
    import asyncio
    asyncio.run(_seed_observations(client.db_path))

    both = client.post("/api/cohort", json={
        "require": ["INCUMBENT_AGENCY", "RUNNING_GOOGLE_ADS"]}).json()
    assert both["size"] == 1
    assert both["companies"][0]["name"] == "Alpha Roofing"

    one = client.post("/api/cohort", json={"require": ["RUNNING_GOOGLE_ADS"]}).json()
    assert one["size"] == 2

    excluded = client.post("/api/cohort", json={
        "require": ["RUNNING_GOOGLE_ADS"], "exclude": ["NO_ONLINE_BOOKING"]}).json()
    assert excluded["size"] == 1
    assert excluded["companies"][0]["name"] == "Alpha Roofing"


async def _seed_large_cohort(db_path, n):
    state = StateManager(str(db_path))
    await state.init_db()
    from harvey.signals import seed_signal_catalog
    await seed_signal_catalog(state)
    from harvey.models import Company

    ids = [await state.add_company(Company(name=f"Co {i:03d}", domain=f"co{i:03d}.com"))
           for i in range(n)]
    await state.add_observations([
        {"company_id": cid, "signal_code": "RUNNING_GOOGLE_ADS", "value_num": 1}
        for cid in ids
    ])


def test_cohort_pages_through_every_match(client):
    """Past the first page used to be unreachable: 147 matches showed 50."""
    import asyncio
    asyncio.run(_seed_large_cohort(client.db_path, 120))

    seen = []
    for offset in (0, 50, 100):
        page = client.post("/api/cohort", json={
            "require": ["RUNNING_GOOGLE_ADS"], "offset": offset, "limit": 50}).json()
        assert page["size"] == 120
        assert page["offset"] == offset
        seen += [c["name"] for c in page["companies"]]

    assert len(seen) == 120
    assert len(set(seen)) == 120, "pages must not overlap"
    assert seen == sorted(seen), "pages are in a stable order"


def test_cohort_page_size_is_capped(client):
    import asyncio
    asyncio.run(_seed_large_cohort(client.db_path, 3))
    page = client.post("/api/cohort", json={
        "require": ["RUNNING_GOOGLE_ADS"], "limit": 100000}).json()
    assert page["limit"] == dash.COHORT_MAX_PAGE_SIZE


def test_cohort_csv_exports_the_whole_cohort(client):
    import asyncio
    asyncio.run(_seed_large_cohort(client.db_path, 120))
    r = client.get("/api/cohort.csv", params={"require": "RUNNING_GOOGLE_ADS"})
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    lines = r.text.strip().splitlines()
    assert lines[0] == "name,domain,website,industry,location"
    assert len(lines) == 121  # header + every match, not one page


def test_cohort_csv_with_no_requirements_is_header_only(client):
    r = client.get("/api/cohort.csv")
    assert r.text.strip() == "name,domain,website,industry,location"


def test_cohort_with_no_requirements_is_empty_not_everything(client):
    """An empty cohort must never mean 'select all' — that would silently
    turn a mis-click into a campaign against the entire database."""
    assert client.post("/api/cohort", json={"require": []}).json()["size"] == 0


def test_signal_counts_surface_in_the_review_ui(client):
    import asyncio
    asyncio.run(_seed_observations(client.db_path))
    data = client.get("/api/signals").json()
    agency = next(s for g in data["groups"] for s in g["signals"]
                  if s["code"] == "INCUMBENT_AGENCY")
    assert agency["companies"] == 2


# ── Today ──


def test_today_leads_with_unconfirmed_signals(client):
    data = client.get("/api/today").json()
    keys = [i["key"] for i in data["items"]]
    assert "signals" in keys
    item = next(i for i in data["items"] if i["key"] == "signals")
    assert item["tab"] == "signals"
    assert str(len(SIGNAL_CATALOG)) in item["title"]


def test_today_flags_a_fully_rejected_vocabulary(client):
    codes = [s["code"] for s in SIGNAL_CATALOG]
    client.post("/api/signals/status", json={"codes": codes, "status": "rejected"})
    data = client.get("/api/today").json()
    keys = [i["key"] for i in data["items"]]
    assert "signals-none" in keys


def test_today_stats_are_present_even_on_an_empty_install(client):
    stats = client.get("/api/today").json()["stats"]
    for key in ("companies", "prospects", "signals_confirmed",
                "outbox_pending", "open_conversations"):
        assert key in stats


# ── Static assets ──


def test_dashboard_serves_its_files(client):
    assert client.get("/").status_code == 200
    assert "app.css" in client.get("/").text
    assert client.get("/static/app.css").status_code == 200
    assert client.get("/static/app.js").status_code == 200


@pytest.mark.parametrize("filename, url, media_type", [
    ("index.html", "/", "text/html"),
    ("app.css", "/static/app.css", "text/css"),
    ("app.js", "/static/app.js", "text/javascript"),
    ("icon.svg", "/static/icon.svg", "image/svg+xml"),
])
def test_dashboard_text_is_utf8_with_windows_default_encoding(
    client, monkeypatch, tmp_path, filename, url, media_type,
):
    """UTF-8 assets must survive a CP1252 host without crashes or mojibake."""
    content = "/* ═ café — 日本語 */"
    (tmp_path / filename).write_bytes(content.encode("utf-8"))
    monkeypatch.setattr(dash, "WEB_DIR", tmp_path)
    read_text = Path.read_text

    def windows_read_text(path, encoding=None, errors=None, **kwargs):
        return read_text(path, encoding=encoding or "cp1252", errors=errors, **kwargs)

    monkeypatch.setattr(Path, "read_text", windows_read_text)
    response = client.get(url)
    assert response.status_code == 200
    assert response.text == content
    assert response.content == content.encode("utf-8")
    assert response.headers["content-type"].startswith(media_type)


def test_static_route_refuses_to_escape_the_web_directory(client):
    assert client.get("/static/..%2f..%2fharvey.yaml").status_code == 404
    assert client.get("/static/nope.css").status_code == 404


# ── Catalog invariants the UI and CLI both rely on ──


def test_cost_notes_are_machine_classifiable():
    """`harvey signals --confirm free` and the dashboard's green/amber cost
    chip both key off the cost note. A signal that costs credits must not
    read as free — that is how someone accidentally turns on paid collection.
    """
    paid = {"SERP_RANK", "MAPS_RANK", "EMAIL_STATUS", "EMAIL_PATTERN", "FOUND_IN_SERP"}
    for sig in SIGNAL_CATALOG:
        free = sig["cost_note"].lower().startswith(("free", "included"))
        if sig["code"] in paid:
            continue
        assert free, f"{sig['code']} has an unclassifiable cost note: {sig['cost_note']!r}"

    serp = next(s for s in SIGNAL_CATALOG if s["code"] == "SERP_RANK")
    assert not serp["cost_note"].lower().startswith("free")
    email = next(s for s in SIGNAL_CATALOG if s["code"] == "EMAIL_STATUS")
    assert not email["cost_note"].lower().startswith("free")


def test_every_signal_has_a_category_the_ui_renders():
    from harvey.dashboard import CATEGORY_ORDER
    for sig in SIGNAL_CATALOG:
        assert sig["category"] in CATEGORY_ORDER, (
            f"{sig['code']} is in category {sig['category']!r}, which the "
            "dashboard would silently drop")


@pytest.mark.parametrize('body', [[], None, {'require': [7]}, {'require': {}},
    {'offset': 'bad'}, {'offset': 1.5}, {'limit': True}, {'offset': 2**64},
    {'require': ['x'] * 201 + ['y' * 129]}])
def test_cohort_invalid_inputs_are_client_errors(client, body):
    response = client.post('/api/cohort', json=body) if body is not None else client.post(
        '/api/cohort', content='null', headers={'Content-Type': 'application/json'})
    assert response.status_code == 400


def test_cohort_boundaries_and_duplicate_codes(client):
    import asyncio
    asyncio.run(_seed_large_cohort(client.db_path, 3))
    response = client.post('/api/cohort', json={
        'require': ['RUNNING_GOOGLE_ADS', 'RUNNING_GOOGLE_ADS'], 'offset': -1, 'limit': 0})
    assert response.status_code == 200
    assert response.json()['size'] == 3
    assert response.json()['offset'] == 0
    assert response.json()['limit'] == 1
    assert len(response.json()['companies']) == 1
    assert client.post('/api/cohort', json={
        'require': ['RUNNING_GOOGLE_ADS'], 'offset': 4}).json()['companies'] == []


def test_cohort_over_previous_cap_and_stable_ties(client):
    import asyncio
    import csv
    import io
    import sqlite3
    from contextlib import closing
    async def initialize():
        state = StateManager(str(client.db_path))
        await state.init_db()
        from harvey.signals import seed_signal_catalog
        await seed_signal_catalog(state)
    asyncio.run(initialize())
    # Bulk seed rather than opening 10,001 individual connections.
    with closing(sqlite3.connect(client.db_path)) as db, db:
        db.executemany('INSERT INTO companies (id, name) VALUES (?, ?)',
                       [(f'{i:05}', 'Same') for i in range(10001)])
        db.executemany('INSERT INTO observations (id, company_id, signal_code, value_num) '
                       'VALUES (?, ?, ?, 1)',
                       [(f'obs{i}', f'{i:05}', 'RUNNING_GOOGLE_ADS') for i in range(10001)])
    page = client.post('/api/cohort', json={
        'require': ['RUNNING_GOOGLE_ADS'], 'offset': 9999, 'limit': 2}).json()
    assert page['size'] == 10001
    assert [row['id'] for row in page['companies']] == ['09999', '10000']
    response = client.get('/api/cohort.csv', params={'require': 'RUNNING_GOOGLE_ADS'})
    assert response.status_code == 200
    assert len(list(csv.reader(io.StringIO(response.text)))) == 10002


def test_cohort_csv_failure_is_not_success(client, monkeypatch):
    async def fail(*args, **kwargs):
        raise RuntimeError('database unavailable')
    monkeypatch.setattr(dash, '_cohort_companies', fail)
    response = client.get('/api/cohort.csv', params={'require': 'RUNNING_GOOGLE_ADS'})
    assert response.status_code == 500
    assert 'text/csv' not in response.headers['content-type']
    assert client.get('/api/cohort.csv').status_code == 200
    assert client.post('/api/cohort', json={'require': ['RUNNING_GOOGLE_ADS']}).status_code == 500


def test_cohort_csv_preserves_text_and_neutralizes_formulas(client, monkeypatch):
    import csv
    import io
    async def rows(*args, **kwargs):
        return 1, [dict(name=' =HYPERLINK("evil")', domain='é, "quoted"',
                        website='\t=1+1', industry='@SUM(1)', location='line\nnext')]
    monkeypatch.setattr(dash, '_cohort_companies', rows)
    response = client.get('/api/cohort.csv', params={'require': 'RUNNING_GOOGLE_ADS'})
    cells = list(csv.reader(io.StringIO(response.text)))[1]
    assert cells == ['\' =HYPERLINK("evil")', 'é, "quoted"', "'\t=1+1", "'@SUM(1)", 'line\nnext']


def test_cohort_csv_requires_authorization(client):
    response = client.get('/api/cohort.csv', headers={'Authorization': ''})
    assert response.status_code == 401


def test_cohort_browser_export_uses_auth_and_handles_failures():
    import json
    import shutil
    import subprocess
    node = shutil.which('node')
    if not node:
        pytest.skip('node unavailable')
    web = Path(__file__).resolve().parent.parent / 'harvey' / 'web'
    script = r'''
const fs = require('fs'), vm = require('vm');
const calls = [], revoked = [], links = [];
global.window = {location: {origin: 'http://127.0.0.1:5555', hash: ''}};
global.sessionStorage = {getItem: () => 'T'.repeat(43)};
global.document = {body: {appendChild: x => links.push(x)}, createElement: () =>
  ({click() {this.clicked = true;}, remove() {this.removed = true;}})};
global.setTimeout = fn => fn();
const locks = [];
global.showLock = reason => locks.push(reason);
global.hideLock = () => {};
URL.createObjectURL = () => 'blob:local';
URL.revokeObjectURL = url => revoked.push(url);
let ok = true, status = 200;
global.fetch = async (url, opts) => {calls.push({url, auth: opts.headers.get('Authorization'),
  redirect: opts.redirect}); return {ok, status, blob: async () => ({})};};
vm.runInThisContext(fs.readFileSync(process.argv[1], 'utf8'));
const app = fs.readFileSync(process.argv[2], 'utf8');
vm.runInThisContext('let _locked = false;' + app.slice(app.indexOf('async function lockIfRefused('), app.indexOf('function showLock(')));
vm.runInThisContext("const _cohort = {require: new Set(['A']), exclude: new Set(['B'])};" +
 app.slice(app.indexOf('async function exportCohort('), app.indexOf('function pageCohort(')));
(async () => {
 const button = {};
 await exportCohort(button);
 const success = {...button};
 ok = false; status = 500;
 await exportCohort(button);
 const failure = {...button};
 status = 401;
 await exportCohort(button);
 console.log(JSON.stringify({calls, revoked, links, success, failure, locks, refused: button}));
})().catch(e => {console.error(e); process.exit(1);});
'''
    result = subprocess.run([node, '-e', script, str(web / 'auth.js'), str(web / 'app.js')],
                            text=True, encoding="utf-8", capture_output=True, check=True)
    data = json.loads(result.stdout)
    assert all(c['auth'] == 'Bearer ' + 'T' * 43 and c['redirect'] == 'error' for c in data['calls'])
    assert data['calls'][0]['url'] == 'http://127.0.0.1:5555/api/cohort.csv?require=A&exclude=B'
    assert len(data['links']) == 1
    assert data['links'][0]['clicked'] and data['links'][0]['removed']
    assert data['revoked'] == ['blob:local']
    assert data['success'] == {'disabled': False, 'textContent': 'Export CSV'}
    assert data['failure'] == {'disabled': False, 'textContent': 'Export failed — retry'}
    assert data['refused'] == data['failure']
    assert len(data['locks']) == 1 and 'saved token was not accepted' in data['locks'][0]


def test_cohort_csv_invalid_codes_are_client_error(client):
    response = client.get('/api/cohort.csv', params={'require': 'x' * 129})
    assert response.status_code == 400
    response = client.get('/api/cohort.csv', params={'exclude': 'x' * 129})
    assert response.status_code == 400


def test_cohort_browser_ignores_superseded_pages_and_cleared_filters():
    import json
    import shutil
    import subprocess
    node = shutil.which('node')
    if not node:
        pytest.skip('node unavailable')
    app = Path(__file__).resolve().parent.parent / 'harvey' / 'web' / 'app.js'
    script = r'''
const fs = require('fs'), vm = require('vm');
const app = fs.readFileSync(process.argv[1], 'utf8');
const el = {innerHTML: 'previous export button'}, pending = [];
global.document = {getElementById: () => el};
global.api = (path, opts) => new Promise(resolve => pending.push({resolve, body: JSON.parse(opts.body)}));
global.escHtml = s => s || '';
vm.runInThisContext("const _cohort = {require: new Set(['A']), exclude: new Set()};" +
 "let _cohortOffset = 0, _cohortGeneration = 0; const COHORT_PAGE = 50;" +
 app.slice(app.indexOf('async function runCohort('), app.indexOf('async function exportCohort(')));
const page = (name, offset) => ({size: 100, offset, companies: [{name}]});
(async () => {
 const first = runCohort();
 const loading = el.innerHTML;
 vm.runInThisContext('_cohortOffset = 50');
 const second = runCohort();
 pending[1].resolve(page('LATEST', 50)); await second;
 const latest = el.innerHTML;
 pending[0].resolve(page('STALE', 0)); await first;
 const afterOld = el.innerHTML;
 const third = runCohort();
 vm.runInThisContext('_cohort.require.clear()');
 await runCohort();
 const cleared = el.innerHTML;
 pending[2].resolve(page('STALE_AFTER_CLEAR', 50)); await third;
 console.log(JSON.stringify({loading, latest, afterOld, cleared, afterClear: el.innerHTML,
   offsets: pending.map(x => x.body.offset)}));
})().catch(e => {console.error(e); process.exit(1);});
'''
    response = subprocess.run([node, '-e', script, str(app)], text=True, encoding="utf-8",
                              capture_output=True, check=True)
    data = json.loads(response.stdout)
    assert data['offsets'] == [0, 50, 50]
    assert 'Loading cohort' in data['loading'] and 'Export CSV' not in data['loading']
    assert 'LATEST' in data['latest'] and 'STALE' not in data['latest']
    assert data['afterOld'] == data['latest']
    assert data['cleared'] == data['afterClear'] == ''


# ── Cohort ranges: "organic rank 11-30" ──


async def _seed_ranks(db_path):
    state = StateManager(str(db_path))
    await state.init_db()
    from harvey.signals import seed_signal_catalog
    await seed_signal_catalog(state)
    from harvey.models import Company
    for name, rank in (("Top", 3), ("Mid", 14), ("Deep", 45)):
        cid = await state.add_company(Company(name=name, domain=f"{name.lower()}.com"))
        await state.add_observation("SERP_RANK", company_id=cid, collector="discover",
                                    value_num=rank, value_text="roofer Denver")


def test_cohort_range_filter_api(client):
    import asyncio
    asyncio.run(_seed_ranks(client.db_path))
    data = client.post("/api/cohort", json={
        "ranges": [{"code": "SERP_RANK", "min": 11, "max": 30}]}).json()
    assert data["size"] == 1 and data["companies"][0]["name"] == "Mid"
    # A range alone is a cohort; nothing at all is still empty.
    assert client.post("/api/cohort", json={}).json()["size"] == 0

    r = client.get("/api/cohort.csv", params={"ranges": "SERP_RANK:11:30"})
    assert r.status_code == 200
    assert [line.split(",")[0] for line in r.text.strip().splitlines()[1:]] == ["Mid"]
    r = client.get("/api/cohort.csv", params={"ranges": "SERP_RANK::10"})
    assert [line.split(",")[0] for line in r.text.strip().splitlines()[1:]] == ["Top"]


@pytest.mark.parametrize("ranges", [
    "SERP_RANK", [{"code": "SERP_RANK", "min": "x"}], [{"min": 1}],
    [{"code": "SERP_RANK"}], [{"code": "SERP_RANK", "min": 30, "max": 11}],
    [{"code": "SERP_RANK", "min": True}], [{"code": "S" * 129, "min": 1}],
    [{"code": "SERP_RANK", "min": 1}] * 21,
])
def test_cohort_invalid_ranges_are_client_errors(client, ranges):
    response = client.post("/api/cohort", json={"ranges": ranges})
    assert response.status_code == 400


def test_signals_api_marks_numeric_signals_for_the_range_ui(client):
    data = client.get("/api/signals").json()
    sigs = {s["code"]: s for g in data["groups"] for s in g["signals"]}
    assert sigs["SERP_RANK"]["value_type"] == "num"
