window.Sweep = (function () {
  const isDemo = location.pathname.startsWith('/demo') || new URLSearchParams(location.search).has('demo');
  function apiUrl(path) {
    if (isDemo && path.startsWith('/api/')) {
      const u = new URL(path, location.origin);
      u.searchParams.set('demo', '1');
      return u.pathname + u.search;
    }
    return path;
  }
  async function api(path, opts = {}) {
    const headers = Object.assign({ 'Content-Type': 'application/json' }, opts.headers || {});
    if (isDemo) headers['X-Demo'] = '1';
    const res = await fetch(apiUrl(path), { ...opts, headers, credentials: 'same-origin' });
    const text = await res.text();
    let data;
    try { data = JSON.parse(text); } catch { data = { raw: text }; }
    if (!res.ok) {
      const err = new Error(data.error || data.message || res.statusText);
      err.status = res.status; err.data = data;
      throw err;
    }
    return data;
  }
  function toast(msg) {
    let el = document.getElementById('toast');
    if (!el) {
      el = document.createElement('div');
      el.id = 'toast'; el.className = 'toast';
      document.body.appendChild(el);
    }
    el.textContent = msg; el.classList.add('show');
    clearTimeout(el._t);
    el._t = setTimeout(() => el.classList.remove('show'), 2800);
  }
  /**
   * SSE with auto-reconnect. On every (re)connect, call onEvent({event:'hello'}) and
   * the page should fully reload state via its own load() — free Render instances sleep
   * after ~15 min idle and may drop the stream.
   */
  function subscribe(onEvent) {
    let es = null;
    let stopped = false;
    let retryMs = 1000;
    function connect() {
      if (stopped) return;
      const url = apiUrl('/api/events');
      es = new EventSource(url);
      es.onmessage = (ev) => {
        retryMs = 1000;
        try {
          const msg = JSON.parse(ev.data);
          onEvent(msg);
        } catch (e) {}
      };
      es.onerror = () => {
        try { es.close(); } catch (e) {}
        if (stopped) return;
        // Full resync after reconnect delay
        setTimeout(() => {
          onEvent({ event: 'reconnect' });
          connect();
        }, retryMs);
        retryMs = Math.min(retryMs * 1.5, 15000);
      };
    }
    connect();
    return {
      close() { stopped = true; try { es && es.close(); } catch (e) {} }
    };
  }
  function partyColor(name) {
    const n = (name || '').toLowerCase();
    if (n.includes('national')) return '#00529F';
    if (n.includes('labour')) return '#D82A20';
    if (n.includes('green')) return '#098137';
    if (n.includes('act')) return '#F5C518';
    if (n.includes('first')) return '#888';
    if (n.includes('māori') || n.includes('maori') || n.includes('pati')) return '#B2001D';
    return '#5dd6ff';
  }
  function fmtCountdown(lockAt, nowIso) {
    if (!lockAt) return '';
    const end = new Date(lockAt).getTime();
    const now = nowIso ? new Date(nowIso).getTime() : Date.now();
    let ms = end - now;
    if (ms <= 0) return 'LOCKED';
    const s = Math.floor(ms / 1000);
    const d = Math.floor(s / 86400);
    const h = Math.floor((s % 86400) / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = s % 60;
    if (d > 0) return `${d}d ${h}h ${m}m`;
    return `${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}:${String(sec).padStart(2,'0')}`;
  }
  function boardLabel(mode) {
    if (mode === 'frozen') return { text: 'FROZEN ELECTION NIGHT', cls: 'frozen' };
    if (mode === 'official') return { text: 'FINAL OFFICIAL', cls: 'official' };
    return { text: 'LIVE PROVISIONAL', cls: 'live' };
  }
  return { api, toast, subscribe, partyColor, fmtCountdown, boardLabel, isDemo, apiUrl };
})();
