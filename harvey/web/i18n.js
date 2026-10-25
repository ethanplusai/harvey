// ── Language ──
//
// English is the source language: every string in index.html and app.js is
// written in English and doubles as its own translation key. Other languages
// live in i18n/<lang>.json as {"English text": "translation"} — a missing entry
// falls back to the English text, so an untranslated string is never blank.
// The same files are read by the server (harvey/i18n.py) for texts the API
// builds itself. Stored per-browser, like the theme.

const LANGUAGES = { en: 'English', de: 'Deutsch' };
const LOCALES = { en: 'en-US', de: 'de-DE' };

let LANG = 'en';
let _dict = {};

function _loadDict(lang) {
  if (lang === 'en') return {};
  // Synchronous on purpose: the dictionary must be there before the first
  // render, and it is a small local file served by the dashboard itself.
  try {
    const xhr = new XMLHttpRequest();
    xhr.open('GET', '/static/i18n/' + lang + '.json', false);
    xhr.send();
    if (xhr.status === 200) return JSON.parse(xhr.responseText);
  } catch { /* fall back to English */ }
  return {};
}

function _norm(s) {
  return String(s).replace(/\s+/g, ' ').trim();
}

// The browser re-serialises markup it reads back (&mdash; comes back as "—"),
// so keys holding markup are also indexed the way innerHTML will return them.
let _canon = {};
function _buildCanon() {
  _canon = {};
  if (typeof document === 'undefined') return;
  const tpl = document.createElement('template');
  for (const key in _dict) {
    if (!/[&<]/.test(key)) continue;
    tpl.innerHTML = key;
    _canon[_norm(tpl.innerHTML)] = _dict[key];
  }
}

// t('Approved {n} email(s).', {n: 3}) — {name} placeholders are filled after
// lookup, so the key stays the same whatever the values are.
function t(text, vars) {
  let out = _dict[text];
  if (out === undefined) out = _dict[_norm(text)];
  if (out === undefined) out = _canon[_norm(text)];
  if (out === undefined || out === '') out = text;
  if (vars) {
    out = out.replace(/\{(\w+)\}/g, (m, k) => (k in vars ? String(vars[k]) : m));
  }
  return out;
}

function locale() {
  return LOCALES[LANG] || 'en-US';
}

// Static markup carries data-i18n (inner HTML), data-i18n-placeholder and
// data-i18n-title. The English original is remembered on first pass so the
// page can switch back and forth without a reload.
function applyStaticTranslations(root) {
  (root || document).querySelectorAll('[data-i18n]').forEach(el => {
    if (el.dataset.i18nSrc === undefined) el.dataset.i18nSrc = el.innerHTML.trim();
    el.innerHTML = t(el.dataset.i18nSrc);
  });
  for (const attr of ['placeholder', 'title']) {
    (root || document).querySelectorAll('[data-i18n-' + attr + ']').forEach(el => {
      const key = 'i18nSrc' + attr.charAt(0).toUpperCase() + attr.slice(1);
      if (el.dataset[key] === undefined) el.dataset[key] = el.getAttribute(attr) || '';
      el.setAttribute(attr, t(el.dataset[key]));
    });
  }
  document.documentElement.lang = LANG;
  const sel = document.getElementById('lang-select');
  if (sel) sel.value = LANG;
}

function setLanguage(lang) {
  if (!LANGUAGES[lang]) lang = 'en';
  LANG = lang;
  _dict = _loadDict(lang);
  _buildCanon();
  try { localStorage.setItem('harvey-lang', lang); } catch { /* private mode */ }
  applyStaticTranslations();
}

(function initLanguage() {
  let saved = 'en';
  try { saved = localStorage.getItem('harvey-lang') || 'en'; } catch { /* ignore */ }
  LANG = LANGUAGES[saved] ? saved : 'en';
  _dict = _loadDict(LANG);
  _buildCanon();
  if (typeof document !== 'undefined' && document.documentElement) document.documentElement.lang = LANG;
})();
