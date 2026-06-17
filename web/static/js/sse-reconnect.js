/**
 * harvester-ops — reconnecting EventSource helper
 *
 * Backend `/api/stream/<runId>` replays the full event log from index 0 on
 * every connection (see api_stream in web/app.py), so a dropped connection
 * is recoverable without explicit Last-Event-ID negotiation: reconnecting
 * yields the same stream, and the caller can choose to dedupe or just
 * accept that already-rendered lines may appear again on a real outage
 * (which is fine — these are short-lived action runs, not chat streams).
 *
 * Usage:
 *   const es = SSEReconnect.connect('/api/stream/abc123', {
 *     on: {
 *       step:   (ev) => { … },
 *       log:    (ev) => { … },
 *       status: (ev) => { … },
 *       end:    (ev) => { … },   // last event from server — closes cleanly
 *     },
 *     onStatus: (s) => console.log('SSE', s.state),
 *     maxRetries: 5,        // default 5
 *     baseDelay:  1000,     // default 1000ms — backoff: 1, 2, 4, 8, 16
 *     maxDelay:   30000,    // default 30000ms
 *   });
 *   …
 *   es.close();   // caller can abort
 *
 * Status payloads passed to onStatus():
 *   { state: 'connecting', attempt: N }
 *   { state: 'open' }
 *   { state: 'retry', attempt: N, delay: ms }
 *   { state: 'dead',  attempts: N }     // retries exhausted
 *   { state: 'closed' }                  // caller called close() or 'end' arrived
 */
(function (root) {
  function connect(url, opts) {
    opts = opts || {};
    const handlers   = opts.on || {};
    const onStatus   = typeof opts.onStatus === 'function' ? opts.onStatus : null;
    const maxRetries = (typeof opts.maxRetries === 'number') ? opts.maxRetries : 5;
    const baseDelay  = (typeof opts.baseDelay  === 'number') ? opts.baseDelay  : 1000;
    const maxDelay   = (typeof opts.maxDelay   === 'number') ? opts.maxDelay   : 30000;

    let es = null;
    let attempt = 0;
    let closed = false;
    let endedNormally = false;
    let retryTimer = null;

    function fire(state, extra) {
      if (!onStatus) return;
      try { onStatus(Object.assign({ state }, extra || {})); } catch {}
    }

    function open() {
      if (closed) return;
      fire('connecting', { attempt });
      try {
        es = new EventSource(url);
      } catch (err) {
        scheduleRetry();
        return;
      }
      es.onopen = () => {
        attempt = 0;
        fire('open');
      };
      es.onerror = () => {
        if (closed || endedNormally) return;
        safeClose();
        scheduleRetry();
      };
      // Wire user handlers. The 'end' event is special: the backend sends it
      // exactly once at the end of a run; we mark endedNormally to suppress
      // the EventSource auto-onerror that fires when the server closes the
      // connection right after sending 'end'.
      Object.keys(handlers).forEach((name) => {
        es.addEventListener(name, (ev) => {
          if (name === 'end') {
            endedNormally = true;
            try { handlers[name](ev); } finally {
              safeClose();
              fire('closed');
            }
            return;
          }
          try { handlers[name](ev); } catch {}
        });
      });
    }

    function scheduleRetry() {
      if (closed || endedNormally) return;
      if (attempt >= maxRetries) {
        fire('dead', { attempts: attempt });
        return;
      }
      const delay = Math.min(maxDelay, baseDelay * Math.pow(2, attempt));
      attempt += 1;
      fire('retry', { attempt, delay });
      retryTimer = setTimeout(open, delay);
    }

    function safeClose() {
      if (retryTimer) { clearTimeout(retryTimer); retryTimer = null; }
      if (es) { try { es.close(); } catch {} es = null; }
    }

    function close() {
      if (closed) return;
      closed = true;
      safeClose();
      if (!endedNormally) fire('closed');
    }

    open();
    return { close, get readyState() { return es ? es.readyState : -1; } };
  }

  root.SSEReconnect = { connect };
})(window);
