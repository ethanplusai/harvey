// ── Other mail (Morthy's job-search mode) ──
//
// Mail from outside the prospect list: ATS answers, recruiters, job alerts.
// Morthy suggests which application an email is about and what it says;
// nothing reaches data/aplicaciones.csv until a button here confirms it.

let _otros = {mail: [], applications: [], noise_hidden: 0};
let _otrosFilter = 'new';
let _otrosNoise = false;

const OTROS_FILTERS = [
  ['new', 'To review', m => m.status === 'new'],
  ['follow', 'Following', m => m.status === 'follow'],
  ['linked', 'Recorded', m => m.status === 'linked'],
  ['ignored', 'Ignored', m => m.status === 'ignored'],
  ['all', 'All', () => true],
];
const OTROS_SUGGESTIONS = {
  rechazo: ['Rejection', 'bad'], prueba: ['Technical test', 'waiting'], entrevista: ['Interview', 'good'],
  acuse: ['Acknowledgement', 'idle'], alerta: ['Job alert', 'idle'],
};
// The outcomes data/aplicaciones.csv records (in Spanish there, by design), as shown here.
const OTROS_OUTCOME_LABELS = {
  nada: 'no answer', rechazo: 'rejection', prueba: 'technical test', entrevista: 'interview', oferta: 'offer',
};
const OTROS_OUTCOMES = [
  ['acuse', 'Just an acknowledgement (changes nothing)'], ['rechazo', 'Rejection'],
  ['prueba', 'Technical test'], ['entrevista', 'Interview'], ['oferta', 'Offer'],
];

async function loadOtherMail() {
  const el = document.getElementById('otros-list');
  const data = await api('/api/other-mail' + (_otrosNoise ? '?noise=1' : ''));
  if (!data) { el.innerHTML = offlineState(); return; }
  _otros = data;
  const nav = document.getElementById('nav-otros');
  if (nav) nav.textContent = data.mail.filter(m => m.status === 'new' && !m.noise).length || '';
  renderOtherMail();
}

function setOtrosFilter(f) { _otrosFilter = f; renderOtherMail(); }
function toggleOtrosNoise() { _otrosNoise = !_otrosNoise; loadOtherMail(); }

function renderOtherMail() {
  const el = document.getElementById('otros-list');
  const filter = OTROS_FILTERS.find(f => f[0] === _otrosFilter) || OTROS_FILTERS[0];
  const bar = '<div class="toolbar"><div class="segmented" role="group" aria-label="Filter mail">' +
    OTROS_FILTERS.map(f => '<button class="' + (f[0] === filter[0] ? 'on' : '') + '" aria-pressed="' +
      (f[0] === filter[0]) + '" onclick="setOtrosFilter(' + jsq(f[0]) + ')">' + f[1] +
      ' <span class="n">' + _otros.mail.filter(f[2]).length + '</span></button>').join('') + '</div>' +
    '<button class="btn btn-ghost btn-sm" onclick="toggleOtrosNoise()">' +
      (_otrosNoise ? 'Hide noise' : 'Show noise (' + _otros.noise_hidden + ')') + '</button>' +
    '<button class="btn btn-secondary btn-sm" id="otros-import" onclick="importOtherMail()">Import last 30 days</button></div>';
  const shown = _otros.mail.filter(filter[2]);
  if (!shown.length) {
    el.innerHTML = bar + '<div class="card">' + emptyState(icon('check'), 'Nothing here',
      _otros.mail.length ? 'No mail matches this filter.'
        : 'Mail that isn\'t from a prospect (an applicant tracking system, a recruiter, a job alert) shows up here. ' +
          'To bring in what arrived earlier, use "Import last 30 days".') + '</div>';
    return;
  }
  el.innerHTML = bar + shown.map(otroCard).join('');
}

function otroCard(m) {
  const sug = OTROS_SUGGESTIONS[m.suggestion];
  const app = _otros.applications.find(a => a.url === m.application_url);
  const tags = (sug ? '<span class="badge t-' + sug[1] + '">' + sug[0] + '</span> ' : '') +
    (m.is_ats ? '<span class="tag">Applicant tracking system</span> ' : '') +
    (m.noise ? '<span class="tag">Noise</span> ' : '') +
    (m.status === 'follow' ? '<span class="tag">Following</span> ' : '');
  const appLine = app
    ? '<div class="sub">' + (m.status === 'linked' ? 'Recorded on' : 'Looks like your application for') + ': <b>' +
      escHtml(app.titulo) + '</b> · ' + escHtml(app.empresa) +
      (app.respuesta ? ' (now: ' + escHtml(OTROS_OUTCOME_LABELS[app.respuesta] || app.respuesta) + ')' : '') + '</div>'
    : '';
  const id = jsq(m.id);
  const options = '<option value="">Which application is it about?</option>' + _otros.applications.slice().reverse()
    .map(a => '<option value="' + escHtml(a.url) + '"' + (a.url === m.application_url ? ' selected' : '') + '>' +
      escHtml(a.titulo.slice(0, 50)) + ' · ' + escHtml(a.empresa.slice(0, 25)) + '</option>').join('');
  const outcomeDefault = OTROS_OUTCOMES.some(o => o[0] === m.suggestion) ? m.suggestion : 'acuse';
  const outcomes = OTROS_OUTCOMES.map(o => '<option value="' + o[0] + '"' +
    (o[0] === outcomeDefault ? ' selected' : '') + '>' + o[1] + '</option>').join('');
  const key = escHtml(m.id);
  const link = m.status === 'linked' ? '' :
    '<div class="btn-group" style="margin-top:var(--sp-2)">' +
      '<select id="otro-app-' + key + '">' + options + '</select>' +
      '<select id="otro-out-' + key + '">' + outcomes + '</select>' +
      '<button class="btn btn-primary btn-sm" onclick="linkOtherMail(' + id + ')">Record</button></div>';
  const actions = '<div class="btn-group" style="margin-top:var(--sp-2)">' +
    (m.status !== 'follow' ? '<button class="btn btn-secondary btn-sm" onclick="otherMailAction(' + id + ',\'follow\')">Follow</button>' : '') +
    (m.status !== 'ignored' ? '<button class="btn btn-ghost btn-sm" onclick="otherMailAction(' + id + ',\'ignore\')">Ignore</button>' : '') +
    (m.status !== 'new' ? '<button class="btn btn-ghost btn-sm" onclick="otherMailAction(' + id + ',\'new\')">Back to review</button>' : '') +
    '</div>';
  return '<div class="convo-card"><div class="convo-head"><div>' +
    '<h3>' + escHtml(m.subject || '(no subject)') + '</h3>' +
    '<div class="sub">' + escHtml(m.from_email) + ' · ' + relTime(m.received_at) + '</div>' +
    appLine + '<div style="margin-top:var(--sp-1)">' + tags + '</div></div></div>' +
    '<div class="te-body" style="max-height:8em;overflow:auto;white-space:pre-wrap">' +
      escHtml((m.snippet || '').slice(0, 700)) + '</div>' + link + actions + '</div>';
}

async function otherMailAction(id, action) {
  const r = await api('/api/other-mail/' + encodeURIComponent(id), {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({action}),
  });
  if (!r || r.success === false) { showToast((r && r.message) || 'Couldn\'t save that', 'error'); return; }
  loadOtherMail();
}

async function linkOtherMail(id) {
  const app = document.getElementById('otro-app-' + id).value;
  const respuesta = document.getElementById('otro-out-' + id).value;
  if (!app) { showToast('Pick the application first', 'error'); return; }
  const r = await api('/api/other-mail/' + encodeURIComponent(id), {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({action: 'link', app_url: app, respuesta}),
  });
  if (!r || r.success === false) { showToast((r && r.message) || 'Couldn\'t record that', 'error'); return; }
  showToast(respuesta === 'acuse' ? 'Mail linked to the application' : 'Recorded in aplicaciones.csv', 'success');
  loadOtherMail();
}

async function importOtherMail() {
  const btn = document.getElementById('otros-import');
  if (btn) { btn.disabled = true; btn.textContent = 'Importing…'; }
  const r = await api('/api/other-mail/import', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({days: 30}),
  });
  if (!r || r.success === false) showToast((r && r.message) || 'Couldn\'t import', 'error');
  else showToast(r.kept + ' new email(s) imported', 'success');
  loadOtherMail();
}
