(function () {
  'use strict';

  var WATCHLIST_KEY = 'mlb_watchlist';
  var MARKET_KEY = 'mlb_market_preferences';
  var THRESHOLD_KEY = 'mlb_alert_edge_threshold';
  var ALERT_LEDGER_KEY = 'mlb_alert_ledger';
  var ALERT_CANDIDATE_STATE_KEY = 'mlb_alert_candidate_state';
  var ALERT_LEDGER_LIMIT = 200;
  var MAX_ALERT_ODDS_AGE_SECONDS = 900;
  var RECOMMENDATION_EVIDENCE_VERSION = '4.69';
  var MATERIAL_EDGE_DELTA_PCT = 1;
  var MATERIAL_PRICE_DELTA = 10;
  var MARKET_OPTIONS = [
    { key: 'batter_hits', label: 'Hits' },
    { key: 'batter_total_bases', label: 'Total Bases' },
    { key: 'batter_home_runs', label: 'Home Runs' },
    { key: 'batter_rbis', label: 'RBIs' },
    { key: 'pitcher_strikeouts', label: 'Strikeouts' }
  ];
  var state = {
    edges: [],
    edgeState: 'loading',
    markets: null,
    tracker: null,
    watchlist: new Set(),
    preferred: new Set(),
    threshold: 5,
    alertLedger: {},
    alertCandidates: {}
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

  function playerKey(value) {
    return String(value || '').trim().toLowerCase();
  }

  function displayPlayerName(value) {
    return playerKey(value).replace(/\b\w/g, function (letter) {
      return letter.toUpperCase();
    });
  }

  function marketLabelOf(row) {
    var option = MARKET_OPTIONS.find(function (item) {
      return item.key === marketKeyOf(row);
    });
    return option ? option.label : marketKeyOf(row).replace(/_/g, ' ');
  }

  function hasEvidenceReceipt(row) {
    var receipt = row && row.evidenceReceipt;
    var selection = receipt && receipt.selection;
    var quoted = receipt && receipt.price;
    var model = receipt && receipt.model;
    var market = receipt && receipt.market;
    var validation = receipt && receipt.validation;
    if (!receipt || !selection || !quoted || !model || !market || !validation) return false;

    var receiptPrice = number(quoted.american);
    var receiptAge = number(quoted.ageSeconds);
    var receiptLine = number(selection.line);
    var receiptModel = probability(model.probability);
    var receiptImplied = probability(market.impliedProbability);
    var receiptFair = probability(market.fairProbability);
    var receiptEdge = number(market.edge);
    var rowEdge = edgeValue(row);
    var timestamp = Date.parse(String(quoted.observedAt || ''));
    return receipt.contractVersion === RECOMMENDATION_EVIDENCE_VERSION &&
      receipt.candidateId === row.canonicalCandidateId &&
      receipt.fingerprint === row.canonicalFingerprint &&
      selection.marketKey === marketKeyOf(row) &&
      String(selection.side || '').toLowerCase() ===
        String(row.canonicalSide || row.side || '').toLowerCase() &&
      receiptLine != null && receiptLine === number(row.line) &&
      receiptPrice != null && receiptPrice === priceOf(row) &&
      String(quoted.book || '').toLowerCase() === String(bookOf(row) || '').toLowerCase() &&
      receiptAge != null && receiptAge >= 0 &&
      receiptAge <= MAX_ALERT_ODDS_AGE_SECONDS &&
      quoted.maximumAgeSeconds === MAX_ALERT_ODDS_AGE_SECONDS &&
      quoted.fresh === true && Number.isFinite(timestamp) &&
      String(quoted.observedAt) === String(row.oddsUpdatedAt || '') &&
      receiptModel != null && receiptModel > 0 && receiptModel < 1 &&
      Boolean(model.version) &&
      receiptImplied != null && receiptImplied > 0 && receiptImplied < 1 &&
      receiptFair != null && receiptFair > 0 && receiptFair < 1 &&
      receiptEdge != null && receiptEdge > 0 &&
      rowEdge != null && Math.abs(receiptEdge * 100 - rowEdge) < 0.11 &&
      validation.actionable === true &&
      String(validation.actionabilityStage || '').toLowerCase() === 'actionable' &&
      String(validation.calibrationStatus || '').toLowerCase() === 'passed' &&
      String(validation.marketGateStatus || '').toLowerCase() === 'promoted' &&
      Boolean(validation.candidateIntegrityVersion) &&
      Boolean(validation.marketValidationVersion) &&
      Boolean(String(receipt.explanation || '').trim());
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
      edge != null && edge > 0 && hasEvidenceReceipt(row);
  }

  function alertIdentity(row) {
    return String(row.canonicalCandidateId) + ':' + String(row.canonicalFingerprint);
  }

  function isAlertFresh(row) {
    var age = number(row.oddsAgeSeconds);
    var timestamp = Date.parse(String(row.oddsUpdatedAt || ''));
    return age != null && age >= 0 && age <= MAX_ALERT_ODDS_AGE_SECONDS &&
      Number.isFinite(timestamp);
  }

  function cleanAlertLedger(value) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
    var valid = {};
    Object.keys(value).slice(-ALERT_LEDGER_LIMIT).forEach(function (key) {
      var item = value[key];
      if (!item || ['new', 'seen', 'dismissed', 'superseded'].indexOf(item.status) === -1) return;
      valid[key] = {
        status: item.status,
        kind: String(item.kind || 'new_opportunity'),
        movement: String(item.movement || ''),
        createdAt: String(item.createdAt || ''),
        updatedAt: String(item.updatedAt || item.createdAt || '')
      };
    });
    return valid;
  }

  function cleanAlertCandidates(value) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
    var valid = {};
    Object.keys(value).slice(-ALERT_LEDGER_LIMIT).forEach(function (key) {
      var item = value[key];
      if (!item || !item.activeAlertId || !item.fingerprint) return;
      valid[key] = {
        activeAlertId: String(item.activeAlertId),
        fingerprint: String(item.fingerprint),
        edge: number(item.edge),
        price: number(item.price),
        oddsUpdatedAt: String(item.oddsUpdatedAt || ''),
        updatedAt: String(item.updatedAt || '')
      };
    });
    return valid;
  }

  function persistAlertState() {
    var candidateKeys = Object.keys(state.alertCandidates).sort(function (left, right) {
      return String(state.alertCandidates[left].updatedAt).localeCompare(String(state.alertCandidates[right].updatedAt));
    });
    while (candidateKeys.length > ALERT_LEDGER_LIMIT) delete state.alertCandidates[candidateKeys.shift()];

    var protectedIds = {};
    Object.keys(state.alertCandidates).forEach(function (key) {
      protectedIds[state.alertCandidates[key].activeAlertId] = true;
    });
    var ledgerKeys = Object.keys(state.alertLedger).sort(function (left, right) {
      return String(state.alertLedger[left].updatedAt).localeCompare(String(state.alertLedger[right].updatedAt));
    });
    var removable = ledgerKeys.filter(function (key) { return !protectedIds[key]; });
    while (ledgerKeys.length > ALERT_LEDGER_LIMIT && removable.length) {
      var removeId = removable.shift();
      delete state.alertLedger[removeId];
      ledgerKeys = ledgerKeys.filter(function (key) { return key !== removeId; });
    }
    writeJson(ALERT_LEDGER_KEY, state.alertLedger);
    writeJson(ALERT_CANDIDATE_STATE_KEY, state.alertCandidates);
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
    state.alertCandidates = cleanAlertCandidates(readJson(ALERT_CANDIDATE_STATE_KEY, {}));
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

  function persistWatchlist() {
    writeJson(WATCHLIST_KEY, Array.from(state.watchlist));
  }

  function setPlayerSaved(name, saved) {
    var key = playerKey(name);
    if (!key) return;
    if (saved) state.watchlist.add(key);
    else state.watchlist.delete(key);
    persistWatchlist();
    renderWatchlist();
    renderSignals();
  }

  function savedPlayerRows(name) {
    var key = playerKey(name);
    return state.edges.filter(function (row) {
      return playerKey(row.player) === key && isActionable(row);
    }).sort(function (left, right) {
      return (edgeValue(right) || 0) - (edgeValue(left) || 0);
    });
  }

  function savedOpportunityHtml(name) {
    var rows = savedPlayerRows(name);
    var label = displayPlayerName(name);
    if (!rows.length) {
      return '<article class="saved-opportunity quiet" data-opportunity-state="none">' +
        '<div><strong>' + esc(label) + '</strong><small>No verified opportunity right now.</small></div>' +
        '<span>UNVERIFIED ROWS HIDDEN</span></article>';
    }
    var row = rows[0];
    var receipt = row.evidenceReceipt;
    var edge = edgeValue(row);
    var price = number(receipt.price.american);
    var more = rows.length > 1 ? ' · +' + (rows.length - 1) + ' more' : '';
    return '<article class="saved-opportunity ready" data-opportunity-state="verified">' +
      '<div><strong>' + esc(label) + '</strong><small>' +
        esc(marketLabelOf(row)) + ' · ' + esc(receipt.selection.side) + ' ' +
        esc(receipt.selection.line) + more + '</small></div>' +
      '<div class="saved-opportunity-proof"><b>+' + edge.toFixed(1) + '%</b><small>' +
        esc(receipt.price.book) + ' ' + (price > 0 ? '+' : '') + esc(price) +
        ' · ' + esc(freshnessLabel(row)) + '</small></div>' +
      '<span>VERIFIED RECEIPT ' + esc(receipt.contractVersion) + '</span></article>';
  }

  function renderSavedOpportunityDigest() {
    var host = document.getElementById('savedOpportunityList');
    var summary = document.getElementById('savedOpportunitySummary');
    var names = Array.from(state.watchlist).sort();
    if (!host || !summary) return;
    if (!names.length) {
      summary.textContent = 'Save a player to create a private opportunity digest.';
      host.innerHTML = '<div class="empty-state">No saved players yet.</div>';
      return;
    }
    if (['loading', 'computing'].indexOf(state.edgeState) >= 0) {
      summary.textContent = 'Checking receipted opportunities for ' + names.length + ' saved player' + (names.length === 1 ? '' : 's') + '…';
      host.innerHTML = '<div class="empty-state">Waiting for fresh canonical evidence…</div>';
      return;
    }
    if (['failed', 'unavailable'].indexOf(state.edgeState) >= 0) {
      summary.textContent = 'Opportunity evidence is unavailable; saved players remain private on this device.';
      host.innerHTML = '<div class="empty-state">No recommendation is shown without verified evidence.</div>';
      return;
    }
    var active = names.filter(function (name) {
      return savedPlayerRows(name).length > 0;
    }).length;
    summary.textContent = active + ' of ' + names.length + ' saved player' +
      (names.length === 1 ? '' : 's') + ' have a verified opportunity now.';
    host.innerHTML = names.map(savedOpportunityHtml).join('');
  }

  function renderWatchlist() {
    var host = document.getElementById('watchlistChips');
    var names = Array.from(state.watchlist).sort();
    document.getElementById('watchlistMetric').textContent = String(names.length);
    if (!names.length) {
      host.innerHTML = '<span class="empty-state">Save a receipted signal or add a player here.</span>';
      renderSavedOpportunityDigest();
      return;
    }
    host.innerHTML = names.map(function (name) {
      return '<span class="watch-chip">⭐ ' + esc(displayPlayerName(name)) +
        '<button type="button" data-remove-player="' + esc(name) + '" aria-label="Remove ' + esc(displayPlayerName(name)) + '">×</button></span>';
    }).join('');
    host.querySelectorAll('[data-remove-player]').forEach(function (button) {
      button.addEventListener('click', function () {
        setPlayerSaved(button.getAttribute('data-remove-player'), false);
      });
    });
    renderSavedOpportunityDigest();
  }

  function wireWatchlistForm() {
    document.getElementById('watchlistForm').addEventListener('submit', function (event) {
      event.preventDefault();
      var input = document.getElementById('watchlistName');
      var name = playerKey(input.value);
      if (!name) return;
      input.value = '';
      setPlayerSaved(name, true);
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
    var receipt = row.evidenceReceipt;
    var edge = edgeValue(row);
    var modelProb = probability(receipt.model.probability);
    var fairProb = probability(receipt.market.fairProbability);
    var price = number(receipt.price.american);
    var player = playerKey(row.player);
    var saved = state.watchlist.has(player);
    return '<article class="signal-card' + (saved ? ' watchlisted' : '') + '">' +
      '<div><div class="signal-title"><strong>' + esc(row.player) + '</strong>' +
      '<button type="button" class="signal-save" data-toggle-player="' + esc(player) +
        '" aria-pressed="' + (saved ? 'true' : 'false') + '" aria-label="' +
        (saved ? 'Remove ' : 'Save ') + esc(row.player) + '">' +
        (saved ? '★ Saved' : '☆ Save') + '</button></div>' +
      '<p class="signal-market">' + esc(marketLabelOf(row)) + ' · ' + esc(receipt.selection.side) + ' ' + esc(receipt.selection.line) + '</p>' +
      '<div class="evidence">' +
        '<span>MODEL ' + (modelProb * 100).toFixed(1) + '%</span>' +
        '<span>FAIR MARKET ' + (fairProb * 100).toFixed(1) + '%</span>' +
        '<span>' + esc(receipt.price.book) + ' ' + (price > 0 ? '+' : '') + esc(price) + '</span>' +
        '<span>' + esc(freshnessLabel(row).toUpperCase()) + '</span>' +
        '<span>RECEIPT 4.69</span>' +
      '</div>' +
      '<p class="signal-why"><strong>Why this qualifies:</strong> ' + esc(receipt.explanation) + '</p></div>' +
      '<div class="signal-edge"><strong>+' + (edge == null ? '—' : edge.toFixed(1)) + '%</strong><small>MODEL EDGE</small></div>' +
      '</article>';
  }

  function renderSignals() {
    var list = personalizedEdges();
    var host = document.getElementById('signalList');
    document.getElementById('actionableCount').textContent = String(state.edges.length);
    document.getElementById('signalFoot').textContent = list.length + ' preferred-market signal' + (list.length === 1 ? '' : 's') + '; saved players rank first.';
    renderSavedOpportunityDigest();
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

  function wireSignalActions() {
    document.getElementById('signalList').addEventListener('click', function (event) {
      var button = event.target.closest('[data-toggle-player]');
      if (!button) return;
      var name = button.getAttribute('data-toggle-player');
      setPlayerSaved(name, !state.watchlist.has(playerKey(name)));
    });
  }

  function thresholdMatches() {
    return personalizedEdges().filter(function (row) {
      return (edgeValue(row) || 0) >= state.threshold;
    });
  }

  function eligibleAlerts() {
    return thresholdMatches().filter(isAlertFresh);
  }

  function freshnessLabel(row) {
    var age = number(row.oddsAgeSeconds);
    if (age == null) return 'freshness unavailable';
    if (age < 60) return 'fresh now';
    return 'fresh ' + Math.floor(age / 60) + 'm ago';
  }

  function materialMovement(previous, row) {
    var edgeDelta = (edgeValue(row) || 0) - (number(previous.edge) || 0);
    var priceDelta = (priceOf(row) || 0) - (number(previous.price) || 0);
    if (Math.abs(edgeDelta) >= MATERIAL_EDGE_DELTA_PCT) {
      return {
        kind: edgeDelta > 0 ? 'edge_up' : 'edge_down',
        text: 'edge ' + (edgeDelta > 0 ? '+' : '') + edgeDelta.toFixed(1) + ' pts'
      };
    }
    if (Math.abs(priceDelta) >= MATERIAL_PRICE_DELTA) {
      return {
        kind: 'price_move',
        text: 'price ' + (priceDelta > 0 ? '+' : '') + priceDelta.toFixed(0)
      };
    }
    return null;
  }

  function snapshotState(row, activeAlertId, now) {
    return {
      activeAlertId: activeAlertId,
      fingerprint: String(row.canonicalFingerprint),
      edge: edgeValue(row),
      price: priceOf(row),
      oddsUpdatedAt: String(row.oddsUpdatedAt),
      updatedAt: now
    };
  }

  function reconcileAlert(row, now) {
    var candidateId = String(row.canonicalCandidateId);
    var snapshotId = alertIdentity(row);
    var previous = state.alertCandidates[candidateId];
    var changed = false;
    var suppressed = false;

    if (!previous || !state.alertLedger[previous.activeAlertId]) {
      if (!state.alertLedger[snapshotId]) {
        state.alertLedger[snapshotId] = {
          status: 'new',
          kind: 'new_opportunity',
          movement: '',
          createdAt: now,
          updatedAt: now
        };
      }
      state.alertCandidates[candidateId] = snapshotState(row, snapshotId, now);
      return { id: snapshotId, changed: true, suppressed: false };
    }

    if (previous.fingerprint === String(row.canonicalFingerprint)) {
      return { id: previous.activeAlertId, changed: false, suppressed: false };
    }

    var movement = materialMovement(previous, row);
    if (movement) {
      var priorAlert = state.alertLedger[previous.activeAlertId];
      if (priorAlert && priorAlert.status !== 'dismissed') {
        priorAlert.status = 'superseded';
        priorAlert.updatedAt = now;
      }
      state.alertLedger[snapshotId] = {
        status: 'new',
        kind: movement.kind,
        movement: movement.text,
        createdAt: now,
        updatedAt: now
      };
      state.alertCandidates[candidateId] = snapshotState(row, snapshotId, now);
      changed = true;
    } else {
      state.alertCandidates[candidateId] = snapshotState(row, previous.activeAlertId, now);
      changed = true;
      suppressed = true;
    }
    return {
      id: state.alertCandidates[candidateId].activeAlertId,
      changed: changed,
      suppressed: suppressed
    };
  }

  function reconcileAlerts(rows) {
    var now = new Date().toISOString();
    var changed = false;
    var suppressed = 0;
    var records = rows.map(function (row) {
      var result = reconcileAlert(row, now);
      changed = changed || result.changed;
      if (result.suppressed) suppressed += 1;
      return { row: row, id: result.id, ledger: state.alertLedger[result.id] };
    });
    if (changed) persistAlertState();
    return { records: records, suppressed: suppressed };
  }

  function alertExplanation(row, ledger) {
    var parts = [];
    if (state.watchlist.has(String(row.player || '').toLowerCase())) parts.push('saved player');
    parts.push('preferred market');
    parts.push((edgeValue(row) || 0).toFixed(1) + '% edge');
    parts.push(bookOf(row) + ' ' + (priceOf(row) > 0 ? '+' : '') + priceOf(row));
    parts.push(freshnessLabel(row));
    if (ledger.movement) parts.push(ledger.movement);
    return parts.join(' · ');
  }

  function alertKindLabel(ledger) {
    if (ledger.status === 'seen') return 'SEEN';
    return {
      new_opportunity: 'NEW',
      edge_up: 'EDGE UP',
      edge_down: 'EDGE DOWN',
      price_move: 'PRICE MOVE'
    }[ledger.kind] || 'NEW';
  }

  function alertHtml(record) {
    var row = record.row;
    var ledger = record.ledger;
    var market = MARKET_OPTIONS.find(function (item) { return item.key === marketKeyOf(row); });
    return '<article class="alert-card" data-alert-id="' + esc(record.id) + '" data-alert-kind="' + esc(ledger.kind) + '">' +
      '<div><span class="alert-state">' + esc(alertKindLabel(ledger)) + '</span>' +
      '<strong>' + esc(row.player) + ' · ' + esc(market ? market.label : marketKeyOf(row)) + '</strong>' +
      '<small>' + esc(alertExplanation(row, ledger)) + '</small></div>' +
      '<div class="alert-actions">' +
      (ledger.status === 'new' ? '<button type="button" data-alert-action="seen">Seen</button>' : '') +
      '<button type="button" data-alert-action="dismiss">Dismiss</button></div></article>';
  }

  function renderAlerts() {
    var matches = thresholdMatches();
    var eligible = matches.filter(isAlertFresh);
    var reconciled = reconcileAlerts(eligible);
    var visible = reconciled.records.filter(function (record) {
      return ['dismissed', 'superseded'].indexOf(record.ledger.status) === -1;
    });
    var unread = visible.filter(function (record) {
      return record.ledger.status === 'new';
    }).length;
    var staleSuppressed = matches.length - eligible.length;
    document.getElementById('alertMetric').textContent = String(unread);
    document.getElementById('alertMetricDetail').textContent = 'fresh ≤15m · material changes';
    document.getElementById('alertSummary').textContent =
      unread + ' new · ' + visible.length + ' active · ' + staleSuppressed +
      ' stale suppressed · ' + reconciled.suppressed + ' quiet refreshes';

    var host = document.getElementById('alertList');
    if (!visible.length) {
      host.innerHTML = '<div class="empty-state">No fresh, materially distinct alerts match your preferences right now.</div>';
      return;
    }
    host.innerHTML = visible.slice(0, 12).map(alertHtml).join('');
  }

  function activeAlertFor(row) {
    var candidate = state.alertCandidates[String(row.canonicalCandidateId)];
    return candidate && state.alertLedger[candidate.activeAlertId];
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
      persistAlertState();
      renderAlerts();
    });
    document.getElementById('markAllAlertsSeen').addEventListener('click', function () {
      var now = new Date().toISOString();
      eligibleAlerts().forEach(function (row) {
        var item = activeAlertFor(row);
        if (item && item.status === 'new') {
          item.status = 'seen';
          item.updatedAt = now;
        }
      });
      persistAlertState();
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
    wireSignalActions();
    wireAlertInbox();
    renderAlerts();

    var failures = 0;
    Promise.all([
      requestJson('/api/edges/today?minEdge=0.03').then(function (payload) {
        var rows = payload && Array.isArray(payload.edges) ? payload.edges : [];
        state.edgeState = String(payload.computationState || 'ready').toLowerCase();
        state.edges = rows.filter(isActionable);
        renderSignals();
      }).catch(function () {
        failures += 1;
        state.edgeState = 'unavailable';
        state.edges = [];
        renderSignals();
      }),
      requestJson('/api/calibration/markets').then(function (payload) { state.markets = payload; renderValidation(); })
        .catch(function () { failures += 1; state.markets = null; renderValidation(); }),
      requestJson('/api/tracker/performance?window=30').then(function (payload) { state.tracker = payload; renderTracker(); })
        .catch(function () { failures += 1; state.tracker = null; renderTracker(); })
    ]).then(function () { updateHero(failures); });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
