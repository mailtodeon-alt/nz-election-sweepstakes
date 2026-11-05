/* National results and electorate leaders. */
(function () {
  const root = document.getElementById('resultsApp');
  if (!root || !window.Sweep) return;
  const pinnedKey = Sweep.isDemo ? 'sweep_electorate_demo' : 'sweep_electorate';
  function pinned() {
    try { return localStorage.getItem(pinnedKey) || ''; } catch (e) { return ''; }
  }
  function setPinned(name) {
    try {
      if (name) localStorage.setItem(pinnedKey, name);
      else localStorage.removeItem(pinnedKey);
    } catch (e) {}
  }
  let data = null;
  function render() {
    if (!data) return;
    House.mount(document.getElementById('resultsHouse'), data.house);
    House.mountBefore(document.getElementById('resultsBefore'), data.advance, data.has_results);
    const note = document.getElementById('resultsNote');
    if (!data.has_results) {
      note.textContent = 'Party vote and electorate leaders appear here from 7pm Saturday, as the official feed updates. Until then this is the current Parliament.';
    } else {
      const pct = data.reporting_pct ? data.reporting_pct + '% of voting places counted' : 'Count underway';
      note.textContent = pct + (data.updated_at ? ' · updated ' + data.updated_at : '');
    }
    const body = document.querySelector('#partyTable tbody');
    body.replaceChildren();
    (data.parties || []).forEach((row) => {
      const tr = document.createElement('tr');
      function td(text, cls) {
        const cell = document.createElement('td');
        if (cls) cell.className = cls;
        cell.textContent = text;
        return cell;
      }
      const change = row.change_2023;
      let changeText = '—';
      let cls = '';
      if (change != null && data.has_results) {
        changeText = (change > 0 ? '+' : '') + Number(change).toFixed(2);
        cls = change > 0 ? 'up' : (change < 0 ? 'down' : '');
      }
      tr.append(
        td(row.party, 'party'),
        td(row.votes == null || !data.has_results ? '—' : Number(row.votes).toLocaleString('en-NZ')),
        td(row.percent == null ? '—' : Number(row.percent).toFixed(2) + '%'),
        td(row.pct_2023 == null ? '—' : Number(row.pct_2023).toFixed(2) + '%'),
        td(changeText, cls),
        td(row.seats == null ? '—' : String(row.seats)),
      );
      body.appendChild(tr);
    });
    const q = (document.getElementById('electorateSearch').value || '').trim().toLowerCase();
    const region = document.getElementById('electorateRegion').value;
    const mine = pinned();
    const rows = (data.electorates || []).filter((row) => {
      if (region && row.region !== region) return false;
      if (!q) return true;
      return (row.name || '').toLowerCase().includes(q) || (row.leader || '').toLowerCase().includes(q) || (row.party || '').toLowerCase().includes(q);
    }).slice().sort((a, b) => {
      if (a.name === mine) return -1;
      if (b.name === mine) return 1;
      return a.name.localeCompare(b.name);
    });
    const list = document.getElementById('electorateList');
    list.replaceChildren();
    if (!rows.length) {
      const empty = document.createElement('p');
      empty.className = 'muted';
      empty.textContent = data.has_results
        ? 'No electorate matches that search.'
        : 'Electorate leaders, including the Māori electorates, show up here once the official feed publishes them.';
      list.appendChild(empty);
      return;
    }
    rows.forEach((row) => {
      const line = document.createElement('div');
      line.className = 'electorate';
      const name = document.createElement('div');
      const strong = document.createElement('strong');
      strong.textContent = (row.name === mine ? '★ ' : '') + row.name;
      const sub = document.createElement('div');
      sub.className = 'muted';
      sub.textContent = row.region || '';
      name.append(strong, sub);
      const who = document.createElement('div');
      who.textContent = row.leader ? (row.leader + (row.party ? ' · ' + row.party : '')) : 'Waiting';
      const margin = document.createElement('div');
      margin.textContent = row.margin == null ? '—' : ('+' + row.margin);
      const counted = document.createElement('div');
      counted.textContent = row.counted == null ? '—' : row.counted + '%';
      const pin = document.createElement('button');
      pin.type = 'button';
      pin.className = 'secondary pin';
      pin.textContent = row.name === mine ? 'Unpin' : 'My electorate';
      pin.onclick = () => { setPinned(row.name === mine ? '' : row.name); render(); };
      line.append(name, who, margin, counted, pin);
      list.appendChild(line);
    });
  }
  function fillRegions() {
    const select = document.getElementById('electorateRegion');
    const regions = [...new Set((data.electorates || []).map((row) => row.region).filter(Boolean))].sort();
    regions.forEach((region) => {
      const opt = document.createElement('option');
      opt.value = region;
      opt.textContent = region;
      select.appendChild(opt);
    });
  }
  document.getElementById('electorateSearch').addEventListener('input', render);
  document.getElementById('electorateRegion').addEventListener('change', render);
  function load() {
    Sweep.api('/api/results').then((payload) => {
      const first = !data;
      data = payload;
      if (first) fillRegions();
      render();
    }).catch(() => {
      document.getElementById('resultsNote').textContent = 'Results are not available right now.';
    });
  }
  load();
  Sweep.subscribe((msg) => { if (msg.event !== 'heartbeat') load(); });
})();
