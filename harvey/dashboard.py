"""Harvey Dashboard — local web UI to visualize everything Harvey creates."""

import json
import logging
from pathlib import Path

import aiosqlite
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

logger = logging.getLogger("harvey.dashboard")

DB_PATH = Path(__file__).parent.parent / "data" / "harvey.db"

app = FastAPI(title="Harvey Dashboard")


async def query_db(sql: str, params: tuple = ()) -> list[dict]:
    """Run a query and return results as list of dicts."""
    db_path = str(DB_PATH)
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


# ── API Endpoints ──


@app.get("/api/stats")
async def get_stats():
    """Pipeline overview stats."""
    try:
        prospects = await query_db(
            "SELECT status, COUNT(*) as count FROM prospects GROUP BY status"
        )
        prospect_total = sum(r["count"] for r in prospects)
        prospect_map = {r["status"]: r["count"] for r in prospects}

        campaigns = await query_db(
            "SELECT status, COUNT(*) as count FROM campaigns GROUP BY status"
        )
        campaign_map = {r["status"]: r["count"] for r in campaigns}

        conversations = await query_db(
            "SELECT status, COUNT(*) as count FROM conversations GROUP BY status"
        )
        convo_map = {r["status"]: r["count"] for r in conversations}

        actions = await query_db("SELECT COUNT(*) as count FROM actions")
        action_count = actions[0]["count"] if actions else 0

        usage = await query_db(
            "SELECT claude_calls FROM usage_log WHERE date = date('now')"
        )
        usage_today = usage[0]["claude_calls"] if usage else 0

        return {
            "prospects": {"total": prospect_total, "by_status": prospect_map},
            "campaigns": {"total": sum(campaign_map.values()), "by_status": campaign_map},
            "conversations": {"total": sum(convo_map.values()), "by_status": convo_map},
            "actions_total": action_count,
            "claude_calls_today": usage_today,
        }
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/prospects")
async def get_prospects():
    """All prospects."""
    rows = await query_db(
        "SELECT * FROM prospects ORDER BY created_at DESC LIMIT 200"
    )
    return rows


@app.get("/api/campaigns")
async def get_campaigns():
    """All campaigns with their email sequences."""
    rows = await query_db(
        "SELECT * FROM campaigns ORDER BY created_at DESC LIMIT 100"
    )
    for row in rows:
        try:
            row["sequence"] = json.loads(row.get("sequence_json", "[]"))
        except (json.JSONDecodeError, TypeError):
            row["sequence"] = []
        try:
            row["prospect_ids"] = json.loads(row.get("prospect_ids_json", "[]"))
        except (json.JSONDecodeError, TypeError):
            row["prospect_ids"] = []
    return rows


@app.get("/api/conversations")
async def get_conversations():
    """All conversations with thread history."""
    rows = await query_db("""
        SELECT c.*, p.first_name, p.last_name, p.email as prospect_email, p.company
        FROM conversations c
        LEFT JOIN prospects p ON c.prospect_id = p.id
        ORDER BY c.updated_at DESC LIMIT 100
    """)
    for row in rows:
        try:
            row["thread"] = json.loads(row.get("thread_json", "[]"))
        except (json.JSONDecodeError, TypeError):
            row["thread"] = []
    return rows


@app.get("/api/activity")
async def get_activity():
    """Recent actions log."""
    rows = await query_db(
        "SELECT * FROM actions ORDER BY created_at DESC LIMIT 100"
    )
    for row in rows:
        try:
            row["details"] = json.loads(row.get("details_json", "{}"))
        except (json.JSONDecodeError, TypeError):
            row["details"] = {}
    return rows


# ── Dashboard UI ──


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """Serve the dashboard UI."""
    return DASHBOARD_HTML


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Harvey Dashboard</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }

  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: #0a0a0a;
    color: #e0e0e0;
    min-height: 100vh;
  }

  header {
    background: #111;
    border-bottom: 1px solid #222;
    padding: 16px 32px;
    display: flex;
    align-items: center;
    justify-content: space-between;
  }

  header h1 {
    font-size: 20px;
    font-weight: 600;
    color: #fff;
  }

  header h1 span {
    color: #666;
    font-weight: 400;
    font-size: 14px;
    margin-left: 8px;
  }

  .refresh-btn {
    background: #1a1a1a;
    border: 1px solid #333;
    color: #888;
    padding: 6px 14px;
    border-radius: 6px;
    cursor: pointer;
    font-size: 13px;
  }

  .refresh-btn:hover { border-color: #555; color: #ccc; }

  nav {
    background: #111;
    border-bottom: 1px solid #222;
    padding: 0 32px;
    display: flex;
    gap: 0;
  }

  nav button {
    background: none;
    border: none;
    border-bottom: 2px solid transparent;
    color: #777;
    padding: 12px 20px;
    cursor: pointer;
    font-size: 14px;
    transition: all 0.15s;
  }

  nav button:hover { color: #bbb; }
  nav button.active { color: #fff; border-bottom-color: #fff; }

  main { padding: 24px 32px; max-width: 1400px; }

  .stats-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 16px;
    margin-bottom: 32px;
  }

  .stat-card {
    background: #141414;
    border: 1px solid #222;
    border-radius: 10px;
    padding: 20px;
  }

  .stat-card .label {
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: #666;
    margin-bottom: 8px;
  }

  .stat-card .value {
    font-size: 32px;
    font-weight: 700;
    color: #fff;
  }

  .stat-card .breakdown {
    margin-top: 10px;
    font-size: 12px;
    color: #555;
    line-height: 1.6;
  }

  .section { display: none; }
  .section.active { display: block; }

  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
  }

  th {
    text-align: left;
    padding: 10px 14px;
    border-bottom: 1px solid #222;
    color: #666;
    font-weight: 500;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }

  td {
    padding: 12px 14px;
    border-bottom: 1px solid #1a1a1a;
    vertical-align: top;
    max-width: 300px;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  tr:hover td { background: #151515; }

  .badge {
    display: inline-block;
    padding: 2px 10px;
    border-radius: 12px;
    font-size: 11px;
    font-weight: 500;
  }

  .badge-new { background: #1a2332; color: #4a9eff; }
  .badge-contacted { background: #2a2215; color: #f0a030; }
  .badge-replied { background: #152a1a; color: #40c060; }
  .badge-meeting { background: #2a1530; color: #c050e0; }
  .badge-draft { background: #1a1a2a; color: #8080ff; }
  .badge-active { background: #152a1a; color: #40c060; }
  .badge-open { background: #2a2215; color: #f0a030; }
  .badge-closed { background: #1a1a1a; color: #666; }
  .badge-lost { background: #2a1515; color: #e05050; }
  .badge-interested { background: #152a1a; color: #40c060; }
  .badge-objection { background: #2a2215; color: #f0a030; }
  .badge-not_interested { background: #2a1515; color: #e05050; }

  .email-preview {
    background: #141414;
    border: 1px solid #222;
    border-radius: 10px;
    padding: 20px;
    margin-bottom: 16px;
  }

  .email-preview h3 {
    font-size: 14px;
    color: #fff;
    margin-bottom: 4px;
  }

  .email-preview .meta {
    font-size: 12px;
    color: #555;
    margin-bottom: 12px;
  }

  .email-preview .subject {
    font-size: 13px;
    font-weight: 600;
    color: #ccc;
    margin-bottom: 8px;
  }

  .email-preview .body {
    font-size: 13px;
    color: #999;
    line-height: 1.6;
    white-space: pre-wrap;
  }

  .email-step {
    border-left: 3px solid #222;
    padding: 16px 20px;
    margin-bottom: 12px;
    margin-left: 8px;
  }

  .email-step .step-num {
    font-size: 11px;
    color: #555;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    margin-bottom: 6px;
  }

  .email-step .subject {
    font-size: 14px;
    font-weight: 600;
    color: #ddd;
    margin-bottom: 8px;
  }

  .email-step .body {
    font-size: 13px;
    color: #888;
    line-height: 1.7;
    white-space: pre-wrap;
  }

  .email-step .delay {
    font-size: 11px;
    color: #444;
    margin-top: 8px;
  }

  .campaign-card {
    background: #141414;
    border: 1px solid #222;
    border-radius: 10px;
    padding: 24px;
    margin-bottom: 20px;
  }

  .campaign-card h3 {
    font-size: 16px;
    color: #fff;
    margin-bottom: 4px;
  }

  .campaign-card .meta {
    font-size: 12px;
    color: #555;
    margin-bottom: 16px;
    display: flex;
    gap: 16px;
    align-items: center;
  }

  .thread-msg {
    padding: 12px 16px;
    margin-bottom: 8px;
    border-radius: 8px;
    max-width: 80%;
    font-size: 13px;
    line-height: 1.6;
    white-space: pre-wrap;
  }

  .thread-msg.sent {
    background: #1a2332;
    color: #b0d0ff;
    margin-left: auto;
  }

  .thread-msg.received {
    background: #1a1a1a;
    color: #ccc;
  }

  .thread-msg .sender {
    font-size: 11px;
    color: #666;
    margin-bottom: 4px;
  }

  .convo-card {
    background: #141414;
    border: 1px solid #222;
    border-radius: 10px;
    padding: 24px;
    margin-bottom: 16px;
  }

  .convo-card h3 {
    font-size: 15px;
    color: #fff;
    margin-bottom: 4px;
  }

  .convo-card .meta {
    font-size: 12px;
    color: #555;
    margin-bottom: 16px;
    display: flex;
    gap: 16px;
    align-items: center;
  }

  .activity-item {
    display: flex;
    gap: 16px;
    padding: 12px 0;
    border-bottom: 1px solid #1a1a1a;
    font-size: 13px;
    align-items: center;
  }

  .activity-item .time {
    color: #444;
    font-size: 12px;
    min-width: 140px;
    font-variant-numeric: tabular-nums;
  }

  .activity-item .agent {
    color: #666;
    min-width: 80px;
  }

  .activity-item .action {
    color: #aaa;
  }

  .empty {
    text-align: center;
    padding: 60px 20px;
    color: #444;
    font-size: 14px;
  }

  .empty .big {
    font-size: 40px;
    margin-bottom: 16px;
  }
</style>
</head>
<body>

<header>
  <h1>Harvey <span>Always Be Closing</span></h1>
  <button class="refresh-btn" onclick="loadAll()">Refresh</button>
</header>

<nav>
  <button class="active" onclick="showTab('overview', this)">Overview</button>
  <button onclick="showTab('prospects', this)">Prospects</button>
  <button onclick="showTab('campaigns', this)">Campaigns</button>
  <button onclick="showTab('conversations', this)">Conversations</button>
  <button onclick="showTab('activity', this)">Activity</button>
</nav>

<main>
  <!-- Overview -->
  <div id="overview" class="section active">
    <div class="stats-grid" id="stats-grid"></div>
  </div>

  <!-- Prospects -->
  <div id="prospects" class="section">
    <div id="prospects-table"></div>
  </div>

  <!-- Campaigns -->
  <div id="campaigns" class="section">
    <div id="campaigns-list"></div>
  </div>

  <!-- Conversations -->
  <div id="conversations" class="section">
    <div id="conversations-list"></div>
  </div>

  <!-- Activity -->
  <div id="activity" class="section">
    <div id="activity-list"></div>
  </div>
</main>

<script>
function showTab(id, btn) {
  document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
  document.querySelectorAll('nav button').forEach(b => b.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  btn.classList.add('active');
}

function badge(status) {
  const cls = 'badge-' + (status || '').replace(/\\s+/g, '_');
  return `<span class="badge ${cls}">${status || 'unknown'}</span>`;
}

function formatDate(d) {
  if (!d) return '';
  try {
    const dt = new Date(d);
    return dt.toLocaleString('en-US', {month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit'});
  } catch { return d; }
}

function escHtml(s) {
  if (!s) return '';
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

async function loadStats() {
  const r = await fetch('/api/stats');
  const data = await r.json();
  if (data.error) {
    document.getElementById('stats-grid').innerHTML = `<div class="empty"><div class="big">!</div>Database not found. Run Harvey first to create it.</div>`;
    return;
  }

  const p = data.prospects || {};
  const c = data.campaigns || {};
  const v = data.conversations || {};

  const statusBreakdown = (map) =>
    Object.entries(map).map(([k,v]) => `${k}: ${v}`).join(' &middot; ') || 'none';

  document.getElementById('stats-grid').innerHTML = `
    <div class="stat-card">
      <div class="label">Prospects</div>
      <div class="value">${p.total || 0}</div>
      <div class="breakdown">${statusBreakdown(p.by_status || {})}</div>
    </div>
    <div class="stat-card">
      <div class="label">Campaigns</div>
      <div class="value">${c.total || 0}</div>
      <div class="breakdown">${statusBreakdown(c.by_status || {})}</div>
    </div>
    <div class="stat-card">
      <div class="label">Conversations</div>
      <div class="value">${v.total || 0}</div>
      <div class="breakdown">${statusBreakdown(v.by_status || {})}</div>
    </div>
    <div class="stat-card">
      <div class="label">Actions Today</div>
      <div class="value">${data.actions_total || 0}</div>
      <div class="breakdown">Claude calls today: ${data.claude_calls_today || 0}</div>
    </div>
  `;
}

async function loadProspects() {
  const r = await fetch('/api/prospects');
  const data = await r.json();

  if (!data.length) {
    document.getElementById('prospects-table').innerHTML = `<div class="empty"><div class="big">No prospects yet</div>Harvey hasn't found any prospects yet. Run the prospecting cycle first.</div>`;
    return;
  }

  let html = `<table><thead><tr>
    <th>Name</th><th>Title</th><th>Company</th><th>Email</th><th>Status</th><th>Score</th><th>Source</th><th>Added</th>
  </tr></thead><tbody>`;

  for (const p of data) {
    html += `<tr>
      <td>${escHtml(p.first_name)} ${escHtml(p.last_name)}</td>
      <td>${escHtml(p.title)}</td>
      <td>${escHtml(p.company)}</td>
      <td>${escHtml(p.email)}</td>
      <td>${badge(p.status)}</td>
      <td>${p.score || 0}</td>
      <td>${escHtml(p.source)}</td>
      <td>${formatDate(p.created_at)}</td>
    </tr>`;
  }

  html += '</tbody></table>';
  document.getElementById('prospects-table').innerHTML = html;
}

async function loadCampaigns() {
  const r = await fetch('/api/campaigns');
  const data = await r.json();

  if (!data.length) {
    document.getElementById('campaigns-list').innerHTML = `<div class="empty"><div class="big">No campaigns yet</div>Harvey hasn't written any email sequences yet.</div>`;
    return;
  }

  let html = '';
  for (const c of data) {
    const seq = c.sequence || [];
    let stepsHtml = '';
    for (const step of seq) {
      stepsHtml += `
        <div class="email-step">
          <div class="step-num">Email ${step.step || '?'}${step.delay_days ? ` &middot; Send after ${step.delay_days} days` : ''}</div>
          <div class="subject">${escHtml(step.subject)}</div>
          <div class="body">${escHtml(step.body)}</div>
        </div>`;
    }

    const prospectCount = (c.prospect_ids || []).length;

    html += `
      <div class="campaign-card">
        <h3>${escHtml(c.name || 'Untitled Campaign')}</h3>
        <div class="meta">
          ${badge(c.status)}
          <span>${c.channel || 'email'}</span>
          <span>${prospectCount} prospect${prospectCount !== 1 ? 's' : ''}</span>
          <span>${formatDate(c.created_at)}</span>
        </div>
        ${stepsHtml || '<div class="empty">No email steps</div>'}
      </div>`;
  }

  document.getElementById('campaigns-list').innerHTML = html;
}

async function loadConversations() {
  const r = await fetch('/api/conversations');
  const data = await r.json();

  if (!data.length) {
    document.getElementById('conversations-list').innerHTML = `<div class="empty"><div class="big">No conversations yet</div>Harvey hasn't received any replies yet.</div>`;
    return;
  }

  let html = '';
  for (const c of data) {
    const thread = c.thread || [];
    let threadHtml = '';
    for (const msg of thread) {
      const cls = msg.sender === 'harvey' ? 'sent' : 'received';
      threadHtml += `
        <div class="thread-msg ${cls}">
          <div class="sender">${escHtml(msg.sender)} &middot; ${formatDate(msg.timestamp)}</div>
          ${escHtml(msg.content)}
        </div>`;
    }

    const name = [c.first_name, c.last_name].filter(Boolean).join(' ') || 'Unknown';

    html += `
      <div class="convo-card">
        <h3>${escHtml(name)} — ${escHtml(c.company || '')}</h3>
        <div class="meta">
          ${badge(c.status)}
          ${c.intent ? badge(c.intent) : ''}
          <span>${escHtml(c.prospect_email || '')}</span>
          <span>${formatDate(c.updated_at)}</span>
        </div>
        ${threadHtml || '<div class="empty">No messages</div>'}
      </div>`;
  }

  document.getElementById('conversations-list').innerHTML = html;
}

async function loadActivity() {
  const r = await fetch('/api/activity');
  const data = await r.json();

  if (!data.length) {
    document.getElementById('activity-list').innerHTML = `<div class="empty"><div class="big">No activity yet</div>Harvey hasn't taken any actions yet.</div>`;
    return;
  }

  let html = '';
  for (const a of data) {
    html += `
      <div class="activity-item">
        <span class="time">${formatDate(a.created_at)}</span>
        <span class="agent">${escHtml(a.agent)}</span>
        <span class="action">${escHtml(a.action_type)}</span>
      </div>`;
  }

  document.getElementById('activity-list').innerHTML = html;
}

function loadAll() {
  loadStats();
  loadProspects();
  loadCampaigns();
  loadConversations();
  loadActivity();
}

loadAll();
setInterval(loadAll, 30000);
</script>
</body>
</html>"""


def start_dashboard(host: str = "127.0.0.1", port: int = 5555):
    """Start the dashboard server."""
    import uvicorn

    print(f"\\n  Harvey Dashboard running at http://{host}:{port}")
    print(f"  Press Ctrl+C to stop.\\n")
    uvicorn.run(app, host=host, port=port, log_level="warning")
