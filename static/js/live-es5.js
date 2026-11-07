/* Baseline for old TV browsers. No let/const, arrows, async, ??, or ?..
   Paints the rows already in the HTML, and keeps polling if the modern scripts cannot run. */
(function () {
  function canModern() {
    try {
      new Function('let a=1;const b=async function(){return a;};var c=window?.z??1;');
      return true;
    } catch (e) {
      return false;
    }
  }
  function bootModern() {
    if (!canModern()) return;
    document.write(
      '<script src="/static/js/common.js"><\/script>' +
      '<script src="/static/js/house.js"><\/script>' +
      '<script src="/static/js/night.js"><\/script>' +
      '<script src="/static/js/live-app.js"><\/script>'
    );
  }
  function xhrJson(url, done) {
    var xhr = new XMLHttpRequest();
    xhr.open('GET', url, true);
    xhr.onreadystatechange = function () {
      if (xhr.readyState !== 4) return;
      if (xhr.status < 200 || xhr.status >= 300) return;
      try { done(JSON.parse(xhr.responseText)); } catch (e) { done(null); }
    };
    xhr.send();
  }
  function unescapeBasic(s) {
    return String(s == null ? '' : s)
      .replace(/&amp;/g, '&')
      .replace(/&lt;/g, '<')
      .replace(/&gt;/g, '>')
      .replace(/&quot;/g, '"');
  }
  function esc(s) {
    return unescapeBasic(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }
  function byPlace(a, b) {
    return (a.place || a.rank || 99) - (b.place || b.rank || 99);
  }
  function rowHtml(row, i, locked) {
    var place = row.place || row.rank || (i + 1);
    var sub = row.submitted ? 'Ballot in' : 'No ballot yet';
    var pts = locked ? row.total : '\u2014';
    var first = place === 1 ? ' first' : '';
    var pot = row.pool === 'pot' ? '<span class="tag-pot">POT</span>' : '';
    return '<div class="leader-row' + first + '">' +
      '<div class="pos">' + place + '</div>' +
      '<div><div class="name">' + esc(row.name) + pot + '</div>' +
      '<div class="sub">' + sub + '</div></div>' +
      '<div style="text-align:right"><div class="pts">' + esc(pts) + '</div></div></div>';
  }
  function paint(list, rows, locked, limit) {
    if (!list || !rows) return;
    var copy = rows.slice(0);
    copy.sort(byPlace);
    var max = copy.length;
    if (limit && max > limit) max = limit;
    var html = '';
    var i;
    for (i = 0; i < max; i++) html += rowHtml(copy[i], i, locked);
    list.innerHTML = html;
  }
  function countdown(lockAt, locked) {
    if (locked) return 'ENTRIES LOCKED';
    if (!lockAt) return '';
    var end = Date.parse(lockAt);
    if (!end) return '';
    var ms = end - (new Date()).getTime();
    if (ms <= 0) return 'ENTRIES LOCKED';
    var s = Math.floor(ms / 1000);
    var d = Math.floor(s / 86400);
    var h = Math.floor((s % 86400) / 3600);
    var m = Math.floor((s % 3600) / 60);
    if (d > 0) return d + 'd ' + h + 'h ' + m + 'm';
    return h + ':' + (m < 10 ? '0' : '') + m;
  }
  function paintParties(rows) {
    var box = document.getElementById('partyBars');
    var card = document.getElementById('partyCard');
    if (!box) return;
    var ballot = { National: 1, Labour: 1, Green: 1, ACT: 1, 'NZ First': 1, 'Te P\u0101ti M\u0101ori': 1 };
    var fromFeed = false;
    var i;
    for (i = 0; i < rows.length; i++) {
      if (rows[i].seats != null) fromFeed = true;
      if (rows[i].percent != null && rows[i].key && !ballot[rows[i].key]) fromFeed = true;
    }
    var shown = [];
    if (fromFeed) {
      for (i = 0; i < rows.length; i++) {
        var pct = parseFloat(rows[i].percent);
        if (rows[i].percent == null || isNaN(pct) || pct <= 0) continue;
        shown.push(rows[i]);
      }
    }
    if (!shown.length) {
      if (card) card.style.display = 'none';
      box.innerHTML = '';
      return;
    }
    if (card) card.style.display = '';
    var html = '';
    for (i = 0; i < shown.length; i++) {
      var row = shown[i];
      var v = parseFloat(row.percent);
      var w = Math.min(100, v * 2);
      var cls = row.minor ? 'party-bar minor' : 'party-bar';
      var seats = row.seats == null ? '' : (' \u00b7 ' + row.seats);
      var name = row.party || row.key || '';
      html += '<div class="' + cls + '"><div class="party-name" title="' + esc(name) + '">' + esc(name) + '</div>' +
        '<div class="bar-track"><div class="bar-fill" style="width:' + w + '%;background:' + esc(row.color || '#8fa3c8') + '"></div></div>' +
        '<div class="party-fig">' + v.toFixed(1) + '%' + esc(seats) + '</div></div>';
    }
    box.innerHTML = html;
  }
  function holdBoard() {
    var tv = document.getElementById('tv');
    if (!tv || !tv.classList) return;
    tv.classList.remove('view-latest');
    tv.classList.remove('view-estimate');
    tv.classList.remove('view-ranks');
    tv.classList.add('view-board');
  }
  var lastLeadId = 0;
  var leadReady = false;
  function isTvBoard() {
    return (document.documentElement.className || '').indexOf('tv-board') !== -1;
  }
  function elecBits(row) {
    var margin = row.margin == null ? '\u2014' : ('+' + row.margin);
    var pct = row.counted == null ? '' : (row.counted + '% counted');
    var second = row.second ? ('Runner-up ' + esc(row.second) + (row.second_party ? ' \u00b7 ' + esc(row.second_party) : '')) : '';
    var cls = row.close ? 'elec-row close' : 'elec-row';
    return '<div class="' + cls + '"><div class="elec-margin">' + margin + '<div class="who">' + esc(pct) + '</div></div>' +
      '<div><strong>' + esc(row.name) + '</strong><div class="who">' + esc(row.leader || '') +
      (row.party ? ' \u00b7 ' + esc(row.party) : '') + (row.votes ? ' \u00b7 ' + row.votes : '') +
      '</div><div class="who">' + second + '</div></div></div>';
  }
  function showEs5Banner(ev) {
    var el = document.getElementById('leadBanner');
    if (!el || !ev) return;
    var text = (ev.electorate || '') + ': ' + (ev.new_leader || '') +
      (ev.new_party ? ' (' + ev.new_party + ')' : '') + ' leads ' + (ev.old_leader || '') +
      (ev.old_party ? ' (' + ev.old_party + ')' : '') +
      (ev.margin == null ? '' : ' by ' + ev.margin);
    el.innerHTML = esc(text);
    el.className = 'lead-banner show';
    clearTimeout(el._t);
    el._t = setTimeout(function () { el.className = 'lead-banner'; }, 6000);
  }
  function paintElectorates(data) {
    if (!data) return;
    var closeBox = document.getElementById('closeSeats');
    var board = document.getElementById('electorateBoard');
    var changesBox = document.getElementById('leadChanges');
    var tv = isTvBoard() || (document.documentElement.className || '').indexOf('tv-mode') !== -1;
    var close = data.close || [];
    var rows = data.electorates || [];
    var changes = data.lead_changes || [];
    var i;
    var n;
    var html = '<h3>Close seats</h3>';
    n = tv && close.length > 3 ? 3 : close.length;
    if (!n) html += '<p class="muted">No seat is inside 500 votes or 2%.</p>';
    for (i = 0; i < n; i++) html += elecBits(close[i]);
    if (closeBox) closeBox.innerHTML = html;
    html = '';
    n = tv ? 0 : rows.length;
    for (i = 0; i < n; i++) html += elecBits(rows[i]);
    if (board) board.innerHTML = html;
    if (changesBox && !tv) {
      if (!changes.length) changesBox.innerHTML = '<p class="muted">No lead changes yet.</p>';
      else {
        html = '';
        n = changes.length > 12 ? 12 : changes.length;
        for (i = 0; i < n; i++) {
          var ev = changes[i];
          html += '<div class="elec-change"><strong>' + esc(ev.electorate) + '</strong> ' +
            esc(ev.old_leader || '') + ' \u2192 ' + esc(ev.new_leader || '') +
            (ev.margin == null ? '' : ' \u00b7 +' + ev.margin) + '</div>';
        }
        changesBox.innerHTML = html;
      }
    }
    if (changes.length && changes[0].id != null) {
      if (leadReady && changes[0].id !== lastLeadId) showEs5Banner(changes[0]);
      lastLeadId = changes[0].id;
    }
    leadReady = true;
  }
  function tick() {
    if (window.__liveModern) return;
    holdBoard();
    var demo = location.pathname.indexOf('/demo') === 0 ? '?demo=1' : '';
    xhrJson('/api/electorates' + demo, paintElectorates);
    xhrJson('/api/state' + demo, function (pot) {
      if (!pot) return;
      var board = pot.combined_leaderboard || pot.leaderboard || [];
      paint(document.getElementById('leaders'), board, !!pot.locked, 0);
      var note = document.getElementById('liveListNote');
      if (note) note.innerHTML = 'All ' + board.length;
      var search = document.getElementById('livePlayerSearch');
      if (search) search.style.display = board.length > 10 ? '' : 'none';
      var cd = document.getElementById('countdown');
      if (cd && pot.settings) cd.innerHTML = countdown(pot.settings.lock_at, pot.locked);
      var reporting = document.getElementById('reporting');
      if (reporting && pot.settings) {
        reporting.innerHTML = pot.settings.reporting_pct ? (pot.settings.reporting_pct + '% of voting places') : '\u2014';
      }
      paintParties(pot.parties || []);
      var latest = pot.latest_count;
      var box = document.getElementById('latestBody');
      if (box && latest) {
        var players = latest.players || [];
        var html = '<p class="muted">' + esc(latest.reporting_pct || '\u2014') + '% counted</p>';
        var n;
        for (n = 0; n < players.length; n++) {
          var p = players[n];
          html += '<div class="count-row"><div class="name">' + esc(p.name) + '</div><div class="sub">' +
            (p.right || 0) + ' right \u00b7 ' + (p.adjacent || 0) + ' close \u00b7 ' +
            (p.wrong || 0) + ' wrong \u00b7 ' + (p.pending || 0) + ' pending</div></div>';
        }
        box.innerHTML = html;
      }
    });
  }
  bootModern();
  if (!window.__liveModern) holdBoard();
  tick();
  setInterval(tick, 15000);
})();
