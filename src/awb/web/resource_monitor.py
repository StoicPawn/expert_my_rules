from __future__ import annotations

import html
import json
import os
import threading
import time
import urllib.request
from pathlib import Path

from fastapi.responses import HTMLResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware

from awb.web.app import app

_CPU_LOCK = threading.Lock()
_CPU_PREVIOUS: tuple[int, int] | None = None
_OLLAMA_LOCK = threading.Lock()
_OLLAMA_CACHE: tuple[float, str] = (0.0, "checking…")


def _read_meminfo(path: Path = Path("/proc/meminfo")) -> dict[str, int]:
    values: dict[str, int] = {}
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if ":" not in line:
                continue
            key, raw = line.split(":", 1)
            parts = raw.strip().split()
            if not parts:
                continue
            try:
                values[key] = int(parts[0]) * 1024
            except ValueError:
                continue
    except OSError:
        return {}
    return values


def _read_cpu_totals(path: Path = Path("/proc/stat")) -> tuple[int, int] | None:
    try:
        first = path.read_text(encoding="utf-8", errors="replace").splitlines()[0]
    except (OSError, IndexError):
        return None
    parts = first.split()
    if not parts or parts[0] != "cpu":
        return None
    try:
        counters = [int(value) for value in parts[1:]]
    except ValueError:
        return None
    if len(counters) < 4:
        return None
    total = sum(counters)
    idle = counters[3] + (counters[4] if len(counters) > 4 else 0)
    return total, idle


def _cpu_percent() -> float | None:
    global _CPU_PREVIOUS
    current = _read_cpu_totals()
    if current is None:
        return None
    with _CPU_LOCK:
        previous = _CPU_PREVIOUS
        _CPU_PREVIOUS = current
    if previous is None:
        return None
    delta_total = current[0] - previous[0]
    delta_idle = current[1] - previous[1]
    if delta_total <= 0:
        return None
    busy = max(0, delta_total - max(0, delta_idle))
    return min(100.0, max(0.0, busy * 100.0 / delta_total))


def _ollama_model() -> str:
    global _OLLAMA_CACHE
    now = time.monotonic()
    with _OLLAMA_LOCK:
        cached_at, cached_value = _OLLAMA_CACHE
        if now - cached_at < 8.0:
            return cached_value

    base = os.environ.get("OLLAMA_BASE_URL", "http://ollama:11434").rstrip("/")
    value = "unavailable"
    try:
        with urllib.request.urlopen(f"{base}/api/ps", timeout=1.0) as response:
            payload = json.loads(response.read().decode("utf-8"))
        models = payload.get("models") or []
        names = [str(item.get("name") or item.get("model") or "").strip() for item in models]
        names = [name for name in names if name]
        value = ", ".join(names) if names else "none loaded"
    except Exception:
        value = "unavailable"

    with _OLLAMA_LOCK:
        _OLLAMA_CACHE = (now, value)
    return value


def _gib(value: int) -> str:
    return f"{value / (1024 ** 3):.1f} GiB"


def _resource_snapshot() -> dict[str, object]:
    mem = _read_meminfo()
    total = int(mem.get("MemTotal", 0))
    available = int(mem.get("MemAvailable", 0))
    if not available:
        available = int(mem.get("MemFree", 0)) + int(mem.get("Buffers", 0)) + int(mem.get("Cached", 0))
    used = max(0, total - available) if total else 0
    ram_percent = used * 100.0 / total if total else None

    swap_total = int(mem.get("SwapTotal", 0))
    swap_free = int(mem.get("SwapFree", 0))
    swap_used = max(0, swap_total - swap_free) if swap_total else 0

    return {
        "ram_used": used,
        "ram_total": total,
        "ram_percent": ram_percent,
        "swap_used": swap_used,
        "swap_total": swap_total,
        "cpu_percent": _cpu_percent(),
        "model": _ollama_model(),
    }


def _resource_html() -> str:
    snap = _resource_snapshot()
    ram_total = int(snap["ram_total"] or 0)
    if ram_total:
        ram = f"{_gib(int(snap['ram_used']))} / {_gib(ram_total)}"
        if snap["ram_percent"] is not None:
            ram += f" ({float(snap['ram_percent']):.0f}%)"
    else:
        ram = "unavailable"

    cpu = "sampling…" if snap["cpu_percent"] is None else f"{float(snap['cpu_percent']):.0f}%"

    swap_total = int(snap["swap_total"] or 0)
    swap = f"{_gib(int(snap['swap_used']))} / {_gib(swap_total)}" if swap_total else "disabled / 0 GiB"
    model = html.escape(str(snap["model"]))
    label = html.escape(os.environ.get("AWB_RESOURCE_LABEL", "ACEPC").strip() or "ACEPC")

    return (
        f"<div class='machine-resource-grid'>"
        f"<div><span>RAM</span><b>{html.escape(ram)}</b></div>"
        f"<div><span>CPU</span><b>{html.escape(cpu)}</b></div>"
        f"<div><span>Swap</span><b>{html.escape(swap)}</b></div>"
        f"<div><span>Active Ollama model</span><b>{model}</b></div>"
        f"</div><div class='muted machine-resource-note'>Host-visible Linux metrics for {label}. CPU is sampled between refreshes; the first reading may show “sampling…”.</div>"
    )


@app.get("/project/{slug}/system-resources", response_class=HTMLResponse)
def project_system_resources(slug: str):
    return HTMLResponse(_resource_html(), headers={"Cache-Control": "no-store"})


RESOURCE_INJECTION = r"""
<style>
#machine-resource-card{border:1px solid #dedee5}.machine-resource-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.machine-resource-grid>div{background:#f7f7f8;border-radius:10px;padding:10px 12px;min-width:0}.machine-resource-grid span{display:block;color:#666;font-size:12px;margin-bottom:4px}.machine-resource-grid b{display:block;overflow-wrap:anywhere;font-size:15px}.machine-resource-note{font-size:12px;margin-top:10px}@media(max-width:640px){.machine-resource-grid{grid-template-columns:1fr}}
</style>
<div class='panel' id='machine-resource-card'><h2>ACEPC resources</h2><p class='muted'>Live RAM, CPU, swap and loaded Ollama model. Read-only monitoring; it does not alter the autonomous run.</p><div id='machine-resource-values'><div class='muted'>Loading machine resources…</div></div></div>
<script>
(function(){
 const parts=window.location.pathname.split('/').filter(Boolean);
 if(parts.length!==2 || parts[0]!=='project') return;
 const slug=encodeURIComponent(parts[1]);
 let busy=false;
 let last='';
 async function refreshResources(){
   if(busy) return;
   busy=true;
   const host=document.getElementById('machine-resource-values');
   if(!host){busy=false;return;}
   try{
     const r=await fetch('/project/'+slug+'/system-resources',{cache:'no-store'});
     if(!r.ok) return;
     const next=await r.text();
     if(next!==last){last=next;host.innerHTML=next;}
   }catch(e){
     if(!last) host.innerHTML='<div class="muted">Machine resource metrics temporarily unavailable.</div>';
   }finally{busy=false;}
 }
 refreshResources();
 setInterval(refreshResources,5000);
})();
</script>
"""


class ResourceMonitorInjectionMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        path = request.url.path.rstrip("/")
        parts = [part for part in path.split("/") if part]
        if request.method != "GET" or len(parts) != 2 or parts[0] != "project":
            return response
        content_type = response.headers.get("content-type", "")
        if "text/html" not in content_type:
            return response
        body = b""
        async for chunk in response.body_iterator:
            body += chunk
        text = body.decode("utf-8", errors="replace")
        text = text.replace("</body>", RESOURCE_INJECTION + "</body>")
        headers = dict(response.headers)
        headers.pop("content-length", None)
        return Response(content=text, status_code=response.status_code, headers=headers, media_type="text/html")


app.add_middleware(ResourceMonitorInjectionMiddleware)
