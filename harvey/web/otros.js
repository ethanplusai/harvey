// ── Otros correos (job-search mode, local fork) ──
//
// Mail from outside the prospect list: ATS answers, recruiters, job alerts.
// Morthy suggests which application an email is about and what it says;
// nothing reaches data/aplicaciones.csv until a button here confirms it.

let _otros = {mail: [], applications: [], noise_hidden: 0};
let _otrosFilter = 'new';
let _otrosNoise = false;

const OTROS_FILTERS = [
  ['new', 'Por revisar', m => m.status === 'new'],
  ['follow', 'Seguimiento', m => m.status === 'follow'],
  ['linked', 'Anotados', m => m.status === 'linked'],
  ['ignored', 'Ignorados', m => m.status === 'ignored'],
  ['all', 'Todos', () => true],
];
const OTROS_SUGGESTIONS = {
  rechazo: ['Rechazo', 'bad'], prueba: ['Prueba técnica', 'waiting'], entrevista: ['Entrevista', 'good'],
  acuse: ['Acuse de recibo', 'idle'], alerta: ['Alerta de vacantes', 'idle'],
};
const OTROS_OUTCOMES = [
  ['acuse', 'Solo acuse (no cambia nada)'], ['rechazo', 'Rechazo'], ['prueba', 'Prueba técnica'],
  ['entrevista', 'Entrevista'], ['oferta', 'Oferta'],
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
  const bar = '<div class="toolbar"><div class="segmented" role="group" aria-label="Filtrar correos">' +
    OTROS_FILTERS.map(f => '<button class="' + (f[0] === filter[0] ? 'on' : '') + '" aria-pressed="' +
      (f[0] === filter[0]) + '" onclick="setOtrosFilter(' + jsq(f[0]) + ')">' + f[1] +
      ' <span class="n">' + _otros.mail.filter(f[2]).length + '</span></button>').join('') + '</div>' +
    '<button class="btn btn-ghost btn-sm" onclick="toggleOtrosNoise()">' +
      (_otrosNoise ? 'Ocultar ruido' : 'Mostrar ruido (' + _otros.noise_hidden + ')') + '</button>' +
    '<button class="btn btn-secondary btn-sm" id="otros-import" onclick="importOtherMail()">Importar últimos 30 días</button></div>';
  const shown = _otros.mail.filter(filter[2]);
  if (!shown.length) {
    el.innerHTML = bar + '<div class="card">' + emptyState(icon('check'), 'Nada aquí',
      _otros.mail.length ? 'Ningún correo en este filtro.'
        : 'Cuando llegue un correo que no es de un prospecto (un ATS, un reclutador, una alerta), aparece aquí. ' +
          'Para traer los que llegaron antes, usa "Importar últimos 30 días".') + '</div>';
    return;
  }
  el.innerHTML = bar + shown.map(otroCard).join('');
}

function otroCard(m) {
  const sug = OTROS_SUGGESTIONS[m.suggestion];
  const app = _otros.applications.find(a => a.url === m.application_url);
  const tags = (sug ? '<span class="badge t-' + sug[1] + '">' + sug[0] + '</span> ' : '') +
    (m.is_ats ? '<span class="tag">Sistema de postulación</span> ' : '') +
    (m.noise ? '<span class="tag">Ruido</span> ' : '') +
    (m.status === 'follow' ? '<span class="tag">En seguimiento</span> ' : '');
  const appLine = app
    ? '<div class="sub">' + (m.status === 'linked' ? 'Anotado en' : 'Parece de tu postulación a') + ': <b>' +
      escHtml(app.titulo) + '</b> · ' + escHtml(app.empresa) +
      (app.respuesta ? ' (ahora: ' + escHtml(app.respuesta) + ')' : '') + '</div>'
    : '';
  const id = jsq(m.id);
  const options = '<option value="">¿De qué postulación es?</option>' + _otros.applications.slice().reverse()
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
      '<button class="btn btn-primary btn-sm" onclick="linkOtherMail(' + id + ')">Anotar</button></div>';
  const actions = '<div class="btn-group" style="margin-top:var(--sp-2)">' +
    (m.status !== 'follow' ? '<button class="btn btn-secondary btn-sm" onclick="otherMailAction(' + id + ',\'follow\')">Seguir</button>' : '') +
    (m.status !== 'ignored' ? '<button class="btn btn-ghost btn-sm" onclick="otherMailAction(' + id + ',\'ignore\')">Ignorar</button>' : '') +
    (m.status !== 'new' ? '<button class="btn btn-ghost btn-sm" onclick="otherMailAction(' + id + ',\'new\')">Volver a revisar</button>' : '') +
    '</div>';
  return '<div class="convo-card"><div class="convo-head"><div>' +
    '<h3>' + escHtml(m.subject || '(sin asunto)') + '</h3>' +
    '<div class="sub">' + escHtml(m.from_email) + ' · ' + relTime(m.received_at) + '</div>' +
    appLine + '<div style="margin-top:var(--sp-1)">' + tags + '</div></div></div>' +
    '<div class="te-body" style="max-height:8em;overflow:auto;white-space:pre-wrap">' +
      escHtml((m.snippet || '').slice(0, 700)) + '</div>' + link + actions + '</div>';
}

async function otherMailAction(id, action) {
  const r = await api('/api/other-mail/' + encodeURIComponent(id), {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({action}),
  });
  if (!r || r.success === false) { showToast((r && r.message) || 'No se pudo guardar', 'error'); return; }
  loadOtherMail();
}

async function linkOtherMail(id) {
  const app = document.getElementById('otro-app-' + id).value;
  const respuesta = document.getElementById('otro-out-' + id).value;
  if (!app) { showToast('Elige la postulación primero', 'error'); return; }
  const r = await api('/api/other-mail/' + encodeURIComponent(id), {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({action: 'link', app_url: app, respuesta}),
  });
  if (!r || r.success === false) { showToast((r && r.message) || 'No se pudo anotar', 'error'); return; }
  showToast(respuesta === 'acuse' ? 'Correo unido a la postulación' : 'Anotado en aplicaciones.csv', 'success');
  loadOtherMail();
}

async function importOtherMail() {
  const btn = document.getElementById('otros-import');
  if (btn) { btn.disabled = true; btn.textContent = 'Importando…'; }
  const r = await api('/api/other-mail/import', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({days: 30}),
  });
  if (!r || r.success === false) showToast((r && r.message) || 'No se pudo importar', 'error');
  else showToast(r.kept + ' correo(s) nuevos importados', 'success');
  loadOtherMail();
}
