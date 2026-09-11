from __future__ import annotations

import html


BASE_CSS = r'''
:root{color-scheme:light;--bg:#f4f6f8;--card:#fff;--ink:#17202a;--muted:#667085;--line:#e4e7ec;--accent:#1d4ed8;--ok:#087443;--warn:#b54708;--bad:#b42318;--soft:#eef4ff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}.shell{max-width:1280px;margin:0 auto;padding:22px}.topbar{display:flex;align-items:center;justify-content:space-between;gap:14px;margin-bottom:18px}.brand{font-weight:800;font-size:20px}.nav{display:flex;gap:8px;flex-wrap:wrap}.nav a,.tab{display:inline-flex;align-items:center;gap:6px;padding:9px 12px;border-radius:10px;text-decoration:none;color:var(--ink);background:#fff;border:1px solid var(--line);font-weight:650}.nav a.active,.tab.active{background:var(--ink);color:#fff;border-color:var(--ink)}h1{font-size:30px;margin:6px 0}h2{font-size:19px;margin:0 0 12px}h3{font-size:15px;margin:0 0 8px}.sub{color:var(--muted);margin:4px 0 18px}.grid{display:grid;grid-template-columns:repeat(12,minmax(0,1fr));gap:14px}.col-12{grid-column:span 12}.col-8{grid-column:span 8}.col-7{grid-column:span 7}.col-6{grid-column:span 6}.col-5{grid-column:span 5}.col-4{grid-column:span 4}.col-3{grid-column:span 3}.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:16px;box-shadow:0 1px 2px rgba(16,24,40,.03)}.hero{padding:20px}.kpis{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}.kpi{background:#f8fafc;border:1px solid var(--line);border-radius:12px;padding:12px}.kpi span{display:block;color:var(--muted);font-size:12px}.kpi b{font-size:20px}.badge{display:inline-flex;align-items:center;padding:4px 8px;border-radius:999px;font-size:12px;font-weight:750;background:#f2f4f7;color:#344054}.badge.ok{background:#ecfdf3;color:var(--ok)}.badge.warn{background:#fffaeb;color:var(--warn)}.badge.bad{background:#fef3f2;color:var(--bad)}.badge.live{background:#eef4ff;color:var(--accent)}form{margin:0}label{display:block;font-size:12px;font-weight:700;color:#475467;margin:9px 0 5px}input,select,textarea{width:100%;padding:10px 11px;border:1px solid #d0d5dd;border-radius:10px;background:#fff;color:var(--ink);font:inherit}textarea{resize:vertical;min-height:76px}button{border:0;border-radius:10px;padding:10px 13px;background:var(--ink);color:#fff;font:inherit;font-weight:750;cursor:pointer}button:disabled{opacity:.45;cursor:not-allowed}button.secondary{background:#eef2f6;color:var(--ink)}button.warn{background:#b54708}button.danger{background:#b42318}.row{display:flex;align-items:center;gap:9px;flex-wrap:wrap}.row>*{min-width:0}.grow{flex:1}.split{display:flex;justify-content:space-between;align-items:flex-start;gap:12px}.muted{color:var(--muted)}.small{font-size:12px}.meter{height:10px;background:#eef2f6;border-radius:999px;overflow:hidden}.meter>i{display:block;height:100%;background:var(--accent);border-radius:999px}.meter.ok>i{background:var(--ok)}.meter.warn>i{background:var(--warn)}.meter.bad>i{background:var(--bad)}.tabs{display:flex;gap:8px;overflow:auto;margin-bottom:14px}.tab{cursor:pointer;white-space:nowrap}.pane{display:none}.pane.active{display:block}.agent{border:1px solid var(--line);border-radius:12px;padding:12px;margin:9px 0}.task{border-left:4px solid #d0d5dd;padding:10px 12px;background:#f9fafb;border-radius:8px;margin:8px 0}.task.done{border-color:var(--ok)}.task.live{border-color:var(--accent);background:#f5f8ff}.task.bad{border-color:var(--bad)}.timeline{display:flex;flex-direction:column;gap:8px}.event{padding:9px 11px;border:1px solid var(--line);border-radius:10px;background:#fff}.event b{display:block;font-size:13px}.event span{font-size:12px;color:var(--muted)}pre.output{white-space:pre-wrap;overflow-wrap:anywhere;background:#101828;color:#f8fafc;border-radius:12px;padding:14px;max-height:420px;overflow:auto;font-size:12px}.linkcard{display:block;text-decoration:none;color:inherit}.linkcard:hover{border-color:#b9c5d8}.empty{padding:18px;border:1px dashed #cfd6df;border-radius:12px;color:var(--muted);text-align:center}.statusdot{width:9px;height:9px;border-radius:50%;display:inline-block;background:#98a2b3}.statusdot.ok{background:#12b76a}.statusdot.bad{background:#f04438}.statusdot.warn{background:#f79009}.hide{display:none!important}
@media(max-width:900px){.col-8,.col-7,.col-6,.col-5,.col-4,.col-3{grid-column:span 12}.kpis{grid-template-columns:repeat(2,minmax(0,1fr))}.shell{padding:14px}.topbar{align-items:flex-start;flex-direction:column}}
'''


def esc(value: object) -> str:
    return html.escape(str(value if value is not None else ''))


def badge(text: object, kind: str = '') -> str:
    return f"<span class='badge {html.escape(kind)}'>{esc(text)}</span>"


def shell(
    title: str,
    body: str,
    *,
    active: str = 'setup',
    extra_head: str = '',
    extra_script: str = '',
    script: str = '',
) -> str:
    """Render the common shell.

    ``script`` is a readable alias for ``extra_script`` used by richer live pages;
    both are concatenated for backwards compatibility.
    """
    del active  # navigation links are supplied by each app because they live on separate ports.
    page_script = f'{extra_script}\n{script}'
    return f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>{esc(title)}</title><style>{BASE_CSS}</style>{extra_head}</head><body><div class='shell'><div class='brand' style='margin-bottom:12px'>Expert My Rules</div>{body}</div><script>
(function(){{
  document.querySelectorAll('[data-tab]').forEach(function(btn){{
    btn.addEventListener('click',function(){{
      var name=btn.dataset.tab;
      document.querySelectorAll('.tab').forEach(x=>x.classList.toggle('active',x===btn));
      document.querySelectorAll('.pane').forEach(x=>x.classList.toggle('active',x.dataset.pane===name));
    }});
  }});
}})();
{page_script}
</script></body></html>"""