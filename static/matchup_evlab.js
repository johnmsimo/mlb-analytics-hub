/* ============================================================================
 * Matchup EV Lab — shared widget
 *
 * Renders two interactive panels automatically against the pitcher/batter being
 * faced:
 *   1) Exit Velocity log  — per-batted-ball table (DATE/PITCHER/ARM/TYPE/EV/
 *      ANGLE/DIST/BAT SPD/PITCH) + L-window & season AVG-EV / BARREL% tiles.
 *   2) Batted-Ball Profile — GB/FB/LD/PU%, HR/FB%, HardHit%, AvgEV, Barrel%,
 *      Blast%, PullBarrel%.
 *
 * Filters mirror the reference UIs: rolling game window, pitcher arm, pitch
 * type(s), day/night, and an "only vs <pitcher>" direct-BvP toggle.
 *
 * Backed by:
 *   GET /api/player/<batterId>/exit-velocity
 *   GET /api/player/<batterId>/batted-ball-profile
 *
 * Public API:
 *   MatchupEVLab.mount(container, ctx)   → render inline into an element
 *   MatchupEVLab.openModal(ctx)          → render inside a full-screen overlay
 *
 *   ctx = { batterId, batterName?, pitcherId?, pitcherName?, pitchHand? }
 * ==========================================================================*/
(function () {
  if (window.MatchupEVLab) return;

  var SEQ = 0;

  // ── one-time scoped styles ────────────────────────────────────────────────
  function injectStyles() {
    if (document.getElementById('mevlab-styles')) return;
    var css = `
.mevlab{font-family:inherit;color:#e8edf4;background:#0b0f17;border:1px solid rgba(255,255,255,.08);border-radius:12px;padding:14px 14px 16px;font-size:13px}
.mevlab *{box-sizing:border-box}
.mevlab-head{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:10px;flex-wrap:wrap}
.mevlab-title{font-weight:700;font-size:15px;letter-spacing:.2px}
.mevlab-title small{display:block;font-weight:400;font-size:11px;color:#8b97a8;margin-top:2px}
.mevlab-tabs{display:flex;gap:6px}
.mevlab-tab{cursor:pointer;padding:5px 12px;border-radius:999px;border:1px solid rgba(255,255,255,.12);background:transparent;color:#9aa6b6;font-size:12px;font-weight:600}
.mevlab-tab.on{background:#7c3aed22;border-color:#7c3aed;color:#c7b3ff}
.mevlab-filters{display:flex;gap:12px;flex-wrap:wrap;align-items:flex-end;margin-bottom:12px;padding-bottom:12px;border-bottom:1px solid rgba(255,255,255,.07)}
.mevlab-fld{display:flex;flex-direction:column;gap:4px}
.mevlab-fld>label{font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:#7e8a9b;font-weight:700}
.mevlab-fld select{background:#121826;color:#e8edf4;border:1px solid rgba(255,255,255,.14);border-radius:8px;padding:6px 8px;font-size:12px;min-width:90px}
.mevlab-chips{display:flex;gap:5px;flex-wrap:wrap;max-width:420px}
.mevlab-chip{cursor:pointer;padding:4px 9px;border-radius:999px;border:1px solid rgba(255,255,255,.14);background:#121826;color:#9aa6b6;font-size:11px;font-weight:600;white-space:nowrap}
.mevlab-chip.on{background:#10341f;border-color:#1f9d55;color:#7be0a6}
.mevlab-tgl{display:flex;align-items:center;gap:6px;cursor:pointer;font-size:12px;color:#cdd6e3;user-select:none}
.mevlab-tgl input{accent-color:#7c3aed}
.mevlab-tiles{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:12px}
.mevlab-tile{background:#0f1623;border:1px solid rgba(255,255,255,.08);border-radius:9px;padding:9px 8px;text-align:center}
.mevlab-tile .lab{font-size:9.5px;letter-spacing:.06em;text-transform:uppercase;color:#7e8a9b;font-weight:700;line-height:1.2}
.mevlab-tile .val{font-size:19px;font-weight:800;margin-top:4px}
.mevlab-tile .sub{font-size:9px;color:#6c7889;margin-top:2px}
.mevlab-tile.heat-hi{background:#10341f;border-color:#1f9d55}
.mevlab-tile.heat-md{background:#15301c}
.mevlab-tblwrap{max-height:360px;overflow:auto;border:1px solid rgba(255,255,255,.07);border-radius:9px}
.mevlab-tbl{width:100%;border-collapse:collapse;font-size:12px}
.mevlab-tbl th{position:sticky;top:0;background:#0f1623;color:#8b97a8;font-size:9.5px;letter-spacing:.05em;text-transform:uppercase;font-weight:700;padding:7px 8px;text-align:left;white-space:nowrap;z-index:1}
.mevlab-tbl td{padding:6px 8px;border-top:1px solid rgba(255,255,255,.05);white-space:nowrap}
.mevlab-tbl tr.barrel td{background:rgba(168,42,42,.18)}
.mevlab-tbl tr.hr td{background:rgba(31,157,85,.20)}
.mevlab-tbl td.num{font-variant-numeric:tabular-nums;font-weight:600}
.mevlab-ev{display:inline-block;min-width:46px;text-align:center;padding:2px 6px;border-radius:6px;font-weight:700}
.mevlab-flags{font-size:10px;color:#9aa6b6;margin:4px 2px 0}
.mevlab-flags b{color:#cdd6e3}
.mevlab-msg{padding:26px 10px;text-align:center;color:#7e8a9b;font-size:12px}
.mevlab-prof{display:grid;grid-template-columns:repeat(5,1fr);gap:8px}
.mevlab-prof .mevlab-tile .val{font-size:17px}
.mevlab-na{color:#5d6878}
/* modal */
.mevlab-ov{position:fixed;inset:0;z-index:4000;display:flex;align-items:flex-start;justify-content:center;padding:28px 14px;overflow:auto;background:rgba(2,6,16,.82);backdrop-filter:blur(4px)}
.mevlab-ov .mevlab{width:min(880px,100%);max-width:880px}
.mevlab-x{position:absolute;top:10px;right:14px;cursor:pointer;color:#9aa6b6;font-size:22px;line-height:1;background:none;border:none}
@media(max-width:640px){.mevlab-tiles{grid-template-columns:repeat(2,1fr)}.mevlab-prof{grid-template-columns:repeat(3,1fr)}}
`;
    var s = document.createElement('style');
    s.id = 'mevlab-styles';
    s.textContent = css;
    document.head.appendChild(s);
  }

  // ── helpers ───────────────────────────────────────────────────────────────
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }
  function pct(v) { return (v == null || isNaN(v)) ? '<span class="mevlab-na">—</span>' : Number(v).toFixed(1) + '%'; }
  function num1(v) { return (v == null || isNaN(v)) ? '<span class="mevlab-na">—</span>' : Number(v).toFixed(1); }

  // exit-velo → color (gray < 88, yellow-green 88-100, bright green 100+, red 108+)
  function evColor(ev) {
    if (ev == null || isNaN(ev)) return 'background:transparent;color:#8b97a8';
    if (ev >= 108) return 'background:#1f9d55;color:#04140a';
    if (ev >= 100) return 'background:#15321f;color:#7be0a6';
    if (ev >= 92) return 'background:#1b2a1d;color:#bfe6c9';
    if (ev >= 85) return 'background:#1a2230;color:#cdd6e3';
    return 'background:#161b27;color:#8b97a8';
  }

  function qs(o) {
    var parts = [];
    Object.keys(o).forEach(function (k) {
      if (o[k] !== '' && o[k] != null) parts.push(encodeURIComponent(k) + '=' + encodeURIComponent(o[k]));
    });
    return parts.length ? '?' + parts.join('&') : '';
  }

  // ── instance ──────────────────────────────────────────────────────────────
  function Lab(container, ctx) {
    this.el = container;
    this.ctx = ctx || {};
    this.id = 'mevlab_' + (++SEQ);
    this.token = 0;
    this.tab = 'ev';                          // 'ev' | 'profile'
    var hand = (this.ctx.pitchHand || '').toString().toUpperCase().charAt(0);
    this.filters = {
      window: 'l10',
      arm: (hand === 'R' || hand === 'L') ? hand : 'ALL',
      daynight: 'all',
      pitches: [],                            // [] = all
      vsPitcher: false
    };
    this.facetsRendered = false;
    this.render();
    this.refresh();
  }

  Lab.prototype.render = function () {
    var c = this.ctx;
    var hasPitcher = !!c.pitcherId;
    var bn = esc(c.batterName || 'Batter');
    var sub = hasPitcher ? ('vs ' + esc(c.pitcherName || 'pitcher')) : 'Exit velocity & batted-ball profile';
    this.el.innerHTML =
      '<div class="mevlab" id="' + this.id + '">' +
        '<div class="mevlab-head">' +
          '<div class="mevlab-title">' + bn + ' — Matchup EV Lab<small>' + sub + '</small></div>' +
          '<div class="mevlab-tabs">' +
            '<button class="mevlab-tab on" data-tab="ev">Exit Velocity</button>' +
            '<button class="mevlab-tab" data-tab="profile">Batted-Ball Profile</button>' +
          '</div>' +
        '</div>' +
        '<div class="mevlab-filters">' +
          fld('Window', sel('f-window', [['l5', 'L5'], ['l10', 'L10'], ['l25', 'L25'], ['season', 'Season'], ['2yr', '2yr']], this.filters.window)) +
          fld('Arm', sel('f-arm', [['ALL', 'All'], ['R', 'RHP'], ['L', 'LHP']], this.filters.arm)) +
          fld('Day/Night', sel('f-dn', [['all', 'All'], ['day', 'Day'], ['night', 'Night']], this.filters.daynight)) +
          '<div class="mevlab-fld" style="flex:1;min-width:160px"><label>Pitch Type</label><div class="mevlab-chips" data-role="chips"><span class="mevlab-na" style="font-size:11px">loading…</span></div></div>' +
          (hasPitcher ? '<div class="mevlab-fld"><label>Matchup</label><label class="mevlab-tgl"><input type="checkbox" data-role="vs"> Only vs ' + esc((c.pitcherName || 'pitcher').split(' ').slice(-1)[0]) + '</label></div>' : '') +
        '</div>' +
        '<div data-role="body"><div class="mevlab-msg">Loading Statcast…</div></div>' +
      '</div>';

    var root = this.el.querySelector('#' + this.id);
    var self = this;
    root.querySelectorAll('.mevlab-tab').forEach(function (t) {
      t.addEventListener('click', function () {
        self.tab = t.getAttribute('data-tab');
        root.querySelectorAll('.mevlab-tab').forEach(function (x) { x.classList.toggle('on', x === t); });
        self.refresh();
      });
    });
    root.querySelector('[data-role="f-window"]').addEventListener('change', function (e) { self.filters.window = e.target.value; self.refresh(); });
    root.querySelector('[data-role="f-arm"]').addEventListener('change', function (e) { self.filters.arm = e.target.value; self.refresh(); });
    root.querySelector('[data-role="f-dn"]').addEventListener('change', function (e) { self.filters.daynight = e.target.value; self.refresh(); });
    var vs = root.querySelector('[data-role="vs"]');
    if (vs) vs.addEventListener('change', function (e) { self.filters.vsPitcher = e.target.checked; self.refresh(); });

    function fld(label, inner) { return '<div class="mevlab-fld"><label>' + label + '</label>' + inner + '</div>'; }
    function sel(role, opts, cur) {
      return '<select data-role="' + role + '">' + opts.map(function (o) {
        return '<option value="' + o[0] + '"' + (o[0] === cur ? ' selected' : '') + '>' + o[1] + '</option>';
      }).join('') + '</select>';
    }
  };

  Lab.prototype.renderChips = function (pitchTypes) {
    var root = this.el.querySelector('#' + this.id);
    if (!root) return;
    var box = root.querySelector('[data-role="chips"]');
    if (!box) return;
    if (!pitchTypes || !pitchTypes.length) { box.innerHTML = '<span class="mevlab-na" style="font-size:11px">—</span>'; return; }
    var self = this;
    box.innerHTML = pitchTypes.map(function (p) {
      var on = self.filters.pitches.indexOf(p.code) >= 0;
      return '<span class="mevlab-chip' + (on ? ' on' : '') + '" data-code="' + esc(p.code) + '">' + esc(p.name) + '</span>';
    }).join('');
    box.querySelectorAll('.mevlab-chip').forEach(function (ch) {
      ch.addEventListener('click', function () {
        var code = ch.getAttribute('data-code');
        var i = self.filters.pitches.indexOf(code);
        if (i >= 0) self.filters.pitches.splice(i, 1); else self.filters.pitches.push(code);
        self.refresh();
      });
    });
    this.facetsRendered = true;
  };

  Lab.prototype.params = function () {
    var f = this.filters;
    return {
      window: f.window,
      arm: f.arm,
      daynight: f.daynight,
      pitches: f.pitches.join(','),
      pitcher_id: (f.vsPitcher && this.ctx.pitcherId) ? this.ctx.pitcherId : ''
    };
  };

  Lab.prototype.body = function () {
    var root = this.el.querySelector('#' + this.id);
    return root ? root.querySelector('[data-role="body"]') : null;
  };

  Lab.prototype.refresh = function () {
    var self = this;
    var tok = ++this.token;
    var b = this.body();
    if (b) b.innerHTML = '<div class="mevlab-msg">Loading Statcast…</div>';
    var url = (this.tab === 'ev' ? '/api/player/' + this.ctx.batterId + '/exit-velocity'
                                 : '/api/player/' + this.ctx.batterId + '/batted-ball-profile') + qs(this.params());
    fetch(url).then(function (r) { return r.json(); }).then(function (d) {
      if (tok !== self.token) return;            // stale
      if (d && d.facets && !self.facetsRendered) self.renderChips(d.facets.pitch_types);
      if (self.tab === 'ev') self.drawEV(d); else self.drawProfile(d);
    }).catch(function () {
      if (tok !== self.token) return;
      var bb = self.body(); if (bb) bb.innerHTML = '<div class="mevlab-msg">Failed to load Statcast data.</div>';
    });
  };

  Lab.prototype.drawEV = function (d) {
    var b = this.body(); if (!b) return;
    if (!d || !d.success) { b.innerHTML = '<div class="mevlab-msg">' + esc((d && d.note) || 'No Statcast batted-ball data for this batter.') + '</div>'; return; }
    var s = d.summary || {};
    var yr = s.season_year || '';
    var tiles =
      '<div class="mevlab-tiles">' +
        tile('Window AVG EV', num1(s.window_avg_ev) + ' <span style="font-size:11px;color:#7e8a9b">mph</span>', (d.filters.window || '').toUpperCase() + ' · ' + (s.window_n || 0) + ' BBE', heat(s.window_avg_ev, 'ev')) +
        tile('Window Barrel%', pct(s.window_barrel_pct), (d.filters.window || '').toUpperCase(), heat(s.window_barrel_pct, 'brl')) +
        tile("'" + String(yr).slice(-2) + ' AVG EV', num1(s.season_avg_ev) + ' <span style="font-size:11px;color:#7e8a9b">mph</span>', (s.season_n || 0) + ' BBE', '') +
        tile("'" + String(yr).slice(-2) + ' Barrel%', pct(s.season_barrel_pct), 'season', '') +
      '</div>';

    var rows = d.rows || [];
    var legend = '<div class="mevlab-flags"><b style="color:#d98686">■</b> Barrel &nbsp; <b style="color:#7be0a6">■</b> Home Run &nbsp; · balls in play only</div>';
    var table;
    if (!rows.length) {
      table = '<div class="mevlab-msg">No batted balls match these filters.</div>';
    } else {
      var body = rows.map(function (r) {
        var cls = r.is_hr ? 'hr' : (r.is_barrel ? 'barrel' : '');
        return '<tr class="' + cls + '">' +
          '<td>' + esc(r.date) + '</td>' +
          '<td>' + esc(r.pitcher) + '</td>' +
          '<td>' + esc(r.arm) + '</td>' +
          '<td>' + esc(r.pitch_name) + '</td>' +
          '<td class="num"><span class="mevlab-ev" style="' + evColor(r.ev) + '">' + (r.ev == null ? '—' : Number(r.ev).toFixed(1)) + '</span></td>' +
          '<td class="num">' + (r.angle == null ? '—' : Number(r.angle).toFixed(0) + '°') + '</td>' +
          '<td class="num">' + (r.distance == null ? '—' : Number(r.distance).toFixed(0)) + '</td>' +
          '<td class="num">' + (r.bat_speed == null ? '—' : Number(r.bat_speed).toFixed(1)) + '</td>' +
          '<td class="num">' + (r.pitch_velo == null ? '—' : Number(r.pitch_velo).toFixed(1)) + '</td>' +
          '</tr>';
      }).join('');
      table = '<div class="mevlab-tblwrap"><table class="mevlab-tbl"><thead><tr>' +
        '<th>Date</th><th>Pitcher</th><th>Arm</th><th>Type</th><th>EV</th><th>Angle</th><th>Dist</th><th>Bat Spd</th><th>Pitch</th>' +
        '</tr></thead><tbody>' + body + '</tbody></table></div>';
    }
    b.innerHTML = tiles + legend + table;
  };

  Lab.prototype.drawProfile = function (d) {
    var b = this.body(); if (!b) return;
    if (!d || !d.success) { b.innerHTML = '<div class="mevlab-msg">' + esc((d && d.note) || 'No Statcast batted-ball data for this batter.') + '</div>'; return; }
    var m = d.metrics || {};
    if (!m.n) { b.innerHTML = '<div class="mevlab-msg">No batted balls match these filters.</div>'; return; }
    var batSpeedNA = !d.bat_speed_available;
    var top =
      '<div class="mevlab-prof" style="margin-bottom:8px">' +
        tile('GB%', pct(m.gb_pct), '', '') +
        tile('FB%', pct(m.fb_pct), '', '') +
        tile('LD%', pct(m.ld_pct), '', '') +
        tile('PU%', pct(m.pu_pct), '', '') +
        tile('HR/FB%', pct(m.hr_fb_pct), '', heat(m.hr_fb_pct, 'hrfb')) +
      '</div>';
    var bot =
      '<div class="mevlab-prof">' +
        tile('Hard Hit%', pct(m.hard_hit_pct), '', heat(m.hard_hit_pct, 'hh')) +
        tile('Avg EV', num1(m.avg_ev), 'mph', heat(m.avg_ev, 'ev')) +
        tile('Barrel%', pct(m.barrel_pct), '', heat(m.barrel_pct, 'brl')) +
        tile('Blast%', batSpeedNA ? '<span class="mevlab-na">n/a</span>' : pct(m.blast_pct), batSpeedNA ? 'no bat-tracking' : '', heat(m.blast_pct, 'blast')) +
        tile('Pull Barrel%', pct(m.pull_barrel_pct), '', '') +
      '</div>';
    b.innerHTML = top + bot + '<div class="mevlab-flags">' + (m.n) + ' batted balls in sample · ' + esc((d.filters.window || '').toUpperCase()) + (d.filters.arm && d.filters.arm !== 'ALL' ? ' vs ' + esc(d.filters.arm) + 'HP' : '') + '</div>';
  };

  function tile(lab, val, sub, heatCls) {
    return '<div class="mevlab-tile ' + (heatCls || '') + '"><div class="lab">' + lab + '</div><div class="val">' + val + '</div>' + (sub ? '<div class="sub">' + sub + '</div>' : '') + '</div>';
  }

  // contextual heat for tiles
  function heat(v, kind) {
    if (v == null || isNaN(v)) return '';
    if (kind === 'ev') return v >= 92 ? 'heat-hi' : (v >= 89 ? 'heat-md' : '');
    if (kind === 'brl') return v >= 12 ? 'heat-hi' : (v >= 8 ? 'heat-md' : '');
    if (kind === 'hh') return v >= 45 ? 'heat-hi' : (v >= 38 ? 'heat-md' : '');
    if (kind === 'blast') return v >= 20 ? 'heat-hi' : (v >= 12 ? 'heat-md' : '');
    if (kind === 'hrfb') return v >= 20 ? 'heat-hi' : (v >= 12 ? 'heat-md' : '');
    return '';
  }

  // ── public API ────────────────────────────────────────────────────────────
  var API = {
    mount: function (container, ctx) {
      injectStyles();
      var el = (typeof container === 'string') ? document.getElementById(container) : container;
      if (!el || !ctx || !ctx.batterId) return null;
      return new Lab(el, ctx);
    },
    openModal: function (ctx) {
      injectStyles();
      if (!ctx || !ctx.batterId) return null;
      var ov = document.createElement('div');
      ov.className = 'mevlab-ov';
      var host = document.createElement('div');
      host.style.position = 'relative';
      host.style.width = 'min(880px,100%)';
      var x = document.createElement('button');
      x.className = 'mevlab-x';
      x.innerHTML = '✕';
      var mountEl = document.createElement('div');
      host.appendChild(x);
      host.appendChild(mountEl);
      ov.appendChild(host);
      document.body.appendChild(ov);
      function close() { if (ov.parentNode) ov.parentNode.removeChild(ov); }
      x.addEventListener('click', close);
      ov.addEventListener('click', function (e) { if (e.target === ov) close(); });
      document.addEventListener('keydown', function esc(e) { if (e.key === 'Escape') { close(); document.removeEventListener('keydown', esc); } });
      return new Lab(mountEl, ctx);
    }
  };

  window.MatchupEVLab = API;
})();
