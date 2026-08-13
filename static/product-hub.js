(function () {
  'use strict';

  var WATCHLIST_KEY = 'mlb_watchlist';
  var MARKET_KEY = 'mlb_market_preferences';
  var THRESHOLD_KEY = 'mlb_alert_edge_threshold';
  var ALERT_LEDGER_KEY = 'mlb_alert_ledger';
  var ALERT_LEDGER_LIMIT = 200;
  var MARKET_OPTIONS = [
    { key: 'batter_hits', label: 'Hits' },
    { key: 'batter_total_bases', label: 'Total Bases' },
    { key: 'batter_home_runs', label: 'Home Runs' },
    { key: 'batter_rbis', label: 'RBIs' },
    { key: 'pitcher_strikeouts', label: 'Strikeouts' }
  ];
  var state = {
    edges: [],
    markets: null,
    tracker: null,
    watchlist: new Set(),
    preferred: new Set(),
    threshold: 5,
    alertLedger: {}
  };

  function readJson(key, fallback) {
    try { return JSON.parse(window.localStorage.getItem(key) || '') || fallback; }
    catch (error) { return fallback; }
  }

  function writeJson(key, value) {
    try { window.localStorage.setItem(key, JSON.stringify(value)); }
    catch (error) { /* device storage is optional */ }
  }

  function readString(key, fallback) {
    try { return window.localStorage.getItem(key) || fallback; }
    catch (error) { return fallback; }
  }

  function writeString(key, value) {
    try { window.localStorage.setItem(key, String(value)); }
    catch (error) { /* device storage is optional */ }
  }

  function esc(value) {
    return String(value == null ? '' : value).replace(/[&<>'"]/g, function (char) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[char];
    });
  }

  function number(value) {
    var parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }

  function probability(value) {
    var parsed = number(value);
    if (parsed == null) return null;
    return parsed > 1 ? parsed / 100 : parsed;
  }

  function edgeValue(row) {
    var value = number(row.edgePct);
    if (value != null) return value;
    value = number(row.canonicalEdge != null ? row.canonicalEdge : row.edge);
    if (value == null) return null;
    return Math.abs(value) <= 1 ? value * 100 : value;
  }

  function priceOf(row) {
    return number(row.canonicalPrice != null ? row.canonicalPrice : (row.bestPrice != null ? row.bestPrice : row.bestAvailablePrice));
  }

  function bookOf(row) {
    var value = row.canonicalBook || row.bestBook || row.bestAvailableBook || row.bookmaker || null;
    return value && String(value).trim() ? String(value).trim() : null;
  }

  function marketKeyOf(row) {
    return String(row.canonicalMarketKey || row.marketKey || '').trim();
  }

  function isActionable(row) {
    var stage = String(row.actionabilityStage || '').toLowerCase();
    var price = priceOf(row);
    var edge = edgeValue(row);
    var book = String(bookOf(row) || '').toLowerCase();
    var invalidBooks = ['model', 'n/a', 'na', 'none', 'projection', 'research', 'sim', 'simulation', 'unknown', 'unpriced'];
    return row.actionable === true && (!stage || stage === 'actionable') &&
      Boolean(row.player && row.playerId && row.canonicalCandidateId) &&
      Boolean(row.canonicalFingerprint) && Boolean(marketKeyOf(row)) &&
      price != null && price !== 0 && Math.abs(price) >= 100 &&
      Boolean(book) && invalidBooks.indexOf(book) === -1 &&
      edge != null && edge > 0;
  }

  function alertIdentity(row) {
    return String(row.canonicalCandidateId) + ':' + String(row.canonicalFingerprint);
  }

  function cleanAlertLedger(value) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
    var valid = {};
    Object.keys(value).slice(-ALERT_LEDGER_LIMIT).forEach(function (key) {
      var item = value[key];
      if (!item || ['new', 'seen', 'dismissed'].indexOf(item.status) === -1) return;
      valid[key] = {
        status: item.status,
        createdAt: String(item.createdAt || ''),
        updatedAt: String(item.updatedAt || item.createdAt || '')
      };
    });
    return valid;
  }

  function persistAlertLedger() {
    var keys = Object.keys(state.alertLedger).sort(function (left, right) {
      return String(state.alertLedger[left].updatedAt).localeCompare(String(state.alertLedger[right].updatedAt));
    });
    while (keys.length > ALERT_LEDGER_LIMIT) delete state.alertLedger[keys.shift()];
    writeJson(ALERT_LEDGER_KEY, state.alertLedger);
  }

  function loadPreferences() {
    var savedWatchlist = readJson(WATCHLIST_KEY, []);
    if (!Array.isArray(savedWatchlist)) savedWatchlist = [];
    state.watchlist = new Set(savedWatchlist.map(function (name) { return String(name).toLowerCase(); }));
    var savedMarkets = readJson(MARKET_KEY, MARKET_OPTIONS.map(function (item) { return item.key; }));
    if (!Array.isArray(savedMarkets)) savedMarkets = MARKET_OPTIONS.map(function (item) { return item.key; });
    state.preferred = new Set(savedMarkets);
    var savedThreshold = number(readString(THRESHOLD_KEY, ''));
    state.threshold = savedThreshold != null ? savedThreshold : 5;
    state.alertLedger = cleanAlertLedger(readJson(ALERT_LEDGER_KEY, {}));
  }

  function renderMarketOptions() {
    var host = document.getElementById('marketOptions');
    host.innerHTML = '';
    MARKET_OPTIONS.forEach(function (item) {
      var button = document.createElement('button');
      button.type = 'button';
      button.className = 'market-chip';
      button.textContent = item.label;
      button.setAttribute('aria-pressed', state.preferred.has(item.key) ? 'true' : 'false');
      button.addEventListener('click', function () {
        if (state.preferred.has(item.key)) state.preferred.delete(item.key);
        else state.preferred.add(item.key);
        button.setAttribute('aria-pressed', state.preferred.has(item.key) ? 'true' : 'false');
        writeJson(MARKET_KEY, Array.from(state.preferred));
        renderSignals();
      });
      host.appendChild(button);
    });
    var select = document.getElementById('alertThreshold');
    select.value = String(state.threshold);
    select.addEventListener('change', function () {
      state.threshold = number(select.value) || 5;
      writeString(THRESHOLD_KEY, state.threshold);
      renderSignals();
    });
  }

  function renderWatchlist() {
    var host = document.getElementById('watchlistChips');
    var names = Array.from(state.watchlist).sort();
    document.getElementById('watchlistMetric').textContent = String(names.length);
    if (!names.length) {
      host.innerHTML = '<span class="empty-state">Star players in Props or add one here.</span>';
      return;
    }
    host.innerHTML = names.map(function (name) {
      return '<span class="watch-chip">⭐ ' + esc(name.replace(/\b\w/g, function (letter) { return letter.toUpperCase(); })) +
        '<button type="button" data-remove-player="' + esc(name) + '" aria-label="Remove ' + esc(name) + '">×</button></span>';
    }).join('');
    host.querySelectorAll('[data-remove-player]').forEach(function (button) {
      button.addEventListener('click', function () {
        state.watchlist.delete(button.getAttribute('data-remove-player'));
        writeJson(WATCHLIST_KEY, Array.from(state.watchlist));
        renderWatchlist();
        renderSignals();
      });
    });
  }

  function wireWatchlistForm() {
    document.getElementById('watchlistForm').addEventListener('submit', function (event) {
      event.preventDefault();
      var input = document.getElementById('watchlistName');
      var name = input.value.trim().toLowerCase();
      if (!name) return;
      state.watchlist.add(name);
      writeJson(WATCHLIST_KEY, Array.from(state.watchlist));
      input.value = '';
      renderWatchlist();
      renderSignals();
    });
  }

  function personalizedEdges() {
    var preferred = state.edges.filter(function (row) { return state.preferred.has(marketKeyOf(row)); });
    return preferred.sort(function (left, right) {
      var leftSaved = state.watchlist.has(String(left.player || '').toLowerCase()) ? 1 : 0;
      var rightSaved = state.watchlist.has(String(right.player || '').toLowerCase()) ? 1 : 0;
      if (leftSaved !== rightSaved) return rightSaved - leftSaved;
      return (edgeValue(right) || 0) - (edgeValue(left) || 0);
    });
  }

  function signalHtml(row) {
    var market = MARKET_OPTIONS.find(function (item) { return item.key === marketKeyOf(row); });
    var edge = edgeValue(row);
    var modelProb = probability(row.canonicalProbability != null ? row.canonicalProbability : row.modelProb);
    var implied = probability(row.marketImplied != null ? row.marketImplied : row.impliedProb);
    var price = priceOf(row);
    var saved = state.watchlist.has(String(row.player || '').toLowerCase());
    return '<article class="signal-card' + (saved ? ' watchlisted' : '') + '">' +
      '<div><div class="signal-title">' + (saved ? '<span class="star">★</span>' : '') + '<strong>' + esc(row.player) + '</strong></div>' +
      '<p class="signal-market">' + esc(market ? market.label : marketKeyOf(row)) + ' · ' + esc(row.side || row.canonicalSide || 'Over') + ' ' + esc(row.line) + '</p>' +
      '<div class="evidence">' +
        (modelProb != null ? '<span>MODEL ' + (modelProb * 100).toFixed(1) + '%</span>' : '') +
        (implied != null ? '<span>MARKET ' + (implied * 100).toFixed(1) + '%</span>' : '') +
        '<span>' + esc(bookOf(row)) + ' ' + (price > 0 ? '+' : '') + esc(price) + '</span>' +
        '<span>VALIDATED</span>' +
      '</div></div>' +
      '<div class="signal-edge"><strong>+' + (edge == null ? '—' : edge.toFixed(1)) + '%</strong><small>MODEL EDGE</small></div>' +
      '</article>';
  }

  function renderSignals() {
    var list = personalizedEdges();
    var host = document.getElementById('signalList');
    document.getElementById('actionableCount').textContent = String(state.edges.length);
    document.getElementById('signalFoot').textContent = list.length + ' preferred-market signal' + (list.length === 1 ? '' : 's') + '; saved players rank first.';
    renderAlerts();
    if (!state.preferred.size) {
      host.innerHTML = '<div class="empty-state">Select at least one preferred market to personalize signals.</div>';
      return;
    }
    if (!list.length) {
      host.innerHTML = '<div class="empty-state">No fully validated, priced signals match your preferred markets right now.</div>';
      return;
    }
    host.innerHTML = list.slice(0, 8).map(signalHtml).join('');
  }

  function eligibleAlerts() {
    return personalizedEdges().filter(function (row) {
      return (edgeValue(row) || 0) >= state.threshold;
    });
  }

  function alertExplanation(row) {
    var parts = [];
    if (state.watchlist.has(String(row.player || '').toLowerCase())) parts.push('saved player');
    parts.push('preferred market');
    parts.push((edgeValue(row) || 0).toFixed(1) + '% edge');
    parts.push(bookOf(row) + ' ' + (priceOf(row) > 0 ? '+' : '') + priceOf(row));
    return parts.join(' · ');
  }

  function alertHtml(row, ledger) {
    var id = alertIdentity(row);
    var market = MARKET_OPTIONS.find(function (item) { return item.key === marketKeyOf(row); });
    return '<article class="alert-card" data-alert-id="' + esc(id) + '">' +
      '<div><span class="alert-state">' + (ledger.status === 'new' ? 'NEW' : 'SEEN') + '</span>' +
      '<strong>' + esc(row.player) + ' · ' + esc(market ? market.label : marketKeyOf(row)) + '</strong>' +
      '<small>' + esc(alertExplanation(row)) + '</small></div>' +
      '<div class="alert-actions">' +
      (ledger.status === 'new' ? '<button type="button" data-alert-action="seen">Seen</button>' : '') +
      '<button type="button" data-alert-action="dismiss">Dismiss</button></div></article>';
  }

  function renderAlerts() {
    var now = new Date().toISOString();
    var eligible = eligibleAlerts();
    var changed = false;
    eligible.forEach(function (row) {
      var id = alertIdentity(row);
      if (!state.alertLedger[id]) {
        state.alertLedger[id] = { status: 'new', createdAt: now, updatedAt: now };
        changed = true;
      }
    });
    if (changed) persistAlertLedger();

    var visible = eligible.filter(function (row) {
      return state.alertLedger[alertIdentity(row)].status !== 'dismissed';
    });
    var unread = visible.filter(function (row) {
      return state.alertLedger[alertIdentity(row)].status === 'new';
    }).length;
    document.getElementById('alertMetric').textContent = String(unread);
    document.getElementById('alertMetricDetail').textContent = state.threshold + '%+ preferred markets';
    document.getElementById('alertSummary').textContent =
      unread + ' new · ' + visible.length + ' active · ' + eligible.length + ' threshold matches';

    var host = document.getElementById('alertList');
    if (!visible.length) {
      host.innerHTML = '<div class="empty-state">No new fully validated alerts match your preferences right now.</div>';
      return;
    }
    host.innerHTML = visible.slice(0, 12).map(function (row) {
      return alertHtml(row, state.alertLedger[alertIdentity(row)]);
    }).join('');
  }

  function wireAlertInbox() {
    document.getElementById('alertList').addEventListener('click', function (event) {
      var button = event.target.closest('[data-alert-action]');
      var card = event.target.closest('[data-alert-id]');
      if (!button || !card) return;
      var id = card.getAttribute('data-alert-id');
      var item = state.alertLedger[id];
      if (!item) return;
      item.status = button.getAttribute('data-alert-action') === 'dismiss' ? 'dismissed' : 'seen';
      item.updatedAt = new Date().toISOString();
      persistAlertLedger();
      renderAlerts();
    });
    document.getElementById('markAllAlertsSeen').addEventListener('click', function () {
      var now = new Date().toISOString();
      eligibleAlerts().forEach(function (row) {
        var item = state.alertLedger[alertIdentity(row)];
        if (item && item.status === 'new') {
          item.status = 'seen';
          item.updatedAt = now;
        }
      });
      persistAlertLedger();
      renderAlerts();
    });
  }

  function marketEntries(payload) {
    var source = payload && (payload.marketGates || payload.markets || payload.byMarket);
    if (!source) return [];
    if (Array.isArray(source)) return source.map(function (item) { return [item.marketKey || item.market || 'market', item]; });
    return Object.keys(source).map(function (key) { return [key, source[key]]; });
  }

  function renderValidation() {
    var entries = marketEntries(state.markets);
    var host = document.getElementById('validationBody');
    if (!entries.length) {
      document.getElementById('healthyMarketCount').textContent = '—';
      host.innerHTML = '<div class="empty-state">Calibration evidence is unavailable. My Hub will continue to suppress unverified signals.</div>';
      return;
    }
    var healthy = entries.filter(function (entry) {
      var gate = entry[1] || {};
      var status = String(gate.status || gate.driftStatus || 'unknown').toLowerCase();
      return ['promoted', 'passed', 'stable', 'ready'].indexOf(status) >= 0;
    }).length;
    host.innerHTML = entries.slice(0, 8).map(function (entry) {
      var key = entry[0];
      var gate = entry[1] || {};
      var status = String(gate.status || gate.driftStatus || 'unknown').toLowerCase();
      var ok = ['promoted', 'passed', 'stable', 'ready'].indexOf(status) >= 0;
      var blocked = ['disabled', 'failed', 'drifted', 'blocked'].indexOf(status) >= 0;
      return '<div class="gate-row"><span>' + esc(key.replace(/_/g, ' ')) + '</span><b class="' + (ok ? 'ok' : (blocked ? 'blocked' : 'watch')) + '">' + esc(status.toUpperCase()) + '</b></div>';
    }).join('');
    document.getElementById('healthyMarketCount').textContent = healthy + '/' + entries.length;
    document.getElementById('healthyMarketDetail').textContent = healthy === entries.length ? 'all calibration gates' : 'review watch/blocked markets';
  }

  function findMetric(payload, keys) {
    var containers = [payload, payload && payload.overall, payload && payload.summary, payload && payload.performance, payload && payload.metrics];
    for (var i = 0; i < containers.length; i += 1) {
      var current = containers[i];
      if (!current) continue;
      for (var j = 0; j < keys.length; j += 1) {
        if (current[keys[j]] != null) return number(current[keys[j]]);
      }
    }
    return null;
  }

  function renderTracker() {
    var payload = state.tracker;
    if (!payload) {
      document.getElementById('trackNote').textContent = 'Tracker evidence is unavailable. Open Tracker to review saved decisions.';
      return;
    }
    var graded = findMetric(payload, ['gradedCount', 'graded', 'totalGraded', 'count']);
    var winRate = findMetric(payload, ['winRate', 'hitRate']);
    var roi = findMetric(payload, ['roi', 'roiPct', 'returnPct']);
    var brier = findMetric(payload, ['brierScore', 'brier']);
    document.getElementById('gradedCount').textContent = graded == null ? '—' : String(Math.round(graded));
    document.getElementById('winRate').textContent = winRate == null ? '—' : ((winRate <= 1 ? winRate * 100 : winRate).toFixed(1) + '%');
    document.getElementById('roi').textContent = roi == null ? '—' : ((roi <= 1 && roi >= -1 ? roi * 100 : roi).toFixed(1) + '%');
    document.getElementById('brier').textContent = brier == null ? '—' : brier.toFixed(3);
    document.getElementById('trackNote').textContent = graded ? 'Last 30 days of graded decisions. Use Tracker for CLV, market splits, and full audit details.' : 'No graded decisions are available for the selected window yet.';
  }

  function updateHero(failures) {
    var hero = document.getElementById('heroStatus');
    hero.setAttribute('data-state', failures ? 'partial' : 'ready');
    document.getElementById('heroStatusLabel').textContent = failures ? 'Workspace partially available' : 'Workspace ready';
    document.getElementById('heroStatusDetail').textContent = failures ? 'Some evidence sources could not be loaded' : 'Canonical evidence loaded successfully';
  }

  function requestJson(url) {
    return fetch(url, { credentials: 'same-origin', headers: { Accept: 'application/json' } }).then(function (response) {
      if (!response.ok) throw new Error('HTTP ' + response.status);
      return response.json();
    });
  }

  function boot() {
    loadPreferences();
    renderMarketOptions();
    renderWatchlist();
    wireWatchlistForm();
    wireAlertInbox();
    renderAlerts();

    var failures = 0;
    Promise.all([
      requestJson('/api/edges/today?minEdge=0.03').then(function (payload) {
        var rows = payload && Array.isArray(payload.edges) ? payload.edges : [];
        state.edges = rows.filter(isActionable);
        renderSignals();
      }).catch(function () { failures += 1; state.edges = []; renderSignals(); }),
      requestJson('/api/calibration/markets').then(function (payload) { state.markets = payload; renderValidation(); })
        .catch(function () { failures += 1; state.markets = null; renderValidation(); }),
      requestJson('/api/tracker/performance?window=30').then(function (payload) { state.tracker = payload; renderTracker(); })
        .catch(function () { failures += 1; state.tracker = null; renderTracker(); })
    ]).then(function () { updateHero(failures); });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
