/**
 * MAX Token Extractor
 *
 * Run this snippet in Chrome DevTools Console while on https://web.max.ru
 * to extract your active session token for use in the bridge.
 *
 * Instructions:
 *   1. Open https://web.max.ru and log in as usual
 *   2. Open DevTools → Console tab  (Cmd+Option+J on Mac)
 *   3. Paste this entire script and press Enter
 *   4. Copy the printed "login_token" value into the bridge session file
 */

(function extractMaxToken() {
  const results = {};

  // ── 1. Check localStorage ──────────────────────────────────────────────────
  try {
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i);
      const val = localStorage.getItem(key);
      if (val && (val.includes('LOGIN') || val.includes('loginToken') || val.includes('token'))) {
        try {
          const parsed = JSON.parse(val);
          results[`localStorage["${key}"]`] = parsed;
        } catch {
          if (val.length > 10 && val.length < 500) {
            results[`localStorage["${key}"] (raw)`] = val;
          }
        }
      }
    }
  } catch (e) {
    console.warn('localStorage scan failed:', e);
  }

  // ── 2. Check sessionStorage ────────────────────────────────────────────────
  try {
    for (let i = 0; i < sessionStorage.length; i++) {
      const key = sessionStorage.key(i);
      const val = sessionStorage.getItem(key);
      if (val && (val.includes('LOGIN') || val.includes('loginToken') || val.includes('token'))) {
        try {
          const parsed = JSON.parse(val);
          results[`sessionStorage["${key}"]`] = parsed;
        } catch {
          if (val.length > 10 && val.length < 500) {
            results[`sessionStorage["${key}"] (raw)`] = val;
          }
        }
      }
    }
  } catch (e) {
    console.warn('sessionStorage scan failed:', e);
  }

  // ── 3. Intercept WebSocket messages (forward-looking) ─────────────────────
  // This patches WebSocket so any future server messages containing a token are printed.
  const OrigWS = window.WebSocket;
  window.WebSocket = function(...args) {
    const ws = new OrigWS(...args);
    if (args[0] && args[0].includes('oneme.ru')) {
      console.log('%c[MAX Token Extractor] Monitoring WebSocket:', 'color:cyan', args[0]);
      ws.addEventListener('message', (event) => {
        try {
          const msg = JSON.parse(event.data);
          const payload = msg.payload || {};
          // opcode 18 (sign_in) or 19 (login_by_token) response contain tokenAttrs
          if (payload.tokenAttrs && payload.tokenAttrs.LOGIN) {
            const token = payload.tokenAttrs.LOGIN.token;
            console.log('%c[MAX Token Extractor] ✅ LOGIN token found (from WS message)!', 'color:lime; font-weight:bold');
            console.log('%clogin_token =', 'color:lime', token);
            console.log('%c↑ Copy this value into sessions/<name>.max_session as {"login_token": "<value>"}', 'color:yellow');
          }
          // opcode 19 response also includes profile
          if (msg.opcode === 19 && payload.profile) {
            console.log('%c[MAX Token Extractor] Profile:', 'color:cyan', {
              userId: payload.profile.userId,
              phone: payload.profile.phone,
              name: payload.profile.name,
            });
          }
        } catch {}
      });
    }
    return ws;
  };
  Object.assign(window.WebSocket, OrigWS);

  // ── 4. Print results ──────────────────────────────────────────────────────
  console.log('%c[MAX Token Extractor] Storage scan results:', 'color:cyan; font-weight:bold');
  if (Object.keys(results).length > 0) {
    console.log(results);
  } else {
    console.log('%cNothing found in localStorage/sessionStorage.', 'color:orange');
    console.log('%cWebSocket is now being monitored — if you refresh or re-login, the token will appear here.', 'color:yellow');
  }

  console.log('\n%c[MAX Token Extractor] WebSocket interceptor is active.', 'color:lime');
  console.log('%cRefresh the page (F5) while this DevTools tab is open to capture the login token.', 'color:yellow');
})();
