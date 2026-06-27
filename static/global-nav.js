/* MLB Analytics Hub — desktop quick-nav strip.
   Injects a sticky page-switcher bar below the existing <nav> on viewports ≥769 px.
   Mobile is handled by mobile-nav.js (bottom tab bar). */
(function () {
  'use strict';

  var NAV_PAGES = [
    { href: '/',                     label: '🏠 Dashboard' },
    { href: '/props',                label: '📊 Props' },
    { href: '/tracker',              label: '📋 Tracker' },
    { href: '/cheatsheets',          label: '🔥 Cheatsheets' },
    { href: '/edge-lab',             label: '⚡ Edge Lab' },
    { href: '/value-bets',           label: '💰 Value Bets' },
    { href: '/nrfi',                 label: '⚾ NRFI' },
    { href: '/batter-vs-pitcher',    label: '⚔️ BvP' },
    { href: '/consistency',          label: '📈 Consistency' },
    { href: '/breakout-detector',    label: '🚀 Breakouts' },
    { href: '/hr-analytics',         label: '💥 HR Analytics' },
    { href: '/pitcher-deep-dive',    label: '🎯 Pitcher Dive' },
  ];

  function currentHref() {
    return window.location.pathname || '/';
  }

  function buildStrip() {
    var strip = document.createElement('div');
    strip.id = 'globalNavStrip';
    strip.setAttribute('role', 'navigation');
    strip.setAttribute('aria-label', 'Quick navigation');

    var path = currentHref();

    NAV_PAGES.forEach(function (pg) {
      var a = document.createElement('a');
      a.href = pg.href;
      a.textContent = pg.label;

      // Exact match or prefix match (e.g. /props/something still highlights Props)
      var active = (pg.href === '/')
        ? path === '/'
        : path === pg.href || path.indexOf(pg.href + '/') === 0;

      if (active) {
        a.className = 'gnav-active';
        a.setAttribute('aria-current', 'page');
      }
      strip.appendChild(a);
    });

    // Timestamp slot — updated by pages via window.setNavAsOf()
    var ts = document.createElement('span');
    ts.id = 'gnavAsOf';
    ts.setAttribute('aria-label', 'Data freshness');
    strip.appendChild(ts);

    return strip;
  }

  function injectStyles() {
    if (document.getElementById('globalNavStyles')) return;
    var s = document.createElement('style');
    s.id = 'globalNavStyles';
    s.textContent = [
      '@media(min-width:769px){',
        '#globalNavStrip{',
          'display:flex;align-items:center;gap:4px;',
          'padding:6px 24px;',
          'background:rgba(5,10,24,.97);',
          'border-bottom:1px solid rgba(0,229,255,.10);',
          'overflow-x:auto;',
          'scrollbar-width:none;',
          'position:sticky;top:57px;z-index:90;',
          'backdrop-filter:blur(12px);',
          '-webkit-backdrop-filter:blur(12px);',
        '}',
        '#globalNavStrip::-webkit-scrollbar{display:none}',
        '#globalNavStrip a{',
          'flex-shrink:0;',
          'padding:5px 11px;',
          'border-radius:999px;',
          'border:1px solid transparent;',
          'background:transparent;',
          'color:#6a8db0;',
          'font-family:"Inter",system-ui,sans-serif;',
          'font-size:.68rem;',
          'font-weight:500;',
          'letter-spacing:.3px;',
          'text-decoration:none;',
          'white-space:nowrap;',
          'transition:all .15s;',
        '}',
        '#globalNavStrip a:hover{',
          'color:#e0f0ff;',
          'border-color:rgba(0,229,255,.2);',
          'background:rgba(0,229,255,.06);',
        '}',
        '#globalNavStrip a.gnav-active{',
          'color:#00e5ff;',
          'border-color:rgba(0,229,255,.35);',
          'background:rgba(0,229,255,.10);',
        '}',
        '#gnavAsOf{',
          'margin-left:auto;',
          'flex-shrink:0;',
          'font-family:"JetBrains Mono","Courier New",monospace;',
          'font-size:.58rem;',
          'color:#3a5a7a;',
          'white-space:nowrap;',
          'padding-left:8px;',
        '}',
      '}',
      '@media(max-width:768px){#globalNavStrip{display:none}}',
    ].join('');
    document.head.appendChild(s);
  }

  function mount() {
    if (document.getElementById('globalNavStrip')) return;
    injectStyles();
    var strip = buildStrip();

    // Insert after the first <nav> on the page, or after <body> opening
    var existingNav = document.querySelector('nav');
    if (existingNav && existingNav.parentNode) {
      existingNav.parentNode.insertBefore(strip, existingNav.nextSibling);
    } else {
      document.body.insertBefore(strip, document.body.firstChild);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', mount);
  } else {
    mount();
  }

  // Global helper: call after data loads to stamp the nav strip.
  // Accepts an ISO string, a Date, a Unix ms number, or a human label.
  window.setNavAsOf = function (value) {
    var el = document.getElementById('gnavAsOf');
    if (!el) return;
    var label = '';
    if (value instanceof Date) {
      label = _fmtTime(value);
    } else if (typeof value === 'number') {
      label = _fmtTime(new Date(value));
    } else if (typeof value === 'string' && /^\d{4}-/.test(value)) {
      label = _fmtTime(new Date(value));
    } else if (typeof value === 'string') {
      label = value;
    }
    el.textContent = label ? 'as of ' + label : '';
  };

  function _fmtTime(d) {
    if (!d || isNaN(d.getTime())) return '';
    return d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', timeZoneName: 'short' });
  }
})();
