from __future__ import annotations

import json
import math
import os
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any

from .research_lab import ResearchLabClient, ResearchLabError


class ScienceToolError(RuntimeError):
    pass


def _bounded_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value if value is not None else default)
    except (TypeError, ValueError) as exc:
        raise ScienceToolError('Expected an integer argument') from exc
    return max(low, min(parsed, high))


def _lab_run(code: str, *, title: str, timeout_seconds: int = 120, project_key: str | None = None) -> dict[str, Any]:
    client = ResearchLabClient()
    key = (project_key or os.getenv('AWB_LAB_PROJECT_KEY') or 'expert-science').strip()
    workspace = client.ensure_workspace(key, key, 'Expert My Rules scientific verification workspace')
    return client.run(
        str(workspace['id']),
        title,
        code,
        timeout_seconds=timeout_seconds,
        metadata={'caller': 'expert_my_rules', 'kind': 'scientific_tool', 'project_key': key},
    )


def _parse_last_json(stdout: str) -> Any:
    for line in reversed(str(stdout or '').splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    raise ScienceToolError('Scientific runner returned no machine-readable result')


def symbolic_math(arguments: dict[str, Any]) -> dict[str, Any]:
    """Execute a fixed, bounded SymPy operation in the standalone Research Lab."""
    expression = str(arguments.get('expression') or '').strip()
    if not expression:
        raise ScienceToolError('expression is required')
    operation = str(arguments.get('operation') or 'simplify').strip().lower()
    allowed = {'simplify', 'factor', 'expand', 'solve', 'diff', 'integrate', 'limit', 'series'}
    if operation not in allowed:
        raise ScienceToolError(f'Unsupported symbolic operation: {operation}')
    variable = str(arguments.get('variable') or 'x').strip() or 'x'
    point = arguments.get('point')
    order = _bounded_int(arguments.get('order'), 6, 1, 30)
    payload = json.dumps({
        'expression': expression,
        'operation': operation,
        'variable': variable,
        'point': point,
        'order': order,
    }, ensure_ascii=False)
    code = f'''import json\nimport sympy as sp\na=json.loads({payload!r})\nx=sp.Symbol(a["variable"])\nexpr=sp.sympify(a["expression"])\nop=a["operation"]\nif op=="simplify": result=sp.simplify(expr)\nelif op=="factor": result=sp.factor(expr)\nelif op=="expand": result=sp.expand(expr)\nelif op=="solve": result=sp.solve(expr, x)\nelif op=="diff": result=sp.diff(expr, x)\nelif op=="integrate": result=sp.integrate(expr, x)\nelif op=="limit": result=sp.limit(expr, x, sp.sympify(a["point"]))\nelif op=="series": result=sp.series(expr, x, sp.sympify(a["point"] if a["point"] is not None else 0), int(a["order"]))\nprint(json.dumps({{"operation":op,"input":str(expr),"result":str(result)}}, ensure_ascii=False))\n'''
    run = _lab_run(code, title=f'SymPy {operation}', timeout_seconds=120, project_key=arguments.get('project_key'))
    result = _parse_last_json(run.get('stdout', '')) if run.get('status') == 'success' else None
    return {'ok': run.get('status') == 'success', 'result': result, 'run': run}


def counterexample_search(arguments: dict[str, Any]) -> dict[str, Any]:
    """Numerically search a bounded box for a witness violating a simple predicate."""
    expression = str(arguments.get('expression') or '').strip()
    variables = arguments.get('variables') or {}
    if not expression or not isinstance(variables, dict) or not variables:
        raise ScienceToolError('expression and a non-empty variables mapping are required')
    cleaned: dict[str, list[float]] = {}
    for name, bounds in variables.items():
        if not isinstance(bounds, (list, tuple)) or len(bounds) != 2:
            raise ScienceToolError(f'Variable {name} must have [low, high] bounds')
        low, high = float(bounds[0]), float(bounds[1])
        if not math.isfinite(low) or not math.isfinite(high) or low >= high:
            raise ScienceToolError(f'Invalid bounds for {name}')
        cleaned[str(name)] = [low, high]
    predicate = str(arguments.get('predicate') or 'nonnegative').strip().lower()
    if predicate not in {'eq_zero', 'nonnegative', 'nonpositive', 'positive', 'finite'}:
        raise ScienceToolError(f'Unsupported predicate: {predicate}')
    samples = _bounded_int(arguments.get('samples'), 5000, 10, 50000)
    seed = _bounded_int(arguments.get('seed'), 0, 0, 2_147_483_647)
    tolerance = float(arguments.get('tolerance', 1e-9))
    payload = json.dumps({
        'expression': expression, 'variables': cleaned, 'predicate': predicate,
        'samples': samples, 'seed': seed, 'tolerance': tolerance,
    }, ensure_ascii=False)
    code = f'''import json\nimport numpy as np\nimport sympy as sp\na=json.loads({payload!r})\nnames=list(a["variables"])\nsymbols=[sp.Symbol(n) for n in names]\nexpr=sp.sympify(a["expression"])\nf=sp.lambdify(symbols, expr, modules=["numpy"])\nrng=np.random.default_rng(int(a["seed"]))\ncols=[rng.uniform(*a["variables"][n], size=int(a["samples"])) for n in names]\ntry:\n    vals=np.asarray(f(*cols))\n    if vals.ndim==0: vals=np.full(int(a["samples"]), vals)\n    vals=np.ravel(vals)\nexcept Exception as exc:\n    print(json.dumps({{"error":type(exc).__name__+": "+str(exc)}})); raise SystemExit(0)\ntol=float(a["tolerance"]); pred=a["predicate"]\nfinite=np.isfinite(vals)\nif pred=="eq_zero": good=finite & (np.abs(vals)<=tol)\nelif pred=="nonnegative": good=finite & (vals>=-tol)\nelif pred=="nonpositive": good=finite & (vals<=tol)\nelif pred=="positive": good=finite & (vals>tol)\nelse: good=finite\nbad=np.where(~good)[0]\nif len(bad):\n    i=int(bad[0]); witness={{names[j]:float(cols[j][i]) for j in range(len(names))}}\n    value=vals[i]; value=float(value) if np.isfinite(value) else str(value)\n    out={{"counterexample_found":True,"witness":witness,"value":value,"checked":int(a["samples"]),"predicate":pred}}\nelse:\n    out={{"counterexample_found":False,"checked":int(a["samples"]),"predicate":pred,"note":"No witness found in this bounded random search; this is not a proof."}}\nprint(json.dumps(out, ensure_ascii=False))\n'''
    run = _lab_run(code, title='Bounded counterexample search', timeout_seconds=120, project_key=arguments.get('project_key'))
    result = _parse_last_json(run.get('stdout', '')) if run.get('status') == 'success' else None
    return {'ok': run.get('status') == 'success', 'result': result, 'run': run}


def research_lab(arguments: dict[str, Any]) -> dict[str, Any]:
    """Thin direct adapter to the existing Research Lab API."""
    action = str(arguments.get('action') or 'capabilities')
    client = ResearchLabClient()
    if action == 'capabilities':
        return {'ok': True, 'result': client.request('GET', '/api/capabilities')}
    if action == 'list_workspaces':
        return {'ok': True, 'result': client.workspaces()}
    project_key = str(arguments.get('project_key') or os.getenv('AWB_LAB_PROJECT_KEY') or 'expert-research')
    workspace = client.ensure_workspace(project_key, str(arguments.get('workspace_name') or project_key), str(arguments.get('workspace_description') or ''))
    workspace_id = str(workspace['id'])
    if action == 'list_runs':
        return {'ok': True, 'workspace_id': workspace_id, 'result': client.runs(workspace_id)}
    if action == 'latest_context':
        return {'ok': True, 'workspace_id': workspace_id, 'result': client.latest_context(workspace_id)}
    if action == 'run':
        code = str(arguments.get('code') or '').strip()
        if not code:
            raise ScienceToolError('run action requires code')
        timeout = _bounded_int(arguments.get('timeout_seconds'), 120, 1, 300)
        result = client.run(workspace_id, str(arguments.get('title') or 'Expert experiment'), code, timeout_seconds=timeout, metadata={'caller': 'expert_my_rules', 'project_key': project_key})
        return {'ok': result.get('status') == 'success', 'workspace_id': workspace_id, 'result': result}
    if action == 'publish_context':
        content = str(arguments.get('content') or '').strip()
        if not content:
            raise ScienceToolError('publish_context requires content')
        result = client.run(
            workspace_id, str(arguments.get('title') or 'Expert context snapshot'),
            "print('Expert My Rules context snapshot published')", timeout_seconds=30,
            metadata={'caller': 'expert_my_rules', 'kind': 'shared_context', 'project_key': project_key, 'content': content, 'sources': arguments.get('sources') or []},
        )
        return {'ok': result.get('status') == 'success', 'workspace_id': workspace_id, 'result': result}
    raise ScienceToolError(f'Unsupported Research Lab action: {action}')


class TutorKnowledgeClient:
    def __init__(self):
        self.base_url = os.getenv('TUTOR_LLM_URL', '').rstrip('/')
        self.token = os.getenv('TUTOR_LLM_TOKEN', '')
        if not self.base_url or not self.token:
            raise ScienceToolError('TUTOR_LLM_URL/TUTOR_LLM_TOKEN are not configured')

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        data = None if payload is None else json.dumps(payload).encode('utf-8')
        request = urllib.request.Request(
            self.base_url + path, data=data, method=method,
            headers={'Authorization': f'Bearer {self.token}', 'Content-Type': 'application/json'},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode('utf-8'))
        except Exception as exc:
            raise ScienceToolError(f'Tutor knowledge service unavailable: {exc}') from exc


def tutor_knowledge(arguments: dict[str, Any]) -> dict[str, Any]:
    """Grounded access to Tutor LLM's indexed library without another LLM answer."""
    client = TutorKnowledgeClient()
    action = str(arguments.get('action') or 'list_workspaces')
    if action == 'list_workspaces':
        result = client.request('GET', '/workspaces')
    else:
        workspace_id = _bounded_int(arguments.get('workspace_id'), 0, 1, 2_147_483_647)
        if action == 'documents':
            result = client.request('GET', f'/workspaces/{workspace_id}/documents')
        elif action == 'knowledge_graph':
            result = client.request('GET', f'/workspaces/{workspace_id}/knowledge')
        elif action == 'retrieve':
            query = str(arguments.get('query') or '').strip()
            if not query:
                raise ScienceToolError('retrieve action requires query')
            result = client.request('POST', f'/workspaces/{workspace_id}/retrieve', {
                'query': query,
                'document_ids': arguments.get('document_ids'),
                'top_k': _bounded_int(arguments.get('top_k'), 8, 1, 20),
            })
        else:
            raise ScienceToolError(f'Unsupported Tutor knowledge action: {action}')
    return {'ok': True, 'result': result, 'grounding': 'Tutor LLM indexed workspace evidence'}


def _http_json(url: str, timeout: float = 15.0) -> Any:
    request = urllib.request.Request(url, headers={'User-Agent': 'ExpertMyRules/0.8 research-agent'})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode('utf-8'))


def literature_search(arguments: dict[str, Any]) -> dict[str, Any]:
    """Discover candidate literature through fixed public metadata endpoints.

    This deliberately returns metadata/provenance, not a claim that novelty has been
    exhaustively established.
    """
    query = str(arguments.get('query') or '').strip()
    if not query:
        raise ScienceToolError('query is required')
    limit = _bounded_int(arguments.get('max_results'), 8, 1, 10)
    sources = arguments.get('sources') or ['crossref', 'arxiv']
    if not isinstance(sources, list):
        raise ScienceToolError('sources must be a list')
    out: list[dict[str, Any]] = []
    errors: list[str] = []

    if 'crossref' in sources:
        try:
            url = 'https://api.crossref.org/works?' + urllib.parse.urlencode({'query.bibliographic': query, 'rows': limit})
            data = _http_json(url)
            for item in (data.get('message') or {}).get('items', [])[:limit]:
                title = ' '.join(item.get('title') or [])
                year_parts = ((item.get('published-print') or item.get('published-online') or {}).get('date-parts') or [[]])
                year = year_parts[0][0] if year_parts and year_parts[0] else None
                authors = [' '.join(filter(None, [a.get('given'), a.get('family')])) for a in item.get('author') or []]
                out.append({'source': 'crossref', 'title': title, 'year': year, 'authors': authors[:8], 'doi': item.get('DOI'), 'url': item.get('URL'), 'container': ' '.join(item.get('container-title') or [])})
        except Exception as exc:
            errors.append(f'crossref: {type(exc).__name__}: {exc}')

    if 'arxiv' in sources:
        try:
            url = 'https://export.arxiv.org/api/query?' + urllib.parse.urlencode({'search_query': f'all:{query}', 'start': 0, 'max_results': limit})
            request = urllib.request.Request(url, headers={'User-Agent': 'ExpertMyRules/0.8 research-agent'})
            with urllib.request.urlopen(request, timeout=15) as response:
                root = ET.fromstring(response.read())
            ns = {'a': 'http://www.w3.org/2005/Atom'}
            for entry in root.findall('a:entry', ns)[:limit]:
                authors = [a.findtext('a:name', default='', namespaces=ns) for a in entry.findall('a:author', ns)]
                out.append({'source': 'arxiv', 'title': ' '.join((entry.findtext('a:title', default='', namespaces=ns)).split()), 'published': entry.findtext('a:published', default='', namespaces=ns), 'authors': authors[:8], 'url': entry.findtext('a:id', default='', namespaces=ns), 'summary': ' '.join((entry.findtext('a:summary', default='', namespaces=ns)).split())[:800]})
        except Exception as exc:
            errors.append(f'arxiv: {type(exc).__name__}: {exc}')

    return {
        'ok': bool(out),
        'query': query,
        'results': out[: limit * max(1, len(sources))],
        'errors': errors,
        'warning': 'Candidate discovery only; absence from these metadata searches is not proof of novelty.',
    }
