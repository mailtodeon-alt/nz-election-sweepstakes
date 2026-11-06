/* Public leaderboard: top 10, your saved place, search, and show all. */
window.PublicBoard = (function () {
  function unescapeHtml(s) {
    const t = document.createElement('textarea');
    t.innerHTML = s || '';
    return t.value;
  }
  function ordinal(n) {
    const v = n % 100;
    if (v >= 11 && v <= 13) return n + 'th';
    const last = n % 10;
    if (last === 1) return n + 'st';
    if (last === 2) return n + 'nd';
    if (last === 3) return n + 'rd';
    return n + 'th';
  }
  function create(opts) {
    const list = opts.list;
    const note = opts.note;
    const toggle = opts.toggle;
    const search = opts.search;
    let showAll = false;
    let query = '';
    let myId = null;
    let locked = false;
    let board = [];
    const prevRanks = {};

    function myToken() {
      try {
        const saved = JSON.parse(localStorage.getItem(Sweep.isDemo ? 'sweep_me_demo' : 'sweep_me') || 'null');
        if (saved && saved.token && saved.pool === 'public') return saved.token;
      } catch (e) {}
      return '';
    }
    async function resolveMe() {
      const token = myToken();
      myId = null;
      if (!token) return;
      try {
        const data = await Sweep.api('/api/player/' + encodeURIComponent(token));
        if ((data.player.pool || 'pot') === 'public') myId = data.player.id;
      } catch (e) {}
    }
    function prepare(rows) {
      const sorted = (rows || []).slice().sort((a, b) =>
        (a.place || a.rank || 99) - (b.place || b.rank || 99) || b.total - a.total || String(a.name).localeCompare(String(b.name)));
      sorted.forEach((row, i) => { row._pos = row.place || row.rank || (i + 1); });
      return sorted;
    }
    function visibleRows() {
      const q = query.trim().toLowerCase();
      if (q) return board.filter((row) => unescapeHtml(row.name).toLowerCase().includes(q));
      if (showAll || board.length <= 10) return board;
      const top = board.slice(0, 10);
      const mine = myId == null ? null : board.find((row) => row.player_id === myId);
      if (!mine || top.some((row) => row.player_id === mine.player_id)) return top;
      return top.concat([{ _gap: true }, mine]);
    }
    function paint() {
      if (search) {
        const showSearch = board.length > 10;
        search.hidden = !showSearch;
        if (!showSearch && query) {
          query = '';
          search.value = '';
        }
      }
      const rows = visibleRows();
      const mine = myId == null ? null : board.find((row) => row.player_id === myId);
      const place = mine ? mine._pos : null;
      if (!board.length) {
        note.textContent = '';
        toggle.hidden = true;
      } else if (query.trim()) {
        note.textContent = rows.length + (rows.length === 1 ? ' match' : ' matches');
        toggle.hidden = true;
      } else if (showAll || board.length <= 10) {
        note.textContent = 'All ' + board.length + (place ? ' · you are ' + ordinal(place) : '');
        toggle.hidden = board.length <= 10;
        toggle.textContent = 'Show top 10';
      } else {
        note.textContent = 'Top 10 of ' + board.length + (place ? ' · you are ' + ordinal(place) : '');
        toggle.hidden = false;
        toggle.textContent = 'Show all';
      }
      list.replaceChildren();
      if (!board.length) {
        const empty = document.createElement('div');
        empty.className = 'card';
        const p = document.createElement('p');
        p.className = 'muted';
        const a = document.createElement('a');
        a.href = Sweep.isDemo ? '/demo/open' : '/open';
        a.textContent = 'Play for fun';
        p.append('No public players yet. ', a, ' to get a link.');
        empty.appendChild(p);
        list.appendChild(empty);
        return;
      }
      if (!rows.length) {
        const p = document.createElement('p');
        p.className = 'muted';
        p.textContent = 'No one with that name.';
        list.appendChild(p);
        return;
      }
      rows.forEach((row, i) => {
        if (row._gap) {
          const gap = document.createElement('div');
          gap.className = 'list-gap muted';
          gap.textContent = 'Your position';
          list.appendChild(gap);
          return;
        }
        const el = document.createElement('div');
        const pos = row._pos || row.place || i + 1;
        const you = row.player_id === myId;
        el.className = 'leader-row' + (pos === 1 ? ' first' : '') + (you ? ' you' : '') + (locked && row.still_in_it === false ? ' out' : '');
        el.dataset.pid = row.player_id;
        const prev = prevRanks[row.player_id];
        let delta = '';
        if (prev != null && locked) {
          const d = prev - pos;
          if (d > 0) delta = '<div class="delta">▲ ' + d + '</div>';
          else if (d < 0) delta = '<div class="delta down">▼ ' + Math.abs(d) + '</div>';
          else delta = '<div class="delta" style="color:var(--muted)">—</div>';
        }
        const outTag = (locked && row.still_in_it === false) ? '<span class="tag-out">OUT</span>' : '';
        const maxBits = locked ? ' · max ' + (row.max_possible_now ?? '—') : '';
        let bw = '';
        if (locked && (row.best_call || row.worst_call)) {
          const b = row.best_call ? 'Best: ' + row.best_call.label + ' (+' + row.best_call.points + ')' : '';
          const w = row.worst_call ? 'Worst: ' + row.worst_call.label + ' (−' + row.worst_call.lost + ')' : '';
          bw = '<div class="bw">' + [b, w].filter(Boolean).join(' · ') + '</div>';
        }
        el.innerHTML =
          '<div class="pos">' + pos + '</div>' +
          '<div><div class="name">' + row.name + outTag + '</div>' +
          '<div class="sub">' + (row.submitted ? 'Ballot in' : 'No ballot yet') + maxBits +
          (row.side_points ? ' · +' + row.side_points + ' side' : '') + '</div>' + bw + '</div>' +
          '<div style="text-align:right"><div class="pts">' + (locked ? row.total : '—') + '</div>' + delta + '</div>';
        list.appendChild(el);
        prevRanks[row.player_id] = pos;
      });
    }
    function setPlayers(rows, isLocked) {
      locked = !!isLocked;
      board = prepare(rows);
      paint();
    }
    if (search) {
      search.addEventListener('input', (ev) => {
        query = ev.target.value || '';
        paint();
      });
    }
    if (toggle) {
      toggle.addEventListener('click', () => {
        showAll = !showAll;
        paint();
      });
    }
    return { resolveMe: resolveMe, setPlayers: setPlayers, paint: paint };
  }
  return { create: create };
})();
