from __future__ import annotations

from fastapi.responses import Response
from starlette.middleware.base import BaseHTTPMiddleware

from awb.web.app import app


INJECTION = r"""
<style>
#research-console-card{border:1px solid #dedee5}.research-console-actions{display:flex;gap:10px;flex-wrap:wrap;align-items:center}.research-console-link{display:inline-block;background:#111;color:#fff!important;text-decoration:none;padding:10px 14px;border-radius:10px;font-weight:600}.research-console-note{font-size:13px;color:#666}
</style>
<div class='panel' id='research-console-card'>
  <h2>Research Console</h2>
  <p>Open a dedicated read-only window with exact persisted Worker candidates, Reviewer objections, Director recovery strategies, verification, tool calls and raw ledger JSON. Formula changes between candidate versions are shown with a literal diff.</p>
  <div class='research-console-actions'><a id='research-console-open' class='research-console-link' target='_blank' rel='noopener'>Open Research Console ↗</a><span class='research-console-note'>The research loop keeps running while you inspect it.</span></div>
</div>
<script>
(function(){
 const parts=window.location.pathname.split('/').filter(Boolean);
 if(parts.length!==2 || parts[0]!=='project') return;
 const slug=encodeURIComponent(parts[1]);
 const link=document.getElementById('research-console-open');
 if(link) link.href='/project/'+slug+'/research-console';
 const card=document.getElementById('research-console-card');
 const clarity=document.getElementById('clarity-card');
 const live=document.getElementById('live-activity-card');
 if(card){
   if(clarity && clarity.parentNode) clarity.parentNode.insertBefore(card,clarity.nextSibling);
   else if(live && live.parentNode) live.parentNode.insertBefore(card,live);
 }
})();
</script>
"""


class ResearchConsoleLinkMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        path = request.url.path.rstrip('/')
        parts = [part for part in path.split('/') if part]
        if request.method != 'GET' or len(parts) != 2 or parts[0] != 'project':
            return response
        content_type = response.headers.get('content-type', '')
        if 'text/html' not in content_type:
            return response
        body = b''
        async for chunk in response.body_iterator:
            body += chunk
        text = body.decode('utf-8', errors='replace').replace('</body>', INJECTION + '</body>')
        headers = dict(response.headers)
        headers.pop('content-length', None)
        return Response(content=text, status_code=response.status_code, headers=headers, media_type='text/html')


app.add_middleware(ResearchConsoleLinkMiddleware)
