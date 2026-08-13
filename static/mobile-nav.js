/* MLB Analytics Hub — shared bottom tab bar.
   Injects a 5-tab nav + "More" overlay on small viewports. No-ops on desktop. */
(function () {
  'use strict';

  // Early-out on wide viewports; re-evaluate on resize via media query listener.
  var mq = window.matchMedia('(max-width: 768px)');

  var TABS = [
    { tab: 'home',   href: '/',            icon: '\u{1F3E0}', label: 'Home' },
    { tab: 'props',  href: '/props',       icon: '\u{1F4CA}', label: 'Props' },
    { tab: 'tracker',href: '/tracker',     icon: '\u{1F4CB}', label: 'Tracker' },
    { tab: 'cheat',  href: '/cheatsheets', icon: '\u{1F525}', label: 'Cheats' }
  ];

  var MORE_LINKS = [
    { href: '/cheatsheets',          label: 'Cheatsheets',       sub: 'Daily validated targets' },
    { href: '/edge-lab',             label: 'Edge Lab',          sub: '100% Club · edges · parlays' },
    { href: '__DEEPDIVE__',          label: 'Deep Dive',         sub: "Today's first game" },
    { href: '/batter-vs-pitcher',    label: 'BvP',               sub: 'Batter vs pitcher' },
    { href: '/value-bets',           label: 'Value Bets',        sub: 'CLV & edges' },
    { href: '/nrfi',                 label: 'NRFI / YRFI',       sub: 'First-inning props' },
    { href: '/consistency',          label: 'Consistency',       sub: 'Hit-rate windows' },
    { href: '/pitcher-deep-dive',    label: 'Pitcher Deep Dive', sub: 'Arsenal + zones' },
    { href: '/gameside-deepdive/0',  label: 'Gameside',          sub: 'F5 / bullpen / lineup' },
    { href: '/breakout-detector',    label: 'Breakouts',         sub: 'Statcast signals' },
    { href: '/hr-analytics',         label: 'HR Analytics',      sub: 'Power simulator' },
    { href: '/tools',                label: 'Tools',             sub: 'Misc utilities' }
  ];

  function pickActiveTab(path) {
    if (path === '/' || path === '/index' || path === '') return 'home';
    if (path.indexOf('/workspace') === 0) return 'hub';
    if (path.indexOf('/props') === 0) return 'props';
    if (path.indexOf('/tracker') === 0) return 'tracker';
    return null;
  }

  function el(tag, attrs, children) {
    var n = document.createElement(tag);
    if (attrs) for (var k in attrs) {
      if (k === 'class') n.className = attrs[k];
      else if (k === 'html') n.innerHTML = attrs[k];
      else if (attrs[k] === true) n.setAttribute(k, '');
      else if (attrs[k] !== false && attrs[k] != null) n.setAttribute(k, attrs[k]);
    }
    if (children) children.forEach(function (c) { if (c) n.appendChild(c); });
    return n;
  }

  function buildTabBar(activeTab) {
    var bar = el('nav', { class: 'mobile-tabbar', 'aria-label': 'Primary' });
    TABS.forEach(function (t) {
      var a = el('a', { href: t.href, 'data-tab': t.tab, class: t.tab === activeTab ? 'active' : '' }, [
        el('span', { class: 'mt-icon', 'aria-hidden': 'true', html: t.icon }),
        el('span', { class: 'mt-label', html: t.label })
      ]);
      bar.appendChild(a);
    });
    var moreBtn = el('button', { type: 'button', 'data-tab': 'more', 'aria-haspopup': 'dialog', 'aria-controls': 'mobileMoreSheet' }, [
      el('span', { class: 'mt-icon', 'aria-hidden': 'true', html: '☰' }),
      el('span', { class: 'mt-label', html: 'More' })
    ]);
    bar.appendChild(moreBtn);
    return bar;
  }

  function buildMoreSheet() {
    var sheet = el('div', { class: 'mobile-more', id: 'mobileMoreSheet', role: 'dialog', 'aria-modal': 'true', 'aria-label': 'More pages', hidden: true });
    var closeBtn = el('button', { type: 'button', class: 'mobile-more__close', 'aria-label': 'Close menu', html: 'Close ✕' });
    var grid = el('div', { class: 'mobile-more__grid' });
    MORE_LINKS.forEach(function (l) {
      var a = el('a', { href: l.href === '__DEEPDIVE__' ? '#' : l.href, 'data-href': l.href }, [
        el('span', { html: l.label }),
        el('span', { class: 'mm-sub', html: l.sub })
      ]);
      grid.appendChild(a);
    });
    sheet.appendChild(closeBtn);
    sheet.appendChild(el('h3', { html: 'All Pages' }));
    sheet.appendChild(grid);
    return sheet;
  }

  function wireMoreSheet(bar, sheet) {
    var moreBtn = bar.querySelector('[data-tab="more"]');
    var open = function () { sheet.hidden = false; moreBtn.classList.add('active'); document.body.style.overflow = 'hidden'; };
    var close = function () { sheet.hidden = true; moreBtn.classList.remove('active'); document.body.style.overflow = ''; };
    moreBtn.addEventListener('click', function (e) { e.preventDefault(); sheet.hidden ? open() : close(); });
    sheet.querySelector('.mobile-more__close').addEventListener('click', close);
    sheet.addEventListener('click', function (e) { if (e.target === sheet) close(); });
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && !sheet.hidden) close(); });

    // Resolve "Deep Dive" to today's first game.
    var deepLink = sheet.querySelector('a[data-href="__DEEPDIVE__"]');
    if (deepLink) {
      deepLink.addEventListener('click', function (e) {
        if (deepLink.getAttribute('href') !== '#') return; // already resolved
        e.preventDefault();
        fetch('/api/games/today', { credentials: 'same-origin' })
          .then(function (r) { return r.ok ? r.json() : null; })
          .then(function (data) {
            var games = (data && (data.games || data)) || [];
            var pk = games[0] && (games[0].gamePk || games[0].game_pk || games[0].id);
            if (pk) { window.location.href = '/deep-dive/' + pk; }
            else { window.alert('No games on the slate yet today.'); }
          })
          .catch(function () { window.alert('Could not load today\'s games.'); });
      });
    }
  }

  function mount() {
    if (document.querySelector('.mobile-tabbar')) return; // idempotent
    var activeTab = pickActiveTab(window.location.pathname || '/');
    var bar = buildTabBar(activeTab);
    var sheet = buildMoreSheet();
    document.body.appendChild(bar);
    document.body.appendChild(sheet);
    wireMoreSheet(bar, sheet);
  }

  function unmount() {
    var bar = document.querySelector('.mobile-tabbar');
    var sheet = document.querySelector('.mobile-more');
    if (bar) bar.remove();
    if (sheet) sheet.remove();
    document.body.style.overflow = '';
  }

  function syncToViewport() {
    if (mq.matches) mount(); else unmount();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', syncToViewport);
  } else {
    syncToViewport();
  }
  // Respond to rotation / window resize
  if (mq.addEventListener) mq.addEventListener('change', syncToViewport);
  else if (mq.addListener) mq.addListener(syncToViewport);
})();
