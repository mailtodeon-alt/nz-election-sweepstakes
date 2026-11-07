window.__liveModern = true;
let prevRanks = {};
let prevLeaderId = null;
let state = null;
let boardQuery = '';
const searchEl = document.getElementById('livePlayerSearch');
if (searchEl) searchEl.addEventListener('input', () => { boardQuery = searchEl.value || ''; render(); });
let soundOn = localStorage.getItem('sweep_sound') === '1';
const soundBtn = document.getElementById('soundBtn');
function syncSoundBtn(){ soundBtn.textContent = soundOn ? '🔊 Sound on' : '🔊 Sound off'; }
syncSoundBtn();
soundBtn.onclick = () => { soundOn = !soundOn; localStorage.setItem('sweep_sound', soundOn?'1':'0'); syncSoundBtn(); };

function beep() {
  if (!soundOn) return;
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const o = ctx.createOscillator(); const g = ctx.createGain();
    o.type = 'triangle'; o.frequency.value = 523.25;
    g.gain.value = 0.04; o.connect(g); g.connect(ctx.destination);
    o.start(); g.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.8); o.stop(ctx.currentTime + 0.85);
  } catch(e) {}
}

function showLeaderTakeover(name) {
  const el = document.getElementById('leaderTakeover');
  document.getElementById('leaderName').textContent = name;
  el.classList.add('show');
  beep();
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.remove('show'), 4000);
}

const PARTIES = [
  { key: 'nat_vote', name: 'National', color: '#00529F' },
  { key: 'lab_vote', name: 'Labour', color: '#D82A20' },
  { key: 'grn_vote', name: 'Green', color: '#098137' },
  { key: 'act_vote', name: 'ACT', color: '#F5C518' },
  { key: 'nzf_vote', name: 'NZ First', color: '#888' },
  { key: 'tpm_vote', name: 'Te Pāti Māori', color: '#B2001D' },
];

function escapeHtml(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
const BALLOT_NAMES = { National: 1, Labour: 1, Green: 1, ACT: 1, 'NZ First': 1, 'Te Pāti Māori': 1 };
function partyVoteRows() {
  const rows = (state && state.parties) || [];
  const fromFeed = rows.some(row => row.seats != null || (row.percent != null && row.key && !BALLOT_NAMES[row.key]));
  if (fromFeed) {
    return rows.filter(row => {
      const v = parseFloat(row.percent);
      return row.percent != null && !Number.isNaN(v) && v > 0;
    }).map(row => ({
      name: row.party,
      percent: row.percent,
      seats: row.seats,
      color: row.color || '#8fa3c8',
      minor: !!row.minor,
      manual: false,
    }));
  }
  return PARTIES.map(p => {
    const r = resultForKey(p.key);
    const v = r ? parseFloat(r.value) : null;
    const known = v != null && !Number.isNaN(v);
    return {
      name: p.name,
      percent: known ? v : null,
      seats: null,
      color: p.color,
      minor: false,
      manual: r && r.source === 'manual',
      known: known,
    };
  }).filter(p => p.known);
}
function qByKey(key) { return state.questions.find(q => q.key === key); }
function resultForKey(key) {
  const q = qByKey(key);
  if (!q) return null;
  return state.results[String(q.id)] || null;
}

function flipAnimate() {
  if (document.documentElement.classList.contains('tv-mode')) return function () {};
  const list = document.getElementById('leaders');
  const rows = [...list.children];
  const first = new Map(rows.map(r => [r.dataset.pid, r.getBoundingClientRect()]));
  return () => {
    rows.forEach(r => {
      const last = r.getBoundingClientRect();
      const f = first.get(r.dataset.pid);
      if (!f) return;
      const dy = f.top - last.top;
      if (Math.abs(dy) < 1) return;
      r.style.transition = 'none';
      r.style.transform = `translateY(${dy}px)`;
      requestAnimationFrame(() => {
        r.style.transition = 'transform 0.55s cubic-bezier(.2,.8,.2,1)';
        r.style.transform = '';
      });
    });
  };
}

let rotateTimer = null;
function showWeekendView(name) {
  const tv = document.getElementById('tv');
  tv.classList.remove('view-board', 'view-latest', 'view-estimate', 'view-ranks');
  tv.classList.add('view-' + (name || 'board'));
  document.querySelectorAll('#weekTabs button').forEach(btn => {
    const on = btn.dataset.view === name;
    btn.classList.toggle('active', on);
    btn.setAttribute('aria-selected', on ? 'true' : 'false');
  });
}
function renderLatest(latest) {
  const box = document.getElementById('latestBody');
  if (!box || !latest) return;
  const rows = (latest.players || []).map(p =>
    '<div class="count-row"><div class="name">' + p.name + '</div><div class="sub">' +
    p.right + ' right · ' + p.adjacent + ' close · ' + p.wrong + ' wrong · ' + p.pending + ' pending</div></div>'
  ).join('');
  const ticks = (latest.ticks || []).map(t => (t.reporting_pct || '—') + '% · ' + Sweep.formatWhen(t.at)).join('<br>');
  box.innerHTML = '<p class="muted">' + (latest.reporting_pct || '—') + '% counted · ' + Sweep.formatWhen(latest.at) + '</p>' + rows +
    '<h3 class="count-ticks">Count over time</h3><div class="muted count-ticks">' + (ticks || 'No saves yet.') + '</div>';
}
function renderEstimate(est) {
  const box = document.getElementById('estimateBody');
  if (!box || !est) return;
  if (!est.ready) { box.textContent = est.note || 'Waiting for a count.'; return; }
  const lines = Object.keys(est.projected_shares || {}).map(party =>
    party + ' ' + est.projected_shares[party] + '% · ' + ((est.seats || {})[party] || 0) + ' seats'
  ).join('<br>');
  const flips = (est.flips || []).map(f => f.name + ': ' + f.calls.join(', ')).join('<br>') || 'No calls would change.';
  const overhang = Object.keys(est.overhang || {}).length ? JSON.stringify(est.overhang) : 'none';
  box.innerHTML = '<p>' + est.note + '</p><div>' + lines + '</div><p class="muted">Overhang: ' + overhang + '</p><h3>Calls that would flip</h3><div>' + flips + '</div>';
}
function startWeekendRotate() {
  if (rotateTimer) return;
  const order = ['board', 'latest', 'estimate', 'ranks'];
  let i = 0;
  rotateTimer = setInterval(() => {
    i = (i + 1) % order.length;
    showWeekendView(order[i]);
    if (order[i] === 'ranks') Night.loadChart('pot', 'rankChart');
  }, 18000);
}
document.querySelectorAll('#weekTabs button').forEach(btn => {
  btn.onclick = () => {
    showWeekendView(btn.dataset.view);
    const view = btn.dataset.view === 'ranks' ? 'history' : btn.dataset.view;
    const url = new URL(location.href);
    if (!view || view === 'board') url.searchParams.delete('view');
    else url.searchParams.set('view', view);
    history.replaceState(null, '', url);
  };
});
function render() {
  if (!state) return;
  const bl = Sweep.boardLabel(state.settings.board_mode);
  const pill = document.getElementById('modePill');
  pill.textContent = bl.text; pill.className = 'pill ' + bl.cls;
  if (state.demo) {
    document.getElementById('demoPill').classList.remove('hidden');
    document.getElementById('tv').classList.add('demo-frame');
  }
  document.getElementById('title').textContent = state.settings.event_title || 'Sweepstakes';
  document.getElementById('p1').textContent = '$' + (state.settings.prize_first || 100);
  document.getElementById('p2').textContent = '$' + (state.settings.prize_second || 20);
  document.getElementById('reporting').textContent =
    state.settings.reporting_pct ? (state.settings.reporting_pct + '% of voting places') : '—';
  const times = Object.values(state.results).map(r => r.updated_at).filter(Boolean).sort();
  document.getElementById('updated').textContent = times.length ? Sweep.formatWhen(times[times.length-1]) : '—';
  document.getElementById('countdown').textContent = state.locked
    ? 'ENTRIES LOCKED' : Sweep.fmtCountdown(state.settings.lock_at, state.server_now);
  const tvMode = document.documentElement.classList.contains('tv-mode');
  const asked = new URLSearchParams(location.search).get('view');
  const view = tvMode ? 'board' : (asked === 'history' ? 'ranks' : (asked || 'board'));
  showWeekendView(view);
  if (Sweep.isDemo) {
    document.getElementById('recapLink').href = '/demo/recap';
    document.getElementById('coalitionLink').href = '/demo/coalition';
    document.getElementById('boardLink').href = '/demo/live';
  }
  renderLatest(state.latest_count);
  renderEstimate(state.projection);
  const mover = state.biggest_mover;
  document.getElementById('moverLine').textContent = mover
    ? ('Biggest mover: ' + Night.unescapeHtml(mover.name) + ' ' + mover.from_place + ' → ' + mover.to_place)
    : 'Biggest mover appears after two saved counts.';
  if (view === 'ranks') Night.loadChart('pot', 'rankChart');
  if (!tvMode && state.settings.weekend_mode === '1') startWeekendRotate();
  const avgCard = document.getElementById('avgCard');
  const cmp = state.compare_averages || {};
  const showAvg = (cmp.pot && cmp.pot.average != null) || (cmp.pub && cmp.pub.average != null);
  avgCard.hidden = !showAvg;
  if (showAvg) Night.renderAverages(avgCard, state.compare_averages);
  const sideCard = document.getElementById('sideCard');
  sideCard.hidden = !(state.side_bets || []).length;
  if (!sideCard.hidden) Night.renderSideTable(sideCard, state.side_bets, state.side_bet_scores, { potOnlyTable: true });
  Night.renderTicker(state.settings.ticker_enabled === '0' ? [] : state.ticker);
  if (history) Night.loadChart('pot');

  const list = document.getElementById('leaders');
  const doFlip = flipAnimate();
  const source = state.combined_leaderboard || state.leaderboard;
  let board = source.slice().sort((a,b) => (a.place||a.rank||99) - (b.place||b.rank||99) || b.total - a.total);
  const q = boardQuery.trim().toLowerCase();
  if (q) board = board.filter((row) => String(row.name || '').toLowerCase().indexOf(q) !== -1);
  const note = document.getElementById('liveListNote');
  if (note) note.textContent = q ? (board.length + (board.length === 1 ? ' match' : ' matches')) : ('All ' + source.length);
  if (searchEl) searchEl.hidden = source.length <= 10 && !q;

  // NEW LEADER takeover
  if (state.locked && board.length) {
    const leader = board.find(r => (r.place||r.rank) === 1) || board[0];
    if (prevLeaderId != null && leader.player_id !== prevLeaderId) {
      showLeaderTakeover(leader.name);
    }
    prevLeaderId = leader.player_id;
  }

  list.innerHTML = '';
  board.forEach((row, i) => {
    const el = document.createElement('div');
    const place = row.place || i+1;
    el.className = 'leader-row' + (place === 1 ? ' first' : '') + (state.locked && row.still_in_it === false ? ' out' : '');
    el.dataset.pid = row.player_id;
    const prev = prevRanks[row.player_id];
    let delta = '';
    if (prev != null && state.locked) {
      const d = prev - place;
      if (d > 0) delta = `<div class="delta">▲ ${d}</div>`;
      else if (d < 0) delta = `<div class="delta down">▼ ${Math.abs(d)}</div>`;
      else delta = `<div class="delta" style="color:var(--muted)">—</div>`;
    }
    const outTag = (state.locked && row.still_in_it === false) ? '<span class="tag-out">OUT</span>' : '';
    const potTag = row.pool === 'pot' ? '<span class="tag-pot">POT</span>' : '';
    const spoon = (state.locked && row.is_last) ? '<span class="spoon" title="Wooden spoon">On morning dishes 🍽️</span>' : '';
    const maxBits = state.locked ? ` · max ${row.max_possible_now == null ? '—' : row.max_possible_now}` : '';
    const prize = row.prize ? ` · $${row.prize}` : '';
    let bw = '';
    if (state.locked && (row.best_call || row.worst_call)) {
      const b = row.best_call ? `Best: ${row.best_call.label} (+${row.best_call.points})` : '';
      const w = row.worst_call ? `Worst: ${row.worst_call.label} (−${row.worst_call.lost})` : '';
      bw = `<div class="bw">${[b,w].filter(Boolean).join(' · ')}</div>`;
    }
    el.innerHTML = `
      <div class="pos">${place}</div>
      <div><div class="name">${row.name}${potTag}${outTag}${spoon}</div>
        <div class="sub">${row.submitted ? 'Ballot in' : 'No ballot yet'}${maxBits}${prize}</div>${bw}</div>
      <div style="text-align:right">
        <div class="pts">${state.locked ? row.total : '—'}</div>
        ${delta}
      </div>`;
    list.appendChild(el);
    prevRanks[row.player_id] = place;
  });
  doFlip();
  if (window.House && state.house) {
    House.mount(document.getElementById('houseHero'), state.house, { tv: true });
    House.mountBefore(document.getElementById('beforeCount'), state.advance, state.house.has_results);
    House.mountNews(document.getElementById('newsStrip'), state.headlines);
  }

  const bars = document.getElementById('partyBars');
  const liveParties = partyVoteRows();
  const anyVote = liveParties.length > 0;
  document.getElementById('partyCard').hidden = !anyVote;
  if (!anyVote) {
    bars.innerHTML = '';
  } else bars.innerHTML = liveParties.map(p => {
    const v = parseFloat(p.percent);
    const known = !Number.isNaN(v);
    const w = known ? Math.min(100, v * 2) : 0;
    const seats = p.seats == null ? '' : (' · ' + p.seats);
    const star = p.manual ? '*' : '';
    const minor = p.minor ? ' minor' : '';
    return `<div class="party-bar${minor}">
      <div class="party-name" title="${escapeHtml(p.name)}">${escapeHtml(p.name)}</div>
      <div class="bar-track${known ? '' : ' pending'}"><div class="bar-fill" style="width:${w}%;background:${p.color}"></div></div>
      <div class="party-fig">${known ? v.toFixed(1)+'%'+seats : 'pending'}${star}</div>
    </div>`;
  }).join('');

  const most = resultForKey('most_seats');
  const parties = resultForKey('parties_in');
  const bloc = resultForKey('bloc_61');
  const minor = resultForKey('minor_highest');
  const seatsBox = document.getElementById('seatsBox');
  const derivedCard = document.getElementById('derivedCard');
  const pendingNote = document.getElementById('pendingNote');
  if (!most && !parties && !bloc && !minor) {
    derivedCard.hidden = true;
    if (pendingNote) pendingNote.hidden = true;
  } else {
    derivedCard.hidden = false;
    if (pendingNote) pendingNote.hidden = false;
    seatsBox.innerHTML = `
    <div>Most seats: <strong>${most?most.value:'PENDING'}</strong></div>
    <div>Parties in Parliament: <strong>${parties?parties.value:'PENDING'}</strong></div>
    <div>NAT+ACT+NZF 61+: <strong>${bloc?bloc.value:'PENDING'}</strong></div>
    <div>Highest minor: <strong>${minor?minor.value:'PENDING'}</strong></div>
    <div class="muted" style="margin-top:0.4em;font-size:0.82em">* = manual · seats include overhang · last place: On morning dishes 🍽️</div>`;
  }
}

async function load() {
  state = await Sweep.api('/api/state');
  render();
}
load();
Sweep.subscribe((msg) => Night.onEvent(msg, load));
if (document.documentElement.classList.contains('tv-mode')) {
  setInterval(function () { load(); }, 20000);
  if (navigator.wakeLock && navigator.wakeLock.request) {
    navigator.wakeLock.request('screen').catch(function () {});
  }
  var drift = 0;
  setInterval(function () {
    drift = (drift + 1) % 8;
    var px = drift < 4 ? drift : 8 - drift;
    var board = document.getElementById('tv');
    if (board) board.style.transform = 'translate(' + px + 'px,' + px + 'px)';
  }, 60000);
}
setInterval(() => {
  if (!state) return;
  document.getElementById('countdown').textContent = state.locked
    ? 'ENTRIES LOCKED' : Sweep.fmtCountdown(state.settings.lock_at);
}, 1000);
