from __future__ import annotations

import re
import unittest

from awb.web import runtime_entry


def _shape(path: str) -> str:
    return re.sub(r'\{[^}]+\}', '{}', path)


class ProjectConsoleRouteTests(unittest.TestCase):
    def test_project_page_has_one_get_owner_even_with_legacy_parameter_names(self):
        routes = [
            route for route in runtime_entry.control_app.router.routes
            if 'GET' in (getattr(route, 'methods', None) or set())
        ]
        project_routes = [r for r in routes if _shape(str(getattr(r, 'path', ''))) == '/project/{}']
        setup_routes = [r for r in routes if _shape(str(getattr(r, 'path', ''))) == '/project/{}/setup']
        self.assertEqual(len(project_routes), 1)
        self.assertEqual(getattr(project_routes[0], 'name', ''), 'project_console')
        self.assertEqual(len(setup_routes), 1)
        self.assertEqual(getattr(setup_routes[0], 'name', ''), 'legacy_setup_redirect')


if __name__ == '__main__':
    unittest.main()
