let currentTab = 'today';
let companyDrill = false;      // true while viewing a single company's contacts
let _companies = [], _prospects = [], _campaigns = [];
let _signals = null;           // last /api/signals payload, for the cohort builder
let _desk = { items: [], i: 0 };

// ── Appearance ──
//
// Three states, not two: "auto" follows the OS and is the default, so the
// dashboard matches the rest of your machine until you deliberately override
// it. Stored per-browser; nothing is sent anywhere.

const THEMES = ['auto', 'light', 'dark'];

function applyTheme(mode) {
  const root = document.documentElement;
  if (mode === 'auto') root.removeAttribute('data-theme');
  else root.setAttribute('data-theme', mode);
  const btn = document.getElementById('theme-btn');
  if (btn) btn.textContent = t(mode);
}

function cycleTheme() {
  const now = localStorage.getItem('harvey-theme') || 'auto';
  const next = THEMES[(THEMES.indexOf(now) + 1) % THEMES.length];
  try { localStorage.setItem('harvey-theme', next); } catch { /* private mode */ }
  applyTheme(next);
}

(function initTheme() {
  let saved = 'auto';
  try { saved = localStorage.getItem('harvey-theme') || 'auto'; } catch { /* ignore */ }
  applyTheme(THEMES.includes(saved) ? saved : 'auto');
})();

// ── Utilities ──

function escHtml(s) {
  if (s === null || s === undefined || s === '') return '';
  return String(s).replace(/[&<>"']/g, ch => (
    {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]
  ));
}

// A value placed inside an onclick handler as a JS string: JSON-quote it,
// then escape for the attribute (escHtml alone is undone before the JS runs).
function jsq(v) {
  return escHtml(JSON.stringify(String(v ?? '')));
}

// The reason a failed action gave, or a fallback when it gave none.
function failMsg(data, fallback) {
  return (data && (data.message || data.error || data.detail)) || fallback;
}

// Lookup tables keyed by stored strings: "constructor" must not hit Object's.
function own(table, key) {
  return Object.prototype.hasOwnProperty.call(table, key) ? table[key] : undefined;
}

// Links built from collected data (websites, profiles) open only as http(s):
// a javascript: URL from a scraped page must never run in the dashboard.
function safeHref(u) {
  const s = String(u || '').trim();
  return /^https?:\/\//i.test(s) ? escHtml(s) : '';
}

// ── One status vocabulary ──
//
// Harvey's tables each carry their own words for state: a prospect is `new`,
// a campaign is `draft`, an email is `pending_review`, a signal is `proposed`.
// Five of those mean "waiting on you" and the old dashboard styled every one
// differently. Everything now resolves through this map, so a status reads the
// same way no matter which table it came out of.
//
// tone: waiting (needs a human) | active (in flight) | good | bad | idle
const STATUS = {
  // prospects
  new:            ['New', 'active'],
  contacted:      ['Contacted', 'active'],
  replied:        ['Replied', 'good'],
  interested:     ['Interested', 'good'],
  meeting:        ['Meeting booked', 'note'],
  not_interested: ['Not interested', 'bad'],
  bounced:        ['Bounced', 'bad'],
  write_failed:   ["Couldn't write", 'bad'],
  queued:         ['Queued', 'active'],
  opted_out:      ['Opted out', 'bad'],
  // campaigns
  draft:          ['Draft', 'waiting'],
  active:         ['Active', 'good'],
  completed:      ['Completed', 'idle'],
  paused:         ['Paused', 'bad'],
  // conversations
  open:           ['Open', 'active'],
  closed:         ['Closed', 'idle'],
  closed_won:     ['Won', 'good'],
  closed_lost:    ['Lost', 'bad'],
  objection:      ['Objection', 'waiting'],
  question:       ['Question', 'active'],
  ooo:            ['Out of office', 'idle'],
  wrong_person:   ['Wrong person', 'waiting'],
  unsubscribe:    ['Unsubscribed', 'bad'],
  escalate:       ['Needs a human', 'bad'],
  needs_human:    ['Needs you', 'waiting'],
  // outbox
  pending_review: ['Waiting on you', 'waiting'],
  approved:       ['Approved', 'good'],
  sending:        ['Sending now', 'active'],
  scheduled:      ['Scheduled', 'active'],
  sent:           ['Sent', 'good'],
  failed:         ['Failed', 'bad'],
  rejected:       ['Rejected', 'idle'],
  cancelled:      ['Cancelled', 'idle'],
  // signals
  proposed:       ['Waiting on you', 'waiting'],
  confirmed:      ['Confirmed', 'good'],
  // email deliverability
  verified:       ['Verified', 'good'],
  risky:          ['Catch-all', 'waiting'],
  guess:          ['Unverified', 'idle'],
  invalid:        ['Invalid', 'bad'],
  // runs
  running:        ['Running', 'active'],
  stale:          ['Stale', 'bad'],
  stalled:        ['Stopped', 'bad'],
};

function statusMeta(status) {
  const key = String(status || '').toLowerCase().replace(/[^a-z0-9]+/g, '_');
  const hit = own(STATUS, key);
  if (hit) return { label: t(hit[0]), tone: hit[1] };
  const label = String(status || 'unknown').replace(/_/g, ' ');
  return { label: label.charAt(0).toUpperCase() + label.slice(1), tone: 'idle' };
}

function badge(status) {
  const m = statusMeta(status);
  return '<span class="badge t-' + m.tone + '">' + escHtml(m.label) + '</span>';
}

function formatDate(d) {
  if (!d) return '';
  try {
    let ts = String(d).trim().replace(' ', 'T');
    if (/^\d{4}-\d\d-\d\dT/.test(ts) && !/[zZ]$|[+-]\d\d:?\d\d$/.test(ts)) ts += 'Z';  // stored UTC
    const dt = new Date(ts);
    if (isNaN(dt)) return escHtml(d);
    return dt.toLocaleString(locale(), {month:'short',day:'numeric',hour:'numeric',minute:'2-digit'});
  } catch { return escHtml(d); }
}

// ── Small render helpers ──

// The sprite in index.html holds every icon; this just references one.
function icon(name, cls) {
  return '<svg class="icon' + (cls ? ' ' + cls : '') + '" aria-hidden="true"><use href="#i-' + name + '"/></svg>';
}

function initials(first, last) {
  const a = String(first || '').trim(), b = String(last || '').trim();
  const ch = x => Array.from(x)[0] || '';  // whole characters, not UTF-16 halves
  const s = (ch(a) + ch(b)) || '?';
  return escHtml(s.toUpperCase());
}

// Stored times are UTC, often without a zone ("2026-10-21 03:07:57").
function parseTs(d) {
  let ts = String(d || '').trim().replace(' ', 'T');
  if (ts && !/[zZ]$|[+-]\d\d:?\d\d$/.test(ts)) ts += 'Z';
  return new Date(ts);
}

// "3m ago", "5h ago", "Yesterday", then a date: recent things read faster.
function relTime(d) {
  if (!d) return '';
  const dt = parseTs(d);
  if (isNaN(dt)) return escHtml(d);
  const mins = Math.round((Date.now() - dt) / 60000);
  if (mins < -1 && mins > -60) return t('in {n}m', {n: -mins});
  if (mins < -1 && mins > -60 * 24) return t('in {n}h', {n: Math.round(-mins / 60)});
  if (mins >= -1 && mins < 1) return t('just now');
  if (mins >= 1 && mins < 60) return t('{n}m ago', {n: mins});
  if (mins >= 60 && mins < 60 * 24) return t('{n}h ago', {n: Math.round(mins / 60)});
  if (mins >= 60 * 24 && mins < 60 * 48) return t('Yesterday');
  const opts = {month: 'short', day: 'numeric'};
  if (dt.getFullYear() !== new Date().getFullYear()) opts.year = 'numeric';
  return dt.toLocaleDateString(locale(), opts);
}

function dayLabel(d) {
  const dt = parseTs(d);
  if (isNaN(dt)) return '';
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const that = new Date(dt); that.setHours(0, 0, 0, 0);
  const diff = Math.round((today - that) / 86400000);
  if (diff === 0) return t('Today');
  if (diff === 1) return t('Yesterday');
  return dt.toLocaleDateString(locale(), {weekday: 'long', month: 'short', day: 'numeric'});
}

function timeOfDay(d) {
  const dt = parseTs(d);
  return isNaN(dt) ? '' : dt.toLocaleTimeString(locale(), {hour: 'numeric', minute: '2-digit'});
}

// Columns of a tiny bar chart; the last day is "today".
function sparkBars(values, title) {
  const vals = (values || []).map(v => Number(v) || 0);
  if (!vals.length) return '';
  const max = Math.max(...vals, 1);
  return '<span class="spark" role="img" aria-label="' + escHtml(title || t('trend')) + '">' +
    vals.map((v, i) => '<i class="' + (v ? 'on' : '') + '" style="height:' +
      Math.max(2, Math.round(v / max * 28)) + 'px" title="' + v + '"></i>').join('') + '</span>';
}

// A segmented meter, 0-100.
function meterHtml(pct, segments) {
  const n = segments || 10;
  const v = Math.max(0, Math.min(100, Number(pct) || 0));
  const on = Math.round(v / 100 * n);
  return '<span class="meter-row"><span class="meter" role="meter" aria-valuemin="0" ' +
    'aria-valuemax="100" aria-valuenow="' + v + '">' +
    Array.from({length: n}, (_, i) => '<i class="' + (i < on ? 'on' : '') + '"></i>').join('') +
    '</span><span class="v">' + v + '</span></span>';
}

const SOURCE_LABELS = {
  company_website: 'Team page', known_company_backfill: 'Team page',
  scraped_mailto: 'Published address', hunter_domain_search: 'Hunter', linkedin_search: 'LinkedIn',
  job_board: 'Job board', web_search: 'Web search', company_inbox: 'Company inbox',
  osm: 'OpenStreetMap', dataforseo_listings: 'DataForSEO', dataforseo_serp: 'DataForSEO SERP', dataforseo_maps: 'DataForSEO Maps',
  serper: 'Serper',
};
function sourceLabel(s) {
  if (!s) return '';
  return escHtml(Object.prototype.hasOwnProperty.call(SOURCE_LABELS, s) ? t(SOURCE_LABELS[s])
    : String(s).replace(/_/g, ' ').replace(/^./, c => c.toUpperCase()));
}

const AGENT_ICONS = {scout: 'discover', writer: 'pen', sender: 'campaigns', handler: 'message',
                     analyst: 'usage', user: 'contacts', main: 'refresh'};
// One plain sentence per logged action, from the details each agent records.
function actionText(a) {
  const d = a.details || {};
  const who = escHtml(d.prospect_email || d.prospect || d.to || '');
  const camp = escHtml(d.campaign_name || d.campaign || '');
  const intent = d.intent ? ' · ' + escHtml(String(d.intent).replace(/_/g, ' ')) : '';
  const n = (v, one, many) => (Number(v) || 0) + ' ' + t((Number(v) || 0) === 1 ? one : many);
  switch (a.action_type) {
    case 'prospect': return t('Found {n}', {n: n(d.prospects_found, 'new contact', 'new contacts')});
    case 'write_campaign': return t('Wrote “{camp}” for {n}', {camp: camp, n: n(d.prospect_count, 'contact', 'contacts')});
    case 'write_failed': return t('Couldn’t write for some contacts') + (camp ? t(' in “{camp}”', {camp: camp}) : '');
    case 'stage_campaign': return t('Queued {n} from “{camp}”', {n: n(d.staged, 'email', 'emails'), camp: camp}) +
      (d.mode ? ' · ' + escHtml(d.mode) : '');
    case 'send_campaign': return t('Sent “{camp}” to Instantly · {n}', {camp: camp, n: n(d.leads_added, 'lead', 'leads')});
    case 'email_sent': return d.kind === 'reply' ? t('Sent a reply to {who}', {who: who})
      : t('Sent email {step} to {who}', {step: escHtml(String(d.step || 1)), who: who});
    case 'reply': return t('Answered {who}{intent}', {who: who, intent: intent});
    case 'reply_queued': return t('Drafted a reply to {who}{intent}', {who: who, intent: intent});
    case 'opt_out': return t('{who} opted out', {who: who}) + (d.why ? ' · ' + escHtml(d.why) : '');
    case 'opt_out_undone': return t('You undid {who}’s opt-out', {who: who || t('a contact')});
    case 'reply_after_opt_out': return t('{who} wrote again after opting out · no reply sent', {who: who});
    case 'escalation': return t('Flagged {who} for you', {who: who}) + (d.reason ? ' · ' + escHtml(d.reason) : '');
    case 'bounce': return t('Email to {who} bounced', {who: who});
    case 'manual_send': return t('You marked a contact as emailed by hand');
    case 'manual_send_undone': return t('You undid a hand-sent mark');
    case 'outbox_edited': return t('You edited a draft');
    case 'conversation_status':
      return d.to === 'closed' ? t('You closed a conversation')
        : d.from === 'needs_human' ? t('You handed a conversation back to Harvey') : t('You reopened a conversation');
  }
  const text = escHtml(String(a.action_type || '').replace(/_/g, ' '));
  return a.agent === 'main' ? t('Heartbeat: {text}', {text: text}) : text;
}

function emptyState(glyph, title, copy) {
  return '<div class="empty"><div class="glyph">' + glyph + '</div>' +
    '<div class="title">' + title + '</div>' +
    '<div class="copy">' + copy + '</div></div>';
}

function offlineState() {
  return emptyState('&#9888;', t('Dashboard can\'t reach the server'),
    t('The dashboard process may have stopped. Restart it with <b>harvey dashboard</b> and refresh this page.'));
}

async function api(path, opts) {
  opts = Object.assign({}, opts);
  opts.headers = Object.assign({'X-Harvey-Lang': LANG}, opts.headers);
  try {
    const r = await authFetch(path, opts);
    if (await lockIfRefused(r)) return null;
    if (!r.ok) {
      // An action the server refused says why ("a run is already going");
      // pass that body on so the toast can show it. Reads stay null.
      if (!opts || !opts.method || opts.method === 'GET') return null;
      const body = await r.json().catch(() => null);
      return body && typeof body === 'object' ? {success: false, ...body} : null;
    }
    return await r.json();
  } catch {
    return null;
  }
}

// ── Locked: no token, a wrong one, or the server can't load its own ──

let _locked = false;

async function lockIfRefused(r) {
  if (r.status !== 401 && r.status !== 503) {
    if (r.ok && _locked) hideLock();
    return false;
  }
  let why = '';
  if (r.status === 503) {
    try { why = ((await r.json()) || {}).message || ''; } catch { /* ignore */ }
  }
  showLock(r.status === 503
    ? t('The dashboard server can\'t use its token file, so it is refusing every request. ') + why
    : authToken()
      ? t('The saved token was not accepted (the dashboard may have a new one).')
      : t('This tab doesn\'t have the dashboard token yet.'));
  return true;
}

function showLock(reason) {
  _locked = true;
  const el = document.getElementById('lock-screen');
  if (!el) return;
  document.getElementById('lock-reason').textContent = reason;
  el.style.display = 'flex';
  const dot = document.getElementById('header-dot');
  const text = document.getElementById('header-status-text');
  if (dot) dot.className = 'status-dot offline';
  if (text) text.textContent = t('Locked');
}

function hideLock() {
  _locked = false;
  const el = document.getElementById('lock-screen');
  if (el) el.style.display = 'none';
}

function unlockWithToken(event) {
  event.preventDefault();
  const input = document.getElementById('lock-token');
  const value = (input.value || '').trim();
  input.value = '';
  if (!setAuthToken(value)) {
    document.getElementById('lock-reason').textContent =
      t('That doesn\'t look like a dashboard token. Copy it again from the link or data/dashboard-token.');
    return;
  }
  hideLock();
  loadCurrentTab();
  loadHarveyStatus();
}

// Downloads go through the same authorised fetch and are handed to the
// browser as a local blob, so no download URL ever carries the token.
async function downloadCsv(path, filename, btn) {
  if (btn) btn.disabled = true;
  try {
    const r = await authFetch(path);
    if (await lockIfRefused(r)) return;
    if (!r.ok) { showToast(t('Export failed.'), 'error'); return; }
    const href = URL.createObjectURL(await r.blob());
    const a = document.createElement('a');
    a.href = href;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(href), 10000);
  } catch {
    showToast(t('Export failed — is the dashboard still running?'), 'error');
  } finally {
    if (btn) btn.disabled = false;
  }
}

function showToast(msg, type) {
  const toast = document.createElement('div');
  toast.className = 'toast ' + type;
  toast.textContent = msg;
  document.body.appendChild(toast);
  setTimeout(() => toast.remove(), 2600);
}

function toggleVisibility(inputId) {
  const el = document.getElementById(inputId);
  el.type = el.type === 'password' ? 'text' : 'password';
}

// ── Tabs ──

function showTab(id, btn) {
  currentTab = id;
  if (id === 'companies') companyDrill = false;
  document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
  document.querySelectorAll('nav button').forEach(b => b.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  if (btn) btn.classList.add('active');
  loadCurrentTab();
}

function loadCurrentTab() {
  switch (currentTab) {
    case 'today': loadLiveStatus(); loadToday(); loadTrend(); loadFunnel(); loadSetupStatus(); loadRuns(); loadTodayActivity(); break;
    case 'signals': loadSignals(); break;
    case 'discover': loadDiscoverProviders(); break;
    case 'companies': if (!companyDrill) loadCompanies(); break;
    case 'prospects': loadProspects(); break;
    case 'campaigns': loadCampaigns(); break;
    case 'outbox': loadOutbox(); break;
    case 'conversations': loadConversations(); break;
    case 'activity': loadActivity(); break;
    case 'usage': loadUsage(); break;
    case 'settings': loadSettings(); loadSendingModeCard(); break;
    case 'controls': loadHarveyStatus(); loadLogs(); break;
  }
}

// ── Today: what needs a human ──

async function loadToday() {
  const data = await api('/api/today');
  const el = document.getElementById('today-queue');
  if (!data) { el.innerHTML = offlineState(); return; }
  if (data.error) {
    // A failed check must never read as "Nothing needs you".
    el.innerHTML = emptyState(icon('alert'), t('Couldn\'t check what needs you'),
      t('The dashboard hit an error reading Harvey\'s data: {error}', {error: escHtml(data.error)}));
    return;
  }

  navCount('nav-today', (data.items || []).filter(i => i.tone !== 'good').length);
  navCount('nav-outbox', (data.stats || {}).outbox_pending || 0);
  navCount('nav-signals', (data.stats || {}).signals_proposed || 0);
  navCount('nav-conversations', (data.stats || {}).needs_human || 0);
  _todayStats = data.stats || {};
  renderFigures(_todayStats);

  const items = data.items || [];
  if (!items.length) {
    el.innerHTML = '<div class="queue-clear">' +
      '<div class="queue-icon">' + icon('check') + '</div><div>' +
      '<div class="qtitle">' + t('Nothing needs you') + '</div>' +
      '<div class="qdetail">' + t('Harvey has everything it needs. Anything that requires a decision — an email to approve, a signal to confirm — shows up here.') + '</div>' +
      '</div></div>';
    return;
  }

  const glyph = {bad: 'alert', warn: 'flag', good: 'check'};
  el.innerHTML = '<div class="queue">' + items.map(it => {
    const tone = escHtml(it.tone || 'warn');
    return '<div class="queue-item ' + tone + '">' +
      '<div class="queue-icon">' + icon(glyph[it.tone] || 'flag') + '</div>' +
      '<div class="qtext">' +
        '<div class="qtitle">' + escHtml(it.title) + '</div>' +
        '<div class="qdetail">' + escHtml(it.detail) + '</div>' +
      '</div>' +
      '<button class="btn btn-secondary btn-sm" onclick="goTab(' + jsq(it.tab) + ')">' +
        escHtml(it.action) + icon('arrow') + '</button>' +
    '</div>';
  }).join('') + '</div>';
}

// ── Live status: the "what's happening?" answer, kept on screen ──

async function loadLiveStatus() {
  const el = document.getElementById('live-status');
  if (!el) return;
  const d = await api('/api/live-status');
  if (!d || !Array.isArray(d.steps)) {
    // Keep the last good numbers on screen, but never let them pass for live.
    const flag = el.querySelector('.live-flag');
    if (flag) flag.textContent = t("Can't reach Harvey right now — numbers below may be old.");
    else el.innerHTML = '<p class="muted live-flag">' + t("Can't reach Harvey right now. Is the dashboard window still open?") + '</p>';
    return;
  }
  const glyph = {done: icon('check'), running: '<span class="pulse"></span>', partial: '', waiting: ''};
  const fill = {done: 'var(--good)', running: 'var(--accent)', partial: 'var(--accent)', waiting: 'var(--line-strong)'};
  const rows = d.steps.map(s => {
    const st = Object.prototype.hasOwnProperty.call(glyph, s.state) ? s.state : 'waiting';
    const pct = s.total ? Math.min(100, Math.round(100 * s.done / s.total)) : 0;
    const bar = s.total
      ? '<div class="bar"><span style="width:' + pct + '%;background:' + fill[st] + '"></span></div>'
      : '';
    return '<div class="step">' +
      '<span class="step-icon ' + st + '">' + glyph[st] + '</span>' +
      '<span class="step-label">' + escHtml(s.label) + '</span>' +
      '<span class="step-detail">' + escHtml(s.detail) + '</span>' + bar +
    '</div>';
  }).join('');
  const jobName = r => r.stage === 'profile' ? t('Reading websites')
    : r.stage === 'discover' ? t('Searching for businesses ({provider})', {provider: escHtml(r.provider || '')})
    : String(r.stage || t('Job'));
  const running = d.running || [];
  const runningHtml = running.length
    ? running.map(r => '<span class="live-now"><span class="pulse"></span>' + escHtml(jobName(r)) +
        ' · ' + r.elapsed_min + ' ' + t('min') + (r.eta_text ? ' · ' + escHtml(r.eta_text) : '') + '</span>').join('')
    : '<span class="live-now">' + t('Nothing running right now') + '</span>';
  const stoppedHtml = (d.stopped || []).length
    ? '<div class="banner bad" style="margin:8px 0 0">' + icon('alert') + '<p style="color:inherit">' +
      (d.stopped || []).map(r => escHtml(jobName(r))).join(', ') + ' ' + t('stopped before finishing.') + '</p></div>'
    : '';
  el.innerHTML =
    '<div class="live-head">' + runningHtml +
      '<span class="setup-pct">' + t('Cost so far ${amount}', {amount: Number(d.cost_usd || 0).toFixed(2)}) + '</span></div>' +
    stoppedHtml +
    '<div class="steps">' + rows + '</div>' +
    (d.next ? '<div class="live-next">' + icon('arrow') + '<span><b>' + t('Next') + '</b> · ' + escHtml(d.next) + '</span>' +
      (d.next_tab ? '<button class="btn btn-secondary btn-sm" onclick="goTab(' + jsq(d.next_tab) + ')">' + t('Go') + '</button>' : '') +
      '</div>' : '') +
    '<div class="live-flag">' + t('Updated {time} · refreshes every 10 seconds', {time: new Date().toLocaleTimeString(locale())}) + '</div>';
}

// KPI cards. Totals come from /api/today; the 14-day bars from /api/trend.
function renderFigures(stats) {
  const el = document.getElementById('today-kpis');
  if (!el) return;
  const trend = _trend || {};
  const sum = a => (a || []).reduce((x, y) => x + (Number(y) || 0), 0);
  const last7 = a => sum((a || []).slice(-7));
  const card = (label, value, spark, note) =>
    '<div class="kpi"><div class="kpi-label">' + label + '</div>' +
    '<div class="kpi-main"><span class="kpi-value' + (value ? '' : ' zero') + '">' + value + '</span>' + spark + '</div>' +
    '<span class="kpi-note">' + note + '</span></div>';

  // Instantly sends from its own servers, so the Outbox never sees them.
  const sent = stats.email_provider === 'instantly'
    ? card(t('Emails sent'), '&mdash;', '', t('Instantly tracks sends'))
    : card(t('Emails sent'), sum(trend.sent), sparkBars(trend.sent, t('Emails sent per day, last 14 days')),
           t('last 14 days · <b>{n}</b> scheduled', {n: stats.outbox_approved || 0}));
  const waiting = (stats.outbox_pending || 0) + (stats.needs_human || 0);
  const waitNote = [
    stats.outbox_pending ? t('{n} to approve', {n: stats.outbox_pending}) : '',
    stats.needs_human ? t('{n} handed to you', {n: stats.needs_human}) : '',
  ].filter(Boolean).join(' · ') || t('nothing to approve');
  const facts = document.getElementById('today-facts');
  if (facts) facts.innerHTML =
    '<span><b>' + (stats.companies || 0) + '</b> ' + t('companies') + '</span>' +
    '<span><b>' + (stats.unprofiled || 0) + '</b> ' + t('sites not read yet') + '</span>' +
    '<span><b>' + (stats.signals_confirmed || 0) + '</b> ' + t('signals on') + '</span>';

  el.innerHTML = '<div class="kpis">' +
    card(t('Contacts found'), stats.prospects || 0, sparkBars(trend.found, t('Contacts found per day, last 14 days')),
         t('<b>+{n}</b> this week', {n: last7(trend.found)})) +
    sent +
    card(t('Conversations'), stats.open_conversations || 0, sparkBars(trend.replies, t('New replies per day, last 14 days')),
         t('<b>{n}</b> new in 14 days', {n: sum(trend.replies)})) +
    card(t('Waiting on you'), waiting, '', waitNote) +
  '</div>';
}

let _todayStats = {};
let _trend = null;

async function loadTrend() {
  const trend = await api('/api/trend?days=14&tz=' + -new Date().getTimezoneOffset());
  if (trend && Array.isArray(trend.days)) { _trend = trend; renderFigures(_todayStats); }
}

// Contacts by stage: one bar, one legend, from /api/stats.
async function loadFunnel() {
  const el = document.getElementById('today-figures');
  if (!el) return;
  const data = await api('/api/stats');
  if (!data) return;  // offline: keep the last numbers; Today's queue says so
  if (data.error) { el.innerHTML = '<p class="muted" style="font-size:13px">' + t("Couldn't read the pipeline.") + '</p>'; return; }
  const total = (data.prospects || {}).total || 0;
  const pipe = data.pipeline || {};
  const stages = [
    [t('To write'), pipe.to_write || 0, 'c-accent-soft'],
    [t('Contacted'), pipe.contacted || 0, 'c-accent'],
    [t('Replied'), pipe.replied || 0, 'c-active'],
    [t('Interested'), pipe.interested || 0, 'c-good'],
    [t('Ready to meet'), pipe.ready_to_meet || 0, 'c-note'],
    [t('Closed out'), pipe.closed_out || 0, 'c-idle'],
  ];
  if (!total) {
    el.innerHTML = '<p class="muted" style="font-size:13px">' + t('No contacts yet. Scout adds them as it finds people.') + '</p>';
    return;
  }
  el.innerHTML =
    '<div class="funnel-bar" role="img" aria-label="' + t('Contacts by stage') + '">' +
      stages.filter(s => s[1] > 0).map(s =>
        '<span class="seg ' + s[2] + '" style="flex:' + s[1] + '" title="' + s[0] + ': ' + s[1] + '"></span>').join('') +
    '</div>' +
    '<div class="legend-rows">' + stages.map(s =>
      '<div class="legend-row"><span class="legend-key"><i class="' + s[2] + '"></i>' + s[0] + '</span>' +
      '<span class="legend-val">' + s[1] + '</span>' +
      '<span class="legend-pct">' + Math.round(100 * s[1] / total) + '%</span></div>'
    ).join('') + '</div>';
}

async function loadTodayActivity() {
  const el = document.getElementById('today-activity');
  if (!el) return;
  const data = await api('/api/activity');
  if (!Array.isArray(data)) return;  // offline: Today's queue says so
  // Heartbeats are noise here; the Activity tab keeps them.
  const shown = data.filter(a => a.agent !== 'main').slice(0, 8);
  if (!shown.length) {
    el.innerHTML = '<p class="muted" style="font-size:13px">' +
      t('Nothing yet. Every action Harvey takes shows up here.') + '</p>';
    return;
  }
  el.innerHTML = '<div class="timeline">' + shown.map(a =>
    '<div class="tl-item">' +
      '<span class="tl-time">' + relTime(a.created_at) + '</span>' +
      '<span class="tl-icon">' + icon(own(AGENT_ICONS, a.agent) || 'activity') + '</span>' +
      '<span class="tl-text">' + actionText(a) + '</span>' +
      '<span class="tl-agent">' + escHtml(a.agent) + '</span>' +
    '</div>').join('') + '</div>';
}

function navCount(id, n) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = n > 0 ? n : '';
  el.className = 'nav-count' + (n > 0 ? ' on' : '');
}

// Jump to a tab from a link that isn't itself a nav button.
function goTab(id) {
  const btns = [...document.querySelectorAll('nav button')];
  const btn = btns.find(b => (b.getAttribute('onclick') || '').includes("'" + id + "'"));
  showTab(id, btn);
}

async function loadRuns() {
  const el = document.getElementById('today-runs');
  const block = document.getElementById('today-runs-block');
  if (!el) return;
  const runs = await api('/api/runs');
  if (!Array.isArray(runs) || !runs.length) {
    if (block) block.style.display = 'none';
    el.innerHTML = '';
    return;
  }
  if (block) block.style.display = '';
  el.innerHTML = '<div class="runs">' + runs.slice(0, 5).map(r =>
    '<div class="run">' +
      '<span class="name">' + escHtml(r.stage) + '</span>' + badge(r.status) +
      '<span class="when">' + relTime(r.started_at) + '</span>' +
      '<span class="stats">' + t('{n} records · {cost}', {
        n: (r.records || 0),
        cost: (r.cost_usd ? '$' + Number(r.cost_usd).toFixed(4) : t('free'))
      }) + '</span>' +
    '</div>').join('') + '</div>';
}

// ── Setup checklist (lives on Today, and disappears once it's done) ──

async function loadSetupStatus() {
  const el = document.getElementById('today-setup');
  if (!el) return;
  const data = await api('/api/setup-status');
  if (!data || !data.checks) { el.innerHTML = ''; return; }

  const pct = data.percent || 0;
  // Finished setup disappears completely rather than greeting you forever.
  if (pct === 100) { el.innerHTML = ''; return; }

  const renderCheck = (c, optional) =>
    '<div class="check-item">' +
      (c.done ? '<span class="check-icon done">' + icon('check') + '</span>'
              : '<span class="check-icon pending"></span>') +
      '<div class="check-info">' +
        '<div class="check-label ' + (c.done ? 'done' : '') + '">' + escHtml(c.label) +
          (optional ? '<span class="optional-tag">' + t('optional') + '</span>' : '') + '</div>' +
        (!c.done ? '<div class="check-help">' + escHtml(c.help) + '</div>' : '') +
      '</div></div>';

  const required = data.checks.filter(c => c.required);
  const optional = data.checks.filter(c => !c.required);
  const done = required.filter(c => c.done).length;

  el.innerHTML =
    '<div class="subhead">' + t('Setup') + ' <span class="setup-pct">' +
      t('{done} of {total} done', {done: done, total: required.length}) + '</span></div>' +
    '<div class="card">' +
      '<div class="bar thick" style="margin-bottom:8px"><span style="width:' + pct + '%;background:var(--accent)"></span></div>' +
      required.map(c => renderCheck(c, false)).join('') +
      (optional.length
        ? '<details style="margin-top:10px"><summary class="muted" ' +
          'style="font-size:12px">' + t('{n} optional', {n: optional.length}) + '</summary>' +
          '<div style="margin-top:6px">' +
          optional.map(c => renderCheck(c, true)).join('') + '</div></details>'
        : '') +
    '</div>';
}

// ── Signals: Harvey proposes, you confirm ──

async function loadSignals() {
  const data = await api('/api/signals');
  const sumEl = document.getElementById('signals-summary');
  const grpEl = document.getElementById('signals-groups');
  if (!data || data.error) { grpEl.innerHTML = offlineState(); sumEl.innerHTML = ''; return; }
  _signals = data;

  const s = data.summary || {};
  navCount('nav-signals', s.proposed || 0);
  const parts = [[t('Confirmed'), s.confirmed || 0, 'c-good'], [t('Waiting on you'), s.proposed || 0, 'c-wait'],
                 [t('Turned off'), s.rejected || 0, 'c-idle']];
  const all = parts.reduce((x, p) => x + p[1], 0) || 1;
  sumEl.innerHTML = '<div class="card sig-summary"><div class="funnel-bar">' +
    parts.filter(p => p[1]).map(p => '<span class="seg ' + p[2] + '" style="flex:' + p[1] + '"></span>').join('') +
    '</div><div class="funnel-legend">' + parts.map(p =>
      '<div class="legend-item"><span class="legend-key"><i class="' + p[2] + '"></i>' + p[0] + '</span>' +
      '<span class="legend-val">' + p[1] + '<small>' + Math.round(100 * p[1] / all) + '%</small></span></div>'
    ).join('') + '</div></div>';

  grpEl.innerHTML = (data.groups || []).map(g => {
    // Only the undecided ones: "Confirm all" must never re-enable a signal
    // you turned off (or "Skip all" turn off one you confirmed).
    const pending = escHtml(JSON.stringify(g.signals.filter(x => x.status === 'proposed').map(x => x.code)));
    const undecided = g.signals.filter(x => x.status === 'proposed').length;
    return '<div class="sig-group">' +
      '<div class="sig-group-head"><div>' +
        '<h3>' + escHtml(g.label) + '</h3><p>' + escHtml(g.blurb) + '</p>' +
      '</div>' +
      (undecided
        ? '<div style="display:flex;gap:6px;flex-shrink:0">' +
            '<button class="btn btn-primary btn-sm" onclick="setSignals(' + pending +
              ', \'confirmed\')">' + t('Confirm all {n}', {n: undecided}) + '</button>' +
            '<button class="btn btn-secondary btn-sm" onclick="setSignals(' + pending +
              ', \'rejected\')">' + t('Skip all {n}', {n: undecided}) + '</button>' +
          '</div>'
        : '') +
      '</div>' +
      '<div class="card flush">' + g.signals.map(sigRow).join('') + '</div>' +
    '</div>';
  }).join('');

  renderCohortBuilder();
}

function sigRow(sig) {
  const free = /free|included/i.test(sig.cost_note || '');
  const costCls = free ? 'free' : (/\$/.test(sig.cost_note || '') ? 'paid' : '');
  const seen = sig.companies
    ? '<span class="cost-chip">' + t(sig.companies === 1 ? 'seen on {n} company so far' : 'seen on {n} companies so far', {n: sig.companies}) + '</span>' : '';
  const floor = sig.confidence_floor > 0
    ? '<span class="cost-chip">' + t('only recorded above {n}% confidence', {n: Math.round(sig.confidence_floor * 100)}) + '</span>' : '';

  const decide = sig.status === 'confirmed'
    ? '<button class="btn btn-secondary btn-sm" onclick="setSignals([' + jsq(sig.code) +
        '],\'rejected\')">' + t('Turn off') + '</button>'
    : sig.status === 'rejected'
      ? '<button class="btn btn-secondary btn-sm" onclick="setSignals([' + jsq(sig.code) +
          '],\'confirmed\')">' + t('Turn on') + '</button>'
      : '<button class="btn btn-primary btn-sm" onclick="setSignals([' + jsq(sig.code) +
          '],\'confirmed\')">' + t('Confirm') + '</button>' +
        '<button class="btn btn-secondary btn-sm" onclick="setSignals([' + jsq(sig.code) +
          '],\'rejected\')">' + t('Skip') + '</button>';

  return '<div class="sig-row ' + (sig.status === 'rejected' ? 'rejected' : '') + '">' +
    '<div class="sig-main">' +
      '<div class="sig-label">' + escHtml(sig.label || sig.code) +
        '<span class="sig-code">' + escHtml(sig.code) + '</span></div>' +
      '<div class="sig-desc">' + escHtml(sig.description) + '</div>' +
      '<div class="sig-meta">' +
        '<span class="cost-chip ' + costCls + '">' + escHtml(sig.cost_note || t('cost unknown')) + '</span>' +
        floor + seen + badge(sig.status) +
      '</div>' +
    '</div>' +
    '<div class="sig-decide">' + decide + '</div>' +
  '</div>';
}

async function setSignals(codes, status) {
  const data = await api('/api/signals/status', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({codes: codes, status: status}),
  });
  if (data && data.success) {
    showToast(
      status === 'confirmed'
        ? t(data.changed === 1
            ? 'Confirmed {n} signal — Harvey will collect it from now on.'
            : 'Confirmed {n} signals — Harvey will collect them from now on.',
            {n: data.changed})
        : t(data.changed === 1
            ? '{n} signal turned off.'
            : '{n} signals turned off.',
            {n: data.changed}),
      'success');
  } else {
    showToast(failMsg(data, t('Could not update that signal.')), 'error');
  }
  loadSignals();
}

// ── Discover: pick a source, price it, then run it ──

let _provider = null;
let _discoverPoll = null;

async function loadDiscoverProviders() {
  const el = document.getElementById('discover-providers');
  const data = await api('/api/discover/providers');
  if (!data || !data.providers) { el.innerHTML = offlineState(); return; }

  _provider = _provider || data.selected || data.default;
  el.innerHTML = '<div class="prov-grid">' + data.providers.map(p => {
    const ready = p.configured
      ? '<span class="badge t-good">' + t('ready') + '</span>'
      : '<span class="badge t-waiting">' + t('needs a key') + '</span>';
    return '<div class="prov-card ' + (p.key === _provider ? 'selected' : '') +
      '" onclick="pickProvider(' + jsq(p.key) + ')">' +
      '<div class="prov-head"><h3>' + escHtml(p.label) + '</h3>' + ready + '</div>' +
      '<div class="blurb">' + escHtml(p.blurb) + '</div>' +
      '<div class="row"><span class="k">' + t('Cost') + '</span><span class="v">' +
        escHtml(p.cost_note) + '</span></div>' +
      '<div class="row"><span class="k">' + t('Free') + '</span><span class="v">' +
        escHtml(p.free_tier) + '</span></div>' +
      (p.needs_key
        ? '<div class="row"><span class="k">' + t('Setup') + '</span><span class="v">' +
          escHtml(p.env_keys.join(', ')) + ' ' + t('in .env') + ' &middot; ' +
          '<a href="' + safeHref(p.signup_url) + '" target="_blank" rel="noopener noreferrer">' + t('get a key') + '</a>' +
          '</span></div>'
        : '') +
      (p.caveat ? '<div class="caveat">' + escHtml(p.caveat) + '</div>' : '') +
    '</div>';
  }).join('') + '</div>';

  const running = data.running;
  // Reading sites can't be stopped part-way; only a discovery run can.
  document.getElementById('disc-stop').style.display = running && data.running_kind !== 'profile' ? '' : 'none';
  renderProfileCard(data.unprofiled || 0, running);
  if (running && !_discoverPoll) {
    _discoverPoll = setInterval(loadDiscoverProviders, 4000);
  } else if (!running && _discoverPoll) {
    clearInterval(_discoverPoll);
    _discoverPoll = null;
    // A finished run leaves the button stuck on "Running…" otherwise.
    const btn = document.getElementById('disc-run');
    btn.disabled = true;
    btn.textContent = t('Estimate first');
    showToast(data.running_kind === 'profile' ? t('Finished reading websites.') : t('Discovery finished.'), 'success');
  }
  renderDiscoverResult(data.last_report, running);
}

function pickProvider(key) {
  _provider = key;
  document.getElementById('disc-run').disabled = true;
  document.getElementById('disc-run').textContent = t('Estimate first');
  document.getElementById('discover-estimate').innerHTML = '';
  loadDiscoverProviders();
}

function discoverBody() {
  const raw = document.getElementById('disc-cities').value.trim();
  return {
    provider: _provider,
    // Semicolons or newlines, never commas: "Denver, CO" is one city.
    cities: raw ? raw.split(/[;\n]/).map(c => c.trim()).filter(Boolean) : [],
    depth: Number(document.getElementById('disc-depth').value) || 30,
    limit: Number(document.getElementById('disc-limit').value) || 100,
    max_spend: Number(document.getElementById('disc-cap').value) || 1,
    preset: (document.getElementById('disc-preset') || {}).value || '',
  };
}

async function estimateDiscovery() {
  const el = document.getElementById('discover-estimate');
  el.innerHTML = '<p class="muted" style="font-size:13px">' + t('Pricing it…') + '</p>';
  const data = await api('/api/discover/estimate', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(discoverBody()),
  });
  if (!data || data.error) {
    el.innerHTML = '<div class="test-result error">' +
      escHtml((data && data.error) || t('Could not estimate.')) + '</div>';
    return;
  }

  const cap = discoverBody().max_spend;
  const over = data.estimated_cost > cap;
  el.innerHTML = '<div class="estimate-box">' +
    '<div class="amount ' + (data.free ? 'free' : 'paid') + '">' +
      (data.free ? t('Free') : '$' + data.estimated_cost.toFixed(4)) + '</div>' +
    '<div class="muted" style="font-size:13px;margin-top:2px">' +
      t(data.query_count === 1 ? '{n} query' : '{n} queries', {n: data.query_count}) +
      (data.free ? '' : ' &middot; ' + t('cap is ${amount}', {amount: cap.toFixed(2)})) + '</div>' +
    (over ? '<div class="test-result error" style="margin-top:12px">' +
      t('Over your cap. Raise the cap or narrow the search.') + '</div>' : '') +
    '<div class="query-list">' +
      data.queries.slice(0, 40).map(escHtml).join('<br>') +
      (data.queries.length > 40 ? '<br>' + t('… and {n} more', {n: data.queries.length - 40}) : '') +
    '</div></div>';

  const btn = document.getElementById('disc-run');
  btn.disabled = over;
  btn.textContent = over ? t('Over cap')
    : (data.free ? t('Run — free') : t('Run — spend up to ${amount}', {amount: data.estimated_cost.toFixed(2)}));
}

async function runDiscovery() {
  const btn = document.getElementById('disc-run');
  btn.disabled = true;
  btn.textContent = t('Running…');
  const data = await api('/api/discover/run', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(discoverBody()),
  });
  if (data && data.success) {
    showToast(t('Discovery started — {n} queries.', {n: data.queries}), 'success');
  } else {
    showToast((data && data.message) || t('Could not start.'), 'error');
    btn.disabled = false;
  }
  loadDiscoverProviders();
}

async function stopDiscovery() {
  const data = await api('/api/discover/stop', {method: 'POST'});
  showToast(data && data.success ? t('Stopping after the current query.')
                                 : failMsg(data, t('Could not stop.')), data && data.success ? 'success' : 'error');
  loadDiscoverProviders();
}

// Companies found but not looked at: reading their sites is free and
// model-free, and it is what makes an email specific.
function renderProfileCard(pending, running) {
  const el = document.getElementById('discover-profile');
  if (!el) return;
  if (!pending) { el.innerHTML = ''; return; }
  el.innerHTML = '<div class="subhead">' + t('Read websites') + '</div><div class="card"><div class="queue-item good" style="padding:0;border:0;box-shadow:none">' +
    '<div class="qtext"><div class="qtitle">' +
    t(pending === 1 ? '{n} company not looked at yet' : '{n} companies not looked at yet', {n: pending}) +
    '</div><div class="qdetail">' + t('Free, no account needed. Harvey reads each site for who their agency is, what they are missing, and whether they run ads. Up to 200 per run.') + '</div></div>' +
    '<button class="btn btn-primary btn-sm" onclick="runProfile(this)"' + (running ? ' disabled' : '') + '>' +
    (running ? t('Busy…') : t('Read their sites')) + '</button></div></div>';
}

async function runProfile(btn) {
  btn.disabled = true;
  btn.textContent = t('Starting…');
  const data = await api('/api/profile/run', {method: 'POST'});
  if (data && data.success) {
    showToast(data.pending ? t('Reading {n} websites.', {n: Math.min(data.pending, 200)}) : t('Every site is already read.'), 'success');
    if (data.pending && !_discoverPoll) _discoverPoll = setInterval(loadDiscoverProviders, 4000);
  } else {
    showToast(failMsg(data, t('Could not start.')), 'error');
  }
  loadDiscoverProviders();
}

function renderDiscoverResult(report, running) {
  const el = document.getElementById('discover-result');
  if (running) {
    el.innerHTML = '<div class="card"><h2>' + t('Running…') + '</h2>' +
      '<p class="muted" style="font-size:13px">' +
      t('Harvey is working through the queries. Results land in Companies, and the run log is on Today.') + '</p></div>';
    return;
  }
  if (!report) { el.innerHTML = ''; return; }

  const stat = (label, value, tone) =>
    '<div class="stat-card"><div class="label">' + label + '</div>' +
    '<div class="value"' + (tone ? ' style="color:var(--' + tone + ')"' : '') + '>' +
    value + '</div></div>';

  if (report.profiled_companies !== undefined && report.new_companies === undefined) {
    el.innerHTML = '<div class="subhead">' + t('Last run') + '</div><div class="stats-grid">' +
      stat(t('Websites read'), report.profiled_companies || 0, 'accent') +
      stat(t('Facts recorded'), report.profile_observations || 0) +
      stat(t('Cost'), t('free')) + '</div>' +
      (report.stopped ? '<div class="test-result error" style="margin-top:14px">' + t('Stopped early: {reason}', {reason: escHtml(report.stopped)}) + '</div>' : '');
    return;
  }
  el.innerHTML = '<div class="subhead">' + t('Last run') + '</div>' +
    '<div class="stats-grid">' +
      stat(t('New companies'), report.new_companies || 0, 'accent') +
      stat(t('Already known'), report.known_companies || 0) +
      stat(t('Observations'), report.observations || 0) +
      stat(t('Filtered as junk'), report.junk || 0) +
      (report.profiled_companies ? stat(t('Websites read'), report.profiled_companies) : '') +
      stat(t('Actual cost'), report.actual_cost ? '$' + report.actual_cost.toFixed(4) : t('free')) +
    '</div>' +
    (report.stopped ? '<div class="test-result error" style="margin-top:14px">' +
      t('Stopped early: {reason}', {reason: escHtml(report.stopped)}) + '</div>' : '') +
    ((report.errors || []).length
      ? '<div class="card" style="margin-top:14px"><h2>' + t('Problems') + '</h2>' +
        (report.errors || []).slice(0, 8).map(e =>
          '<div class="check-help">' + escHtml(e) + '</div>').join('') + '</div>'
      : '');
}

// ── Cohort builder: a prospect list is a query ──

let _cohort = { require: new Set(), exclude: new Set(), ranges: {} };
let _cohortOffset = 0;
let _cohortGeneration = 0;
const COHORT_PAGE = 50;

function renderCohortBuilder() {
  const el = document.getElementById('cohort-builder');
  if (!el || !_signals) return;

  const confirmed = (_signals.groups || [])
    .flatMap(g => g.signals)
    .filter(s => s.status === 'confirmed');

  if (!confirmed.length) {
    el.innerHTML = '<p class="muted" style="font-size:13px">' +
      t('Confirm some signals above and they become the building blocks here.') + '</p>';
    document.getElementById('cohort-result').innerHTML = '';
    return;
  }

  const col = (title, key, hint) =>
    '<div><div class="subhead" style="margin-top:0">' + title +
      ' <span class="muted" style="font-weight:400;text-transform:none;letter-spacing:0">' +
      hint + '</span></div><div class="cohort-grid">' +
    confirmed.map(s =>
      '<label class="cohort-pick"><input type="checkbox" ' +
        (_cohort[key].has(s.code) ? 'checked ' : '') +
        'onchange="toggleCohort(\'' + key + '\',' + jsq(s.code) + ',this.checked)">' +
        '<span>' + escHtml(s.label || s.code) + '</span>' +
        '<span class="n">' + (s.companies || 0) + '</span></label>'
    ).join('') + '</div></div>';

  // Numeric signals (ranks, ratings) take a range: "organic rank 11-30".
  const numeric = confirmed.filter(s => s.value_type === 'num');
  const bound = (code, side) => {
    const v = (_cohort.ranges[code] || {})[side];
    return '<input type="number" class="cohort-bound" placeholder="' + side + '" ' +
      'value="' + (v === undefined || v === null ? '' : escHtml(String(v))) + '" ' +
      'aria-label="' + escHtml(code) + ' ' + side + '" ' +
      'onchange="setCohortRange(' + jsq(code) + ',' + jsq(side) + ',this.value)">';
  };
  const rangeCol = numeric.length ?
    '<div style="height:18px"></div><div><div class="subhead" style="margin-top:0">' + t('In range') +
      ' <span class="muted" style="font-weight:400;text-transform:none;letter-spacing:0">' +
      t('&mdash; e.g. organic rank 11 to 30; leave a side blank for no limit') + '</span></div>' +
      '<div class="cohort-grid">' +
      numeric.map(s =>
        '<div class="cohort-pick cohort-range"><span>' + escHtml(s.label || s.code) + '</span>' +
          bound(s.code, 'min') + '<span class="muted">' + t('to') + '</span>' + bound(s.code, 'max') + '</div>'
      ).join('') + '</div></div>' : '';

  el.innerHTML = col(t('Must have'), 'require', t('&mdash; every company in the cohort carries all of these')) +
    '<div style="height:18px"></div>' +
    col(t('Must not have'), 'exclude', t('&mdash; disqualifiers')) + rangeCol;
}

function setCohortRange(code, side, value) {
  const r = _cohort.ranges[code] || {min: null, max: null};
  r[side] = value === '' || isNaN(Number(value)) ? null : Number(value);
  if (r.min === null && r.max === null) delete _cohort.ranges[code];
  else _cohort.ranges[code] = r;
  _cohortOffset = 0;
  runCohort();
}

function toggleCohort(key, code, on) {
  if (on) _cohort[key].add(code); else _cohort[key].delete(code);
  // A company can't be both required and excluded on the same signal.
  const other = key === 'require' ? 'exclude' : 'require';
  if (on) _cohort[other].delete(code);
  _cohortOffset = 0;  // a new cohort starts on its first page
  runCohort();
  renderCohortBuilder();
}

async function runCohort() {
  const generation = ++_cohortGeneration;
  const offset = _cohortOffset;
  const el = document.getElementById('cohort-result');
  if (!el) return;
  const require = [..._cohort.require], exclude = [..._cohort.exclude];
  const ranges = _cohortRanges();
  if (!require.length && !ranges.length) { el.innerHTML = ''; return; }
  // Remove the prior export button as soon as its criteria become stale.
  el.innerHTML = '<p class="muted">' + t('Loading cohort…') + '</p>';

  const data = await api('/api/cohort', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({require: require, exclude: exclude, ranges: ranges,
                          offset: offset, limit: COHORT_PAGE}),
  });
  if (generation !== _cohortGeneration) return;
  if (!data || data.error) {
    el.innerHTML = data ? '<div class="test-result error">' + escHtml(data.error) + '</div>' : '';
    return;
  }

  let html = '<div style="border-top:1px solid var(--border);margin-top:20px;padding-top:20px">' +
    '<div class="cohort-size">' + data.size + '</div>' +
    '<div class="muted" style="font-size:13px;margin-top:2px">' +
      t(data.size === 1 ? '{n} company matches this cohort right now' : '{n} companies match this cohort right now', {n: data.size}) +
      '</div>';

  if (data.companies && data.companies.length) {
    html += '<div class="table-card" style="margin-top:16px"><table><thead><tr>' +
      '<th>' + t('Company') + '</th><th>' + t('Domain') + '</th><th>' + t('Industry') + '</th><th>' + t('Location') + '</th>' +
      '</tr></thead><tbody>' +
      data.companies.map(c =>
        '<tr><td>' + escHtml(c.name) + '</td><td class="muted">' + escHtml(c.domain) + '</td>' +
        '<td class="muted">' + escHtml(c.industry) + '</td>' +
        '<td class="muted">' + escHtml(c.location) + '</td></tr>'
      ).join('') + '</tbody></table></div>';

    const first = data.offset + 1, last = data.offset + data.companies.length;
    const hasPrev = data.offset > 0, hasNext = last < data.size;
    html += '<div style="display:flex;align-items:center;gap:8px;margin-top:10px">' +
      '<span class="muted" style="font-size:12px">' +
        t('Showing {first}&ndash;{last} of {total}', {first: first, last: last, total: data.size}) +
        '</span>' +
      '<span style="flex:1"></span>' +
      '<button class="btn btn-secondary btn-sm" onclick="pageCohort(-1)"' +
        (hasPrev ? '' : ' disabled') + '>' + t('Prev') + '</button>' +
      '<button class="btn btn-secondary btn-sm" onclick="pageCohort(1)"' +
        (hasNext ? '' : ' disabled') + '>' + t('Next') + '</button>' +
      '<button class="btn btn-secondary btn-sm" onclick="exportCohort(this)">' + t('Export CSV') + '</button>' +
      '</div>';
  } else if (data.size === 0) {
    html += '<p class="muted" style="font-size:13px;margin-top:12px">' +
      t('No company carries all of those yet. Either loosen the cohort, or run prospecting to collect more.') +
      '</p>';
  } else if (_cohortOffset > 0) {
    // The cohort shrank under us (a re-profile, a delete): back to page one.
    _cohortOffset = 0;
    return runCohort();
  }
  el.innerHTML = html + '</div>';
}

function _cohortRanges() {
  return Object.entries(_cohort.ranges || {})
    .filter(([, r]) => r.min !== null || r.max !== null)
    .map(([code, r]) => ({code: code, min: r.min, max: r.max}));
}

async function exportCohort(button) {
  const qs = new URLSearchParams({require: [..._cohort.require].join(','),
                                  exclude: [..._cohort.exclude].join(',')});
  // Ranges travel as "CODE:min:max", a blank side meaning no limit.
  const ranges = Object.entries(_cohort.ranges || {})
    .map(([code, r]) => code + ':' + (r.min ?? '') + ':' + (r.max ?? ''));
  if (ranges.length) qs.set('ranges', ranges.join(','));
  button.disabled = true;
  button.textContent = t('Exporting…');
  let url;
  let link;
  try {
    const response = await authFetch('/api/cohort.csv?' + qs);
    if (await lockIfRefused(response)) throw new Error('Dashboard authorization required');
    if (!response.ok) throw new Error('Export failed (HTTP ' + response.status + ')');
    url = URL.createObjectURL(await response.blob());
    link = document.createElement('a');
    link.href = url;
    link.download = 'cohort.csv';
    document.body.appendChild(link);
    link.click();
    button.textContent = t('Export CSV');
  } catch (error) {
    button.textContent = t('Export failed — retry');
  } finally {
    if (link) link.remove();
    // Give the browser time to start consuming the download before revoking.
    if (url) setTimeout(() => URL.revokeObjectURL(url), 1000);
    button.disabled = false;
  }
}

function pageCohort(dir) {
  _cohortOffset = Math.max(0, _cohortOffset + dir * COHORT_PAGE);
  runCohort();
}

// ── Settings ──

async function loadSettings() {
  const data = await api('/api/settings');
  if (!data) return;

  // Active provider indicator
  const prov = data.provider || 'instantly';
  const provEl = document.getElementById('active-provider');
  if (provEl) provEl.textContent = prov;

  // Secret fields never echo a value — show a "saved" placeholder instead.
  const savedPh = (id, isSet, base) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.value = '';
    el.placeholder = isSet ? t('Saved — enter new value to change') : base;
  };
  const tag = (id, ok, okLabel) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.className = 'email-tag ' + (ok ? 'verified' : 'guess');
    el.textContent = ok ? okLabel : t('not set');
  };

  // Non-secret values repopulate
  document.getElementById('gmail-id').value = data.gmail_client_id || '';
  document.getElementById('smtp-host').value = data.smtp_host || '';
  document.getElementById('smtp-port').value = data.smtp_port || '';
  document.getElementById('smtp-user').value = data.smtp_username || '';
  document.getElementById('imap-host').value = data.imap_host || '';
  document.getElementById('imap-port').value = data.imap_port || '';
  document.getElementById('imap-user').value = data.imap_username || '';
  document.getElementById('m365-client').value = data.m365_client_id || '';
  document.getElementById('m365-tenant').value = data.m365_tenant_id || '';
  document.getElementById('linkedin-email').value = data.linkedin_email || '';
  document.getElementById('cf-account-id').value = data.cloudflare_account_id || '';

  savedPh('gmail-secret', data.gmail_client_secret_set, t('Enter client secret'));
  savedPh('smtp-pass', data.smtp_password_set, t('Enter password / app password'));
  savedPh('imap-pass', data.imap_password_set, t('Same as SMTP password'));
  document.getElementById('imap-same-pass').checked = !data.imap_password_set;
  savedPh('instantly-key', data.instantly_api_key_set, t('Enter your Instantly API key'));
  savedPh('reoon-key', data.reoon_api_key_set, t('600 free/mo — reoon.com/email-verifier'));
  savedPh('zerobounce-key', data.zerobounce_api_key_set, t('100 free/mo — best for M365/Workspace catch-alls'));
  savedPh('hunter-key', data.hunter_api_key_set, t('50 free/mo + email-pattern lookup'));
  savedPh('linkedin-password', data.linkedin_password_set, t('Enter password'));
  savedPh('cf-api-token', data.cloudflare_api_token_set, t('Your Cloudflare API Token'));

  // Gmail auth status chip
  const g = document.getElementById('gmail-status');
  if (g) {
    if (data.gmail_authorized) { g.className = 'email-tag verified'; g.textContent = t('authorized'); }
    else if (data.gmail_client_secret_set) { g.className = 'email-tag risky'; g.textContent = t('run: harvey gmail auth'); }
    else { g.className = 'email-tag guess'; g.textContent = t('not set up'); }
  }
  const m = document.getElementById('m365-status');
  if (m) {
    if (data.m365_authorized) { m.className = 'email-tag verified'; m.textContent = t('authorized'); }
    else if (data.m365_client_id) { m.className = 'email-tag risky'; m.textContent = t('run: harvey microsoft auth'); }
    else { m.className = 'email-tag guess'; m.textContent = t('not set up'); }
  }
  tag('reoon-status', data.reoon_api_key_set, t('set'));
  tag('zerobounce-status', data.zerobounce_api_key_set, t('set'));
  tag('hunter-status', data.hunter_api_key_set, t('set'));
}

function saveGmail() {
  const payload = {GMAIL_CLIENT_ID: document.getElementById('gmail-id').value.trim()};
  const sec = document.getElementById('gmail-secret').value;
  if (sec) payload.GMAIL_CLIENT_SECRET = sec;
  saveEnv(payload, t('Gmail OAuth saved. Now run "harvey gmail auth" in your terminal.')).then(loadSettings);
}

function saveSmtp() {
  const payload = {
    SMTP_HOST: document.getElementById('smtp-host').value.trim(),
    SMTP_PORT: document.getElementById('smtp-port').value.trim(),
    SMTP_USERNAME: document.getElementById('smtp-user').value.trim(),
  };
  const pass = document.getElementById('smtp-pass').value;
  if (pass) payload.SMTP_PASSWORD = pass;
  // Blank IMAP fields fall back to the SMTP ones. A typed IMAP password wins;
  // otherwise the checkbox clears it back to the SMTP one, and an unticked
  // empty box keeps whatever IMAP password is saved.
  payload.IMAP_HOST = document.getElementById('imap-host').value.trim();
  payload.IMAP_PORT = document.getElementById('imap-port').value.trim();
  payload.IMAP_USERNAME = document.getElementById('imap-user').value.trim();
  const imapPass = document.getElementById('imap-pass').value;
  if (imapPass) payload.IMAP_PASSWORD = imapPass;
  else if (document.getElementById('imap-same-pass').checked) payload.IMAP_PASSWORD = '';
  saveEnv(payload, t('SMTP settings saved.')).then(loadSettings);
}

function saveMicrosoft() {
  const client = document.getElementById('m365-client').value.trim();
  if (!client) { showToast(t('Paste the Application (client) ID first.'), 'error'); return; }
  saveEnv({M365_CLIENT_ID: client, M365_TENANT_ID: document.getElementById('m365-tenant').value.trim()},
          t('Saved. Now run "harvey microsoft auth" in your terminal.')).then(loadSettings);
}

function saveVerifiers() {
  const payload = {};
  const r = document.getElementById('reoon-key').value;
  const z = document.getElementById('zerobounce-key').value;
  const h = document.getElementById('hunter-key').value;
  if (r) payload.REOON_API_KEY = r;
  if (z) payload.ZEROBOUNCE_API_KEY = z;
  if (h) payload.HUNTER_API_KEY = h;
  if (!Object.keys(payload).length) { showToast(t('Enter at least one key first.'), 'error'); return; }
  saveEnv(payload, t('Verification keys saved.')).then(loadSettings);
}

async function saveEnv(payload, okMsg) {
  const data = await api('/api/settings/env', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  });
  if (data && data.success) showToast(okMsg, 'success');
  else showToast((data && data.message) || t('Save failed — is the dashboard still running?'), 'error');
}

// Secrets are only sent when typed: an empty box means "keep the saved one".
function saveInstantly() {
  const key = document.getElementById('instantly-key').value.trim();
  if (!key) { showToast(t('Enter a new key first. The saved one is kept.'), 'error'); return; }
  saveEnv({INSTANTLY_API_KEY: key}, t('Instantly API key saved.'));
}

function saveLinkedIn() {
  const payload = {LINKEDIN_EMAIL: document.getElementById('linkedin-email').value.trim()};
  const pass = document.getElementById('linkedin-password').value;
  if (pass) payload.LINKEDIN_PASSWORD = pass;
  saveEnv(payload, t('LinkedIn credentials saved.'));
}

function saveCloudflare() {
  const payload = {CLOUDFLARE_ACCOUNT_ID: document.getElementById('cf-account-id').value.trim()};
  const token = document.getElementById('cf-api-token').value.trim();
  if (token) payload.CLOUDFLARE_API_TOKEN = token;
  saveEnv(payload, t('Cloudflare credentials saved.'));
}

async function testInstantly() {
  const key = document.getElementById('instantly-key').value.trim();
  const el = document.getElementById('instantly-test-result');
  el.innerHTML = '<div class="test-result pending">' + t('Testing&hellip;') + '</div>';
  const data = await api('/api/settings/test-instantly', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({api_key: key})
  });
  if (!data) {
    el.innerHTML = '<div class="test-result error">' + t('Could not reach the dashboard server.') + '</div>';
    return;
  }
  el.innerHTML = '<div class="test-result ' + (data.success ? 'success' : 'error') + '">' + escHtml(failMsg(data, '')) + '</div>';
}

// ── Controls ──

async function loadHarveyStatus() {
  const data = await api('/api/harvey/status');
  const headerDot = document.getElementById('header-dot');
  const headerText = document.getElementById('header-status-text');

  if (_locked) return;  // the header already says "Locked"
  if (!data) {
    headerDot.className = 'status-dot offline';
    headerText.textContent = t('Offline');
    return;
  }
  const running = !!data.running;

  headerDot.className = 'status-dot ' + (running ? 'running' : 'stopped');
  headerText.textContent = running ? t('Harvey is running') : t('Harvey is stopped');
  document.getElementById('control-dot').className = 'dot ' + (running ? 'running' : 'stopped');
  const label = document.getElementById('control-label');
  label.className = 'label ' + (running ? 'running' : 'stopped');
  label.textContent = running ? t('Running') : t('Stopped');

  const meta = document.getElementById('control-meta');
  if (running && data.pid) {
    let info = t('PID {pid}', {pid: escHtml(String(data.pid))});
    if (data.started_at) info += ' &middot; ' + t('started {date}', {date: formatDate(data.started_at)});
    meta.innerHTML = info;
  } else {
    meta.innerHTML = t('Harvey wakes every few minutes, does what needs doing, and sleeps.');
  }

  document.getElementById('btn-start').style.display = running ? 'none' : '';
  document.getElementById('btn-stop').style.display = running ? '' : 'none';
}

async function startHarvey() {
  const btn = document.getElementById('btn-start');
  btn.disabled = true;
  const data = await api('/api/harvey/start', {method: 'POST'});
  if (data && data.success) showToast(t('Harvey started.'), 'success');
  else showToast((data && data.message) || t('Failed to start.'), 'error');
  btn.disabled = false;
  loadHarveyStatus();
}

async function stopHarvey() {
  const btn = document.getElementById('btn-stop');
  btn.disabled = true;
  const data = await api('/api/harvey/stop', {method: 'POST'});
  if (data && data.success) showToast(t('Harvey stopped.'), 'success');
  else showToast((data && data.message) || t('Failed to stop.'), 'error');
  btn.disabled = false;
  loadHarveyStatus();
}

async function loadLogs() {
  const data = await api('/api/harvey/logs');
  const el = document.getElementById('log-viewer');
  if (data && data.lines && data.lines.length) {
    const stick = el.scrollTop + el.clientHeight >= el.scrollHeight - 30;
    el.textContent = data.lines.join('\n');
    if (stick) el.scrollTop = el.scrollHeight;
  } else {
    el.textContent = t('No logs yet. Start Harvey to see activity.');
  }
}

// ── Pipeline data ──

// Calls today per subscription — each labelled with the provider it ran on.
function brainCallChips(data) {
  const chip = (label, n) => '<span class="chip">' + label + ' ' + t('calls today') + ' <b>' + escHtml(String(n || 0)) + '</b></span>';
  const codex = data.codex_calls_today || 0;
  const st = data.brain_status || {};
  // A paused brain must not look healthy: say so, and why.
  const paused = st.status === 'paused'
    ? '<span class="chip" title="' + escHtml(st.reason || '') + '">' +
      escHtml(st.provider === 'codex' ? 'Codex' : 'Brain') + ' <b>' + t('paused') + '</b>: ' +
      escHtml((st.reason || '').slice(0, 120)) + '</span>'
    : '';
  if (data.brain_provider === 'codex') {
    return paused + chip('Codex', codex) + (data.claude_calls_today ? chip('Claude', data.claude_calls_today) : '');
  }
  return paused + chip('Claude', data.claude_calls_today) + (codex ? chip('Codex', codex) : '');
}

function fmtTokens(n) {
  n = n || 0;
  if (n >= 1e9) return (n / 1e9).toFixed(1) + 'B';
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'k';
  return String(n);
}


function emailTag(p) {
  if (!p.email) return '';
  // Fall back to the legacy boolean for rows predating email_status.
  const status = p.email_status || (p.email_verified ? 'verified' : 'guess');
  if (!own(STATUS, status)) return '';
  const m = statusMeta(status);
  return ' <span class="mini t-' + m.tone + '" title="' + escHtml(t('Email {status}', {status: m.label.toLowerCase()})) + '">' +
    escHtml(m.label) + '</span>';
}

// Quota gauges for the ACTIVE provider only: Claude's are the numbers
// `/usage` shows in Claude Code; Codex's are the ChatGPT plan's limits.
// When they can't be read, say so rather than hiding the card.
function quotaCardHtml(data) {
  const provider = data && data.quota_provider;
  if (!provider) return '';
  const title = provider === 'codex' ? t('Codex (ChatGPT plan) Quota') : t('Claude Subscription Quota');
  let html = '<h2>' + escHtml(title) + '</h2>';
  if (!data.quota || !Object.keys(data.quota).length) {
    const why = provider === 'codex'
      ? t("Quota unavailable: Harvey couldn't read your ChatGPT plan's limits (check codex login, the Codex CLI version, and network).")
      : t("Quota unavailable: Harvey couldn't read your Claude subscription's limits (check claude login and network).");
    return html + '<p class="muted">' + escHtml(why) + ' ' + t("Until it can, budgets count Harvey's own calls.") + '</p>';
  }
  const labels = {five_hour: t('5-hour window'), seven_day: t('Weekly'), primary: t('Primary window'),
                  secondary: t('Secondary window'), limit_reached: t('Usage limit reached')};
  for (const [key, w] of Object.entries(data.quota)) {
    const pct = Math.min(100, Math.max(0, w.utilization || 0));
    const color = pct >= 80 ? 'yellow' : 'green';
    const resets = w.resets_at ? t('resets {date}', {date: formatDate(w.resets_at)}) : '';
    html += '<div class="progress-wrap">' +
      '<div class="progress-label">' +
        '<span class="text">' + escHtml(labels[key] || key) + (resets ? ' &middot; ' + escHtml(resets) : '') + '</span>' +
        '<span class="pct">' + pct.toFixed(0) + '%</span>' +
      '</div>' +
      '<div class="progress-bar"><div class="progress-fill ' + color + '" style="width:' + pct + '%"></div></div>' +
    '</div>';
  }
  return html;
}

async function loadUsage() {
  const data = await api('/api/usage');
  const statsEl = document.getElementById('usage-stats');
  if (!data) { statsEl.innerHTML = offlineState(); return; }

  const quotaEl = document.getElementById('usage-quota');
  const qHtml = quotaCardHtml(data);
  quotaEl.innerHTML = qHtml;
  quotaEl.style.display = qHtml ? 'block' : 'none';

  // Totals cards — usage-first (calls is the headline; tokens as chips).
  // No dollars: on a subscription plan usage isn't billed per token.
  const totals = data.totals || {};
  const card = (label, p) => {
    p = p || {};
    return '<div class="stat-card"><div class="label">' + label + '</div>' +
      '<div class="value">' + (p.calls || 0) + '</div>' +
      '<div class="breakdown">' +
        '<span class="chip">' + t('calls') + '</span>' +
        '<span class="chip">' + t('out') + ' <b>' + fmtTokens(p.output_tokens) + '</b></span>' +
        '<span class="chip">' + t('in') + ' <b>' + fmtTokens(p.input_tokens) + '</b></span>' +
        '<span class="chip">' + t('cached') + ' <b>' + fmtTokens(p.cache_read_tokens) + '</b></span>' +
      '</div></div>';
  };
  statsEl.innerHTML = card(t('Today'), totals.today) + card(t('Last 7 Days'), totals.week) + card(t('Last 30 Days'), totals.month);

  // Daily columns — output tokens per day (the work done)
  const dailyEl = document.getElementById('usage-daily');
  const days = (data.by_day || []).slice(-30);
  if (days.length) {
    const maxOut = Math.max(...days.map(d => d.output_tokens || 0), 1);
    dailyEl.innerHTML = '<h2>' + t('Output tokens per day') + ' <span class="hint muted" style="font-weight:400">' +
      t('last {n} days · peak {peak}', {n: days.length, peak: fmtTokens(maxOut)}) + '</span></h2>' +
      '<div class="chart-cols" role="img" aria-label="' + escHtml(t('Output tokens per day')) + '">' + days.map(d =>
        '<span class="col" title="' + escHtml(d.day || '') + ': ' + escHtml(t('{out} out, {calls} calls', {out: fmtTokens(d.output_tokens), calls: d.calls || 0})) + '"><i style="height:' + Math.max(2, (d.output_tokens || 0) / maxOut * 100) + '%"></i></span>'
      ).join('') + '</div>' +
      '<div class="chart-axis"><span>' + escHtml(days[0].day || '') + '</span><span>' +
        escHtml(days[days.length - 1].day || '') + '</span></div>';
    dailyEl.style.display = 'block';
  } else {
    dailyEl.style.display = 'none';
  }

  // Breakdown tables — calls + tokens, no cost column
  const tablesEl = document.getElementById('usage-tables');
  const table = (title, rows, keyName) => {
    if (!rows || !rows.length) return '';
    const max = Math.max(...rows.map(r => r.calls || 0), 1);
    let h = '<div class="card flush"><div class="card-head"><h3>' + title + '</h3></div><table><thead><tr>' +
      '<th>' + keyName + '</th><th>' + t('Calls') + '</th><th class="num">' + t('Input') + '</th><th class="num">' + t('Output') + '</th><th class="num">' + t('Cache read') + '</th>' +
      '</tr></thead><tbody>';
    for (const r of rows) {
      h += '<tr><td class="cell-main">' + escHtml(String(r[keyName.toLowerCase()] || '')) + '</td>' +
        '<td><span class="meter-row"><span class="bar" style="width:120px"><span style="width:' +
          (100 * (r.calls || 0) / max) + '%;background:var(--accent)"></span></span><span class="v">' + (r.calls || 0) + '</span></span></td>' +
        '<td class="num muted">' + fmtTokens(r.input_tokens) + '</td>' +
        '<td class="num">' + fmtTokens(r.output_tokens) + '</td>' +
        '<td class="num muted">' + fmtTokens(r.cache_read_tokens) + '</td></tr>';
    }
    return h + '</tbody></table></div>';
  };

  const anyRows = (data.by_agent || []).length || (data.by_task || []).length;
  if (!anyRows) {
    tablesEl.innerHTML = emptyState(icon('usage'), t('No usage recorded yet'),
      t('Once Harvey starts making model calls (Claude or Codex), every one is logged here with exact calls and tokens by provider, agent and task.'));
  } else {
    const providers = data.by_provider || [];
    const showProviders = providers.length > 1 || (providers.length === 1 && providers[0].provider !== 'claude');
    tablesEl.innerHTML =
      (showProviders ? table(t('By Provider (30 days)'), providers, t('Provider')) : '') +
      table(t('By Agent (30 days)'), data.by_agent, t('Agent')) +
      table(t('By Task (30 days)'), data.by_task, t('Task')) +
      table(t('By Model (30 days)'), data.by_model, t('Model'));
  }
}

// ── Outbox: a decisions desk, not a wall of drafts ──
//
// Approving mail is a queue of one-at-a-time judgements. Showing all of them
// stacked invites a single "approve all" reflex, which is exactly the review
// the approval ladder exists to prevent. One email fills the pane; the rest
// wait in the rail.

async function loadOutbox() {
  const [data, mode] = await Promise.all([api('/api/outbox'), api('/api/sending-mode')]);
  _sendingMode = mode && !mode.error ? mode : null;
  const banner = document.getElementById('outbox-banner');
  const desk = document.getElementById('outbox-desk');
  const list = document.getElementById('outbox-list');
  const actions = document.getElementById('outbox-actions');
  if (!data) { desk.innerHTML = offlineState(); list.innerHTML = ''; return; }
  if (data.error) {
    // Never let a failed read pass for "Nothing to review".
    desk.innerHTML = emptyState(icon('alert'), t('Couldn\'t read the Outbox'), escHtml(data.error));
    list.innerHTML = '';
    return;
  }

  const pending = data.pending || [];
  _desk.items = pending;
  _desk.footer = data.footer || '';
  // Keep the desk on the draft being edited even if the list shifted.
  const editingAt = _desk.editing ? pending.findIndex(p => p.id === _desk.editing) : -1;
  if (editingAt >= 0) _desk.i = editingAt;
  else if (_desk.editing) {
    _desk.editing = null;
    _desk.unsaved = null;
  }
  if (_desk.i >= pending.length) _desk.i = Math.max(0, pending.length - 1);
  navCount('nav-outbox', pending.length);

  actions.innerHTML =
    (pending.length && !data.paused
      ? (pending.length > pending.filter(p => heldReason(p)).length
        ? '<button class="btn btn-primary btn-sm" onclick="outboxApproveAll()" ' +
          'title="' + t('Emails Harvey held are left for you to approve one by one') + '">' + t('Approve all') + ' ' +
          (pending.length - pending.filter(p => heldReason(p)).length) + '</button>' : '') : '') +
    (data.paused
      ? '<button class="btn btn-secondary btn-sm" onclick="sendingToggle(\'resume\')">' +
        t('Resume sending') + '</button>'
      : '<button class="btn btn-secondary btn-sm" onclick="sendingToggle(\'pause\')">' +
        t('Pause all sending') + '</button>');

  // The kill switch gets a banner only when it's actually on — a permanent
  // bar for a thing that isn't happening is just noise.
  banner.innerHTML = sendingModeBanner(_sendingMode) + (data.paused
    ? '<div class="banner bad">' + icon('alert') + '<div><b>' + t('Sending is paused') + '</b>' +
        '<p>' + escHtml(data.paused) + t('. Approved mail stays queued until you resume.') + '</p></div></div>'
    : '');
  // Only once there is mail to warn about: a fresh install sends nothing.
  const anyMail = pending.length || (data.approved || []).length || (data.sent || []).length;
  if (data.compliance_notice && anyMail) {
    banner.innerHTML +=
      '<div class="banner wait">' + icon('alert') + '<div><b>' + t('Add your postal address') + '</b>' +
        '<p>' + escHtml(data.compliance_notice) + '</p></div></div>';
  }

  desk.innerHTML = pending.length ? renderDesk(pending, _desk.i) :
    '<div class="card">' + emptyState(icon('check'), t('Nothing to review'),
      _sendingMode && _sendingMode.mode === 'autopilot'
        ? t('Autopilot is on, so Harvey sends on its own. Anything it holds for you shows up here.')
        : t('Every draft Harvey writes lands here first. Approve one and it sends on schedule.')) +
    '</div>';

  const table = (title, rows, cols) => {
    if (!rows || !rows.length) return '';
    let h = '<div class="subhead">' + title + ' <span class="hint">' + rows.length + '</span></div>' +
      '<div class="table-card"><table class="nowrap clip"><thead><tr>' +
      cols.map(c => '<th>' + c[0] + '</th>').join('') + '</tr></thead><tbody>';
    for (const r of rows) {
      h += '<tr>' + cols.map(c => '<td' + (c[2] ? ' class="muted"' : '') + '>' +
        (c[3] ? c[1](r) : escHtml(String(c[1](r) ?? ''))) + '</td>').join('') + '</tr>';
    }
    return h + '</tbody></table></div>';
  };

  list.innerHTML =
    table(t('Approved &amp; scheduled'), data.approved, [
      [t('To'), r => r.to_email], [t('Step'), r => r.step], [t('Subject'), r => r.subject],
      [t('Sends'), r => r.status === 'sending' ? badge(r.status) : escHtml(formatDate(r.send_at)), true, true],
    ]) +
    table(t('Recently sent'), data.sent, [
      [t('To'), r => r.to_email], [t('Step'), r => r.step], [t('Subject'), r => r.subject],
      [t('Sent'), r => formatDate(r.sent_at), true],
    ]) +
    table(t('Didn\'t send'), data.failed, [
      [t('To'), r => r.to_email], [t('Status'), r => badge(r.status), false, true],
      [t('Reason'), r => r.error, true], [t('Updated'), r => formatDate(r.updated_at), true],
    ]);
}

const CONTACT_SOURCES = {
  known_company_backfill: 'team page on their site',
  company_website: 'team page on their site',
  scraped_mailto: 'address published on their site',
  linkedin_search: 'LinkedIn search',
  hunter_domain_search: 'Hunter domain search',
  job_board: 'job board',
  web_search: 'web search',
};

// What the draft is built on, so every claim can be checked against it.
function renderDeskContext(ctx) {
  if (!ctx) return '';
  const row = (k, v) => v ? '<div class="k">' + k + '</div><div class="v">' + v + '</div>' : '';
  const href = safeHref(ctx.website);
  const site = href
    ? '<a href="' + href + '" target="_blank" rel="noopener noreferrer">' +
        escHtml(ctx.website.replace(/^https?:\/\//i, '')) + ' &#8599;</a>'
    : '';
  const who = [ctx.contact, ctx.contact_title].filter(Boolean).map(escHtml).join(' &middot; ');
  const email = [ctx.email_status, ctx.score ? t('score {n}', {n: ctx.score}) : '']
    .filter(Boolean).map(escHtml).join(' &middot; ');
  const signals = Object.entries(ctx.signals || {})
    .map(([k, v]) => escHtml(k) + ': <b>' + escHtml(v) + '</b>').join('<br>');
  return '<div class="desk-context">' +
    row(t('Company'), escHtml(ctx.company)) +
    row(t('Website'), site) +
    row(t('About'), escHtml(ctx.description)) +
    row(t('Contact'), who) +
    row(t('Found via'), escHtml(Object.prototype.hasOwnProperty.call(CONTACT_SOURCES, ctx.contact_source)
      ? t(CONTACT_SOURCES[ctx.contact_source]) : ctx.contact_source)) +
    row(t('Email'), email) +
    row(t('Signals'), signals) +
    row(t('Campaign'), ctx.campaign ? escHtml(ctx.campaign) +
      ' <span class="muted">' + t('(how they were found, not a fact about them)') + '</span>' : '') +
    row(t('Writer notes'), escHtml(ctx.notes)) +
  '</div>';
}

// ── Approval or autopilot ──
// Approval (the default): every email waits in the Outbox for you.
// Autopilot (channels.email.require_approval: false): Harvey sends on its
// own, and still holds what gate.autopilot_review flags. Turning autopilot
// on takes typing the word; turning it off is one click and works at once.
let _sendingMode = null, _autopilotArmed = false, _modeBusy = false;

function sendingModeBanner(m) {
  if (!m) return '';
  if (m.mode === 'autopilot') {
    return '<div class="banner wait mode-banner">' + icon('alert') + '<div><b>' + t('Autopilot is on') + '</b>' +
      '<p>' + t('Harvey sends email on its own. It holds anything that links to a site that isn’t yours or quotes its own instructions; those wait here') +
      (m.held ? t(' ({n} now)', {n: m.held}) : '') + '.</p></div>' +
      '<button class="btn btn-secondary btn-sm" onclick="setSendingMode(\'approval\')"' +
        (_modeBusy ? ' disabled' : '') + '>' + t('Turn off autopilot') + '</button></div>';
  }
  const instantly = m.provider === 'instantly';
  return '<div class="banner mode-banner">' + icon('check') + '<div><b>' + t('Approval mode') + '</b>' +
    '<p>' + (instantly
      ? t('Instantly can’t hold single emails, so campaigns aren’t sent to Instantly until you turn on autopilot.')
      : t('Every email Harvey writes waits here until you approve it.')) +
    ' <a href="#" onclick="goTab(\'settings\');return false">' + t('About autopilot') + '</a></p></div></div>';
}

function sendingModeCard(m) {
  if (!m) return '<div class="card muted">' + t('Couldn’t read the sending mode.') + '</div>';
  const auto = m.mode === 'autopilot';
  const instantly = m.provider === 'instantly';
  const domains = (m.your_domains || []).map(escHtml).join(', ') || t('none yet');
  let html = '<div class="card mode-card"><h3>' + t('Sending mode: {mode}', {mode: auto ? t('Autopilot') : t('Approval')}) + '</h3>' +
    '<p><b>' + t('Approval') + '</b> ' + t('(the default): every email Harvey writes, first emails, follow-ups and replies, waits in the Outbox until you approve it.') +
      (instantly ? ' ' + t('On Instantly, which can’t hold single emails, that means campaigns aren’t deployed.') : '') + '</p>' +
    '<p><b>' + t('Autopilot') + '</b>: ' + t('Harvey sends on its own, on schedule, within your daily limit and quiet hours. Opt-outs, bounces and Pause all sending work the same. It still holds an email for you when it links to, or gives an address at, a site that isn’t yours, or quotes Harvey’s own instructions: text from websites and replies can try to steer what it writes. The first reply after you undo an opt-out waits too.') + '</p>' +
    '<p class="muted">' + t('Counted as yours: {domains}', {domains: domains}) +
      (m.booking_page ? t('; your booking page {page}', {page: escHtml(m.booking_page)}) : '') +
      '. ' + t('Add others under <code>channels.email.link_domains</code> in <code>{file}</code>.', {file: escHtml(m.file)}) + '</p>';
  if (auto) {
    html += '<div class="convo-actions"><span class="muted">' + t('Turning it off works at once: emails autopilot approved that haven’t gone out come back to the Outbox.') +
      (instantly ? ' ' + t('Campaigns already on Instantly keep running until you pause them there.') : '') + '</span>' +
      '<button class="btn btn-primary btn-sm" onclick="setSendingMode(\'approval\')"' +
        (_modeBusy ? ' disabled' : '') + '>' + t('Turn off autopilot') + '</button></div>';
  } else if (_autopilotArmed) {
    html += '<div class="convo-actions mode-confirm"><span class="muted">' + t('Approve a batch or two by hand first and read what Harvey writes. Type <b>autopilot</b> to turn it on. It starts from Harvey’s next cycle.') + '</span>' +
      '<input class="form-input" id="autopilot-confirm" autocomplete="off" aria-label="' + t('Type autopilot to confirm') + '" ' +
        'oninput="document.getElementById(\'autopilot-go\').disabled = this.value.trim().toLowerCase() !== \'autopilot\'" ' +
        'onkeydown="if (event.key === \'Enter\' && this.value.trim().toLowerCase() === \'autopilot\') setSendingMode(\'autopilot\')">' +
      '<button class="btn btn-primary btn-sm" id="autopilot-go" disabled onclick="setSendingMode(\'autopilot\')">' + t('Turn on autopilot') + '</button>' +
      '<button class="btn btn-ghost btn-sm" onclick="_autopilotArmed=false;renderSendingModeCard()">' + t('Cancel') + '</button></div>';
  } else {
    html += '<div class="convo-actions"><button class="btn btn-secondary btn-sm" ' +
      'onclick="_autopilotArmed=true;renderSendingModeCard()">' + t('Turn on autopilot…') + '</button></div>';
  }
  return html + '</div>';
}

function renderSendingModeCard() {
  const el = document.getElementById('sending-mode-card');
  if (el) el.innerHTML = sendingModeCard(_sendingMode);
  const input = document.getElementById('autopilot-confirm');
  if (input) input.focus();
}

async function loadSendingModeCard() {
  const m = await api('/api/sending-mode');
  _sendingMode = m && !m.error ? m : null;
  renderSendingModeCard();
}

async function setSendingMode(mode) {
  if (_modeBusy) return;
  const body = {mode};
  if (mode === 'autopilot') {
    const input = document.getElementById('autopilot-confirm');
    body.confirm = input ? input.value.trim().toLowerCase() : '';
  }
  _modeBusy = true;
  document.querySelectorAll('.mode-banner button, .mode-card button').forEach(b => { b.disabled = true; });
  try {
    const res = await api('/api/sending-mode', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
    });
    if (res && res.success) {
      _autopilotArmed = false;
      showToast(mode === 'autopilot'
        ? t('Autopilot is on from Harvey’s next cycle. Held emails still wait for you.')
        : t('Autopilot is off. Every email waits for you') +
          (res.requeued ? t('; {n} queued by autopilot came back to the Outbox.', {n: res.requeued}) : '.') +
          (_sendingMode && _sendingMode.provider === 'instantly'
            ? t(' Campaigns already on Instantly keep running: pause them in Instantly.') : ''), 'success');
    } else {
      showToast(failMsg(res, t('Couldn’t change the sending mode.')), 'error');
    }
  } finally {
    _modeBusy = false;
  }
  loadSendingModeCard();
  loadOutbox();
}

// Why autopilot sent this back for a human (sender: gate.autopilot_review).
const HELD_PREFIX = 'held for your review: ';   // state.HELD_FOR_REVIEW
function heldReason(item) {
  const e = String((item && item.error) || '');
  const why = e.startsWith(HELD_PREFIX) ? e.slice(HELD_PREFIX.length) : '';
  return why.charAt(0).toUpperCase() + why.slice(1);
}

function renderDesk(items, i) {
  const cur = items[i];
  const rail = items.map((it, n) => {
    const who = it.context && it.context.contact ? it.context.contact : it.to_email;
    const parts = String(who || '').split(/[\s@.]+/);
    return '<div class="desk-item ' + (n === i ? 'active' : '') + '" onclick="deskGo(' + n + ')">' +
      '<span class="avatar">' + initials(parts[0], parts[1]) + '</span>' +
      '<div class="di-text"><div class="to">' + escHtml(who) +
        (heldReason(it) ? ' <span class="tag t-bad" title="' + t('Harvey held this instead of sending it') + '">' + t('Held') + '</span>' : '') +
        '</div>' +
      '<div class="sub">' + escHtml(it.subject || t('(no subject)')) + '</div></div>' +
    '</div>';
  }).join('');

  const editing = _desk.editing === cur.id;
  const unsaved = (editing && _desk.unsaved) || {subject: cur.subject || '', body: cur.body || ''};
  // A reply keeps its subject: Gmail threads by it.
  const fixedSubject = cur.kind === 'reply'
    ? ' readonly title="' + t('A reply keeps its subject so it stays in the thread') + '"' : '';
  const draft = editing
    ? '<input id="desk-subject" class="desk-edit-subject" value="' + escHtml(unsaved.subject) + '"' +
        fixedSubject + ' oninput="deskKeep()">' +
      '<textarea id="desk-body" class="desk-edit-body" rows="14" oninput="deskKeep()">' +
        escHtml(unsaved.body) + '</textarea>'
    : '<div class="subject">' + escHtml(cur.subject || t('(no subject)')) + '</div>' +
      '<div class="body">' + escHtml(cur.body) + '</div>';
  // Cold email leaves with the footer underneath; show it as it will go out.
  // Not added again when the body already ends with it, as in the Sender.
  const added = cur.kind === 'sequence' && !(cur.body || '').trimEnd().endsWith(_desk.footer)
    ? _desk.footer : '';
  const footer = added
    ? '<div class="desk-footer" title="' + t('Added when it sends (compliance in harvey.yaml)') + '">' +
        escHtml(added) + '</div>' : '';
  const actions = editing
    ? '<button class="btn btn-primary" onclick="deskSave(' + jsq(cur.id) + ')">' + t('Save') + '</button>' +
      '<button class="btn btn-secondary" onclick="deskEdit(null)">' + t('Cancel') + '</button>'
    : '<button class="btn btn-primary" onclick="outboxAct(' + jsq(cur.id) + ',\'approve\')">' +
        t('Approve <kbd>A</kbd>') + '</button>' +
      '<button class="btn btn-secondary" onclick="deskEdit(' + jsq(cur.id) + ')">' +
        t('Edit <kbd>E</kbd>') + '</button>' +
      '<button class="btn btn-secondary" onclick="outboxAct(' + jsq(cur.id) + ',\'reject\')">' +
        t('Reject <kbd>R</kbd>') + '</button>';

  return '<div class="desk">' +
    '<div class="desk-list">' + rail + '</div>' +
    '<div class="desk-pane">' +
      '<div class="to-line">' + t('To') + ' <b>' + escHtml(cur.to_email) + '</b>' +
        '<span class="tag">' + (cur.kind === 'reply' ? t('Reply') : t('Email {n}', {n: escHtml(String(cur.step))})) + '</span>' +
        '<span class="muted">' + t('sends {date}', {date: formatDate(cur.send_at)}) + '</span></div>' +
      renderDeskContext(cur.context) +
      (heldReason(cur)
        ? '<div class="banner wait desk-held">' + icon('alert') + '<div><b>' + t('Harvey held this instead of sending it') + '</b>' +
          '<div>' + t('{reason}. Read it before approving: text from a website or an email can try to steer what Harvey writes.', {reason: escHtml(heldReason(cur))}) + '</div></div></div>'
        : '') +
      draft + footer +
      '<div class="desk-actions">' + actions +
        '<span class="muted" style="font-size:12px;margin-left:auto">' +
          t('{i} of {n}', {i: i + 1, n: items.length}) + '</span>' +
      '</div>' +
    '</div></div>';
}

function deskEdit(id) {
  _desk.editing = id;
  _desk.unsaved = null;
  document.getElementById('outbox-desk').innerHTML = renderDesk(_desk.items, _desk.i);
  if (id) document.getElementById('desk-body').focus();
}

// What's typed survives a re-render (switching tabs and back reloads the desk).
function deskKeep() {
  _desk.unsaved = {
    subject: document.getElementById('desk-subject').value,
    body: document.getElementById('desk-body').value,
  };
}

// While a draft is being edited, nothing else on the desk may act on the
// stored version of it: Save or Cancel first.
function deskBusy() {
  if (!_desk.editing) return false;
  showToast(t('Save or cancel your edit first.'), 'error');
  return true;
}

async function deskSave(id) {
  const subject = document.getElementById('desk-subject').value.trim();
  const body = document.getElementById('desk-body').value.trim();
  if (!subject || !body) { showToast(t('Subject and body are both required.'), 'error'); return; }
  let data = null;
  try {
    const r = await authFetch('/api/outbox/' + encodeURIComponent(id) + '/edit', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({subject: subject, body: body}),
    });
    if (await lockIfRefused(r)) return;
    data = await r.json();
  } catch {}
  if (data && data.success) {
    _desk.editing = null;
    _desk.unsaved = null;
    showToast(t('Saved. Still waiting for your approval.'), 'success');
    loadOutbox();
  } else {
    showToast((data && data.message) || t('Save failed.'), 'error');
  }
}

function deskGo(n) {
  if (n < 0 || n >= _desk.items.length || deskBusy()) return;
  _desk.i = n;
  document.getElementById('outbox-desk').innerHTML = renderDesk(_desk.items, n);
}

// Keyboard review. Ignored while typing into a field, so Settings still works.
document.addEventListener('keydown', e => {
  if (currentTab !== 'outbox' || e.metaKey || e.ctrlKey || e.altKey) return;
  // Mid-edit, A would approve the stored draft, not what's typed. Only
  // Escape (cancel) works until the edit is saved or cancelled.
  if (_desk.editing) {
    const typing = /^(input|textarea)$/i.test(e.target.tagName || '');
    if (e.key === 'Escape') { e.preventDefault(); deskEdit(null); }
    else if (!typing && 'jkare'.includes(e.key.toLowerCase())) { e.preventDefault(); deskBusy(); }
    return;
  }
  const tag = (e.target.tagName || '').toLowerCase();
  if (tag === 'input' || tag === 'textarea' || e.target.isContentEditable) return;
  const cur = _desk.items[_desk.i];
  const k = e.key.toLowerCase();
  if (k === 'j') { e.preventDefault(); deskGo(_desk.i + 1); }
  else if (k === 'k') { e.preventDefault(); deskGo(_desk.i - 1); }
  else if (k === 'a' && cur) { e.preventDefault(); outboxAct(cur.id, 'approve'); }
  else if (k === 'r' && cur) { e.preventDefault(); outboxAct(cur.id, 'reject'); }
  else if (k === 'e' && cur) { e.preventDefault(); deskEdit(cur.id); }
});

async function outboxAct(id, action) {
  if (deskBusy()) return;
  const data = await api('/api/outbox/' + encodeURIComponent(id) + '/' + action, {method: 'POST'});
  if (data && data.success) showToast(action === 'approve' ? t('Approved — will send on schedule.') : t('Rejected.'), 'success');
  else showToast(failMsg(data, t('Action failed.')), 'error');
  loadOutbox();
}

async function outboxApproveAll() {
  if (deskBusy()) return;
  const data = await api('/api/outbox/approve-all', {method: 'POST'});
  if (data && data.success) showToast(t('Approved {n} email(s).', {n: data.approved}) + (data.held
    ? ' ' + t(data.held === 1
        ? '{n} Harvey draft is left for you to read and approve one by one.'
        : '{n} Harvey drafts are left for you to read and approve one by one.', {n: data.held})
    : ''), 'success');
  else showToast(failMsg(data, t('Approve-all failed.')), 'error');
  loadOutbox();
}

async function sendingToggle(action) {
  const data = await api('/api/sending/' + action, {method: 'POST'});
  if (data && data.success) showToast(action === 'pause' ? t('Sending paused.') : t('Sending resumed.'), 'success');
  else showToast(failMsg(data, action === 'pause' ? t('Could not pause sending.') : t('Could not resume sending.')), 'error');
  loadOutbox();
}

async function loadCompanies() {
  companyDrill = false;
  const el = document.getElementById('companies-list');
  const data = await api('/api/companies');
  if (!data) { el.innerHTML = offlineState(); return; }
  _companies = data;
  if (!data.length) {
    el.innerHTML = emptyState(icon('companies'), t('No companies yet'),
      t('Confirm your <b>Signals</b>, then find businesses on the <b>Discover</b> tab. ' +
      'OpenStreetMap is free and needs no account.'));
    return;
  }
  let html = '<div class="table-card"><table class="nowrap"><thead><tr><th>' + t('Company') + '</th><th>' + t('Industry') + '</th>' +
    '<th>' + t('Location') + '</th><th>' + t('Size') + '</th><th class="num">' + t('Contacts') + '</th><th>' + t('Source') + '</th><th>' + t('Added') + '</th></tr></thead><tbody>';
  data.forEach((c, i) => {
    const website = c.website || (c.domain ? 'https://' + c.domain : '');
    const domain = safeHref(website)
      ? '<a href="' + safeHref(website) + '" target="_blank" rel="noopener noreferrer" onclick="event.stopPropagation()">' +
        escHtml(c.domain || website.replace(/^https?:\/\//i, '')) + '</a>'
      : escHtml(c.domain);
    html += '<tr class="clickable" onclick="showCompanyContacts(' + i + ')">' +
      '<td><div class="cell-stack"><span class="cell-main">' + escHtml(c.name) + '</span>' +
        '<span class="cell-sub">' + domain + '</span></div></td>' +
      '<td>' + (c.industry ? '<span class="tag">' + escHtml(c.industry) + '</span>' : '') + '</td>' +
      '<td>' + escHtml(c.location) + '</td>' +
      '<td class="muted">' + escHtml(c.company_size) + '</td>' +
      '<td class="num">' + (c.contact_count || 0) + '</td>' +
      '<td class="muted">' + sourceLabel(c.source) + '</td>' +
      '<td class="muted">' + relTime(c.created_at) + '</td></tr>';
  });
  el.innerHTML = html + '</tbody></table></div>' +
    '<div class="table-foot">' + t(data.length === 1 ? '{n} company' : '{n} companies', {n: data.length}) + '</div>';
}

async function showCompanyContacts(index) {
  const company = _companies[index];
  if (!company) return;
  companyDrill = true;
  const el = document.getElementById('companies-list');
  const data = await api('/api/companies/' + encodeURIComponent(company.id) + '/contacts');
  let html = '<button class="btn btn-ghost btn-sm" onclick="loadCompanies()" style="margin-bottom:12px">' +
    t('&larr; All companies') + '</button>' +
    '<div class="card flush"><div class="card-head"><h3>' + escHtml(company.name) + '</h3>' +
    '<span class="meta">' + escHtml([company.industry, company.location].filter(Boolean).join(' · ')) + '</span></div>';
  if (!data || !data.length) {
    html += '<div class="empty"><div class="copy">' + t('No contacts found at this company yet.') + '</div></div></div>';
  } else {
    html += '<table><thead><tr><th>' + t('Name') + '</th><th>' + t('Email') + '</th><th>' + t('Phone') + '</th><th>' + t('LinkedIn') + '</th><th>' + t('Status') + '</th><th>' + t('Source') + '</th></tr></thead><tbody>';
    for (const p of data) {
      const phoneIcon = p.phone_verified ? ' <span class="muted">&#10003;</span>' : '';
      html += '<tr><td><div class="cell-row"><span class="avatar">' + initials(p.first_name, p.last_name) + '</span>' +
          '<div class="cell-stack"><span class="cell-main">' + escHtml(p.first_name) + ' ' + escHtml(p.last_name) + '</span>' +
          '<span class="cell-sub">' + escHtml(p.title) + '</span></div></div></td>' +
        '<td>' + escHtml(p.email) + emailTag(p) + '</td>' +
        '<td>' + escHtml(p.phone) + phoneIcon + '</td>' +
        '<td>' + (safeHref(p.linkedin_url) ? '<a href="' + safeHref(p.linkedin_url) + '" target="_blank" rel="noopener noreferrer">' + t('Profile') + '</a>' : '') + '</td>' +
        '<td>' + badge(p.status) + '</td><td class="muted">' + sourceLabel(p.source) + '</td></tr>';
    }
    html += '</tbody></table></div>';
  }
  el.innerHTML = html;
}

async function submitFeedback(entityType, entityId, promptText) {
  const comment = prompt(promptText || t('Add your feedback:'));
  if (!comment) return;
  const data = await api('/api/feedback', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({entity_type: entityType, entity_id: entityId, comment: comment})
  });
  if (data && data.success) showToast(entityType === 'campaign'
    ? t('Saved. Harvey follows it in the emails it writes from now on.')
    : t('Saved. Harvey uses it if it writes this person\'s first email (Gmail, SMTP or Microsoft 365).'), 'success');
  else showToast((data && data.message) || t('Could not save feedback.'), 'error');
}

function fbProspect(i) {
  const p = _prospects[i];
  if (p) submitFeedback('contact', p.id, t('Anything Harvey should know before writing to this person?'));
}

function fbCampaign(i) {
  const c = _campaigns[i];
  if (c) submitFeedback('campaign', c.id, t('What should Harvey do differently in the emails it writes next? (e.g. "shorter; don\'t mention reviews")'));
}

// Search and status filter live outside the table, so the 15-second refresh
// re-renders rows without losing what you typed.
let _contactQuery = '', _contactFilter = 'all';
const CONTACT_FILTERS = [
  ['all', 'All', null],
  ['todo', 'To write', ['new', 'queued']],
  ['contacted', 'Contacted', ['contacted']],
  ['replied', 'Replied', ['replied', 'interested', 'meeting']],
  ['closed', 'Closed out', null],
];

async function loadProspects() {
  const el = document.getElementById('prospects-table');
  const data = await api('/api/prospects');
  if (!data) { el.innerHTML = offlineState(); return; }
  _prospects = data;
  if (!data.length) {
    el.innerHTML = emptyState(icon('contacts'), t('No contacts yet'),
      t('Harvey hasn\'t found any prospects. Once it\'s running, the Scout agent searches the web for people matching your ideal customer profile in <b>harvey.yaml</b>.'));
    return;
  }
  if (!document.getElementById('contact-rows')) {
    el.innerHTML = '<div class="toolbar">' +
      '<label class="search">' + icon('discover') +
        '<input class="form-input" id="contact-search" type="search" placeholder="' + t('Search name, company, email') + '" ' +
        'aria-label="' + t('Search contacts') + '" oninput="_contactQuery=this.value;renderContacts()"></label>' +
      '<div class="segmented" id="contact-filters" role="group" aria-label="' + t('Filter by status') + '"></div>' +
      '</div><div id="contact-rows"></div>';
    // A rebuild after an offline blip must show the search still applied.
    document.getElementById('contact-search').value = _contactQuery;
  }
  renderContacts();
}

function contactMatches(p, f) {
  const known = ['new', 'queued', 'contacted', 'replied', 'interested', 'meeting'];
  if (f === 'all') return true;
  if (f === 'closed') return !known.includes(p.status);
  const hit = CONTACT_FILTERS.find(x => x[0] === f);
  return !!(hit && hit[2] && hit[2].includes(p.status));
}

function setContactFilter(f) { _contactFilter = f; renderContacts(); }

function renderContacts() {
  const rowsEl = document.getElementById('contact-rows');
  const filtersEl = document.getElementById('contact-filters');
  if (!rowsEl) return;
  const q = _contactQuery.trim().toLowerCase();
  const found = p => !q || [p.first_name, p.last_name, p.company, p.email, p.title].join(' ').toLowerCase().includes(q);
  filtersEl.innerHTML = CONTACT_FILTERS.map(f =>
    '<button class="' + (f[0] === _contactFilter ? 'on' : '') + '" aria-pressed="' + (f[0] === _contactFilter) + '" onclick="setContactFilter(\'' + f[0] + '\')">' +
      t(f[1]) + ' <span class="n">' + _prospects.filter(p => contactMatches(p, f[0]) && found(p)).length + '</span></button>'
  ).join('');

  const rows = _prospects.map((p, i) => [p, i]).filter(([p]) => contactMatches(p, _contactFilter) && found(p));
  if (!rows.length) {
    rowsEl.innerHTML = '<div class="table-card">' + emptyState(icon('discover'), t('No matches'),
      t('Nothing matches that search and filter.')) + '</div>';
    return;
  }
  let html = '<div class="table-card"><table class="nowrap"><thead><tr><th>' + t('Name') + '</th><th>' + t('Company') + '</th><th>' + t('Email') + '</th>' +
    '<th>' + t('Score') + '</th><th>' + t('Status') + '</th><th>' + t('Source') + '</th><th>' + t('Added') + '</th><th></th></tr></thead><tbody>';
  for (const [p, i] of rows) {
    html += '<tr><td><div class="cell-row"><span class="avatar">' + initials(p.first_name, p.last_name) + '</span>' +
        '<div class="cell-stack"><span class="cell-main">' + escHtml(p.first_name) + ' ' + escHtml(p.last_name) + '</span>' +
        '<span class="cell-sub">' + escHtml(p.title) + '</span></div></div></td>' +
      '<td>' + escHtml(p.company) + '</td>' +
      '<td><div class="cell-stack"><span>' + escHtml(p.email) + '</span>' +
        '<span class="cell-sub">' + [emailTag(p).trim(), p.phone ? escHtml(p.phone) +
          (p.phone_verified ? ' <span class="verified" title="' + t('Phone verified') + '">&#10003;</span>' : '') : '']
          .filter(Boolean).join(' · ') + '</span>' +
        '</div></td>' +
      '<td>' + (p.score || p.score === 0 ? meterHtml(p.score) : '') + '</td>' +
      '<td>' + badge(p.status) + '</td>' +
      '<td class="muted">' + sourceLabel(p.source) + '</td>' +
      '<td class="muted" style="white-space:nowrap">' + relTime(p.created_at) + '</td>' +
      '<td><div class="cell-row">' + (p.status === 'opted_out' || p.opt_out_undone || _optOut.id === p.id
        ? '<button class="btn btn-ghost btn-sm" onclick="toggleOptOut(' + i + ')" ' +
          'aria-expanded="' + (_optOut.id === p.id) + '" title="' + t('What made Harvey stop emailing them') + '">' +
          (p.status === 'opted_out' ? t('Why?') : t('Opt-out history')) + '</button>' : '') +
        '<button class="btn btn-ghost btn-sm row-action" onclick="fbProspect(' + i + ')" ' +
        'title="' + t('Leave feedback on this contact') + '">' + t('Feedback') + '</button></div></td></tr>';
    if (_optOut.id === p.id) html += '<tr class="detail-row"><td colspan="8">' + optOutPanel(p) + '</td></tr>';
  }
  rowsEl.innerHTML = html + '</tbody></table></div>' +
    '<div class="table-foot">' + t('{shown} of {total} contacts', {shown: rows.length, total: _prospects.length}) + '</div>';
}

// Why someone is opted out, and undoing it. Harvey never moves anyone off
// "opted out" on its own, so a mistaken one (a bare "stop" that meant
// something else) needs you: see what triggered it, then undo it here.
let _optOut = {id: null, data: null, armed: false, busy: false};
const OPT_OUT_RULES = {
  phrase: 'Matched an opt-out phrase',
  no_thanks: 'Matched “no thanks”',
  only_words: 'Their reply was only an opt-out word',
  model: 'The model’s judgement (no phrase matched)',
};

async function toggleOptOut(i) {
  const p = _prospects[i];
  if (!p) return;
  if (_optOut.id === p.id) { _optOut = {id: null, data: null, armed: false, busy: false}; renderContacts(); return; }
  _optOut = {id: p.id, data: null, armed: false, busy: false};
  renderContacts();
  const data = await api('/api/prospects/' + encodeURIComponent(p.id) + '/opt-out');
  if (_optOut.id !== p.id) return;   // closed or switched meanwhile
  _optOut.data = data || {error: true};
  renderContacts();
}

function optOutPanel(p) {
  const d = _optOut.data;
  const name = escHtml([p.first_name, p.last_name].filter(Boolean).join(' ') || p.email);
  if (!d) return '<div class="optout-panel muted">' + t('Loading…') + '</div>';
  if (d.error) return '<div class="optout-panel muted">' + t('Couldn’t load the opt-out record. Try again.') + '</div>';
  let html = '<div class="optout-panel"><h4>' + (d.opted_out ? t('Why Harvey stopped emailing ') : t('Opt-out history for ')) +
    name + '</h4>';
  if (d.recorded) {
    html += '<p>' + escHtml(d.why) + '</p><div class="optout-meta">' +
      escHtml(own(OPT_OUT_RULES, d.rule) ? t(OPT_OUT_RULES[d.rule]) : (d.rule || '')) + (d.at ? ' · ' + relTime(d.at) : '') + '</div>';
  } else if (d.opted_out || (d.history || []).length) {
    html += '<p>' + t('They opted out before Harvey kept a record of why.') +
      (d.message ? t(' The last thing they wrote:') : t(' There’s no reply from them on file.')) + '</p>';
  }
  if (d.message) {
    html += '<div class="thread-msg received"><div class="sender">' + escHtml(p.first_name || t('They')) +
      (d.at ? ' &middot; ' + relTime(d.at) : '') + '</div>' + escHtml(d.message) + '</div>';
  }
  const undone = (d.history || []).filter(h => h.action === 'opt_out_undone');
  if (undone.length) {
    html += '<div class="optout-meta">' + undone.map(h => t('You undid an opt-out {when}', {when: relTime(h.at)})).join(' · ') + '</div>';
  }
  if (!d.opted_out) {
    html += '<div class="convo-actions"><span class="muted">' +
      t('Not opted out now ({status}).', {status: escHtml(statusMeta(d.status).label)}) + '</span></div>';
  } else if (_optOut.armed) {
    html += '<div class="convo-actions"><span class="muted">' +
      t('Harvey will treat them as having replied. Nothing is sent automatically, emails the opt-out cancelled stay cancelled, and Harvey’s answer to their next reply waits for your approval, even on autopilot. Only undo it if they didn’t mean to opt out.') + '</span>' +
      '<button class="btn btn-primary btn-sm" onclick="undoOptOut()"' + (_optOut.busy ? ' disabled' : '') +
        '>' + t('Confirm undo') + '</button>' +
      '<button class="btn btn-ghost btn-sm" onclick="_optOut.armed=false;renderContacts()">' + t('Cancel') + '</button></div>';
  } else {
    html += '<div class="convo-actions"><button class="btn btn-secondary btn-sm" ' +
      'onclick="_optOut.armed=true;renderContacts()">' + t('Undo opt-out') + '</button></div>';
  }
  return html + '</div>';
}

async function undoOptOut() {
  const id = _optOut.id;
  if (!id || _optOut.busy) return;
  _optOut.busy = true;
  renderContacts();
  const res = await api('/api/prospects/' + encodeURIComponent(id) + '/opt-out/undo', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({confirm: true}),
  });
  if (res && res.success) showToast(t('Opt-out undone. They’re marked as replied; Harvey sends nothing on its own.'), 'success');
  else showToast(failMsg(res, t('Could not undo the opt-out. Try again.')), 'error');
  if (_optOut.id !== id) return;
  _optOut.armed = false;
  _optOut.busy = false;
  _optOut.data = await api('/api/prospects/' + encodeURIComponent(id) + '/opt-out') || {error: true};
  loadProspects();
}

// From a conversation: the contact, with the opt-out record open.
async function showOptOut(prospectId, email) {
  goTab('prospects');
  _contactQuery = email || '';
  const search = document.getElementById('contact-search');
  if (search) search.value = _contactQuery;
  await loadProspects();
  const i = _prospects.findIndex(p => p.id === prospectId);
  if (i >= 0 && _optOut.id !== prospectId) toggleOptOut(i);
}

// Opens a filled-in compose window in whatever Gmail account the browser is
// logged into. Nothing is sent from here — the user reads it and presses Send.
// Same values as the Sender's _render, so a hand-sent email reads the same.
function fillMergeVars(text, r) {
  const inbox = r.source === 'company_inbox';
  const vals = {
    first_name: r.first_name || (inbox ? t('there') : ''), last_name: r.last_name || '',
    company: r.company || '', title: r.title || '',
    personalization: r.personalization_notes || '',
  };
  return String(text || '').replace(/\{\{\s*(\w+)\s*\}\}/g, (m, k) => {
    k = k.toLowerCase();
    return Object.prototype.hasOwnProperty.call(vals, k) ? vals[k] : m;
  });
}

let _gmailBusy = false;  // one compose window per click, never two

async function openInGmail(ci, si, ri) {
  if (_gmailBusy) return;
  const c = _campaigns[ci];
  const step = c && (c.sequence || [])[si];
  const r = c && (c.recipients || [])[ri];
  if (!step || !r) return;
  // Cold email carries the postal address and opt-out line, sent by hand too.
  const filled = fillMergeVars(step.body, r).trimEnd();
  const body = filled + (c.footer && !filled.endsWith(c.footer) ? '\n\n' + c.footer : '');
  // authuser picks the Google account Harvey sends as, not just whichever
  // one the browser happens to be signed into.
  const url = 'https://mail.google.com/mail/?view=cm&fs=1' +
    (c.send_as ? '&authuser=' + encodeURIComponent(c.send_as) : '') +
    '&to=' + encodeURIComponent(r.email) +
    '&su=' + encodeURIComponent(fillMergeVars(step.subject, r)) +
    '&body=' + encodeURIComponent(body);
  const left = /\{\{\s*\w+\s*\}\}/.test(fillMergeVars(step.subject, r) + fillMergeVars(step.body, r));
  // No 'noopener' feature string: with it window.open always returns null,
  // so a blocked pop-up could not be told apart from a successful one. The
  // tab opens blank right away (inside the click, so it isn't blocked) and
  // only gets the draft once the send is recorded.
  const w = window.open('', '_blank');
  if (!w) {
    showToast(t('Your browser blocked the Gmail tab. Allow pop-ups for this page and click again.'), 'error');
    return;
  }
  w.opener = null;
  // Record the send BEFORE the user can press Send in Gmail, so Harvey can
  // never email them as well — not even if they forget to tick the box.
  _gmailBusy = true;
  let claimed = r.sent_by_hand;
  try { if (!claimed) claimed = await markSent(ci, ri, true); } finally { _gmailBusy = false; }
  if (!claimed) { w.close(); return; }
  w.location.href = url;
  if (left) showToast(t('This draft still has a {{placeholder}} in it. Fix it in Gmail before sending.'), 'error');
}

async function markSent(ci, ri, sent) {
  const r = (_campaigns[ci] && _campaigns[ci].recipients || [])[ri];
  if (!r) return;
  let res = null;
  try {
    const resp = await authFetch('/api/prospects/' + encodeURIComponent(r.id) + '/sent', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({sent}),
    });
    if (await lockIfRefused(resp)) return;
    res = await resp.json();
  } catch {}
  const ok = !!(res && res.success);
  if (!ok) showToast((res && res.message) || t('Could not save that. Try again.'), 'error');
  else if (!sent && res.sent) showToast(t('They already replied, so this stays marked as sent.'));
  else showToast(sent ? t('Marked as sent by you.') : t('Unmarked.'), 'success');
  loadCampaigns();
  // True only when this call changed it: a status means they replied or
  // opted out, 'already' means another click or tab got there first.
  return ok && sent === !!res.sent && !res.status && !res.already;
}

// Where a campaign's people stand, as one stacked bar.
function campaignProgress(c) {
  const rs = c.recipients || [];
  if (!rs.length) return '';
  const count = st => rs.filter(r => st.includes(r.status)).length;
  const parts = [
    [t('Contacted'), count(['contacted']), 'c-accent'],
    [t('Replied'), count(['replied']), 'c-active'],
    [t('Interested'), count(['interested', 'meeting']), 'c-good'],
    [t('Waiting'), count(['new', 'queued']), 'c-accent-soft'],
  ];
  const closed = rs.length - parts.reduce((x, p) => x + p[1], 0);
  if (closed > 0) parts.push([t('Closed out'), closed, 'c-idle']);
  return '<div class="campaign-progress"><div class="bar" role="img" aria-label="' + t('Where this campaign\'s people stand') + '">' +
    parts.filter(p => p[1]).map(p => '<span class="' + p[2] + '" style="width:' + (100 * p[1] / rs.length) +
      '%" title="' + p[0] + ': ' + p[1] + '"></span>').join('') + '</div>' +
    '<div class="campaign-legend">' + parts.filter(p => p[1]).map(p =>
      '<span><i class="' + p[2] + '"></i>' + p[0] + ' <b>' + p[1] + '</b></span>').join('') + '</div></div>';
}

async function loadCampaigns() {
  const el = document.getElementById('campaigns-list');
  const data = await api('/api/campaigns');
  if (!data) { el.innerHTML = offlineState(); return; }
  _campaigns = data;
  if (!data.length) {
    el.innerHTML = emptyState(icon('campaigns'), t('No campaigns yet'),
      t('The Writer agent hasn\'t drafted any sequences. It kicks in automatically once Harvey has scored prospects to write for.'));
    return;
  }
  let html = '';
  data.forEach((c, i) => {
    let stepsHtml = '';
    (c.sequence || []).forEach((step, s) => {
      const sendBtns = (c.recipients || []).map((r, ri) => {
        // Only offered when Harvey can't send itself and this person can
        // still be emailed (the server decides: can_send_by_hand).
        if (r.sent_by_hand) {
          const tick = '<label style="display:inline-flex;align-items:center;gap:4px;font-size:13px;cursor:pointer">' +
            '<input type="checkbox" checked onchange="markSent(' + i + ',' + ri + ',this.checked)"> ' +
            '&#10003; ' + t('Sent by you') + '</label>';
          if (s === 0) return '<span class="rcpt-who">' + escHtml(r.email) + '</span>' + tick;
          // Follow-ups are the user's too: Harvey never emails someone they emailed.
          return '<span class="rcpt-who">' + escHtml(r.email) + '</span>' +
            '<button class="btn btn-secondary btn-sm" onclick="openInGmail(' + i + ',' + s + ',' + ri + ')">' +
            t('Open in Gmail') + '</button>' +
            '<span class="muted" style="font-size:12px">' + t('Harvey won\'t send follow-ups to people you emailed yourself.') + '</span>';
        }
        if (s !== 0) return '';
        if (!r.can_send_by_hand) {
          // Harvey sends here; this only records an email the user already sent.
          return r.can_mark_sent
            ? '<label class="muted" style="display:inline-flex;align-items:center;gap:4px;font-size:12px;cursor:pointer">' +
              '<input type="checkbox" onchange="markSent(' + i + ',' + ri + ',this.checked)"> ' +
              t('I already emailed {email} myself', {email: escHtml(r.email)}) + '</label>'
            : '';
        }
        const risky = r.email_status === 'risky'
          ? ' <span class="mini t-waiting">' + t('Catch-all, may bounce') + '</span>' : '';
        return '<span class="rcpt-who">' + escHtml(r.email) + risky + '</span>' +
          '<button class="btn btn-secondary btn-sm" onclick="openInGmail(' + i + ',' + s + ',' + ri + ')">' +
          t('Open in Gmail') + '</button>' +
          '<label><input type="checkbox" onchange="markSent(' + i + ',' + ri + ',this.checked)"> ' + t('Mark as sent') + '</label>'
      }).filter(Boolean).map(x => '<div class="rcpt">' + x + '</div>').join('');
      stepsHtml += '<div class="email-step"><span class="step-dot">' + escHtml(String(step.step || '?')) + '</span>' +
        '<div><div class="step-num">' + (s === 0 ? t('First email') : t('Follow-up')) +
        (step.delay_days ? ' &middot; ' + t('{n} days after the last', {n: escHtml(String(step.delay_days))}) : '') + '</div>' +
        '<div class="subject">' + escHtml(step.subject) + '</div>' +
        '<div class="body">' + escHtml(step.body) + '</div>' +
        (sendBtns ? '<div class="step-actions">' + sendBtns + '</div>' : '') +
        '</div></div>';
    });
    const pc = (c.prospect_ids || []).length;
    html += '<div class="campaign-card"><div class="campaign-top">' +
      '<div class="campaign-title"><h3>' + escHtml(c.name || t('Untitled Campaign')) + '</h3>' +
        '<button class="btn btn-ghost btn-sm" onclick="fbCampaign(' + i + ')">' + t('Feedback') + '</button></div>' +
      '<div class="meta">' + badge(c.status) + '<span>' + t(pc === 1 ? '{n} prospect' : '{n} prospects', {n: pc}) + '</span>' +
        '<span>' + t('{n} emails', {n: (c.sequence || []).length}) + '</span><span>' + t('Created {when}', {when: relTime(c.created_at)}) + '</span></div>' +
      campaignProgress(c) + '</div>' +
      '<div class="sequence">' +
      (stepsHtml || '<p class="muted" style="font-size:13px;padding-top:16px">' + t('No email steps in this campaign.') + '</p>') + '</div></div>';
  });
  el.innerHTML = html;
}

// The whole exchange in order: Harvey's cold emails (from the Outbox), the
// replies, Harvey's answers, and any answer still waiting for approval.
let _convos = [], _convoFilter = 'needs';
const STAGE_LABELS = {
  initial_outreach: 'Outreach', engaged: 'Engaged', qualifying: 'Qualifying', presenting: 'Presenting',
  negotiating: 'Negotiating', closing: 'Closing', closed_won: 'Won', closed_lost: 'Lost',
};
const CONVO_FILTERS = [
  ['needs', 'Needs you', c => !!c.draft || c.status === 'needs_human'],
  ['open', 'Open', c => c.status !== 'closed'],
  ['closed', 'Closed', c => c.status === 'closed'],
  ['all', 'All', () => true],
];

async function loadConversations() {
  const el = document.getElementById('conversations-list');
  const data = await api('/api/conversations');
  if (!data) { el.innerHTML = offlineState(); return; }
  _convos = data;
  if (!data.length) {
    el.innerHTML = emptyState(icon('conversations'), t('No conversations yet'),
      t('No prospects have replied so far. When they do, the Handler agent classifies each reply and responds — every thread shows up here.'));
    return;
  }
  // Open on "Needs you" only when something needs you.
  if (_convoFilter === 'needs' && !data.some(CONVO_FILTERS[0][2])) _convoFilter = 'open';
  renderConversations();
}

function setConvoFilter(f) { _convoFilter = f; renderConversations(); }

function renderConversations() {
  const el = document.getElementById('conversations-list');
  const filter = CONVO_FILTERS.find(f => f[0] === _convoFilter) || CONVO_FILTERS[3];
  const bar = '<div class="toolbar"><div class="segmented" role="group" aria-label="' + t('Filter conversations') + '">' +
    CONVO_FILTERS.map(f => '<button class="' + (f[0] === filter[0] ? 'on' : '') + '" aria-pressed="' + (f[0] === filter[0]) +
      '" onclick="setConvoFilter(\'' + f[0] + '\')">' + t(f[1]) + ' <span class="n">' +
      _convos.filter(f[2]).length + '</span></button>').join('') + '</div></div>';
  // Handed-to-you first, then drafts waiting; otherwise newest first (as served).
  const rank = c => c.status === 'needs_human' ? 0 : c.draft ? 1 : 2;
  const shown = _convos.filter(filter[2]).map((c, i) => [c, i])
    .sort((a, b) => rank(a[0]) - rank(b[0]) || a[1] - b[1]).map(x => x[0]);
  if (!shown.length) {
    el.innerHTML = bar + '<div class="card">' + emptyState(icon('check'), t('Nothing here'),
      t('No conversation matches this filter.')) + '</div>';
    return;
  }
  el.innerHTML = bar + shown.map(convoCard).join('');
}

function convoCard(c) {
  const items = (c.emails || []).map(e => ({ts: e.sent_at, email: e}))
    .concat((c.thread || []).map(m => ({ts: m.timestamp, msg: m})))
    .map(it => ({...it, key: +parseTs(it.ts)}))
    // An item with no time goes last instead of scrambling the order.
    .sort((a, b) => (isNaN(a.key) ? Infinity : a.key) - (isNaN(b.key) ? Infinity : b.key) || 0);
  const first = c.first_name || t('Them');
  const thread = items.map(it => {
    if (it.email) {
      return '<div class="thread-email"><div class="te-head">' + icon('campaigns') +
        '<b>' + t('Email {n}', {n: escHtml(String(it.email.step))}) + '</b><span>' + escHtml(it.email.subject) + '</span>' +
        '<span class="te-when">' + relTime(it.email.sent_at) + '</span></div>' +
        '<div class="te-body">' + escHtml(it.email.body) + '</div></div>';
    }
    const m = it.msg, sent = m.sender === 'harvey';
    return '<div class="thread-msg ' + (sent ? 'sent' : 'received') + '"><div class="sender">' +
      (sent ? 'Harvey' : escHtml(first)) + ' &middot; ' + relTime(m.timestamp) + '</div>' +
      escHtml(m.content) + '</div>';
  }).join('');
  const draft = c.draft
    ? '<div class="thread-draft"><div class="sender">' + icon('pen') + t('Harvey drafted a reply · waiting for you') + '</div>' +
      '<div class="td-body">' + escHtml(c.draft.body) + '</div>' +
      '<button class="btn btn-primary btn-sm" onclick="goTab(\'outbox\')">' + t('Review in Outbox') + icon('arrow') + '</button></div>'
    : '';
  const name = [c.first_name, c.last_name].filter(Boolean).join(' ') || t('Unknown');
  const stage = own(STAGE_LABELS, c.stage) ? '<span class="tag">' + t(STAGE_LABELS[c.stage]) + '</span>' : '';
  return '<div class="convo-card"><div class="convo-head">' +
    '<span class="avatar">' + initials(c.first_name, c.last_name) + '</span>' +
    '<div><h3>' + escHtml(name) + '</h3><div class="sub">' +
      escHtml([c.company, c.prospect_email].filter(Boolean).join(' · ')) + ' · ' + relTime(c.updated_at) + '</div></div>' +
    // An escalation's intent badge says the same as its status: show one.
    '<div class="tags">' + (c.intent && !(c.status === 'needs_human' && c.intent === 'escalate') ? badge(c.intent) : '') +
      stage + badge(c.status) + '</div></div>' +
    '<div class="thread">' + (thread || '<p class="muted" style="font-size:13px">' + t('No messages in this thread yet.') + '</p>') +
    draft + '</div>' + convoActions(c) + '</div>';
}

// Harvey answers new replies only on open conversations: an escalated one
// waits for you to reply yourself, then close it or hand it back.
function convoActions(c) {
  if (c.status === 'needs_human') {
    return '<div class="convo-actions"><span class="muted">' + icon('alert') +
      t('Harvey handed this to you. Reply from your inbox, then:') + '</span>' +
      '<button class="btn btn-secondary btn-sm" onclick="setConvoStatus(' + jsq(c.id) + ',\'open\')">' + t('Hand back to Harvey') + '</button>' +
      '<button class="btn btn-secondary btn-sm" onclick="setConvoStatus(' + jsq(c.id) + ',\'closed\')">' + t('Close') + '</button></div>';
  }
  if (c.status === 'open') {
    return '<div class="convo-actions"><button class="btn btn-ghost btn-sm" onclick="setConvoStatus(' +
      jsq(c.id) + ',\'closed\')">' + t('Close conversation') + '</button></div>';
  }
  if (c.status === 'closed' && c.prospect_status === 'opted_out') {
    // Reopening wouldn't help: Harvey never answers someone who opted out.
    return '<div class="convo-actions"><span class="muted">' + icon('alert') + t('Opted out. Harvey won’t write to them.') + '</span>' +
      '<button class="btn btn-secondary btn-sm" onclick="showOptOut(' + jsq(c.prospect_id) + ',' + jsq(c.prospect_email) +
      ')">' + t('Why opted out?') + '</button></div>';
  }
  if (c.status === 'closed') {
    return '<div class="convo-actions"><button class="btn btn-ghost btn-sm" onclick="setConvoStatus(' +
      jsq(c.id) + ',\'open\')">' + t('Reopen') + '</button></div>';
  }
  return '';
}

async function setConvoStatus(id, status) {
  const data = await api('/api/conversations/' + encodeURIComponent(id) + '/status', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({status: status}),
  });
  if (data && data.success) showToast(status === 'open' ? t('Open again. Harvey will answer the next reply.') : t('Closed. A new reply starts a new conversation.'), 'success');
  else showToast(failMsg(data, t('Could not update that conversation.')), 'error');
  loadConversations();
  loadToday();
}

async function loadActivity() {
  const el = document.getElementById('activity-list');
  const data = await api('/api/activity');
  if (!data) { el.innerHTML = offlineState(); return; }
  if (!data.length) {
    el.innerHTML = emptyState(icon('activity'), t('No activity yet'),
      t('Harvey hasn\'t taken any actions. Every prospect found, email written, and reply handled will appear here the moment it happens.'));
    return;
  }
  let html = '<div class="card"><div class="timeline">';
  let day = '';
  for (const a of data) {
    const label = dayLabel(a.created_at);
    if (label !== day) { day = label; html += '<div class="tl-day">' + escHtml(label) + '</div>'; }
    html += '<div class="tl-item' + (a.agent === 'main' ? ' quiet' : '') + '"><span class="tl-time">' + timeOfDay(a.created_at) + '</span>' +
      '<span class="tl-icon">' + icon(own(AGENT_ICONS, a.agent) || 'activity') + '</span>' +
      '<span class="tl-text">' + actionText(a) + '</span>' +
      '<span class="tl-agent">' + escHtml(a.agent) + '</span></div>';
  }
  el.innerHTML = html + '</div></div>';
}

// Switching language re-renders in place: static markup swaps immediately,
// everything built from API data is simply loaded again.
function changeLanguage(lang) {
  setLanguage(lang);
  let theme = 'auto';
  try { theme = localStorage.getItem('harvey-theme') || 'auto'; } catch { /* ignore */ }
  applyTheme(THEMES.includes(theme) ? theme : 'auto');
  loadHarveyStatus();
  loadCurrentTab();
}

// ── Init & live refresh ──

applyStaticTranslations();
loadToday();
loadTrend();
loadFunnel();
loadSetupStatus();
loadRuns();
loadTodayActivity();
loadHarveyStatus();

loadLiveStatus();

// Agent status: quick poll (not while locked: it would only be refused)
setInterval(() => { if (!_locked) loadHarveyStatus(); }, 8000);

// Live status panel: refreshed while the Today tab is open and visible.
setInterval(() => {
  if (!document.hidden && !_locked && currentTab === 'today') loadLiveStatus();
}, 10000);

// Nav counts stay live wherever you are, so "something needs me" is visible
// from any tab without polling that tab's contents.
setInterval(async () => {
  if (document.hidden || _locked || currentTab === 'today') return;
  const data = await api('/api/today');
  if (!data) return;
  navCount('nav-today', (data.items || []).filter(i => i.tone !== 'good').length);
  navCount('nav-outbox', (data.stats || {}).outbox_pending || 0);
}, 20000);

// Data tabs: auto-refresh live views without clobbering anything in progress.
// Outbox, settings and help are deliberately excluded — re-rendering the desk
// under someone mid-decision loses their place, and settings may be mid-edit.
setInterval(() => {
  if (document.hidden || _locked) return;
  switch (currentTab) {
    case 'today': loadToday(); loadTrend(); loadFunnel(); loadRuns(); loadTodayActivity(); break;
    case 'companies': if (!companyDrill) loadCompanies(); break;
    case 'prospects': loadProspects(); break;
    case 'campaigns': loadCampaigns(); break;
    case 'conversations': loadConversations(); break;
    case 'activity': loadActivity(); break;
    case 'usage': loadUsage(); break;
    case 'controls': loadLogs(); break;
  }
}, 15000);
