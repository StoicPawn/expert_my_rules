from __future__ import annotations

import html

from fastapi import File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from awb.core.models import JobStatus
from awb.core.private_import import PrivateImportError, import_private_bundle
from awb.core.storage import Ledger
from awb.web.app import _start, app, base_dir, page


@app.get('/private-import', response_class=HTMLResponse)
def private_import_page():
    body = """
    <a href='/'>← Projects</a>
    <h1>Import private research project</h1>
    <div class='panel'>
      <p>Upload a private ZIP bundle containing <code>PROJECT_BOOTSTRAP.json</code> and the project sources.</p>
      <p class='muted'>The bundle is stored only in the local workspace volume. Its contents are not committed to the Expert My Rules repository or emitted to GitHub Actions logs.</p>
      <form method='post' action='/private-import' enctype='multipart/form-data'>
        <input type='file' name='bundle' accept='.zip,application/zip' required>
        <button>Import and start if requested by bundle</button>
      </form>
    </div>
    """
    return page('Private research import', body)


@app.post('/private-import')
async def private_import(bundle: UploadFile = File(...)):
    try:
        payload = await bundle.read()
        root, bootstrap = import_private_bundle(payload, base_dir())
    except PrivateImportError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(500, f'Private import failed safely: {type(exc).__name__}: {exc}') from exc

    if bool(bootstrap.get('auto_launch', False)):
        ledger = Ledger(root / 'ledger.sqlite3')
        job_id = ledger.create_job(0, 0, continuous=True)
        ledger.update_job(job_id, status=JobStatus.RUNNING, detail='autonomous private research project active')
        _start(root, job_id)
    return RedirectResponse(f'/project/{html.escape(root.name)}', status_code=303)
