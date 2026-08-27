from pathlib import Path
import unittest


class NativeWindowsInstallTests(unittest.TestCase):
    def test_native_installer_keeps_three_baseline_models(self):
        script = Path('install.ps1').read_text(encoding='utf-8')
        for model in ('qwen3:4b', 'llama3.2:3b', 'gemma3:4b'):
            self.assertIn(model, script)

    def test_native_installer_has_disk_preflight_and_real_model_smoke_test(self):
        script = Path('install.ps1').read_text(encoding='utf-8')
        self.assertIn('[int]$RequiredFreeGB = 15', script)
        self.assertIn('Disk preflight', script)
        self.assertIn('[switch]$TestModels', script)
        self.assertIn('/api/generate', script)
        self.assertIn('keep_alive = 0', script)

    def test_native_install_documented(self):
        doc = Path('NATIVE_WINDOWS.md').read_text(encoding='utf-8')
        self.assertIn('no Docker', doc)
        self.assertIn('.\\install.ps1 -TestModels', doc)
        self.assertIn('http://localhost:8000', doc)


if __name__ == '__main__':
    unittest.main()
