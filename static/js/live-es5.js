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
      '<script src="/static/js/public-board.js"><\/script>' +
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
    return '<div class="leader-row' + first + '">' +
      '<div class="pos">' + place + '</div>' +
      '<div><div class="name">' + esc(row.name) + '</div>' +
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
  function tick() {
    if (window.__liveModern) return;
    holdBoard();
    var demo = location.pathname.indexOf('/demo') === 0 ? '?demo=1' : '';
    xhrJson('/api/state' + demo, function (pot) {
      if (!pot) return;
      paint(document.getElementById('leaders'), pot.leaderboard || [], !!pot.locked, 0);
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
    xhrJson('/api/open/state' + demo, function (pub) {
      if (!pub) return;
      var rows = pub.leaderboard || [];
      paint(document.getElementById('publicLeaders'), rows, !!pub.locked, rows.length > 10 ? 10 : 0);
      var search = document.getElementById('livePlayerSearch');
      var toggle = document.getElementById('liveShowAll');
      if (search) search.style.display = rows.length > 10 ? '' : 'none';
      if (toggle) toggle.style.display = rows.length > 10 ? '' : 'none';
      var note = document.getElementById('liveListNote');
      if (note) note.innerHTML = rows.length > 10 ? ('Top 10 of ' + rows.length) : ('All ' + rows.length);
    });
  }
  bootModern();
  if (!window.__liveModern) holdBoard();
  tick();
  setInterval(tick, 15000);
})();
