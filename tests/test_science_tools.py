import json
import os
import unittest
from unittest.mock import patch

from awb.core.science_tools import counterexample_search, literature_search, symbolic_math
from awb.core.workspace import _attach_optional_science_services


class ScientificToolsTests(unittest.TestCase):
    def test_symbolic_math_runs_fixed_code_in_research_lab(self):
        run = {
            'status': 'success',
            'stdout': json.dumps({'operation': 'factor', 'input': 'x**2 - 1', 'result': '(x - 1)*(x + 1)'}) + '\n',
        }
        with patch('awb.core.science_tools._lab_run', return_value=run) as lab:
            result = symbolic_math({'expression': 'x**2 - 1', 'operation': 'factor'})
        self.assertTrue(result['ok'])
        self.assertEqual(result['result']['result'], '(x - 1)*(x + 1)')
        code = lab.call_args.args[0]
        self.assertIn('sp.factor', code)
        self.assertNotIn('eval(', code)

    def test_counterexample_result_is_explicitly_not_a_proof(self):
        run = {
            'status': 'success',
            'stdout': json.dumps({
                'counterexample_found': False,
                'checked': 5000,
                'predicate': 'nonnegative',
                'note': 'No witness found in this bounded random search; this is not a proof.',
            }) + '\n',
        }
        with patch('awb.core.science_tools._lab_run', return_value=run):
            result = counterexample_search({
                'expression': 'x**2',
                'variables': {'x': [-10, 10]},
                'predicate': 'nonnegative',
            })
        self.assertTrue(result['ok'])
        self.assertFalse(result['result']['counterexample_found'])
        self.assertIn('not a proof', result['result']['note'])

    def test_literature_search_returns_provenance_and_novelty_warning(self):
        payload = {
            'message': {
                'items': [{
                    'title': ['A theorem'],
                    'DOI': '10.1/example',
                    'URL': 'https://doi.org/10.1/example',
                    'author': [{'given': 'Ada', 'family': 'Lovelace'}],
                    'published-online': {'date-parts': [[2026]]},
                    'container-title': ['Journal'],
                }]
            }
        }
        with patch('awb.core.science_tools._http_json', return_value=payload):
            result = literature_search({'query': 'martingale theorem', 'sources': ['crossref'], 'max_results': 3})
        self.assertTrue(result['ok'])
        self.assertEqual(result['results'][0]['source'], 'crossref')
        self.assertIn('not proof of novelty', result['warning'])

    def test_research_workspace_inherits_available_service_tools_without_rewrite(self):
        data = {
            'name': 'r', 'type': 'research', 'goal': 'prove x',
            'agents': [
                {'id': 'w', 'role': 'worker', 'instructions': 'work', 'tools': ['read', 'lab_execute']},
                {'id': 'v', 'role': 'verifier', 'instructions': 'verify', 'tools': []},
            ],
            'gates': [], 'tools': [
                {'id': 'read', 'type': 'read_file'},
                {'id': 'lab_execute', 'type': 'shell', 'command': 'legacy'},
            ],
        }
        env = {
            'RESEARCH_LAB_URL': 'http://research-lab:8300',
            'RESEARCH_LAB_TOKEN': 'secret',
            'TUTOR_LLM_URL': 'http://tutor:8000',
            'TUTOR_LLM_TOKEN': 'secret2',
        }
        with patch.dict(os.environ, env, clear=False):
            attached = _attach_optional_science_services(data)
        tools = {t['id']: t for t in attached['tools']}
        self.assertEqual(tools['research_lab']['type'], 'research_lab')
        self.assertIn('symbolic_math', tools)
        self.assertIn('counterexample_search', tools)
        self.assertIn('literature_search', tools)
        self.assertIn('tutor_knowledge', tools)
        worker = attached['agents'][0]
        self.assertNotIn('lab_execute', worker['tools'])
        self.assertTrue({'research_lab', 'symbolic_math', 'counterexample_search', 'literature_search', 'tutor_knowledge'}.issubset(worker['tools']))


if __name__ == '__main__':
    unittest.main()
