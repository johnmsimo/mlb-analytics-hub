/* NFL Analytics Hub — shared shell: nav, week state, bet slip, helpers. */
(function () {
  const $ = (q, el) => (el || document).querySelector(q);
  const esc = x => String(x ?? '').replace(/[<>&"]/g, c => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;' }[c]));
  const am = p => p == null ? '—' : (p > 0 ? '+' : '') + p;
  const pc = (x, d = 1) => x == null ? '—' : (x * 100).toFixed(d) + '%';

  let _auth = null;
  async function authSession() {
    if (!_auth) _auth = await fetch('/api/auth/session').then(r => { if (!r.ok) throw new Error('authentication required'); return r.json(); });
    return _auth;
  }
  async function fetchJSON(url, opts = {}) {
    opts = { ...opts, headers: { ...(opts.headers || {}) } };
    const method = (opts.method || 'GET').toUpperCase();
    if (['POST','PUT','PATCH','DELETE'].includes(method) && !url.endsWith('/api/auth/login')) {
      const a = await authSession();
      opts.headers['X-CSRF-Token'] = a.csrf_token;
    }
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
    const failed = [];
    for (const r of rows) {
      try {
        await fetchJSON('/api/tracker/pick', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ ...r, source: 'bet_slip' }),
        });
        ok++;
      } catch (e) { console.error('slip save', e); failed.push(r); }
    }
    // Only successfully saved picks leave the slip — a failed save must
    // never silently discard the user's picks.
    saveSlip(failed);
    $('#slip-confirm').textContent = 'Confirm picks';
    $('#slip-confirm').disabled = false;
    if (!failed.length) closeSlip();
    toast(failed.length
      ? `${ok} saved · ${failed.length} failed — kept in slip`
      : `${ok} pick${ok === 1 ? '' : 's'} saved to tracker`);
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
    dashboard:'<svg viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>',
    games:'<svg viewBox="0 0 24 24"><rect x="3" y="5" width="18" height="16" rx="2"/><path d="M8 3v4m8-4v4M3 10h18"/></svg>',
    players:'<svg viewBox="0 0 24 24"><circle cx="12" cy="8" r="4"/><path d="M4 21c1-5 4-7 8-7s7 2 8 7"/></svg>',
    teams:'<svg viewBox="0 0 24 24"><circle cx="8" cy="8" r="3"/><circle cx="17" cy="9" r="3"/><path d="M2 21c1-5 3-7 6-7s5 2 6 7m0 0c.5-4 2-6 5-6 2 0 3 2 4 6"/></svg>',
    projections:'<svg viewBox="0 0 24 24"><path d="M3 18l5-5 4 3 8-10"/><path d="M15 6h5v5"/></svg>',
    analytics:'<svg viewBox="0 0 24 24"><path d="M4 20V10m5 10V4m5 16v-7m5 7V7"/></svg>',
    rankings:'<svg viewBox="0 0 24 24"><path d="M8 4h8v5a4 4 0 01-8 0z"/><path d="M12 13v5m-4 2h8M8 6H4v2c0 3 2 5 5 5m7-7h4v2c0 3-2 5-5 5"/></svg>',
    admin:'<svg viewBox="0 0 24 24"><path d="M4 5h16v14H4z"/><path d="M8 9h8M8 13h5"/></svg>',
    settings:'<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M19 13.5l2-1.5-2-3-2.4.7a7 7 0 00-1.4-.8L14.5 6h-5l-.7 2.9a7 7 0 00-1.4.8L5 9l-2 3 2 1.5a7 7 0 000 1L3 16l2 3 2.4-.7a7 7 0 001.4.8l.7 2.9h5l.7-2.9a7 7 0 001.4-.8L19 19l2-3-2-1.5a7 7 0 000-1z"/></svg>',
    props:'<svg viewBox="0 0 24 24"><path d="M4 20V10m6 10V4m6 16v-7"/><path d="M2 20h20"/></svg>',
    tracker:'<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 3"/></svg>',
    live:'<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="2.5"/><path d="M7.5 7.5a6.5 6.5 0 000 9m9-9a6.5 6.5 0 010 9M4.6 4.6a10.5 10.5 0 000 14.8m14.8-14.8a10.5 10.5 0 010 14.8"/></svg>',
    slip:'<svg viewBox="0 0 24 24"><path d="M6 3h12v18l-3-2-3 2-3-2-3 2z"/><path d="M9 8h6M9 12h6"/></svg>',
    menu:'<svg viewBox="0 0 24 24"><path d="M4 6h16M4 12h16M4 18h16"/></svg>',
  };
  function shell(active) {
    document.body.classList.add('ai-shell');
    const nav = [
      ['dashboard','Dashboard','/'],['games','Games','/games'],['players','Players','/players'],
      ['teams','Teams','/teams'],['projections','Projections','/projections'],
      ['live','Live Center','/live'],['analytics','Analytics','/analytics'],['rankings','Rankings','/rankings'],
      ['props','Props','/props'],['tracker','Tracker','/tracker'],['settings','Settings','/settings'],['admin','Data Ops','/admin/data']
    ];
    const sidebar=document.createElement('aside'); sidebar.className='ai-sidebar';
    sidebar.innerHTML=`<a class="ai-brand" href="/"><span class="ai-mark">N</span><span>NFL ANALYTICS<small>AI Intelligence Hub</small></span></a>
      <nav class="ai-nav">${nav.map(([k,l,h])=>`<a href="${h}" class="${k===active?'on':''}">${ICONS[k]}${l}</a>`).join('')}</nav>
      <div class="ai-engine"><strong><span class="pulse"></span>AI ENGINE ONLINE</strong><span id="engine-version">Intelligence v2.0</span></div>`;
    document.body.prepend(sidebar);
    const top=document.createElement('header'); top.className='ai-topbar';
    top.innerHTML=`<input class="ai-search" id="global-search" placeholder="Search players, teams, games, stats…" aria-label="Global search">
      <div class="ai-top-actions"><span class="weekchip" id="weekchip"></span><button class="ai-iconbtn" id="open-slip" title="Bet slip">⌁<span class="n slip-count">0</span></button><button class="ai-iconbtn">♢</button><div class="ai-profile"><span class="ai-avatar" id="profile-avatar">JS</span><span><b id="profile-name">Account</b><button class="logout-btn" id="logout-btn">Sign out</button></span></div></div>`;
    document.body.prepend(top);
    const drawer=document.createElement('div'); drawer.innerHTML=`<div class="slip-backdrop" id="slip-backdrop"></div><aside class="slip" id="slip" aria-label="Bet slip"><div class="head"><h3>Bet Slip</h3><button class="x" aria-label="close">×</button></div><div class="body" id="slip-body"></div><div class="foot"><div style="display:flex;justify-content:space-between" class="muted">Total stake <b id="slip-total" style="color:var(--t)">$0</b></div><button class="btn primary" id="slip-confirm">Confirm picks</button></div></aside>`;
    document.body.appendChild(drawer);

    /* Mobile (≤820px): the sidebar is hidden, so ship a bottom nav with the
       core destinations + a Menu tab that opens the sidebar as a drawer. */
    const navBackdrop=document.createElement('div'); navBackdrop.className='nav-backdrop'; navBackdrop.id='nav-backdrop';
    document.body.appendChild(navBackdrop);
    const bnav=document.createElement('nav'); bnav.className='bnav';
    bnav.innerHTML=[
      `<a href="/" class="${active==='dashboard'?'on':''}">${ICONS.dashboard}Home</a>`,
      `<a href="/props" class="${active==='props'?'on':''}">${ICONS.props}Props</a>`,
      `<a href="#" id="bnav-slip">${ICONS.slip}Slip<span class="n slip-count">0</span></a>`,
      `<a href="/tracker" class="${active==='tracker'?'on':''}">${ICONS.tracker}Tracker</a>`,
      `<a href="#" id="bnav-menu">${ICONS.menu}Menu</a>`,
    ].join('');
    document.body.appendChild(bnav);
    const closeMenu=()=>{sidebar.classList.remove('open');navBackdrop.classList.remove('open')};
    $('#bnav-menu').onclick=e=>{e.preventDefault();sidebar.classList.toggle('open');navBackdrop.classList.toggle('open')};
    $('#bnav-slip').onclick=e=>{e.preventDefault();closeMenu();openSlip();};
    navBackdrop.onclick=closeMenu;

    $('#open-slip').onclick=openSlip; $('#slip .x').onclick=closeSlip; $('#slip-backdrop').onclick=closeSlip; $('#slip-confirm').onclick=confirmSlip;
    renderBadges(); renderWeekChip(getWeek());
    authSession().then(a => { const name=a.user?.name||a.user?.username||'Account'; $('#profile-name').textContent=name; $('#profile-avatar').textContent=name.split(/\s+/).map(x=>x[0]).join('').slice(0,2).toUpperCase(); }).catch(()=>{});
    $('#logout-btn').onclick=async()=>{try{await fetchJSON('/api/auth/logout',{method:'POST'});}finally{location.href='/login';}};
    const search=$('#global-search'); search.onkeydown=e=>{if(e.key==='Enter'&&search.value.trim()) location.href='/players?q='+encodeURIComponent(search.value.trim())};
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
    addToSlip, openSlip, slip, poll, settings, authSession,
  };
})();
