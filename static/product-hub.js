(function () {
  'use strict';

  var WATCHLIST_KEY = 'mlb_watchlist';
  var MARKET_KEY = 'mlb_market_preferences';
  var THRESHOLD_KEY = 'mlb_alert_edge_threshold';
  var ALERT_LEDGER_KEY = 'mlb_alert_ledger';
  var ALERT_CANDIDATE_STATE_KEY = 'mlb_alert_candidate_state';
  var ALERT_LEDGER_LIMIT = 200;
  var MAX_ALERT_ODDS_AGE_SECONDS = 900;
  var DAILY_DECISION_BOARD_VERSION = '5.5';
  var RECOMMENDATION_EVIDENCE_VERSION = '4.69';
  var VERIFIED_DECISION_DRAFT_VERSION = '4.71';
  var VERIFIED_DECISION_DRAFT_KEY = 'mlb_verified_decision_draft_v471';
  var PAGE_LOADED_AT = Date.now();
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
    edgePayload: null,
    edgeState: 'loading',
    markets: null,
    tracker: null,
    watchlist: new Set(),
    preferred: new Set(),
    preferenceReceipt: null,
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
    state.preferred = new Set(savedMarkets.map(function (key) {
      return String(key || '');
    }).filter(isSupportedMarketKey));
    var savedThreshold = number(readString(THRESHOLD_KEY, ''));
    state.threshold = savedThreshold != null ? savedThreshold : 5;
    state.alertLedger = cleanAlertLedger(readJson(ALERT_LEDGER_KEY, {}));
    state.alertCandidates = cleanAlertCandidates(readJson(ALERT_CANDIDATE_STATE_KEY, {}));
  }

  function isSupportedMarketKey(key) {
    return MARKET_OPTIONS.some(function (item) { return item.key === key; });
  }

  function syncMarketPreferenceControls() {
    document.querySelectorAll('[data-market-preference-key]').forEach(function (button) {
      var key = String(button.getAttribute('data-market-preference-key') || '');
      var preferred = isSupportedMarketKey(key) && state.preferred.has(key);
      button.setAttribute('aria-pressed', preferred ? 'true' : 'false');
      if (button.classList.contains('market-learning-preference')) {
        button.textContent = preferred ? 'Preferred' : 'Add preference';
      }
    });
  }

  function renderMarketPreferenceReceipt() {
    var host = document.getElementById('marketPreferenceReceipt');
    var text = document.getElementById('marketPreferenceReceiptText');
    var undo = document.getElementById('undoMarketPreference');
    if (!host || !text || !undo) return;
    var receipt = state.preferenceReceipt;
    if (!receipt) {
      host.setAttribute('data-receipt-state', 'idle');
      text.textContent = 'Your next explicit market change will appear here.';
      undo.hidden = true;
      return;
    }
    if (!isSupportedMarketKey(receipt.marketKey)) {
      host.setAttribute('data-receipt-state', 'unavailable');
      text.textContent = 'Preference receipt unavailable; no additional change was made.';
      undo.hidden = true;
      return;
    }

    var label = marketLabelOf({ canonicalMarketKey: receipt.marketKey });
    var action = receipt.current ? 'added to' : 'removed from';
    var source = receipt.source === 'market_learning' ? 'Learn' : 'Your markets';
    var impact = state.edgeState === 'ready'
      ? personalizedEdges().length + ' current actionable signal' +
        (personalizedEdges().length === 1 ? '' : 's') + ' match your preferences.'
      : 'Matching signal count is unavailable until current edges are ready.';
    if (receipt.state === 'undone') {
      action = receipt.current ? 'restored to' : 'removed again from';
      text.textContent = label + ' was ' + action + ' your preferences. ' + impact;
      host.setAttribute('data-receipt-state', 'undone');
      undo.hidden = true;
      return;
    }
    text.textContent = label + ' was ' + action + ' your preferences from ' +
      source + '. ' + impact;
    host.setAttribute('data-receipt-state', 'applied');
    undo.hidden = false;
  }

  function applyPreferredMarket(key, preferred) {
    if (!isSupportedMarketKey(key)) return false;
    if (preferred) state.preferred.add(key);
    else state.preferred.delete(key);
    writeJson(MARKET_KEY, Array.from(state.preferred));
    syncMarketPreferenceControls();
    renderSignals();
    return true;
  }

  function setPreferredMarket(key, preferred, source) {
    if (!isSupportedMarketKey(key) || state.preferred.has(key) === preferred) return;
    state.preferenceReceipt = {
      state: 'applied',
      marketKey: key,
      previous: state.preferred.has(key),
      current: preferred,
      source: source === 'market_learning' ? 'market_learning' : 'preferences'
    };
    applyPreferredMarket(key, preferred);
  }

  function undoMarketPreferenceChange() {
    var receipt = state.preferenceReceipt;
    if (!receipt || receipt.state !== 'applied' ||
        !isSupportedMarketKey(receipt.marketKey)) return;
    var restored = receipt.previous;
    if (!applyPreferredMarket(receipt.marketKey, restored)) return;
    state.preferenceReceipt = {
      state: 'undone',
      marketKey: receipt.marketKey,
      previous: receipt.current,
      current: restored,
      source: 'undo'
    };
    renderMarketPreferenceReceipt();
  }

  function wireMarketPreferenceReceipt() {
    var undo = document.getElementById('undoMarketPreference');
    if (!undo) return;
    undo.addEventListener('click', undoMarketPreferenceChange);
  }

  function renderMarketOptions() {
    var host = document.getElementById('marketOptions');
    host.innerHTML = '';
    MARKET_OPTIONS.forEach(function (item) {
      var button = document.createElement('button');
      button.type = 'button';
      button.className = 'market-chip';
      button.textContent = item.label;
      button.setAttribute('data-market-preference-key', item.key);
      button.setAttribute('aria-pressed', state.preferred.has(item.key) ? 'true' : 'false');
      button.addEventListener('click', function () {
        setPreferredMarket(item.key, !state.preferred.has(item.key), 'preferences');
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

  function decisionDraftFrom(row) {
    if (!isActionable(row)) return null;
    var receipt = row.evidenceReceipt;
    var receiptAge = number(receipt.price.ageSeconds);
    var elapsedSeconds = Math.max(0, (Date.now() - PAGE_LOADED_AT) / 1000);
    var currentAge = receiptAge == null ? null : receiptAge + elapsedSeconds;
    if (currentAge == null || currentAge > MAX_ALERT_ODDS_AGE_SECONDS) return null;

    var probabilityValue = probability(receipt.model.probability);
    var edge = number(receipt.market.edge);
    var price = number(receipt.price.american);
    var line = number(receipt.selection.line);
    if (probabilityValue == null || edge == null || price == null || line == null) return null;

    var preparedAt = Date.now();
    var remainingSeconds = Math.max(0, MAX_ALERT_ODDS_AGE_SECONDS - currentAge);
    return {
      version: VERIFIED_DECISION_DRAFT_VERSION,
      state: 'prepared',
      preparedAt: new Date(preparedAt).toISOString(),
      expiresAt: new Date(preparedAt + remainingSeconds * 1000).toISOString(),
      receiptVersion: receipt.contractVersion,
      canonicalCandidateId: row.canonicalCandidateId,
      canonicalFingerprint: row.canonicalFingerprint,
      player: String(row.player || ''),
      team: String(row.team || ''),
      opp: String(row.opp || row.opponent || ''),
      marketKey: marketKeyOf(row),
      line: line,
      recommendedSide: String(receipt.selection.side || ''),
      adjProb: probabilityValue,
      edge: edge,
      modelMean: number(row.modelMean != null ? row.modelMean : row.projection),
      bookmaker: String(receipt.price.book || ''),
      marketPrice: price,
      bestAvailablePrice: price,
      bestAvailableBook: String(receipt.price.book || ''),
      oddsObservedAt: String(receipt.price.observedAt || ''),
      reason: String(receipt.explanation || ''),
      serverMutation: false
    };
  }

  function writeVerifiedDecisionDraft(draft) {
    try {
      window.localStorage.setItem(VERIFIED_DECISION_DRAFT_KEY, JSON.stringify(draft));
      var stored = JSON.parse(window.localStorage.getItem(VERIFIED_DECISION_DRAFT_KEY) || 'null');
      return Boolean(stored && stored.version === VERIFIED_DECISION_DRAFT_VERSION &&
        stored.canonicalCandidateId === draft.canonicalCandidateId);
    } catch (error) {
      return false;
    }
  }

  function prepareDecisionDraft(candidateId, origin) {
    var draftOrigin = origin === 'eligible_alert' ? 'eligible_alert' : 'saved_player_digest';
    var row = state.edges.find(function (item) {
      return String(item.canonicalCandidateId || '') === String(candidateId || '');
    });
    var draft = decisionDraftFrom(row);
    var summary = document.getElementById(
      draftOrigin === 'eligible_alert' ? 'alertSummary' : 'savedOpportunitySummary'
    );
    if (!draft) {
      if (summary) summary.textContent = 'That opportunity is no longer fresh and no tracking draft was created.';
      return;
    }
    draft.preparedFrom = draftOrigin;
    if (!writeVerifiedDecisionDraft(draft)) {
      if (summary) summary.textContent = 'This device could not store the draft; no tracking action was taken.';
      return;
    }
    window.location.assign('/tracker?decisionDraft=4.71');
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
      '<span>VERIFIED RECEIPT ' + esc(receipt.contractVersion) + '</span>' +
      '<button type="button" class="saved-opportunity-track" data-prepare-track="' +
        esc(row.canonicalCandidateId) + '">Review in Tracker</button></article>';
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

  function personalizedSignalReasons(row) {
    var marketKey = marketKeyOf(row);
    if (!isActionable(row) || !isSupportedMarketKey(marketKey) ||
        !state.preferred.has(marketKeyOf(row))) return [];
    var reasons = [{ key: 'preferred_market', label: 'Preferred market' }];
    if (state.watchlist.has(playerKey(row.player))) {
      reasons.push({ key: 'saved_player', label: 'Saved player' });
    }
    return reasons;
  }

  function personalizedEdges() {
    var preferred = state.edges.filter(function (row) {
      return personalizedSignalReasons(row).length > 0;
    });
    return preferred.sort(function (left, right) {
      var leftSaved = state.watchlist.has(playerKey(left.player)) ? 1 : 0;
      var rightSaved = state.watchlist.has(playerKey(right.player)) ? 1 : 0;
      if (leftSaved !== rightSaved) return rightSaved - leftSaved;
      return (edgeValue(right) || 0) - (edgeValue(left) || 0);
    });
  }

  function friendlyAdmissionReason(reason) {
    var value = String(reason || '').toLowerCase();
    if (/price|book|odds|quote/.test(value)) return 'Missing or stale sportsbook quote';
    if (/calibrat|market gate|promot/.test(value)) return 'Calibration or market gate blocked';
    if (/identity|entity|player|team|lineup|pitcher|handed/.test(value)) return 'Identity or lineup evidence failed';
    if (/edge|expected value|\bev\b|threshold/.test(value)) return 'Edge or EV below threshold';
    if (/receipt|fingerprint|evidence/.test(value)) return 'Decision evidence receipt incomplete';
    if (/simulation|matchup/.test(value)) return 'Simulation or matchup evidence incomplete';
    if (/probability|projection|model/.test(value)) return 'Projection evidence incomplete';
    var label = String(reason || 'Other validation gate').replace(/[_-]+/g, ' ').trim();
    return label.charAt(0).toUpperCase() + label.slice(1);
  }

  function auditObjects(payload) {
    var result = [];
    ['candidateIntegrityAudit', 'actionabilityAudit'].forEach(function (key) {
      var audit = payload && payload[key];
      if (audit && typeof audit === 'object' && !Array.isArray(audit)) result.push(audit);
    });
    var collections = payload && payload.canonicalCandidateAudit &&
      payload.canonicalCandidateAudit.collections;
    if (collections && typeof collections === 'object') {
      Object.keys(collections).forEach(function (key) {
        var audit = collections[key];
        if (audit && typeof audit === 'object' && !Array.isArray(audit)) result.push(audit);
      });
    }
    return result;
  }

  function admissionReasons(payload) {
    var totals = {};
    auditObjects(payload).forEach(function (audit) {
      var reasons = audit.rejectionReasons;
      if (!reasons || typeof reasons !== 'object' || Array.isArray(reasons)) return;
      Object.keys(reasons).forEach(function (reason) {
        var count = number(reasons[reason]);
        if (count == null || count <= 0) return;
        var label = friendlyAdmissionReason(reason);
        totals[label] = (totals[label] || 0) + count;
      });
    });
    return Object.keys(totals).map(function (label) {
      return { label: label, count: totals[label] };
    }).sort(function (left, right) {
      return right.count - left.count || left.label.localeCompare(right.label);
    });
  }

  function auditRejectedCount(payload) {
    return auditObjects(payload).reduce(function (largest, audit) {
      var count = number(audit.rejectedCount);
      return count == null ? largest : Math.max(largest, count);
    }, 0);
  }

  function dailyDecisionRows() {
    return state.edges.slice().sort(function (left, right) {
      return (edgeValue(right) || 0) - (edgeValue(left) || 0);
    }).slice(0, 8);
  }

  function decisionBoardCardHtml(row) {
    var receipt = row.evidenceReceipt;
    var modelProb = probability(receipt.model.probability);
    var fairProb = probability(receipt.market.fairProbability);
    var edge = edgeValue(row);
    var price = number(receipt.price.american);
    var player = playerKey(row.player);
    var saved = state.watchlist.has(player);
    var context = [row.team, row.matchup].filter(Boolean).join(' · ');
    return '<article class="decision-card" data-board-version="' +
      DAILY_DECISION_BOARD_VERSION + '">' +
      '<div class="decision-card-top"><span>VERIFIED PLAY</span><small>' +
      esc(freshnessLabel(row)) + '</small></div>' +
      '<div class="decision-card-title"><div><strong>' + esc(row.player) +
      '</strong><small>' + esc(context) + '</small></div>' +
      '<b>+' + (edge == null ? '—' : edge.toFixed(1)) + '% EDGE</b></div>' +
      '<p class="decision-selection">' + esc(marketLabelOf(row)) + ' · ' +
      esc(receipt.selection.side) + ' ' + esc(receipt.selection.line) + '</p>' +
      '<div class="decision-proof">' +
      '<span><small>MODEL</small><b>' + (modelProb * 100).toFixed(1) + '%</b></span>' +
      '<span><small>FAIR MARKET</small><b>' + (fairProb * 100).toFixed(1) + '%</b></span>' +
      '<span><small>BEST PRICE</small><b>' + esc(receipt.price.book) + ' ' +
      (price > 0 ? '+' : '') + esc(price) + '</b></span>' +
      '</div>' +
      '<p class="decision-explanation"><strong>Why it qualifies:</strong> ' +
      esc(receipt.explanation) + '</p>' +
      '<div class="decision-card-actions"><button type="button" data-board-save-player="' +
      esc(player) + '" aria-pressed="' + (saved ? 'true' : 'false') + '">' +
      (saved ? '★ Saved player' : '☆ Save player') +
      '</button><a href="/value-bets">Review full evidence →</a></div>' +
      '</article>';
  }

  function renderAdmissionSummary(boardState) {
    var host = document.getElementById('admissionReasonList');
    var text = document.getElementById('admissionSummaryText');
    var reasons = admissionReasons(state.edgePayload);
    if (reasons.length) {
      text.textContent = 'Aggregate gate results from today’s candidate audits. Rejected rows remain hidden.';
      host.innerHTML = reasons.slice(0, 6).map(function (reason) {
        return '<div class="admission-reason"><span>' + esc(reason.label) +
          '</span><strong>' + esc(reason.count) + '</strong></div>';
      }).join('');
      return;
    }
    var messages = {
      computing: 'The durable scan is still running. No interim candidates are promoted.',
      unavailable: 'Required evidence is unavailable. The board is failing closed.',
      no_bet: 'The scan is ready, but no candidate cleared every required gate.',
      verified_plays: 'Displayed plays cleared every gate; nonqualifying candidates remain hidden.'
    };
    text.textContent = messages[boardState] || 'Waiting for candidate audits…';
    host.innerHTML = '<div class="admission-reason neutral"><span>No aggregate rejection counts reported</span><strong>—</strong></div>';
  }

  function renderDailyDecisionBoard() {
    var board = document.getElementById('dailyDecisionBoard');
    if (!board) return;
    var rows = dailyDecisionRows();
    var sourceState = String(state.edgeState || 'loading').toLowerCase();
    var boardState = ['loading', 'computing'].indexOf(sourceState) >= 0 ? 'computing' :
      ['failed', 'unavailable'].indexOf(sourceState) >= 0 ? 'unavailable' :
      rows.length ? 'verified_plays' : 'no_bet';
    var payloadMessage = state.edgePayload && String(state.edgePayload.message || '').trim();
    var copy = {
      computing: {
        status: 'COMPUTING',
        headline: 'Scanning today’s market',
        detail: payloadMessage || 'No recommendation is shown while the durable scan is incomplete.'
      },
      unavailable: {
        status: 'UNAVAILABLE',
        headline: 'Decision evidence is unavailable',
        detail: payloadMessage || 'The board is failing closed. Refresh after the evidence source recovers.'
      },
      no_bet: {
        status: 'NO BET',
        headline: 'No verified play qualifies right now',
        detail: 'The scan is ready. No candidate cleared identity, price, freshness, calibration, edge, and receipt gates.'
      },
      verified_plays: {
        status: 'VERIFIED',
        headline: rows.length + ' verified opportunit' + (rows.length === 1 ? 'y' : 'ies') + ' cleared every gate',
        detail: 'Ranked by canonical model edge. Always review the current sportsbook quote before tracking.'
      }
    }[boardState];

    board.setAttribute('data-board-state', boardState);
    document.getElementById('decisionBoardStatus').textContent = copy.status;
    document.getElementById('decisionBoardHeadline').textContent = copy.headline;
    document.getElementById('decisionBoardDetail').textContent = copy.detail;
    document.getElementById('boardQualifiedCount').textContent =
      boardState === 'computing' ? '—' : String(rows.length);
    document.getElementById('boardRejectedCount').textContent =
      boardState === 'computing' ? '—' : String(auditRejectedCount(state.edgePayload));
    document.getElementById('boardSourceState').textContent = sourceState.toUpperCase();

    var host = document.getElementById('decisionBoardList');
    if (boardState === 'verified_plays') {
      host.innerHTML = rows.map(decisionBoardCardHtml).join('');
    } else {
      host.innerHTML = '<div class="decision-board-empty" data-empty-state="' +
        esc(boardState) + '"><strong>' + esc(copy.headline) + '</strong><span>' +
        esc(copy.detail) + '</span></div>';
    }
    renderAdmissionSummary(boardState);
  }

  function wireDailyDecisionBoard() {
    var host = document.getElementById('decisionBoardList');
    var refresh = document.getElementById('decisionBoardRefresh');
    if (host) {
      host.addEventListener('click', function (event) {
        var button = event.target.closest('[data-board-save-player]');
        if (!button) return;
        var name = button.getAttribute('data-board-save-player');
        setPlayerSaved(name, !state.watchlist.has(playerKey(name)));
      });
    }
    if (refresh) refresh.addEventListener('click', function () { window.location.reload(); });
  }

  function signalHtml(row) {
    var reasons = personalizedSignalReasons(row);
    if (!reasons.length) return '';
    var reasonKeys = reasons.map(function (reason) { return reason.key; });
    var reasonLabels = reasons.map(function (reason) { return reason.label; });
    var receipt = row.evidenceReceipt;
    var edge = edgeValue(row);
    var modelProb = probability(receipt.model.probability);
    var fairProb = probability(receipt.market.fairProbability);
    var price = number(receipt.price.american);
    var player = playerKey(row.player);
    var saved = state.watchlist.has(player);
    return '<article class="signal-card' + (saved ? ' watchlisted' : '') +
      '" data-personalization-reasons="' + esc(reasonKeys.join(',')) + '">' +
      '<div><div class="signal-title"><strong>' + esc(row.player) + '</strong>' +
      '<button type="button" class="signal-save" data-toggle-player="' + esc(player) +
        '" aria-pressed="' + (saved ? 'true' : 'false') + '" aria-label="' +
        (saved ? 'Remove ' : 'Save ') + esc(row.player) + '">' +
        (saved ? '★ Saved' : '☆ Save') + '</button></div>' +
      '<p class="signal-market">' + esc(marketLabelOf(row)) + ' · ' + esc(receipt.selection.side) + ' ' + esc(receipt.selection.line) + '</p>' +
      '<p class="signal-provenance" aria-label="Personalization reasons: ' +
        esc(reasonLabels.join(', ')) + '"><strong>Shown because</strong>' +
        reasons.map(function (reason) {
          return '<span data-personalization-reason="' + esc(reason.key) + '">' +
            esc(reason.label) + '</span>';
        }).join('') + '</p>' +
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
    renderDailyDecisionBoard();
    var list = personalizedEdges();
    var host = document.getElementById('signalList');
    document.getElementById('actionableCount').textContent = String(state.edges.length);
    document.getElementById('signalFoot').textContent = list.length + ' preferred-market signal' + (list.length === 1 ? '' : 's') + '; saved players rank first.';
    renderSavedOpportunityDigest();
    renderAlerts();
    renderMarketPreferenceReceipt();
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

  function wireSavedOpportunityActions() {
    document.getElementById('savedOpportunityList').addEventListener('click', function (event) {
      var button = event.target.closest('[data-prepare-track]');
      if (!button) return;
      prepareDecisionDraft(button.getAttribute('data-prepare-track'), 'saved_player_digest');
    });
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

  function alertEligibilityReasons(row, ledger) {
    if (!row || !ledger) return [];
    var marketKey = marketKeyOf(row);
    var edge = edgeValue(row);
    var validKinds = ['new_opportunity', 'edge_up', 'edge_down', 'price_move'];
    var activeStates = ['new', 'seen'];
    if (!isActionable(row) || !isSupportedMarketKey(marketKey) ||
        !state.preferred.has(marketKeyOf(row)) ||
        edge == null || edge < state.threshold ||
        !isAlertFresh(row) || !ledger ||
        activeStates.indexOf(ledger.status) === -1 ||
        validKinds.indexOf(ledger.kind) === -1) return [];
    return [
      { key: 'preferred_market', label: 'Preferred market' },
      { key: 'threshold_match', label: 'Threshold ' + state.threshold + '%+' },
      { key: 'fresh_quote', label: 'Fresh quote ≤15m' },
      {
        key: 'eligible_event',
        label: ledger.kind === 'new_opportunity' ? 'New opportunity' : 'Material change'
      }
    ];
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
    var reasons = alertEligibilityReasons(row, ledger);
    if (!reasons.length) return '';
    var reasonKeys = reasons.map(function (reason) { return reason.key; });
    var reasonLabels = reasons.map(function (reason) { return reason.label; });
    var market = MARKET_OPTIONS.find(function (item) { return item.key === marketKeyOf(row); });
    return '<article class="alert-card" data-alert-id="' + esc(record.id) +
      '" data-alert-kind="' + esc(ledger.kind) +
      '" data-alert-provenance="' + esc(reasonKeys.join(',')) + '">' +
      '<div><span class="alert-state">' + esc(alertKindLabel(ledger)) + '</span>' +
      '<strong>' + esc(row.player) + ' · ' + esc(market ? market.label : marketKeyOf(row)) + '</strong>' +
      '<small>' + esc(alertExplanation(row, ledger)) + '</small>' +
      '<p class="alert-provenance" aria-label="Alert eligibility reasons: ' +
        esc(reasonLabels.join(', ')) + '"><strong>Alert because</strong>' +
        reasons.map(function (reason) {
          return '<span data-alert-provenance-reason="' + esc(reason.key) + '">' +
            esc(reason.label) + '</span>';
        }).join('') + '</p></div>' +
      '<div class="alert-actions">' +
      '<button type="button" class="alert-review" data-prepare-alert-track="' +
        esc(row.canonicalCandidateId) + '">Review in Tracker</button>' +
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
    var cards = visible.slice(0, 12).map(alertHtml).filter(Boolean);
    if (!cards.length) {
      host.innerHTML = '<div class="empty-state">No fresh, materially distinct alerts match your preferences right now.</div>';
      return;
    }
    host.innerHTML = cards.join('');
  }

  function prepareAlertDecisionDraft(candidateId, alertId) {
    var row = state.edges.find(function (item) {
      return String(item.canonicalCandidateId || '') === String(candidateId || '');
    });
    var ledger = state.alertLedger[String(alertId || '')];
    if (!alertEligibilityReasons(row, ledger).length) {
      document.getElementById('alertSummary').textContent =
        'That alert is no longer eligible and no tracking draft was created.';
      return;
    }
    prepareDecisionDraft(candidateId, 'eligible_alert');
  }

  function activeAlertFor(row) {
    var candidate = state.alertCandidates[String(row.canonicalCandidateId)];
    return candidate && state.alertLedger[candidate.activeAlertId];
  }

  function wireAlertInbox() {
    document.getElementById('alertList').addEventListener('click', function (event) {
      var review = event.target.closest('[data-prepare-alert-track]');
      var card = event.target.closest('[data-alert-id]');
      if (review) {
        if (!card) return;
        prepareAlertDecisionDraft(
          review.getAttribute('data-prepare-alert-track'),
          card.getAttribute('data-alert-id')
        );
        return;
      }
      var button = event.target.closest('[data-alert-action]');
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

  function renderVerifiedDecisionMarketLearning(learning) {
    var host = document.getElementById('verifiedDecisionMarketLearning');
    var list = document.getElementById('marketLearningList');
    var marketLearning = learning && learning.marketLearning;
    var supported = MARKET_OPTIONS.map(function (item) { return item.key; });
    var validStates = ['no_market_history', 'awaiting_outcomes', 'learning', 'sample_ready'];
    var valid = marketLearning &&
      marketLearning.version === '4.73' &&
      marketLearning.aggregateOnly === true &&
      marketLearning.trackerRowsIncluded === false &&
      marketLearning.rankingEnabled === false &&
      marketLearning.preferenceMutation === false &&
      marketLearning.recommendation === false &&
      marketLearning.failClosed === true &&
      validStates.indexOf(String(marketLearning.state || '')) >= 0 &&
      Array.isArray(marketLearning.markets) &&
      marketLearning.markets.length <= supported.length;
    var seen = {};
    if (valid) {
      valid = marketLearning.markets.every(function (item) {
        var key = String(item && item.marketKey || '');
        var decisions = number(item && item.decisionCount);
        var graded = number(item && item.gradedCount);
        var stateName = String(item && item.state || '');
        var ok = supported.indexOf(key) >= 0 && !seen[key] &&
          decisions != null && decisions >= 0 &&
          graded != null && graded >= 0 && graded <= decisions &&
          ['awaiting_outcomes', 'learning', 'sample_ready'].indexOf(stateName) >= 0;
        seen[key] = true;
        return ok;
      });
    }
    if (!host || !list || !valid) {
      if (host) host.setAttribute('data-market-learning-state', 'unavailable');
      document.getElementById('marketLearningState').textContent = 'UNAVAILABLE';
      if (list) list.innerHTML = '<div class="empty-state">No market conclusion is shown.</div>';
      document.getElementById('marketLearningNote').textContent =
        'Market attribution is unavailable or malformed; preferences remain unchanged.';
      return;
    }

    var stateName = String(marketLearning.state);
    host.setAttribute('data-market-learning-state', stateName);
    document.getElementById('marketLearningState').textContent =
      stateName === 'no_market_history' ? 'NO HISTORY' :
      stateName === 'awaiting_outcomes' ? 'AWAITING OUTCOMES' :
      stateName === 'sample_ready' ? 'SAMPLE READY' : 'LEARNING';

    if (!marketLearning.markets.length) {
      list.innerHTML = '<div class="empty-state">No verified market history in this window.</div>';
      document.getElementById('marketLearningNote').textContent =
        'Market samples appear after verified decisions are tracked; no preference is changed automatically.';
      return;
    }

    list.innerHTML = marketLearning.markets.map(function (item) {
      var key = String(item.marketKey);
      var label = marketLabelOf({ canonicalMarketKey: key });
      var beatClose = number(item.beatCloseRate);
      var closeText = beatClose == null ? 'CLV —' :
        'Beat close ' + ((beatClose <= 1 ? beatClose * 100 : beatClose).toFixed(1)) + '%';
      var preferred = state.preferred.has(key);
      return '<div class="market-learning-row" data-sample-ready="' +
        (item.sampleReady === true ? 'true' : 'false') + '">' +
        '<strong>' + esc(label) + '</strong><span>' +
        esc(item.gradedCount) + ' graded / ' + esc(item.decisionCount) +
        ' decisions</span><b>' + esc(closeText) + '</b>' +
        '<button type="button" class="market-learning-preference" ' +
        'data-market-learning-preference="' + esc(key) + '" ' +
        'data-market-preference-key="' + esc(key) + '" aria-pressed="' +
        (preferred ? 'true' : 'false') + '" aria-label="' +
        (preferred ? 'Remove ' : 'Add ') + esc(label) + ' market preference">' +
        (preferred ? 'Preferred' : 'Add preference') + '</button></div>';
    }).join('');
    syncMarketPreferenceControls();
    document.getElementById('marketLearningNote').textContent =
      'Markets stay in canonical order. Tap to review a device preference; performance never changes it automatically.';
  }

  function wireMarketLearningActions() {
    var list = document.getElementById('marketLearningList');
    if (!list) return;
    list.addEventListener('click', function (event) {
      var button = event.target.closest('[data-market-learning-preference]');
      if (!button || !list.contains(button)) return;
      var key = String(button.getAttribute('data-market-learning-preference') || '');
      setPreferredMarket(key, !state.preferred.has(key), 'market_learning');
    });
  }

  function renderVerifiedDecisionLearning() {
    var host = document.getElementById('verifiedDecisionLearning');
    var learning = state.tracker && state.tracker.verifiedDecisionLearning;
    var validStates = ['no_verified_decisions', 'awaiting_outcomes', 'learning', 'sample_ready'];
    var valid = learning &&
      learning.version === '4.72' &&
      learning.source === 'my_hub_verified_decision_draft' &&
      learning.aggregateOnly === true &&
      learning.rowsIncluded === false &&
      learning.failClosed === true &&
      validStates.indexOf(String(learning.state || '')) >= 0;
    var stateName = valid ? String(learning.state) : 'unavailable';
    var decisions = valid ? number(learning.decisionCount) : null;
    var pending = valid ? number(learning.pendingCount) : null;
    var graded = valid ? number(learning.gradedCount) : null;
    var beatClose = valid ? number(learning.beatCloseRate) : null;
    if (!host || decisions == null || pending == null || graded == null) {
      if (host) host.setAttribute('data-learning-state', 'unavailable');
      document.getElementById('learningState').textContent = 'UNAVAILABLE';
      document.getElementById('learningDecisions').textContent = '—';
      document.getElementById('learningPending').textContent = '—';
      document.getElementById('learningGraded').textContent = '—';
      document.getElementById('learningBeatClose').textContent = '—';
      document.getElementById('learningNote').textContent =
        'Source-attributed learning is unavailable; no conclusion is shown.';
      renderVerifiedDecisionMarketLearning(null);
      return;
    }

    host.setAttribute('data-learning-state', stateName);
    renderVerifiedDecisionMarketLearning(learning);
    document.getElementById('learningDecisions').textContent = String(Math.round(decisions));
    document.getElementById('learningPending').textContent = String(Math.round(pending));
    document.getElementById('learningGraded').textContent = String(Math.round(graded));
    document.getElementById('learningBeatClose').textContent =
      beatClose == null ? '—' : ((beatClose <= 1 ? beatClose * 100 : beatClose).toFixed(1) + '%');

    if (stateName === 'no_verified_decisions') {
      document.getElementById('learningState').textContent = 'NO DECISIONS';
      document.getElementById('learningNote').textContent =
        'No decisions saved through the verified My Hub handoff are in this 30-day window.';
    } else if (stateName === 'awaiting_outcomes') {
      document.getElementById('learningState').textContent = 'AWAITING OUTCOMES';
      document.getElementById('learningNote').textContent =
        'Verified decisions are tracked, but none are graded; no performance conclusion is available.';
    } else if (stateName === 'learning') {
      var minimum = number(learning.minimumGradedSample) || 10;
      var remaining = Math.max(0, minimum - graded);
      document.getElementById('learningState').textContent = 'LEARNING';
      document.getElementById('learningNote').textContent =
        Math.round(graded) + ' graded decision' + (graded === 1 ? '' : 's') + '; ' +
        Math.round(remaining) + ' more before the source sample is ready. Early metrics are descriptive only.';
    } else {
      document.getElementById('learningState').textContent = 'SAMPLE READY';
      document.getElementById('learningNote').textContent =
        Math.round(graded) + ' graded verified decisions are ready for source-level review in Tracker.';
    }
  }

  function renderTracker() {
    var payload = state.tracker;
    if (!payload) {
      document.getElementById('trackNote').textContent = 'Tracker evidence is unavailable. Open Tracker to review saved decisions.';
      renderVerifiedDecisionLearning();
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
    renderVerifiedDecisionLearning();
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
    renderMarketPreferenceReceipt();
    renderWatchlist();
    renderDailyDecisionBoard();
    wireDailyDecisionBoard();
    wireWatchlistForm();
    wireSignalActions();
    wireSavedOpportunityActions();
    wireMarketLearningActions();
    wireMarketPreferenceReceipt();
    wireAlertInbox();
    renderAlerts();

    var failures = 0;
    Promise.all([
      requestJson('/api/edges/today?minEdge=0.03').then(function (payload) {
        var rows = payload && Array.isArray(payload.edges) ? payload.edges : [];
        state.edgePayload = payload;
        state.edgeState = String(payload.computationState || 'ready').toLowerCase();
        state.edges = rows.filter(isActionable);
        renderSignals();
      }).catch(function () {
        failures += 1;
        state.edgePayload = null;
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
