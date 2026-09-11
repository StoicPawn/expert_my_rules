from __future__ import annotations

import json

from fastapi import Request
from fastapi.responses import HTMLResponse

from awb.web.control_v3 import control_app, project_v3


# V4 keeps the project document stable. Setup state and ACEPC telemetry are
# updated through the tiny setup-status endpoint, so mobile scroll/tab/form
# state is never lost while a local model is working.
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
    return document.querySelector('[data-setup-card]') ||
      document.querySelector("form[action='/project/" + CSS.escape(project) + "/auto-setup']")?.closest('.card');
  }

  function fmtSeconds(value) {
    const n = Math.max(0, Number(value) || 0);
    if (n < 60) return Math.round(n) + ' s';
    const m = Math.floor(n / 60), s = Math.round(n % 60);
    if (m < 60) return m + 'm ' + s + 's';
    const h = Math.floor(m / 60);
    return h + 'h ' + (m % 60) + 'm';
  }

  function fmtBytes(value) {
    const n = Number(value) || 0;
    if (!n) return '—';
    return (n / 1073741824).toFixed(1) + ' GB';
  }

  function monitorPanel(card) {
    let panel = card.querySelector('[data-setup-monitor]');
    if (panel) return panel;
    panel = document.createElement('div');
    panel.dataset.setupMonitor = '1';
    panel.style.margin = '12px 0';
    panel.innerHTML = `
      <div class="kpis" style="grid-template-columns:repeat(2,minmax(0,1fr))">
        <div class="kpi"><span>Fase</span><b data-kpi="stage" style="font-size:14px">—</b></div>
        <div class="kpi"><span>Tempo</span><b data-kpi="elapsed">—</b></div>
        <div class="kpi"><span>CPU ACEPC</span><b data-kpi="cpu">—</b></div>
        <div class="kpi"><span>RAM ACEPC</span><b data-kpi="ram" style="font-size:14px">—</b></div>
        <div class="kpi"><span>Modello</span><b data-kpi="model" style="font-size:13px">—</b></div>
        <div class="kpi"><span>Attività modello</span><b data-kpi="modelstate" style="font-size:13px">—</b></div>
      </div>
      <div class="small muted" data-kpi="work" style="margin-top:8px"></div>`;
    const form = card.querySelector('form');
    if (form) card.insertBefore(panel, form); else card.appendChild(panel);
    return panel;
  }

  function setKpi(panel, key, value) {
    const node = panel.querySelector('[data-kpi="' + key + '"]');
    if (node) node.textContent = value;
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
    const detail = card.querySelector('[data-setup-detail]');
    if (detail) detail.textContent = String(data.detail || '');

    const panel = monitorPanel(card);
    const r = data.resources || {};
    const p = data.model_progress || {};
    setKpi(panel, 'stage', String(data.stage || status));
    setKpi(panel, 'elapsed', data.elapsed_seconds == null ? '—' : fmtSeconds(data.elapsed_seconds));
    setKpi(panel, 'cpu', r.cpu_percent == null ? 'sampling…' : Math.round(Number(r.cpu_percent)) + '%');
    const ram = r.ram_total ? fmtBytes(r.ram_used) + ' / ' + fmtBytes(r.ram_total) + (r.ram_percent == null ? '' : ' · ' + Math.round(Number(r.ram_percent)) + '%') : '—';
    setKpi(panel, 'ram', ram);
    setKpi(panel, 'model', String(p.model || r.model || '—'));
    setKpi(panel, 'modelstate', String(p.state || data.liveness || '—'));
    const stats = [];
    if (p.chunks != null) stats.push(String(p.chunks) + ' chunk');
    if (p.output_chars != null) stats.push(String(p.output_chars) + ' caratteri output');
    if (p.last_stream_activity_seconds != null) stats.push('ultimo segnale ' + fmtSeconds(p.last_stream_activity_seconds) + ' fa');
    if (data.liveness === 'ACTIVE' && Number(data.elapsed_seconds || 0) > 900) stats.push('lento, ma il modello risponde');
    if (data.liveness === 'DEGRADED') stats.push('attenzione: health check del modello in errore');
    setKpi(panel, 'work', stats.join(' · '));

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
    if (status === 'READY' || status === 'ERROR' || status === 'DEFERRED') stopped = true;
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
      timer = window.setTimeout(poll, 12000);
      return;
    }
    inFlight = true;
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 3000);
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

  timer = window.setTimeout(poll, 500);
})();
</script>
"""


@control_app.get('/project/{project}', response_class=HTMLResponse)
def project_v4(request: Request, project: str):
    response = project_v3(request, project)
    html = response.body.decode('utf-8')
    html = html.replace('setTimeout(()=>location.reload(),2500);', '')
    script = _POLL_SCRIPT.replace('__PROJECT_JSON__', json.dumps(project))
    html = html.replace('</body></html>', script + '</body></html>', 1)
    return HTMLResponse(html, headers={'Cache-Control': 'no-store'})
