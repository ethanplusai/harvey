// ── Dashboard authorisation ──
//
// Every API call needs the token `harvey dashboard` prints. It arrives once,
// in the link's #fragment (browsers never put fragments in HTTP requests or
// Referer, so it stays out of server and proxy logs), is moved to
// sessionStorage (this tab only, gone when it closes) and replaced in the
// address bar before anything else on the page runs. The browser itself
// sees the full link before then, so its history or extensions may too. It is only ever sent as an
// Authorization header, and only to this origin: never a cookie, never a
// query string, never another host.

const AUTH_KEY = 'harvey-dashboard-token';
const AUTH_TOKEN_RE = /^[A-Za-z0-9_-]{43,128}$/;

(function takeFragmentToken() {
  const hash = window.location.hash || '';
  if (!hash) return;
  const token = new URLSearchParams(hash.slice(1)).get('token');
  history.replaceState(null, '', window.location.pathname + window.location.search);
  if (token && AUTH_TOKEN_RE.test(token)) setAuthToken(token);
})();

function authToken() {
  try { return sessionStorage.getItem(AUTH_KEY) || ''; } catch { return ''; }
}

function setAuthToken(token) {
  if (!AUTH_TOKEN_RE.test(token || '')) return false;
  try { sessionStorage.setItem(AUTH_KEY, token); return true; } catch { return false; }
}

function clearAuthToken() {
  try { sessionStorage.removeItem(AUTH_KEY); } catch { /* ignore */ }
}

// A same-origin URL for an absolute path ("/api/..."), or null.
function sameOriginUrl(path) {
  if (typeof path !== 'string' || !path.startsWith('/') || path.startsWith('//') ||
      path.includes('\\')) return null;
  let url;
  try { url = new URL(path, window.location.origin); } catch { return null; }
  return url.origin === window.location.origin ? url : null;
}

// fetch() for this dashboard's own API, with the token attached. Refuses
// anything that isn't same-origin, and never follows redirects.
function authFetch(path, opts) {
  const url = sameOriginUrl(path);
  if (!url) return Promise.reject(new Error('refusing to send the dashboard token off-origin'));
  const o = Object.assign({}, opts || {});
  const headers = new Headers(o.headers || {});
  const token = authToken();
  if (token) headers.set('Authorization', 'Bearer ' + token);
  o.headers = headers;
  o.mode = 'same-origin';
  o.credentials = 'omit';
  o.cache = 'no-store';
  o.redirect = 'error';
  // Not 'no-referrer': per the Fetch spec that makes a POST's Origin "null"
  // (Safari follows it), and the guard then refuses every action.
  o.referrerPolicy = 'same-origin';
  return fetch(url.href, o);
}
