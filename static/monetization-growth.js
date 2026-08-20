(function () {
  'use strict';

  var STATUS_URL = '/api/monetization/status';
  var FALLBACK_KEYS = {
    onboarding: 'mlb_growth_onboarding_v511',
    referrals: 'mlb_growth_referral_v511',
    events: 'mlb_growth_events_v511'
  };
  var EVENT_ALLOWLIST = [
    'pricing_viewed',
    'premium_interest',
    'onboarding_step_completed',
    'referral_landed'
  ];

  function safeParse(raw, fallback) {
    try { return JSON.parse(raw); } catch (_) { return fallback; }
  }

  function readStorage(key, fallback) {
    try { return safeParse(window.localStorage.getItem(key), fallback); } catch (_) { return fallback; }
  }

  function writeStorage(key, value) {
    try {
      window.localStorage.setItem(key, JSON.stringify(value));
      return true;
    } catch (_) { return false; }
  }

  function receiptId() {
    if (window.crypto && typeof window.crypto.randomUUID === 'function') return window.crypto.randomUUID();
    return 'receipt-' + Date.now() + '-' + Math.random().toString(16).slice(2);
  }

  function eventKey(contract) {
    return contract && contract.conversionAnalytics && contract.conversionAnalytics.storageKey || FALLBACK_KEYS.events;
  }

  function recordEvent(contract, name, detail) {
    if (EVENT_ALLOWLIST.indexOf(name) === -1) return;
    var key = eventKey(contract);
    var maximum = contract && contract.conversionAnalytics && contract.conversionAnalytics.maximumReceipts || 100;
    var receipts = readStorage(key, []);
    if (!Array.isArray(receipts)) receipts = [];
    receipts.push({
      id: receiptId(),
      event: name,
      occurredAt: new Date().toISOString(),
      detail: String(detail || '').slice(0, 48)
    });
    writeStorage(key, receipts.slice(-maximum));
    renderReceiptSummary(contract);
  }

  function captureReferral(contract) {
    var referral = new URLSearchParams(window.location.search).get('ref');
    if (!referral || !/^[A-Za-z0-9_-]{3,32}$/.test(referral)) return;
    var key = contract.referrals.storageKey || FALLBACK_KEYS.referrals;
    var current = readStorage(key, null);
    if (current && current.code === referral) return;
    writeStorage(key, { code: referral, capturedAt: new Date().toISOString() });
    recordEvent(contract, 'referral_landed', referral);
  }

  function createFeatureList(features) {
    var list = document.createElement('ul');
    (features || []).forEach(function (feature) {
      var item = document.createElement('li');
      item.textContent = feature;
      list.appendChild(item);
    });
    return list;
  }

  function renderPlans(contract) {
    var grid = document.getElementById('planGrid');
    if (!grid) return;
    grid.replaceChildren();
    contract.plans.forEach(function (plan) {
      var card = document.createElement('article');
      card.className = 'plan-card ' + (plan.key === 'premium' ? 'premium' : 'free');
      var top = document.createElement('div');
      top.className = 'plan-top';
      var label = document.createElement('span');
      label.textContent = plan.availability === 'available' ? 'AVAILABLE NOW' : 'PREVIEW';
      var title = document.createElement('h2');
      title.textContent = plan.label;
      var price = document.createElement('strong');
      price.textContent = plan.price || (plan.key === 'free' ? '$0' : 'Price not set');
      top.append(label, title, price);
      card.append(top, createFeatureList(plan.features));
      if (plan.key === 'free') {
        var link = document.createElement('a');
        link.href = '/workspace';
        link.textContent = 'Open My Hub';
        card.appendChild(link);
      } else {
        var button = document.createElement('button');
        button.type = 'button';
        button.textContent = 'Preview Premium';
        button.addEventListener('click', function () {
          recordEvent(contract, 'premium_interest', 'pricing');
          button.textContent = 'Interest saved on this device';
          button.disabled = true;
        });
        card.appendChild(button);
      }
      grid.appendChild(card);
    });
  }

  function renderBilling(contract) {
    var state = document.getElementById('growthRolloutState');
    if (state) {
      state.setAttribute('data-state', contract.rolloutState);
      state.querySelector('strong').textContent = contract.billing.checkoutAvailable ? 'Premium checkout ready' : 'Premium preview only';
      state.querySelector('span').textContent = contract.billing.checkoutAvailable ? 'Paid access is bound to a verified server entitlement.' : 'Free remains available; no paid entitlement can be granted yet.';
    }
    var copy = document.getElementById('billingReceiptCopy');
    if (copy) copy.textContent = 'Checkout: unavailable · Entitlement source: server-verified subscription · Client storage cannot unlock Premium.';
    var list = document.getElementById('billingBlockers');
    if (list) {
      list.replaceChildren();
      contract.billing.blockers.forEach(function (blocker) {
        var item = document.createElement('li');
        item.textContent = blocker;
        list.appendChild(item);
      });
    }
  }

  function onboardingKey(contract) {
    return contract && contract.onboarding && contract.onboarding.storageKey || FALLBACK_KEYS.onboarding;
  }

  function renderOnboarding(contract) {
    var root = document.getElementById('growthOnboarding');
    if (!root) return;
    var progress = readStorage(onboardingKey(contract), {});
    if (!progress || Array.isArray(progress) || typeof progress !== 'object') progress = {};
    root.querySelectorAll('[data-onboarding-step]').forEach(function (button) {
      var step = button.getAttribute('data-onboarding-step');
      var complete = progress[step] === true;
      button.setAttribute('aria-pressed', complete ? 'true' : 'false');
      button.querySelector('b').textContent = complete ? 'DONE' : 'MARK DONE';
      button.onclick = function () {
        progress[step] = !complete;
        writeStorage(onboardingKey(contract), progress);
        if (!complete) recordEvent(contract, 'onboarding_step_completed', step);
        renderOnboarding(contract);
      };
    });
    var completed = contract.onboarding.steps.filter(function (step) { return progress[step] === true; }).length;
    var meter = document.getElementById('growthOnboardingMeter');
    if (meter) {
      meter.textContent = completed + ' / ' + contract.onboarding.steps.length;
      meter.setAttribute('data-complete', completed === contract.onboarding.steps.length ? 'true' : 'false');
    }
  }

  function renderReceiptSummary(contract) {
    var target = document.getElementById('growthReceiptSummary');
    if (!target) return;
    var receipts = readStorage(eventKey(contract), []);
    if (!Array.isArray(receipts)) receipts = [];
    var counts = {};
    receipts.forEach(function (receipt) { counts[receipt.event] = (counts[receipt.event] || 0) + 1; });
    target.textContent = receipts.length
      ? receipts.length + ' local receipts · ' + (counts.premium_interest || 0) + ' Premium interest · ' + (counts.referral_landed || 0) + ' referral landings'
      : 'No local conversion receipts yet.';
  }

  function renderHubStatus(contract) {
    var state = document.getElementById('growthEntitlementState');
    if (state) state.textContent = contract.billing.checkoutAvailable ? 'CHECKOUT READY' : 'PREVIEW ONLY';
    var limit = document.getElementById('growthFreeLimit');
    if (limit) limit.textContent = contract.freeUsage.configuredLimit == null ? 'Not configured' : String(contract.freeUsage.configuredLimit) + ' / day (shadow)';
    var button = document.getElementById('growthPremiumInterest');
    if (button) button.onclick = function () {
      recordEvent(contract, 'premium_interest', 'workspace');
      button.textContent = 'Interest saved on this device';
      button.disabled = true;
    };
    renderOnboarding(contract);
    renderReceiptSummary(contract);
  }

  function boot() {
    fetch(STATUS_URL, { credentials: 'same-origin', headers: { Accept: 'application/json' } })
      .then(function (response) { if (!response.ok) throw new Error('HTTP ' + response.status); return response.json(); })
      .then(function (contract) {
        captureReferral(contract);
        if (document.body.getAttribute('data-growth-surface') === 'pricing') {
          renderPlans(contract);
          renderBilling(contract);
          recordEvent(contract, 'pricing_viewed', 'pricing');
        }
        renderHubStatus(contract);
      })
      .catch(function () {
        var state = document.getElementById('growthRolloutState');
        if (state) {
          state.setAttribute('data-state', 'unavailable');
          state.querySelector('strong').textContent = 'Plan status unavailable';
          state.querySelector('span').textContent = 'Premium remains locked. Free product pages are unaffected.';
        }
        var hubState = document.getElementById('growthEntitlementState');
        if (hubState) hubState.textContent = 'UNAVAILABLE';
      });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
