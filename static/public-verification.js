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

  document.querySelectorAll('[data-window]').forEach(function (button) {
    button.addEventListener('click', function () {
      state.window = Number(button.dataset.window);
      document.querySelectorAll('[data-window]').forEach(function (peer) {
        var active = peer === button;
        peer.classList.toggle('active', active);
        peer.setAttribute('aria-pressed', active ? 'true' : 'false');
      });
      loadLedger();
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
})();
