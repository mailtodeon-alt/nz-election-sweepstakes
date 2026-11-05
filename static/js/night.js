window.Night = (function () {
  const EMOJIS = ["🎉", "😱", "🔥", "😂", "👏", "😬"];

  function unescapeHtml(s) {
    const t = document.createElement('textarea');
    t.innerHTML = s || '';
    return t.value;
  }

  function floatBursts(bursts) {
    const layer = document.getElementById('reactLayer');
    if (!layer || !bursts) return;
    bursts.forEach(b => {
      const shown = Math.min(b.n || 1, 6);
      for (let i = 0; i < shown; i++) {
        const el = document.createElement('div');
        el.className = 'floater';
        el.textContent = b.emoji + (i === 0 && b.n > 1 ? ' ×' + b.n : '');
        el.style.left = (8 + Math.random() * 80) + '%';
        el.style.animationDelay = (i * 0.12) + 's';
        layer.appendChild(el);
        setTimeout(() => el.remove(), 3000);
      }
    });
  }

  function onEvent(msg, reload) {
    if (!msg || msg.event === 'heartbeat') return;
    if (msg.event === 'reactions') {
      const data = msg.data || {};
      if (!!data.demo !== !!Sweep.isDemo) return;
      floatBursts(data.bursts || []);
      return;
    }
    if (reload) reload();
  }

  async function sendReaction(emoji, token) {
    await Sweep.api('/api/reactions', {
      method: 'POST',
      body: JSON.stringify({ emoji, token: token || '' })
    });
  }

  function emojiBar(container, token) {
    container.innerHTML = '';
    EMOJIS.forEach(emoji => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.textContent = emoji;
      btn.onclick = async () => {
        btn.disabled = true;
        try { await sendReaction(emoji, token); }
        catch (e) { Sweep.toast(e.data?.message || e.message); }
        setTimeout(() => { btn.disabled = false; }, 1000);
      };
      container.appendChild(btn);
    });
  }

  function renderTicker(messages) {
    const bar = document.getElementById('ticker');
    const tv = document.getElementById('tv');
    if (!bar) return;
    if (!messages || !messages.length) {
      bar.classList.add('hidden');
      if (tv) tv.classList.remove('has-ticker');
      return;
    }
    bar.classList.remove('hidden');
    if (tv) tv.classList.add('has-ticker');
    const track = document.createElement('div');
    track.className = 'ticker-track';
    messages.forEach(m => {
      const item = document.createElement('span');
      item.className = 'ticker-item';
      const who = document.createElement('strong');
      who.textContent = unescapeHtml(m.player_name || '');
      item.appendChild(who);
      item.appendChild(document.createTextNode('  ' + unescapeHtml(m.body || '')));
      track.appendChild(item);
    });
    bar.innerHTML = '';
    bar.appendChild(track);
  }

  function renderAverages(box, compare) {
    if (!box) return;
    const pot = (compare && compare.pot) || {};
    const pub = (compare && compare.public) || {};
    const fmt = (side) => side.average == null ? '—' : String(side.average);
    box.innerHTML =
      '<h3 style="margin-top:0">Group vs public</h3>' +
      '<div class="avg-pair">' +
      '<div class="prize"><div class="muted">Pot avg</div><div class="amt">' + fmt(pot) + '</div><div class="muted">' + (pot.submitted || 0) + ' submitted</div></div>' +
      '<div class="prize"><div class="muted">Public avg</div><div class="amt">' + fmt(pub) + '</div><div class="muted">' + (pub.submitted || 0) + ' submitted</div></div>' +
      '</div>' +
      '<p class="muted" style="margin:8px 0 0;font-size:12px">Main ballot only. Side bets are separate.</p>';
  }

  function renderSideTable(box, bets, scores, { potOnlyTable }) {
    if (!box) return;
    const rows = (scores || []).filter(r => !potOnlyTable || true);
    const visible = potOnlyTable ? rows : rows.filter(r => r.pool === 'public' && r.points);
    let html = '<h3 style="margin-top:0">Side bets</h3>';
    if (!(bets || []).length) {
      html += '<p class="muted">None yet.</p>';
      box.innerHTML = html;
      return;
    }
    html += (bets || []).map(b => {
      const ans = b.answer ? ' · answer ' + b.answer : '';
      return '<div class="muted">' + b.prompt + ' · ' + b.points + ' pts · ' + b.status + ans + '</div>';
    }).join('');
    if (potOnlyTable) {
      html += '<div class="side-mini">';
      html += visible.filter(r => r.points).map(r =>
        '<div>' + r.name + ' <span class="muted">(' + r.pool + ')</span> <strong>+' + r.points + '</strong></div>'
      ).join('') || '<div class="muted">No side-bet points yet.</div>';
      html += '</div>';
    }
    box.innerHTML = html;
  }

  function drawChartCanvas(canvas, series) {
    const dpr = Math.min(window.devicePixelRatio || 1, 3);
    const cssW = Math.max(320, canvas.clientWidth || canvas.parentElement.clientWidth || 900);
    const cssH = Math.max(220, Math.min(window.innerHeight * 0.5, 560));
    canvas.width = Math.round(cssW * dpr);
    canvas.height = Math.round(cssH * dpr);
    canvas.style.width = cssW + 'px';
    canvas.style.height = cssH + 'px';
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = '#0c1426';
    ctx.fillRect(0, 0, cssW, cssH);
    const list = (series || []).slice().sort((a, b) => {
      const at = (a.points[a.points.length - 1] || {}).total || 0;
      const bt = (b.points[b.points.length - 1] || {}).total || 0;
      return bt - at;
    }).slice(0, 10);
    const pts = list.flatMap(s => s.points);
    const maxY = Math.max(10, ...pts.map(p => p.total || 0));
    const pad = 40;
    const colors = ['#5dd6ff', '#ffd166', '#3ddc97', '#ff5d7a', '#c9a0ff', '#f5c518', '#9ec1ff', '#ff9f68'];
    list.forEach((s, si) => {
      ctx.strokeStyle = colors[si % colors.length];
      ctx.lineWidth = 2.5;
      ctx.beginPath();
      s.points.forEach((p, i) => {
        const x = pad + (s.points.length < 2 ? 0 : i / (s.points.length - 1)) * (cssW - pad * 2);
        const y = cssH - pad - ((p.total || 0) / maxY) * (cssH - pad * 2);
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      });
      ctx.stroke();
    });
  }
  function drawChart(svg, series) {
    if (svg && svg.tagName === 'CANVAS') return drawChartCanvas(svg, series);
    const width = 920, height = 420, pad = 36;
    const list = (series || []).slice().sort((a, b) => {
      const at = (a.points[a.points.length - 1] || {}).total || 0;
      const bt = (b.points[b.points.length - 1] || {}).total || 0;
      return bt - at;
    }).slice(0, 10);
    const pts = list.flatMap(s => s.points);
    const maxY = Math.max(10, ...pts.map(p => p.total || 0));
    const xs = pts.map(p => parseFloat(p.reporting_pct));
    const usePct = xs.length && xs.every(n => !Number.isNaN(n) && n > 0);
    const maxX = usePct ? Math.max(100, ...xs) : Math.max(1, ...list.map(s => s.points.length - 1));
    const colors = ['#5dd6ff', '#ffd166', '#3ddc97', '#ff5d7a', '#c9a0ff', '#f5c518', '#9ec1ff', '#ff9f68', '#7ad7ff', '#e0e0e0'];
    const xOf = (point, index) => {
      const x = usePct ? (parseFloat(point.reporting_pct) || 0) : index;
      return pad + (x / maxX) * (width - pad * 2);
    };
    const yOf = (total) => height - pad - (total / maxY) * (height - pad * 2);
    let body = '<rect width="100%" height="100%" fill="#0c1426"/>';
    for (let g = 0; g <= 4; g++) {
      const y = yOf(maxY * g / 4);
      body += '<line x1="' + pad + '" y1="' + y + '" x2="' + (width - pad) + '" y2="' + y + '" stroke="#243860"/>';
      body += '<text x="4" y="' + (y + 4) + '" fill="#8fa3c8" font-size="11">' + Math.round(maxY * g / 4) + '</text>';
    }
    list.forEach((s, si) => {
      const color = colors[si % colors.length];
      const d = s.points.map((p, i) => (i ? 'L' : 'M') + xOf(p, i) + ' ' + yOf(p.total || 0)).join(' ');
      body += '<path d="' + d + '" fill="none" stroke="' + color + '" stroke-width="2.5"/>';
      const last = s.points[s.points.length - 1];
      if (last) {
        body += '<text x="' + (width + 4) + '" y="' + yOf(last.total || 0) + '" fill="' + color + '" font-size="12">' + (s.name || '') + '</text>';
      }
    });
    svg.setAttribute('viewBox', '0 0 ' + (width + 80) + ' ' + height);
    svg.innerHTML = body;
  }

  async function loadChart(pool, elementId) {
    const svg = document.getElementById(elementId || 'historyChart');
    if (!svg) return;
    const data = await Sweep.api('/api/history?pool=' + encodeURIComponent(pool));
    drawChart(svg, data.series || []);
  }

  return { onEvent, emojiBar, renderTicker, renderAverages, renderSideTable, loadChart, unescapeHtml, EMOJIS };
})();
