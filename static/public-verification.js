(function () {
  'use strict';

  var state = { window: 90, filter: 'all', rows: [] };
  var byId = function (id) { return document.getElementById(id); };

  function percent(value, signed) {
    if (value == null || !isFinite(value)) return '—';
    var scaled = value * 100;
    var prefix = signed && scaled > 0 ? '+' : '';
    return prefix + scaled.toFixed(1) + '%';
  }

  function number(value, digits) {
    return value == null || !isFinite(value) ? '—' : Number(value).toFixed(digits);
  }

  function american(value) {
    if (value == null) return '—';
    return (value > 0 ? '+' : '') + String(value);
  }

  function releasedAt(value) {
    var parsed = new Date(value);
    if (isNaN(parsed.getTime())) return 'Unknown release time';
    return parsed.toLocaleString('en-US', {
      month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', timeZoneName: 'short'
    });
  }

  function setText(id, value) {
    var node = byId(id);
    if (node) node.textContent = value;
  }

  function renderMetrics(metrics) {
    setText('recordMetric', metrics.wins + '–' + metrics.losses);
    setText('recordSample', metrics.gradedCount + ' graded · ' + metrics.pending + ' pending');
    setText('roiMetric', percent(metrics.roi, true));
    setText('roiSample', metrics.roiEligibleCount + ' ROI-graded');
    setText('brierMetric', number(metrics.brierScore, 3));
    setText('brierSample', metrics.gradedCount + ' graded');
    setText('eceMetric', percent(metrics.ece, false));
    setText('clvMetric', percent(metrics.averageClvEdge, true));
    setText('clvSample', metrics.clvGradedCount + ' CLV-graded');
    setText('beatCloseMetric', percent(metrics.beatCloseRate, false));
    byId('metrics').hidden = false;
  }

  function accuracyMessage(stateName) {
    if (stateName === 'market_leading') return 'The model cleared the paired Brier and Beat Close confidence gates.';
    if (stateName === 'not_market_leading') return 'The verified evidence does not support a market-leading accuracy claim.';
    if (stateName === 'insufficient_clv_sample') return 'Paired accuracy is promising, but the verified CLV sample is still too small.';
    return 'The verified paired sample is still below the 500-decision claim threshold.';
  }

  function renderAccuracy(payload) {
    var overall = payload.overall || {};
    var coverage = payload.coverage || {};
    var stateNode = byId('accuracyGateState');
    setText('accuracyPairedSample', String(overall.pairedSampleSize || 0));
    setText('accuracyModelBrier', number(overall.modelBrier, 3));
    setText('accuracyClosingBrier', number(overall.closingMarketBrier, 3));
    setText('accuracyBrierDelta', number(overall.pairedBrierDelta, 3));
    setText('accuracyBeatClose', percent(overall.beatCloseRate, false));
    setText('accuracyClvSample', (overall.clvGradedCount || 0) + ' CLV-graded');
    setText('accuracyClaim', payload.industryClaimMade === true ? 'Supported' : 'Withheld');
    setText('accuracyGateCopy', accuracyMessage(payload.state));
    setText('accuracyCoverage',
      (coverage.pairedEligibleCount || 0) + ' paired · ' +
      (coverage.rejectedCount || 0) + ' withheld · no row-level Tracker data published'
    );
    if (stateNode) {
      stateNode.textContent = String(payload.state || 'unavailable').replace(/_/g, ' ');
      stateNode.className = 'accuracy-state accuracy-state--' + String(payload.state || 'unavailable');
    }
  }

  function renderAccuracyUnavailable() {
    var stateNode = byId('accuracyGateState');
    if (stateNode) {
      stateNode.textContent = 'Unavailable';
      stateNode.className = 'accuracy-state accuracy-state--unavailable';
    }
    setText('accuracyGateCopy', 'The paired accuracy contract could not be verified, so no model-vs-market claim is shown.');
    setText('accuracyClaim', 'Withheld');
  }

  function phaseState(value) {
    return String(value || 'insufficient_sample').replace(/_/g, ' ');
  }

  function renderIntelligence(payload) {
    var phases = payload.phases || {};
    var coverage = payload.coverage || {};
    var atlas = phases.errorAtlas || {};
    var challengers = phases.championChallenger || {};
    var drift = phases.driftControl || {};
    var simulation = phases.simulationCalibration || {};
    var policy = phases.policyLab || {};
    var stateNode = byId('intelligenceState');
    var challengerRows = Array.isArray(challengers.challengers) ? challengers.challengers : [];
    var marketRows = drift.markets && typeof drift.markets === 'object' ? Object.keys(drift.markets).map(function (key) { return drift.markets[key]; }) : [];
    var proposals = Array.isArray(policy.proposals) ? policy.proposals : [];
    var interventions = marketRows.filter(function (row) { return row && row.recommendedAction !== 'none'; }).length;
    var reviewCandidates = challengerRows.filter(function (row) { return row && row.state === 'review_candidate'; }).length;
    var policyCandidates = proposals.filter(function (row) { return row && row.state === 'review_candidate'; }).length;
    var simulationMetrics = simulation.simulation || {};

    if (stateNode) {
      stateNode.textContent = phaseState(payload.state);
      stateNode.className = 'accuracy-state accuracy-state--' + String(payload.state || 'insufficient_sample');
    }
    setText('intelligenceCopy', payload.state === 'ready'
      ? 'Verified pre-outcome evidence now drives cohort diagnosis, shadow evaluation, drift interventions, simulation checks, and review-only policy proposals.'
      : 'The program is live, but evidence-gated outputs remain withheld until their required samples verify.');
    setText('errorAtlasState', phaseState(atlas.state));
    setText('errorAtlasDetail', (Array.isArray(atlas.cohorts) ? atlas.cohorts.length : 0) + ' visible cohorts · ' + (atlas.suppressedCohortCount || 0) + ' suppressed');
    setText('challengerState', phaseState(challengers.state));
    setText('challengerDetail', reviewCandidates + ' review candidates · ' + challengerRows.length + ' shadow models');
    setText('driftState', phaseState(drift.state));
    setText('driftDetail', interventions ? interventions + ' market interventions' : 'No market intervention');
    setText('simulationState', phaseState(simulation.state));
    setText('simulationDetail', (simulationMetrics.sampleSize || 0) + ' verified observations · ' + ((simulation.correlationPairs || []).length) + ' measured pairs');
    setText('policyState', phaseState(policy.state));
    setText('policyDetail', policyCandidates + ' review proposals · ' + proposals.length + ' markets tested');
    setText('intelligenceCoverage',
      (coverage.verifiedObservationCount || 0) + ' verified · ' +
      (coverage.rejectedObservationCount || 0) + ' withheld · read only · no automatic model, probability, threshold, or staking changes'
    );
  }

  function renderIntelligenceUnavailable() {
    var stateNode = byId('intelligenceState');
    if (stateNode) {
      stateNode.textContent = 'Unavailable';
      stateNode.className = 'accuracy-state accuracy-state--unavailable';
    }
    setText('intelligenceCopy', 'The intelligence contract could not be verified, so diagnostics, interventions, and policy proposals are withheld.');
    setText('intelligenceCoverage', 'Fail closed · no automatic changes');
  }

  function fact(label, value) {
    var node = document.createElement('div');
    node.className = 'fact';
    var name = document.createElement('span');
    var strong = document.createElement('strong');
    name.textContent = label;
    strong.textContent = value;
    node.appendChild(name);
    node.appendChild(strong);
    return node;
  }

  function releaseCard(row) {
    var article = document.createElement('article');
    article.className = 'release';
    article.dataset.result = row.result;

    var top = document.createElement('div');
    top.className = 'release__top';
    var identity = document.createElement('div');
    var player = document.createElement('p');
    player.className = 'release__player';
    player.textContent = row.player;
    var market = document.createElement('p');
    market.className = 'release__market';
    market.textContent = String(row.marketKey || '').replace(/_/g, ' ') + ' · ' + row.side + ' ' + row.line;
    identity.appendChild(player);
    identity.appendChild(market);
    var result = document.createElement('span');
    result.className = 'result result--' + row.result;
    result.textContent = row.result;
    top.appendChild(identity);
    top.appendChild(result);

    var facts = document.createElement('div');
    facts.className = 'release__facts';
    facts.appendChild(fact('Released', releasedAt(row.releasedAt)));
    facts.appendChild(fact('Sportsbook', row.sportsbook));
    facts.appendChild(fact('Release price', american(row.openingPrice)));
    facts.appendChild(fact('Model probability', percent(row.probability, false)));
    facts.appendChild(fact('Closing price', american(row.closingPrice)));
    facts.appendChild(fact('CLV', percent(row.clvEdge, true)));

    var receipt = document.createElement('div');
    receipt.className = 'release__receipt';
    var verified = document.createElement('span');
    verified.textContent = '✓ PUBLICATION ' + row.receiptVersion + ' · PREDICTION ' + row.predictionReceiptVersion;
    var fingerprint = document.createElement('code');
    fingerprint.textContent = row.receiptFingerprint;
    receipt.appendChild(verified);
    receipt.appendChild(fingerprint);

    article.appendChild(top);
    article.appendChild(facts);
    article.appendChild(receipt);
    return article;
  }

  function visible(row) {
    if (state.filter === 'pending') return row.result === 'pending';
    if (state.filter === 'settled') return row.result !== 'pending';
    return true;
  }

  function renderRows() {
    var container = byId('ledgerRows');
    var empty = byId('emptyState');
    container.textContent = '';
    var rows = state.rows.filter(visible);
    rows.forEach(function (row) { container.appendChild(releaseCard(row)); });
    empty.hidden = rows.length !== 0;
  }

  function setStatus(message, error) {
    var node = byId('status');
    node.textContent = message;
    node.className = error ? 'status status--error' : 'status';
    node.hidden = !message;
  }

  function loadLedger() {
    setStatus('Verifying released recommendations…', false);
    fetch('/api/verification/ledger?window=' + state.window, { credentials: 'omit' })
      .then(function (response) {
        if (!response.ok) throw new Error('Verification endpoint returned ' + response.status);
        return response.json();
      })
      .then(function (payload) {
        if (!payload || payload.success !== true || payload.version !== '5.6' || !Array.isArray(payload.ledger)) {
          throw new Error('Verification contract did not validate');
        }
        state.rows = payload.ledger;
        renderMetrics(payload.metrics);
        renderRows();
        setText('windowLabel', payload.window.from + ' through ' + payload.window.through + ' · ' + payload.metrics.releasedCount + ' released');
        setStatus('', false);
        if (window.setNavAsOf) window.setNavAsOf(payload.generatedAt);
      })
      .catch(function () {
        state.rows = [];
        renderRows();
        setStatus('Verification data is unavailable. No performance claim is shown until the receipt ledger can be validated.', true);
      });
  }

  function loadAccuracy() {
    fetch('/api/accuracy/control-plane?window=' + state.window, { credentials: 'omit' })
      .then(function (response) {
        if (!response.ok) throw new Error('Accuracy endpoint returned ' + response.status);
        return response.json();
      })
      .then(function (payload) {
        if (!payload || payload.success !== true || payload.version !== '6.0' || !payload.overall) {
          throw new Error('Accuracy contract did not validate');
        }
        renderAccuracy(payload);
      })
      .catch(renderAccuracyUnavailable);
  }

  function loadIntelligence() {
    fetch('/api/accuracy/intelligence?window=' + state.window, { credentials: 'omit' })
      .then(function (response) {
        if (!response.ok) throw new Error('Intelligence endpoint returned ' + response.status);
        return response.json();
      })
      .then(function (payload) {
        if (!payload || payload.success !== true || payload.version !== '6.5' || !payload.phases || !payload.safety) {
          throw new Error('Intelligence contract did not validate');
        }
        renderIntelligence(payload);
      })
      .catch(renderIntelligenceUnavailable);
  }

  document.querySelectorAll('[data-window]').forEach(function (button) {
    button.addEventListener('click', function () {
      state.window = Number(button.dataset.window);
      document.querySelectorAll('[data-window]').forEach(function (peer) {
        var active = peer === button;
        peer.classList.toggle('active', active);
        peer.setAttribute('aria-pressed', active ? 'true' : 'false');
      });
      loadLedger();
      loadAccuracy();
      loadIntelligence();
    });
  });

  document.querySelectorAll('[data-filter]').forEach(function (button) {
    button.addEventListener('click', function () {
      state.filter = button.dataset.filter;
      document.querySelectorAll('[data-filter]').forEach(function (peer) {
        var active = peer === button;
        peer.classList.toggle('active', active);
        peer.setAttribute('aria-pressed', active ? 'true' : 'false');
      });
      renderRows();
    });
  });

  loadLedger();
  loadAccuracy();
  loadIntelligence();
})();
