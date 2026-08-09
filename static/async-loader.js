/* Shared bounded JSON loader for deep-dive pages. */
(function () {
  class RequestBudgetError extends Error {
    constructor(message, code) {
      super(message);
      this.name = 'RequestBudgetError';
      this.code = code;
    }
  }

  window.fetchJsonWithTimeout = async function (url, options) {
    const opts = options || {};
    const timeoutMs = Number(opts.timeoutMs || 8000);
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(url, {
        ...opts,
        signal: controller.signal,
      });
      if (!response.ok) {
        throw new RequestBudgetError(
          'Request failed with HTTP ' + response.status,
          'http_error'
        );
      }
      return await response.json();
    } catch (error) {
      if (error && error.name === 'AbortError') {
        throw new RequestBudgetError(
          'Request timed out after ' + timeoutMs + 'ms',
          'timeout'
        );
      }
      throw error;
    } finally {
      clearTimeout(timer);
    }
  };

  window.renderDeepDiveFallback = function (target, title, detail) {
    const element = typeof target === 'string'
      ? document.getElementById(target)
      : target;
    if (!element) return;
    element.innerHTML =
      '<div class="empty" role="status">' +
      '<div style="font-size:1.4rem;margin-bottom:8px">⚠️</div>' +
      '<div>' + String(title || 'DATA TEMPORARILY UNAVAILABLE') + '</div>' +
      '<div style="font-family:Inter,sans-serif;font-size:.68rem;letter-spacing:0;line-height:1.6;margin-top:8px">' +
      String(detail || 'The request exceeded its response budget. Try again shortly.') +
      '</div></div>';
  };
})();
