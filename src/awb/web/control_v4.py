from __future__ import annotations

from fastapi import Request
from fastapi.responses import HTMLResponse

from awb.web.control_v3 import control_app, project_v3


# Control Center v3 refreshed the whole project page every 2.5 seconds while the
# automatic setup was RUNNING/QUEUED. On mobile this stole scroll/tab state and,
# under local-inference load, could leave Safari on ERR_CONNECTION_CLOSED. V4
# keeps the document stable and polls only the tiny setup-status JSON endpoint.
def _remove_project_get() -> None:
    control_app.router.routes[:] = [
        route for route in control_app.router.routes
        if not (
            getattr(route, 'path', None) == '/project/{project}'
            and 'GET' in (getattr(route, 'methods', None) or set())
        )
    ]


_remove_project_get()


_POLL_SCRIPT = r"""
<script>
(() => {
  const project = __PROJECT_JSON__;
  const endpoint = '/project/' + encodeURIComponent(project) + '/setup-status';
  let stopped = false;
  let delayMs = 4000;
  let timer = null;
  let inFlight = false;

  function setupCard() {
    const form = document.querySelector("form[action='/project/" + CSS.escape(project) + "/auto-setup']");
    return form ? form.closest('.card') : null;
  }

  function paint(data) {
    const card = setupCard();
    if (!card) return;
    const status = String(data.status || 'NOT_STARTED');
    const labels = {
      READY: 'SETUP PRONTO', RUNNING: 'SETUP IN CORSO', QUEUED: 'SETUP IN CODA',
      DEFERRED: 'SETUP RINVIATO', ERROR: 'ERRORE SETUP', NOT_STARTED: 'NON AVVIATO'
    };
    const badge = card.querySelector('.badge');
    if (badge) {
      badge.textContent = labels[status] || status;
      badge.classList.remove('ok', 'warn', 'bad');
      badge.classList.add(status === 'READY' ? 'ok' : status === 'ERROR' ? 'bad' : 'warn');
    }
    const paragraphs = card.querySelectorAll('p');
    if (paragraphs.length) paragraphs[paragraphs.length - 1].textContent = String(data.detail || '');

    let action = card.querySelector('[data-setup-refresh]');
    if ((status === 'READY' || status === 'ERROR') && !action) {
      action = document.createElement('button');
      action.type = 'button';
      action.className = 'secondary';
      action.dataset.setupRefresh = '1';
      action.textContent = status === 'READY' ? 'Carica setup aggiornato' : 'Ricarica dettagli';
      action.addEventListener('click', () => window.location.reload());
      card.appendChild(action);
    }
    if (status === 'READY' || status === 'ERROR' || status === 'DEFERRED') {
      stopped = true;
    }
  }

  function showOffline() {
    const card = setupCard();
    if (!card) return;
    let note = card.querySelector('[data-ui-connection-note]');
    if (!note) {
      note = document.createElement('p');
      note.dataset.uiConnectionNote = '1';
      note.className = 'small muted';
      card.appendChild(note);
    }
    note.textContent = 'Connessione al backend momentaneamente non disponibile. La pagina resta utilizzabile; ritento automaticamente.';
  }

  function clearOffline() {
    const note = setupCard()?.querySelector('[data-ui-connection-note]');
    if (note) note.remove();
  }

  async function poll() {
    if (stopped || inFlight) return;
    if (document.hidden) {
      timer = window.setTimeout(poll, 10000);
      return;
    }
    inFlight = true;
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 2500);
    try {
      const response = await fetch(endpoint, {
        cache: 'no-store',
        headers: {'Accept': 'application/json'},
        signal: controller.signal,
      });
      if (!response.ok) throw new Error('HTTP ' + response.status);
      const data = await response.json();
      clearOffline();
      delayMs = 4000;
      paint(data);
    } catch (_) {
      showOffline();
      delayMs = Math.min(30000, Math.round(delayMs * 1.7));
    } finally {
      window.clearTimeout(timeout);
      inFlight = false;
      if (!stopped) timer = window.setTimeout(poll, delayMs);
    }
  }

  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && !stopped) {
      if (timer) window.clearTimeout(timer);
      timer = window.setTimeout(poll, 250);
    }
  });

  timer = window.setTimeout(poll, 800);
})();
</script>
"""


@control_app.get('/project/{project}', response_class=HTMLResponse)
def project_v4(request: Request, project: str):
    response = project_v3(request, project)
    html = response.body.decode('utf-8')

    # V3 may have injected this exact full-page refresh when setup is active.
    # Remove it unconditionally. The user must never lose scroll/tab/form state.
    html = html.replace('setTimeout(()=>location.reload(),2500);', '')

    import json
    script = _POLL_SCRIPT.replace('__PROJECT_JSON__', json.dumps(project))
    html = html.replace('</body></html>', script + '</body></html>', 1)
    return HTMLResponse(html, headers={'Cache-Control': 'no-store'})
