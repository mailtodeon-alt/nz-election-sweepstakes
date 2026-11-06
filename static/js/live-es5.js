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
  function tick() {
    if (window.__liveModern) return;
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
  tick();
  setInterval(tick, 15000);
})();
