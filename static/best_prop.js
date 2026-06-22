/* ============================================================================
 * Best Prop — shared recommendation card
 *
 * Click a game matchup → the engine ranks every batter/pitcher prop and this
 * card surfaces THE recommended target (best Hit / best K), with reasoning.
 *
 * Backed by: GET /api/best-prop/<gamePk>
 *
 * Public API:
 *   BestProp.render(container, gamePk, opts?)
 *     opts.odds   (default true)  → include market edges/EV
 *     opts.title  (default "Recommended Prop")
 * ==========================================================================*/
(function () {
  if (window.BestProp) return;

  function injectStyles() {
    if (document.getElementById('bestprop-styles')) return;
    var css = `
.bestprop{font-family:inherit;color:#e8edf4;background:linear-gradient(180deg,#10172300,#0d1320);border:1px solid rgba(124,58,237,.35);border-radius:12px;padding:13px 14px;font-size:13px}
.bestprop *{box-sizing:border-box}
.bp-top{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:10px}
.bp-kicker{font-size:10px;letter-spacing:.14em;text-transform:uppercase;font-weight:800;color:#c7b3ff}
.bp-sub{font-size:10.5px;color:#8b97a8;margin-left:auto}
.bp-msg{padding:18px 8px;text-align:center;color:#8b97a8;font-size:12px}
.bp-card{display:flex;gap:12px;align-items:flex-start;background:#0f1623;border:1px solid rgba(255,255,255,.08);border-radius:10px;padding:11px 12px}
.bp-pick{flex:1;min-width:0}
.bp-pl{font-size:16px;font-weight:800;line-height:1.15}
.bp-mk{font-size:13px;font-weight:700;color:#7be0a6;margin-top:2px}
.bp-mk .side{color:#cdd6e3}
.bp-sum{font-size:12px;color:#cdd6e3;margin-top:7px;line-height:1.45}
.bp-chips{display:flex;gap:5px;flex-wrap:wrap;margin-top:8px}
.bp-chip{font-size:10.5px;font-weight:600;color:#9aa6b6;background:#141b29;border:1px solid rgba(255,255,255,.1);border-radius:999px;padding:3px 8px}
.bp-chip.good{color:#7be0a6;border-color:#1f9d5566;background:#10341f}
.bp-meta{display:flex;flex-direction:column;align-items:center;gap:6px;min-width:88px}
.bp-tier{width:46px;height:46px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-weight:900;font-size:20px;border:2px solid}
.bp-tier.A{color:#04140a;background:#1f9d55;border-color:#1f9d55}
.bp-tier.B{color:#7be0a6;background:#10341f;border-color:#1f9d55}
.bp-tier.C{color:#ffd479;background:#2a2410;border-color:#caa53d}
.bp-tier.D{color:#9aa6b6;background:#161b27;border-color:#3a4456}
.bp-tlbl{font-size:9px;letter-spacing:.06em;text-transform:uppercase;color:#7e8a9b;font-weight:700}
.bp-badges{display:flex;gap:5px;flex-wrap:wrap;justify-content:center}
.bp-badge{font-size:10px;font-weight:800;border-radius:6px;padding:2px 6px}
.bp-badge.edge{color:#7be0a6;background:#10341f}
.bp-badge.prob{color:#cdd6e3;background:#1a2230}
.bp-splits{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:9px}
.bp-mini{background:#0f1623;border:1px solid rgba(255,255,255,.07);border-radius:9px;padding:8px 10px}
.bp-mini .lab{font-size:9px;letter-spacing:.07em;text-transform:uppercase;color:#7e8a9b;font-weight:800}
.bp-mini .pl{font-size:12.5px;font-weight:700;margin-top:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.bp-mini .ln{font-size:11px;color:#9aa6b6;margin-top:1px}
.bp-more{margin-top:9px;font-size:11px;color:#9aa6b6;cursor:pointer;user-select:none}
.bp-more:hover{color:#cdd6e3}
.bp-board{margin-top:8px;border:1px solid rgba(255,255,255,.07);border-radius:9px;overflow:hidden;display:none}
.bp-row{display:flex;align-items:center;gap:8px;padding:6px 10px;border-top:1px solid rgba(255,255,255,.05);font-size:12px}
.bp-row:first-child{border-top:none}
.bp-row .r-pl{flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.bp-row .r-mk{color:#9aa6b6;font-size:11px}
.bp-row .r-edge{font-weight:700;font-variant-numeric:tabular-nums}
.bp-row .r-edge.pos{color:#7be0a6}
.bp-na{color:#5d6878}
@media(max-width:560px){.bp-splits{grid-template-columns:1fr}}
`;
    var s = document.createElement('style');
    s.id = 'bestprop-styles';
    s.textContent = css;
    document.head.appendChild(s);
  }

  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }
  function pctSigned(v) { return (v == null || isNaN(v)) ? '' : (v >= 0 ? '+' : '') + (v * 100).toFixed(1) + '%'; }

  function pickCardHtml(rec) {
    var tier = (rec.confidenceTier || 'D').toUpperCase();
    var probTxt = rec.adjProb != null ? Math.round(rec.adjProb * 100) + '%' : '—';
    var badges = '<span class="bp-badge prob">' + probTxt + ' model</span>';
    if (rec.edge != null) badges += '<span class="bp-badge edge">' + pctSigned(rec.edge) + ' edge</span>';
    var chips = (rec.drivers || []).map(function (d, i) {
      return '<span class="bp-chip' + (i === 0 ? ' good' : '') + '">' + esc(d) + '</span>';
    }).join('');
    return '<div class="bp-card">' +
      '<div class="bp-pick">' +
        '<div class="bp-pl">' + esc(rec.player) + '</div>' +
        '<div class="bp-mk"><span class="side">' + esc(rec.side || 'Over') + '</span> ' + esc(rec.line) + ' · ' + esc(rec.marketLabel) + '</div>' +
        '<div class="bp-sum">' + esc(rec.summary) + '</div>' +
        '<div class="bp-chips">' + chips + '</div>' +
      '</div>' +
      '<div class="bp-meta">' +
        '<div class="bp-tier ' + tier + '">' + tier + '</div>' +
        '<div class="bp-tlbl">Conf</div>' +
        '<div class="bp-badges">' + badges + '</div>' +
      '</div>' +
    '</div>';
  }

  function miniHtml(lab, r) {
    if (!r) return '<div class="bp-mini"><div class="lab">' + lab + '</div><div class="pl bp-na">no qualifying target</div></div>';
    var edge = r.edge != null ? ' · ' + pctSigned(r.edge) + ' edge' : '';
    return '<div class="bp-mini"><div class="lab">' + lab + '</div>' +
      '<div class="pl">' + esc(r.player) + '</div>' +
      '<div class="ln">' + esc(r.side || 'Over') + ' ' + esc(r.line) + ' ' + esc(r.marketLabel) +
      ' · ' + (r.adjProb != null ? Math.round(r.adjProb * 100) + '%' : '—') + edge + '</div></div>';
  }

  function boardRowHtml(r) {
    var edge = r.edge != null ? pctSigned(r.edge) : '—';
    var cls = (r.edge != null && r.edge > 0) ? ' pos' : '';
    return '<div class="bp-row"><span class="r-pl">' + esc(r.player) +
      ' <span class="r-mk">' + esc(r.side || 'O') + ' ' + esc(r.line) + ' ' + esc(r.marketLabel) + '</span></span>' +
      '<span class="r-edge' + cls + '">' + edge + '</span></div>';
  }

  var API = {
    render: function (container, gamePk, opts) {
      injectStyles();
      opts = opts || {};
      var el = (typeof container === 'string') ? document.getElementById(container) : container;
      if (!el || !gamePk) return;
      var title = opts.title || 'Recommended Prop';
      el.innerHTML = '<div class="bestprop"><div class="bp-top"><span class="bp-kicker">⭐ ' + esc(title) + '</span></div>' +
        '<div class="bp-msg">Analyzing matchup &amp; ranking props…</div></div>';
      var odds = (opts.odds === false) ? '0' : '1';
      fetch('/api/best-prop/' + gamePk + '?odds=' + odds).then(function (r) { return r.json(); }).then(function (d) {
        if (!el.isConnected) return;
        if (!d || !d.success || !d.recommendation) {
          el.querySelector('.bestprop').innerHTML =
            '<div class="bp-top"><span class="bp-kicker">⭐ ' + esc(title) + '</span></div>' +
            '<div class="bp-msg">' + esc((d && d.error) ? d.error : 'No prop recommendation available for this game yet.') + '</div>';
          return;
        }
        var sub = (d.oddsUsed ? 'ranked vs market' : 'model-only (no odds posted)') + (d.matchup ? ' · ' + esc(d.matchup) : '');
        var boardId = 'bpboard_' + gamePk + '_' + Math.floor(Math.random() * 1e6);
        var html =
          '<div class="bp-top"><span class="bp-kicker">⭐ ' + esc(title) + '</span><span class="bp-sub">' + sub + '</span></div>' +
          pickCardHtml(d.recommendation) +
          '<div class="bp-splits">' + miniHtml('Top Hit Target', d.topBatter) + miniHtml('Top Strikeout Target', d.topPitcherK) + '</div>' +
          (d.board && d.board.length ? '<div class="bp-more" data-board="' + boardId + '">▾ Show full ranked board (' + d.board.length + ')</div>' +
            '<div class="bp-board" id="' + boardId + '">' + d.board.map(boardRowHtml).join('') + '</div>' : '');
        var root = el.querySelector('.bestprop');
        root.innerHTML = html;
        var more = root.querySelector('.bp-more');
        if (more) more.addEventListener('click', function () {
          var b = document.getElementById(more.getAttribute('data-board'));
          if (!b) return;
          var open = b.style.display === 'block';
          b.style.display = open ? 'none' : 'block';
          more.textContent = (open ? '▾ Show' : '▴ Hide') + ' full ranked board (' + d.board.length + ')';
        });
      }).catch(function () {
        if (!el.isConnected) return;
        var root = el.querySelector('.bestprop');
        if (root) root.innerHTML = '<div class="bp-top"><span class="bp-kicker">⭐ ' + esc(title) + '</span></div><div class="bp-msg">Could not load recommendation.</div>';
      });
    }
  };

  window.BestProp = API;
})();
