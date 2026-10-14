"""Windows installation guards; all state-changing commands are mocked."""

import importlib.util
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(
    os.name == "nt" and shutil.which("powershell.exe"), "Requires Windows PowerShell"
)
class WindowsScriptTests(unittest.TestCase):
    def test_disable_from_another_installation_does_not_touch_config(self):
        tests = Path(__file__).resolve().parent
        result = subprocess.run(
            [
                shutil.which("powershell.exe"),
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "RemoteSigned",
                "-File",
                str(tests / "disable_guard_probe.ps1"),
            ],
            env=dict(os.environ, RETRY_TEST_DISABLE_SCRIPT=str(tests.parent / "Disable.ps1")),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_validation_failure_preserves_previous_config(self):
        tests = Path(__file__).resolve().parent
        source = tests.parent / "retry_proxy.py"
        spec = importlib.util.spec_from_file_location("installer_fixture", source)
        proxy = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(proxy)
        for scenario in ("fresh", "unchanged", "upgrade", "concurrent"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                config = Path(directory) / "config.toml"
                config.write_text('model = "test-only"\n')
                if scenario in ("unchanged", "upgrade"):
                    proxy.configure(config, True)
                if scenario == "upgrade":
                    config.write_text(
                        config.read_text().replace(
                            proxy.CONFIG_HEADER, proxy.PREVIOUS_CONFIG_HEADER, 1
                        )
                    )
                original = config.read_bytes()
                result = subprocess.run(
                    [
                        shutil.which("powershell.exe"),
                        "-NoProfile",
                        "-NonInteractive",
                        "-ExecutionPolicy",
                        "RemoteSigned",
                        "-File",
                        str(tests / "setup_validation_probe.ps1"),
                    ],
                    env=dict(
                        os.environ,
                        CODEX_HOME=directory,
                        RETRY_TEST_PROXY_SCRIPT=str(source),
                        RETRY_TEST_ENABLE_SCRIPT=str(tests.parent / "Enable.ps1"),
                        RETRY_TEST_SCENARIO=scenario,
                    ),
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=20,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                if scenario == "concurrent":
                    self.assertIn("# concurrent user edit", config.read_text())
                    self.assertIn(proxy.CONFIG_HEADER, config.read_text())
                else:
                    self.assertEqual(config.read_bytes(), original)

    def test_manual_gateway_is_rejected_before_task_registration(self):
        tests = Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.toml"
            config.write_text('model = "test-only"\n')
            original = config.read_bytes()
            result = subprocess.run(
                [
                    shutil.which("powershell.exe"),
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "RemoteSigned",
                    "-File",
                    str(tests / "manual_gateway_probe.ps1"),
                ],
                env=dict(
                    os.environ,
                    CODEX_HOME=directory,
                    RETRY_TEST_ENABLE_SCRIPT=str(tests.parent / "Enable.ps1"),
                ),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(config.read_bytes(), original)
