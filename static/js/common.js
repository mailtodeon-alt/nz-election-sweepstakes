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
  function netBanner(text, show) {
    let el = document.getElementById('netBanner');
    if (!el) {
      el = document.createElement('div');
      el.id = 'netBanner';
      el.className = 'net-banner';
      el.setAttribute('role', 'status');
      document.body.appendChild(el);
    }
    el.textContent = text;
    el.classList.toggle('show', !!show);
  }
  window.addEventListener('offline', () => netBanner('You are offline. We will retry when you are back.', true));
  window.addEventListener('online', () => netBanner('Back online.', false));
  function subscribe(onEvent) {
    let es = null;
    let stopped = false;
    let retryMs = 1000;
    let connectedOnce = false;
    function connect() {
      if (stopped) return;
      const url = apiUrl('/api/events');
      es = new EventSource(url);
      es.onmessage = (ev) => {
        retryMs = 1000;
        if (!connectedOnce) connectedOnce = true;
        netBanner('', false);
        try {
          const msg = JSON.parse(ev.data);
          onEvent(msg);
        } catch (e) {}
      };
      es.onerror = () => {
        try { es.close(); } catch (e) {}
        if (stopped) return;
        netBanner('Reconnecting to live updates…', true);
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
    if (n.includes('first')) return '#6b7280';
    if (n.includes('māori') || n.includes('maori') || n.includes('pati')) return '#B2001D';
    return '#5dd6ff';
  }
  function formatWhen(iso, style) {
    if (!iso) return '';
    const raw = String(iso).trim();
    const dateOnly = /^\d{4}-\d{2}-\d{2}$/.test(raw);
    const d = new Date(dateOnly ? raw + 'T12:00:00+13:00' : raw);
    if (isNaN(d.getTime())) return raw;
    const parts = new Intl.DateTimeFormat('en-NZ', {
      timeZone: 'Pacific/Auckland',
      weekday: 'long',
      day: 'numeric',
      month: 'long',
      year: 'numeric',
      hour: 'numeric',
      minute: '2-digit',
      hour12: true,
    }).formatToParts(d);
    const get = (type) => (parts.find((p) => p.type === type) || {}).value || '';
    const clock = (get('hour') + ':' + get('minute') + (get('dayPeriod') || '')).replace(/\s/g, '').toLowerCase();
    if (style === 'date' || (dateOnly && style !== 'datetime')) {
      return get('day') + ' ' + get('month') + ' ' + get('year');
    }
    return clock + ' ' + get('weekday') + ' ' + get('day') + ' ' + get('month');
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
  return { api, toast, subscribe, partyColor, fmtCountdown, formatWhen, boardLabel, isDemo, apiUrl };
})();

/* A new worker calls skipWaiting + clients.claim. Reload once so an open TV
   board leaves the previous shell. The timestamp blocks a second reload if
   controllerchange also fires on the page we just loaded. */
(function () {
  if (!('serviceWorker' in navigator)) return;
  var GUARD_KEY = 'sw-controller-reload-at';
  var GUARD_MS = 15000;
  var reloading = false;
  navigator.serviceWorker.addEventListener('controllerchange', function () {
    if (reloading) return;
    var now = Date.now();
    var last = 0;
    try { last = parseInt(sessionStorage.getItem(GUARD_KEY) || '0', 10) || 0; } catch (err) {}
    if (now - last < GUARD_MS) return;
    reloading = true;
    try { sessionStorage.setItem(GUARD_KEY, String(now)); } catch (err) {}
    window.location.reload();
  });
  navigator.serviceWorker.register('/sw.js').then(function (reg) {
    // The live board stays open for hours. Ask for a new worker so a deploy
    // can controllerchange-reload without someone refreshing the TV.
    setInterval(function () { reg.update().catch(function () {}); }, 60000);
  }).catch(function () {});
})();

/* Header + burger menu on every page that loads common.js. */
(function () {
  if (!window.Sweep || document.getElementById('siteHeader')) return;
  const demo = Sweep.isDemo;
  function pathFor(p) {
    if (!demo) return p;
    if (p === '/') return '/demo/';
    return '/demo' + p;
  }
  function meKey() { return demo ? 'sweep_me_demo' : 'sweep_me'; }
  function readMe() {
    try {
      const saved = JSON.parse(localStorage.getItem(meKey()) || 'null');
      if (saved && saved.token) return saved;
    } catch (e) {}
    return null;
  }
  const here = location.pathname.replace(/\/+$/, '') || '/';
  function current(href) {
    const target = href.replace(/\/+$/, '') || '/';
    if (target === '/demo' && (here === '/demo' || here === '/')) return true;
    return here === target;
  }
  function items() {
    const me = readMe();
    const picks = me && me.path
      ? { href: me.path, label: 'My picks', icon: '✎' }
      : { href: pathFor('/open'), label: 'Get your own link', icon: '✎' };
    const board = me && me.pool === 'public'
      ? { href: pathFor('/open/live'), label: 'Live board', icon: '▣' }
      : { href: pathFor('/live'), label: 'Live board', icon: '▣' };
    const publicBoard = { href: pathFor('/open/live'), label: 'Public leaderboard', icon: '◎' };
    const recap = me && me.pool === 'public' ? pathFor('/open/recap') : pathFor('/recap');
    const nav = [
      { href: pathFor('/'), label: 'Home', icon: '⌂' },
      { href: pathFor('/results'), label: 'Results', icon: '▤' },
      { href: pathFor('/seats'), label: 'Seats', icon: '◕' },
      { href: pathFor('/news'), label: 'News', icon: '✎' },
      board,
    ];
    if (board.href !== publicBoard.href) nav.push(publicBoard);
    return nav.concat([
      picks,
      { href: pathFor('/coalition'), label: 'Coalition', icon: '⚑' },
      { href: recap, label: 'Recap', icon: '★' },
    ]);
  }
  const header = document.createElement('header');
  header.className = 'site-header';
  header.id = 'siteHeader';
  const title = document.createElement('a');
  title.className = 'site-title';
  title.href = pathFor('/');
  const mark = document.createElement('span');
  mark.className = 'brand-mark';
  mark.setAttribute('aria-hidden', 'true');
  mark.textContent = '✓';
  title.append(mark, document.createTextNode(demo ? 'NZ Election 2026 · demo' : 'NZ Election 2026'));
  if (document.title && document.title.indexOf('NZ Election 2026') === -1) {
    document.title = document.title.replace(/\s·\sNZ Election$/, '') + ' · NZ Election 2026';
  }
  const burger = document.createElement('button');
  burger.className = 'burger';
  burger.type = 'button';
  burger.setAttribute('aria-expanded', 'false');
  burger.setAttribute('aria-controls', 'siteMenu');
  burger.setAttribute('aria-label', 'Open menu');
  const bars = document.createElement('span');
  bars.className = 'burger-icon';
  bars.setAttribute('aria-hidden', 'true');
  burger.appendChild(bars);
  header.append(title, burger);

  const overlay = document.createElement('div');
  overlay.className = 'nav-overlay';
  overlay.hidden = true;
  const drawer = document.createElement('nav');
  drawer.className = 'nav-drawer';
  drawer.id = 'siteMenu';
  drawer.setAttribute('aria-label', 'Pages');
  drawer.hidden = true;
  const head = document.createElement('div');
  head.className = 'nav-drawer-head';
  const heading = document.createElement('strong');
  heading.textContent = 'Menu';
  const closeBtn = document.createElement('button');
  closeBtn.className = 'nav-close';
  closeBtn.type = 'button';
  closeBtn.setAttribute('aria-label', 'Close menu');
  closeBtn.textContent = '×';
  head.append(heading, closeBtn);
  const list = document.createElement('ul');
  list.className = 'nav-list';
  function renderList() {
    list.replaceChildren();
    items().forEach((item) => {
      const li = document.createElement('li');
      const a = document.createElement('a');
      a.href = item.href;
      if (current(item.href)) a.setAttribute('aria-current', 'page');
      const ico = document.createElement('span');
      ico.className = 'nav-ico';
      ico.setAttribute('aria-hidden', 'true');
      ico.textContent = item.icon;
      a.append(ico, document.createTextNode(item.label));
      li.appendChild(a);
      list.appendChild(li);
    });
  }
  renderList();
  drawer.append(head, list);
  document.body.prepend(overlay);
  document.body.prepend(drawer);
  document.body.prepend(header);

  if (!document.querySelector('.tv') && !/\/admin$/.test(here)) {
    const foot = document.createElement('p');
    foot.className = 'site-foot';
    const a = document.createElement('a');
    a.href = demo ? '/' : '/demo';
    a.textContent = demo ? 'Back to the real site' : 'Demo boards';
    foot.appendChild(a);
    document.body.appendChild(foot);
  }

  let lastFocus = null;
  function focusables() {
    return [...drawer.querySelectorAll('a, button')].filter((el) => !el.hidden);
  }
  function openMenu() {
    lastFocus = document.activeElement;
    overlay.hidden = false;
    drawer.hidden = false;
    document.body.classList.add('nav-open');
    requestAnimationFrame(() => drawer.classList.add('open'));
    burger.setAttribute('aria-expanded', 'true');
    burger.setAttribute('aria-label', 'Close menu');
    closeBtn.focus();
  }
  function closeMenu() {
    drawer.classList.remove('open');
    burger.setAttribute('aria-expanded', 'false');
    burger.setAttribute('aria-label', 'Open menu');
    document.body.classList.remove('nav-open');
    window.setTimeout(() => {
      if (!drawer.classList.contains('open')) {
        drawer.hidden = true;
        overlay.hidden = true;
      }
    }, 280);
    if (lastFocus && lastFocus.focus) lastFocus.focus();
  }
  burger.addEventListener('click', () => {
    if (burger.getAttribute('aria-expanded') === 'true') closeMenu();
    else openMenu();
  });
  closeBtn.addEventListener('click', closeMenu);
  overlay.addEventListener('click', closeMenu);
  document.addEventListener('keydown', (ev) => {
    if (drawer.hidden) return;
    if (ev.key === 'Escape') { ev.preventDefault(); closeMenu(); return; }
    if (ev.key !== 'Tab') return;
    const nodes = focusables();
    if (!nodes.length) return;
    const first = nodes[0];
    const last = nodes[nodes.length - 1];
    if (ev.shiftKey && document.activeElement === first) { ev.preventDefault(); last.focus(); }
    else if (!ev.shiftKey && document.activeElement === last) { ev.preventDefault(); first.focus(); }
  });

  Sweep.bindIdentity = function (saved) {
    if (!saved || !saved.token) return;
    try { localStorage.setItem(meKey(), JSON.stringify(saved)); } catch (e) {}
    renderList();
  };

  if (document.querySelector('.tv')) {
    let idle;
    const wide = window.matchMedia('(min-width: 900px)');
    const poke = () => {
      document.body.classList.remove('nav-idle');
      clearTimeout(idle);
      if (!wide.matches) return;
      idle = setTimeout(() => { if (wide.matches) document.body.classList.add('nav-idle'); }, 4000);
    };
    ['mousemove', 'pointerdown', 'keydown'].forEach((name) => document.addEventListener(name, poke));
    poke();
  }
})();
