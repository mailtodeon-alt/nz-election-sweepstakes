/* Seats hemicycle, advance-voting panel, and a short news strip. */
window.House = (function () {
  const COLORS = {
    National: '#00529F',
    Labour: '#D82A20',
    Green: '#098137',
    ACT: '#F5C518',
    'NZ First': '#6b7280',
    'Te Pāti Māori': '#B2001D',
    TOP: '#32b5e8',
  };
  function num(value) {
    const n = Number(String(value == null ? '' : value).replace(/,/g, ''));
    if (!isFinite(n)) return '—';
    return Math.round(n).toLocaleString('en-NZ');
  }
  function asOf(iso) {
    if (!iso) return '';
    if (window.Sweep && Sweep.formatWhen) return Sweep.formatWhen(iso, 'date');
    return iso;
  }
  function ago(iso) {
    if (!iso) return '';
    const t = new Date(iso).getTime();
    if (!isFinite(t)) return '';
    const mins = Math.max(0, Math.round((Date.now() - t) / 60000));
    if (mins < 1) return 'just now';
    if (mins < 60) return mins + ' min ago';
    const hours = Math.round(mins / 60);
    if (hours < 36) return hours + ' hr ago';
    return Math.round(hours / 24) + ' days ago';
  }
  function layout(count) {
    const rows = count > 90 ? 6 : 4;
    const weights = [];
    for (let r = 1; r <= rows; r++) weights.push(r + 1);
    const sum = weights.reduce((a, b) => a + b, 0);
    let left = count;
    const per = weights.map((w, i) => {
      if (i === rows - 1) return left;
      const n = Math.min(left, Math.max(1, Math.round(count * w / sum)));
      left -= n;
      return n;
    });
    const pts = [];
    per.forEach((n, r) => {
      const radius = 26 + r * (60 / rows);
      for (let i = 0; i < n; i++) {
        const t = n === 1 ? 0.5 : i / (n - 1);
        const angle = Math.PI * (1 - t);
        pts.push({ x: 100 + radius * Math.cos(angle), y: 98 - radius * Math.sin(angle), angle: angle });
      }
    });
    pts.sort((a, b) => b.angle - a.angle || a.y - b.y);
    return pts;
  }
  function sequence(view) {
    // Green, Te Pāti Māori, Labour, then TOP in the middle (left of the
    // majority line when that group is short), then NZ First, National, ACT.
    const order = view.order || ['Green', 'Te Pāti Māori', 'Labour', 'TOP', 'NZ First', 'National', 'ACT'];
    const seats = view.seats || {};
    const dots = [];
    order.forEach((party) => {
      const n = seats[party] || 0;
      for (let i = 0; i < n; i++) dots.push(party);
    });
    Object.keys(seats).forEach((party) => {
      if (order.includes(party)) return;
      for (let i = 0; i < (seats[party] || 0); i++) dots.push(party);
    });
    return dots;
  }
  function draw(host, view) {
    const dots = sequence(view);
    const pts = layout(Math.max(dots.length, 1));
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 200 112');
    svg.setAttribute('class', 'house-svg');
    svg.setAttribute('role', 'img');
    svg.setAttribute('aria-label', view.label + ', ' + view.house + ' seats, ' + view.majority + ' to govern');
    if (view.majority && pts.length >= view.majority) {
      const mark = pts[Math.min(view.majority, pts.length) - 1];
      const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
      line.setAttribute('x1', '100');
      line.setAttribute('y1', '98');
      line.setAttribute('x2', mark.x.toFixed(2));
      line.setAttribute('y2', mark.y.toFixed(2));
      line.setAttribute('stroke', '#ffd166');
      line.setAttribute('stroke-width', '0.45');
      line.setAttribute('stroke-dasharray', '1.2 0.9');
      svg.appendChild(line);
    }
    const r = dots.length > 100 ? 2.15 : 2.6;
    pts.forEach((pt, i) => {
      const party = dots[i];
      const c = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
      c.setAttribute('cx', pt.x.toFixed(2));
      c.setAttribute('cy', pt.y.toFixed(2));
      c.setAttribute('r', r);
      c.setAttribute('fill', party ? (COLORS[party] || '#8fa3c8') : '#2a3548');
      if (party === 'NZ First') {
        c.setAttribute('fill', '#6b7280');
        c.setAttribute('stroke', '#ffffff');
        c.setAttribute('stroke-width', '1');
        c.setAttribute('vector-effect', 'non-scaling-stroke');
      } else if (party === 'TOP') {
        c.setAttribute('fill', '#32b5e8');
        c.setAttribute('fill-opacity', '0.16');
        c.setAttribute('stroke', '#7fd4f5');
        c.setAttribute('stroke-width', '1.2');
        c.setAttribute('vector-effect', 'non-scaling-stroke');
      }
      svg.appendChild(c);
    });
    host.appendChild(svg);
  }
  function legend(host, view) {
    const box = document.createElement('div');
    box.className = 'house-legend';
    const order = view.order || Object.keys(view.seats || {});
    order.forEach((party) => {
      const n = (view.seats || {})[party] || 0;
      if (!n) return;
      const span = document.createElement('span');
      const swatch = document.createElement('i');
      swatch.style.background = COLORS[party] || '#8fa3c8';
      if (party === 'NZ First') {
        swatch.style.background = '#6b7280';
        swatch.style.boxShadow = '0 0 0 1px #ffffff';
      }
      var label = party === 'TOP' ? 'Opportunity (TOP)' : party;
      var text = label + ' ' + n;
      if (party === 'TOP') {
        swatch.className = 'hollow';
        swatch.style.background = 'transparent';
        swatch.style.boxShadow = 'inset 0 0 0 2px #32b5e8';
        text += ' · uncommitted, leans left';
      }
      span.append(swatch, document.createTextNode(text));
      box.appendChild(span);
    });
    host.appendChild(box);
  }
  function swingBloc(view) {
    if (view.swing_left && view.swing_left.names) return view.swing_left;
    const names = ['Labour', 'Green', 'Te Pāti Māori', 'TOP'];
    const table = view.seats || {};
    const seats = names.reduce((sum, name) => sum + (table[name] || 0), 0);
    const majority = Number(view.majority) || 0;
    const short = Math.max(0, majority - seats);
    return { names: names, seats: seats, short: short, majority: short === 0 };
  }
  function blocs(host, view) {
    const wrap = document.createElement('div');
    wrap.className = 'house-blocs';
    [view.right, view.left].forEach((bloc) => {
      if (!bloc) return;
      const card = document.createElement('div');
      card.className = 'bloc ' + (bloc.majority ? 'yes' : 'no');
      const title = document.createElement('strong');
      title.textContent = (bloc.names || []).join(' + ');
      const line = document.createElement('div');
      line.textContent = bloc.seats + ' seats · ' + (bloc.majority ? 'has a majority' : bloc.short + ' seats short');
      card.append(title, line);
      wrap.appendChild(card);
    });
    const topSeats = (view.seats || {}).TOP || 0;
    if (topSeats > 0) {
      const swing = swingBloc(view);
      const card = document.createElement('div');
      card.className = 'bloc swing ' + (swing.majority ? 'yes' : 'no');
      card.style.gridColumn = '1 / -1';
      card.style.borderStyle = 'dashed';
      const title = document.createElement('strong');
      title.textContent = (swing.names || []).join(' + ');
      const note = document.createElement('div');
      note.className = 'bloc-note';
      note.textContent = 'if TOP swings left';
      const line = document.createElement('div');
      line.textContent = swing.seats + ' seats · ' + (swing.majority ? 'has a majority' : swing.short + ' short');
      card.append(title, note, line);
      wrap.appendChild(card);
    }
    host.appendChild(wrap);
  }
  function mount(el, house, opts) {
    if (!el || !house) return;
    const compact = opts && opts.compact;
    let mode = 'current';
    if (house.has_results && house.live) mode = 'live';
    function paint() {
      const view = mode === 'poll' && house.poll ? house.poll : (mode === 'live' && house.live ? house.live : house.current);
      el.replaceChildren();
      const card = document.createElement('div');
      card.className = 'house-card';
      const h = document.createElement(compact ? 'h3' : 'h2');
      h.className = 'house-label';
      h.textContent = view.label;
      const meta = document.createElement('p');
      meta.className = 'house-meta';
      meta.textContent = view.house + ' seats · ' + view.majority + ' to govern';
      card.append(h, meta);
      draw(card, view);
      legend(card, view);
      if (!compact) blocs(card, view);
      if (view.note && !compact && !(opts && opts.tv)) {
        const note = document.createElement('p');
        note.className = 'muted';
        note.textContent = view.note;
        card.appendChild(note);
      }
      if (house.poll && !compact) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'secondary';
        btn.style.marginTop = '8px';
        btn.textContent = mode === 'poll' ? 'Show the count' : 'Projected from latest poll';
        btn.onclick = () => { mode = mode === 'poll' ? (house.live ? 'live' : 'current') : 'poll'; paint(); };
        card.appendChild(btn);
      }
      el.appendChild(card);
    }
    paint();
  }
  function mountBefore(el, advance, hasResults) {
    if (!el) return;
    el.replaceChildren();
    if (hasResults || !advance) return;
    const card = document.createElement('section');
    card.className = 'card before-count';
    const h = document.createElement('h2');
    h.textContent = 'Before the count';
    const p = document.createElement('p');
    p.className = 'muted';
    p.textContent = 'Advance votes cast in New Zealand, not including overseas votes.';
    const bars = document.createElement('div');
    bars.className = 'advance-bars';
    const rows = [
      ['2026', advance.votes_2026],
      ['2023', advance.votes_2023_same],
      ['2020', advance.votes_2020_same],
    ];
    const max = Math.max(...rows.map((row) => Number(row[1]) || 0), 1);
    rows.forEach((row) => {
      const line = document.createElement('div');
      line.className = 'advance-row';
      const label = document.createElement('div');
      label.textContent = row[0];
      const track = document.createElement('div');
      track.className = 'advance-track';
      const fill = document.createElement('span');
      fill.style.width = Math.max(4, (Number(row[1]) || 0) / max * 100) + '%';
      track.appendChild(fill);
      const val = document.createElement('div');
      val.textContent = num(row[1]);
      line.append(label, track, val);
      bars.appendChild(line);
    });
    const as = document.createElement('p');
    as.innerHTML = '';
    as.append(document.createTextNode('2026 total as of ' + asOf(advance.as_of) + '. Same point in 2023 and 2020. '));
    as.append(document.createTextNode('2023 finished advance voting on ' + num(advance.votes_2023_total) + '. '));
    const link = document.createElement('a');
    link.href = advance.source || 'https://elections.nz';
    link.target = '_blank';
    link.rel = 'noopener';
    link.textContent = 'Electoral Commission';
    as.appendChild(link);
    const list = document.createElement('ol');
    list.className = 'timeline';
    [
      '7:00pm — polls close and entries lock',
      'About 7:30pm — first advance-vote results',
      'Through the night — election-day votes',
      'About two weeks later — special votes in the official count',
    ].forEach((text) => {
      const li = document.createElement('li');
      li.textContent = text;
      list.appendChild(li);
    });
    const when = document.createElement('h3');
    when.textContent = 'What happens tonight';
    card.append(h, p, bars, as, when, list);
    el.appendChild(card);
  }
  function mountNews(el, items) {
    if (!el) return;
    el.replaceChildren();
    (items || []).forEach((item) => {
      const a = document.createElement('a');
      a.href = item.url;
      a.target = '_blank';
      a.rel = 'noopener';
      const title = document.createElement('strong');
      title.textContent = item.title || '';
      const small = document.createElement('small');
      small.textContent = [item.source, ago(item.published)].filter(Boolean).join(' · ');
      a.append(title, small);
      el.appendChild(a);
    });
  }
  return { mount: mount, mountBefore: mountBefore, mountNews: mountNews, ago: ago };
})();
