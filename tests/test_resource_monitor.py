import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from awb.web import resource_monitor


class ResourceMonitorTests(unittest.TestCase):
    def test_meminfo_parser_converts_kib_to_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "meminfo"
            path.write_text(
                "MemTotal:       8192000 kB\n"
                "MemAvailable:   2048000 kB\n"
                "SwapTotal:      1024000 kB\n"
                "SwapFree:        512000 kB\n",
                encoding="utf-8",
            )
            values = resource_monitor._read_meminfo(path)
            self.assertEqual(values["MemTotal"], 8192000 * 1024)
            self.assertEqual(values["SwapFree"], 512000 * 1024)

    def test_cpu_totals_parse_proc_stat(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "stat"
            path.write_text("cpu  100 20 30 400 50 0 0 0 0 0\n", encoding="utf-8")
            total, idle = resource_monitor._read_cpu_totals(path)
            self.assertEqual(total, 600)
            self.assertEqual(idle, 450)

    def test_resource_html_contains_requested_metrics(self):
        snapshot = {
            "ram_used": 6 * 1024**3,
            "ram_total": 8 * 1024**3,
            "ram_percent": 75.0,
            "swap_used": 512 * 1024**2,
            "swap_total": 2 * 1024**3,
            "cpu_percent": 94.0,
            "model": "qwen3:4b",
        }
        with patch.object(resource_monitor, "_resource_snapshot", return_value=snapshot):
            rendered = resource_monitor._resource_html()
        self.assertIn("RAM", rendered)
        self.assertIn("6.0 GiB / 8.0 GiB (75%)", rendered)
        self.assertIn("CPU", rendered)
        self.assertIn("94%", rendered)
        self.assertIn("Swap", rendered)
        self.assertIn("qwen3:4b", rendered)

    def test_injection_is_read_only_and_polls_without_page_reload(self):
        injection = resource_monitor.RESOURCE_INJECTION
        self.assertIn("ACEPC resources", injection)
        self.assertIn("/system-resources", injection)
        self.assertIn("setInterval(refreshResources,5000)", injection)
        self.assertIn("Read-only monitoring", injection)


if __name__ == "__main__":
    unittest.main()
