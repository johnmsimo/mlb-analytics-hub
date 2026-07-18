/* NFL Analytics Hub — shared shell: nav, week state, bet slip, helpers. */
(function () {
  const $ = (q, el) => (el || document).querySelector(q);
  const esc = x => String(x ?? '').replace(/[<>&"]/g, c => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;' }[c]));
  const am = p => p == null ? '—' : (p > 0 ? '+' : '') + p;
  const pc = (x, d = 1) => x == null ? '—' : (x * 100).toFixed(d) + '%';

  async function fetchJSON(url, opts) {
    const r = await fetch(url, opts);
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.status);
    return r.json();
  }

  /* ---------------- week state (persists across pages) ---------------- */
  const WK = 'nfl.week';
  function getWeek() { try { return JSON.parse(localStorage.getItem(WK)) || null; } catch { return null; } }
  function setWeek(w) { localStorage.setItem(WK, JSON.stringify(w)); renderWeekChip(w); }
  async function resolveWeek() {
    let w = getWeek();
    if (w && w.season) return w;
    const cur = await fetchJSON('/api/games/current');
    w = { season: cur.season, week: cur.week, type: cur.season_type || 'REG' };
    setWeek(w);
    return w;
  }
  function renderWeekChip(w) {
    const el = $('#weekchip');
    if (el && w) el.innerHTML = `<b>${w.season}</b> · ${w.type === 'POST' ? 'PO' : 'WK'} <b>${w.week}</b>`;
  }

  /* ------------------------------ slip ------------------------------- */
  const SLIP = 'nfl.slip';
  function slip() { try { return JSON.parse(localStorage.getItem(SLIP)) || []; } catch { return []; } }
  function saveSlip(rows) { localStorage.setItem(SLIP, JSON.stringify(rows)); renderBadges(); renderSlip(); }
  function slipKey(r) { return [r.gameId, r.playerId || r.player, r.marketKey, r.line, r.side].join('|'); }

  let _settings = null;
  async function settings() {
    if (!_settings) _settings = await fetchJSON('/api/tracker/settings');
    return _settings;
  }

  async function addToSlip(row) {
    const rows = slip();
    const key = slipKey(row);
    if (rows.some(r => slipKey(r) === key)) { toast('Already in slip'); openSlip(); return; }
    const s = await settings();
    const stake = row.kellyPct ? Math.max(1, Math.round(s.bankroll * row.kellyPct))
      : Math.max(1, Math.round(s.bankroll * (s.unit_pct || 0.01)));
    rows.push({ ...row, stakeDollars: stake });
    saveSlip(rows);
    toast('Added to slip');
  }
  function removeFromSlip(i) { const rows = slip(); rows.splice(i, 1); saveSlip(rows); }

  function renderBadges() {
    const n = slip().length;
    for (const el of document.querySelectorAll('.slip-count')) {
      el.textContent = n;
      el.style.display = n ? '' : 'none';
    }
  }

  function sideLabel(r) {
    if (r.marketKey === 'anytime_td') return r.side === 'over' ? 'YES' : 'NO';
    if (r.marketKey === 'h2h') return r.side.toUpperCase();
    return `${r.side === 'over' ? 'O' : 'U'} ${r.line}`;
  }

  function renderSlip() {
    const body = $('#slip-body');
    if (!body) return;
    const rows = slip();
    if (!rows.length) {
      body.innerHTML = '<div class="slip-empty">Slip is empty.<br><span class="muted">Add picks from the Props board or a game page.</span></div>';
      $('#slip-total').textContent = '$0';
      $('#slip-confirm').disabled = true;
      return;
    }
    body.innerHTML = rows.map((r, i) => `
      <div class="slip-item">
        <div class="r1">
          <span class="tag ${r.side === 'over' ? 'over' : 'under'}">${sideLabel(r)}</span>
          <span class="nm">${esc(r.player)}</span>
          <button class="rm" data-i="${i}" aria-label="remove">×</button>
        </div>
        <div class="r2">
          <span>${esc(r.marketLabel || r.marketKey)}</span>
          <span>${r.price != null ? am(r.price) : 'no price'}${r.book ? ' · ' + esc(r.book) : ''}</span>
          <span class="stake">$<input type="number" min="0" step="1" value="${r.stakeDollars || 0}" data-i="${i}"></span>
        </div>
      </div>`).join('');
    $('#slip-total').textContent = '$' + rows.reduce((a, r) => a + (+r.stakeDollars || 0), 0);
    $('#slip-confirm').disabled = false;
    body.querySelectorAll('.rm').forEach(b => b.onclick = () => removeFromSlip(+b.dataset.i));
    body.querySelectorAll('input').forEach(inp => inp.onchange = () => {
      const rows2 = slip(); rows2[+inp.dataset.i].stakeDollars = +inp.value || 0; saveSlip(rows2);
    });
  }

  async function confirmSlip() {
    const rows = slip();
    if (!rows.length) return;
    $('#slip-confirm').disabled = true;
    $('#slip-confirm').textContent = 'Saving…';
    let ok = 0;
    for (const r of rows) {
      try {
        await fetchJSON('/api/tracker/pick', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ ...r, source: 'bet_slip' }),
        });
        ok++;
      } catch (e) { console.error('slip save', e); }
    }
    saveSlip([]);
    $('#slip-confirm').textContent = 'Confirm picks';
    closeSlip();
    toast(`${ok} pick${ok === 1 ? '' : 's'} saved to tracker`);
    if (location.pathname === '/tracker' && window.trackerRefresh) window.trackerRefresh();
  }

  function openSlip() { $('#slip').classList.add('open'); $('#slip-backdrop').classList.add('open'); renderSlip(); }
  function closeSlip() { $('#slip').classList.remove('open'); $('#slip-backdrop').classList.remove('open'); }

  /* ------------------------------ toast ------------------------------ */
  let toastT = null;
  function toast(msg) {
    let t = $('#toast');
    if (!t) { t = document.createElement('div'); t.id = 'toast'; t.className = 'toast'; document.body.appendChild(t); }
    t.textContent = msg;
    t.classList.add('on');
    clearTimeout(toastT);
    toastT = setTimeout(() => t.classList.remove('on'), 1900);
  }

  /* ------------------------------ shell ------------------------------ */
  const ICONS = {
    slate: '<svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="17" rx="2"/><path d="M3 9h18M8 4V2m8 2V2"/></svg>',
    props: '<svg viewBox="0 0 24 24"><path d="M4 20V10m6 10V4m6 16v-7"/><path d="M2 20h20"/></svg>',
    slip: '<svg viewBox="0 0 24 24"><path d="M6 3h12v18l-3-2-3 2-3-2-3 2z"/><path d="M9 8h6M9 12h6"/></svg>',
    tracker: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 3"/></svg>',
  };
  function shell(active) {
    const nav = [
      ['slate', 'Slate', '/'],
      ['props', 'Props', '/props'],
      ['tracker', 'Tracker', '/tracker'],
    ];
    const top = document.createElement('header');
    top.className = 'topbar';
    top.innerHTML = `
      <a class="brand" href="/"><span class="lg">NFL</span>HUB <span class="sub">Analytics</span></a>
      <nav class="topnav">${nav.map(([k, l, h]) => `<a href="${h}" class="${k === active ? 'on' : ''}">${l}</a>`).join('')}</nav>
      <div class="spacer"></div>
      <span class="weekchip" id="weekchip"></span>
      <button class="slipbtn" id="open-slip">SLIP <span class="n slip-count">0</span></button>`;
    document.body.prepend(top);

    const bnav = document.createElement('nav');
    bnav.className = 'bnav';
    bnav.innerHTML = [
      ...nav.slice(0, 2).map(([k, l, h]) => `<a href="${h}" class="${k === active ? 'on' : ''}">${ICONS[k]}${l}</a>`),
      `<a href="#" id="bnav-slip">${ICONS.slip}Slip<span class="n slip-count">0</span></a>`,
      `<a href="/tracker" class="${active === 'tracker' ? 'on' : ''}">${ICONS.tracker}Tracker</a>`,
    ].join('');
    document.body.appendChild(bnav);

    const drawer = document.createElement('div');
    drawer.innerHTML = `
      <div class="slip-backdrop" id="slip-backdrop"></div>
      <aside class="slip" id="slip" aria-label="Bet slip">
        <div class="head"><h3>Bet Slip</h3><button class="x" aria-label="close">×</button></div>
        <div class="body" id="slip-body"></div>
        <div class="foot">
          <div style="display:flex;justify-content:space-between" class="muted">Total stake <b id="slip-total" style="color:var(--t)">$0</b></div>
          <button class="btn primary" id="slip-confirm">Confirm picks</button>
        </div>
      </aside>`;
    document.body.appendChild(drawer);

    $('#open-slip').onclick = openSlip;
    $('#bnav-slip').onclick = e => { e.preventDefault(); openSlip(); };
    $('#slip .x').onclick = closeSlip;
    $('#slip-backdrop').onclick = closeSlip;
    $('#slip-confirm').onclick = confirmSlip;
    renderBadges();
    renderWeekChip(getWeek());
  }

  /* --------------------------- week controls -------------------------- */
  async function weekControls(container, onLoad) {
    const w = await resolveWeek();
    container.innerHTML = `
      <select id="wc-season"></select>
      <div class="seg" id="wc-type">
        <button data-v="REG" class="${w.type === 'REG' ? 'on' : ''}">REG</button>
        <button data-v="POST" class="${w.type === 'POST' ? 'on' : ''}">PO</button>
      </div>
      <select id="wc-week"></select>`;
    const seasons = [w.season - 1, w.season, w.season + 1].filter(v => v >= 2022);
    $('#wc-season', container).innerHTML = seasons.map(v => `<option ${v === w.season ? 'selected' : ''}>${v}</option>`).join('');
    const fillWeeks = (type, sel) => {
      const ws = type === 'POST' ? [1, 2, 3, 4, 5] : Array.from({ length: 18 }, (_, i) => i + 1);
      $('#wc-week', container).innerHTML = ws.map(v => `<option ${v == sel ? 'selected' : ''}>${v}</option>`).join('');
    };
    fillWeeks(w.type, w.week);
    const current = () => ({
      season: +$('#wc-season', container).value,
      week: +$('#wc-week', container).value,
      type: $('#wc-type button.on', container).dataset.v,
    });
    const fire = () => { const c = current(); setWeek(c); onLoad(c); };
    $('#wc-season', container).onchange = fire;
    $('#wc-week', container).onchange = fire;
    container.querySelectorAll('#wc-type button').forEach(b => b.onclick = () => {
      container.querySelectorAll('#wc-type button').forEach(x => x.classList.remove('on'));
      b.classList.add('on');
      fillWeeks(b.dataset.v, 1);
      fire();
    });
    onLoad(w);
    return { current };
  }

  /* ------------------------------ polling ----------------------------- */
  function poll(fn, ms) {
    let t = null;
    const tick = async () => { if (!document.hidden) { try { await fn(); } catch (e) { console.error(e); } } t = setTimeout(tick, ms); };
    t = setTimeout(tick, ms);
    return () => clearTimeout(t);
  }

  window.NFLHub = {
    $, esc, am, pc, fetchJSON, toast,
    shell, weekControls, resolveWeek, getWeek,
    addToSlip, openSlip, slip, poll, settings,
  };
})();
