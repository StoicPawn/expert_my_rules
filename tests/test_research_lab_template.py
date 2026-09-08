import unittest

from awb.templates.templates import research_manifest


class ResearchLabTemplateTests(unittest.TestCase):
    def test_research_template_exposes_controlled_lab_runner(self):
        manifest = research_manifest('example', 'test a scientific claim')
        tools = {item['id']: item for item in manifest['tools']}
        self.assertIn('lab_execute', tools)
        self.assertEqual(tools['lab_execute']['type'], 'shell')
        self.assertEqual(
            tools['lab_execute']['command'],
            'python -m awb.core.research_lab execute lab_request.json',
        )
        researcher = next(a for a in manifest['agents'] if a['id'] == 'researcher')
        verifier = next(a for a in manifest['agents'] if a['id'] == 'verifier')
        self.assertIn('lab_execute', researcher['tools'])
        self.assertIn('lab_execute', verifier['tools'])
        self.assertIn('write', researcher['tools'])

    def test_research_lab_is_not_enabled_for_unrelated_templates(self):
        # The shared Lab is opt-in at the research-template level; adding it must not
        # mutate software/custom project semantics.
        from awb.templates.templates import software_manifest, custom_manifest

        software = software_manifest('software', 'ship code')
        custom = custom_manifest('custom', 'do work')
        self.assertNotIn('lab_execute', {t['id'] for t in software['tools']})
        self.assertNotIn('lab_execute', {t['id'] for t in custom['tools']})


if __name__ == '__main__':
    unittest.main()
