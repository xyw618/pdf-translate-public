import importlib.util
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient


class AiModelsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="pdf-translate-api-tests-")
        root = Path(cls.temp.name)
        shutil.copy2(Path(__file__).resolve().parents[1] / "server.py", root / "server.py")
        (root / "static").mkdir()
        spec = importlib.util.spec_from_file_location("pdf_translate_test_server", root / "server.py")
        cls.server = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.server)
        cls.server.SESSIONS_FILE = root / "sessions.json"
        cls.client = TestClient(cls.server.app)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.temp.cleanup()

    def setUp(self):
        self.server.AI_CONFIG_FILE.write_text(json.dumps({
            "provider": "opencode",
            "providers": {"opencode": {"cmd": ["/configured/opencode", "run", "--model", "{model}", "{prompt}"]}},
        }))

    def test_models_use_configured_cli_and_headless_environment(self):
        result = subprocess.CompletedProcess([], 0, " beta/nested/model\n\x1b[32malpha/chat\x1b[0m\nbeta/nested/model\n", "")
        with patch.object(self.server.subprocess, "run", return_value=result) as run:
            response = self.client.get("/api/ai/models")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"provider": "opencode", "models": ["alpha/chat", "beta/nested/model"]})
        self.assertEqual(run.call_args.args[0], ["/configured/opencode", "models"])
        self.assertEqual(set(run.call_args.kwargs["env"]), {"HOME", "PATH"})
        self.assertGreater(run.call_args.kwargs["timeout"], 0)

    def test_other_harness_has_no_catalog_and_does_not_run_cli(self):
        with patch.object(self.server.subprocess, "run") as run:
            response = self.client.get("/api/ai/models?provider=zcode")
        self.assertEqual(response.json(), {"provider": "zcode", "models": []})
        run.assert_not_called()

    def test_unknown_harness_is_rejected(self):
        with patch.object(self.server.subprocess, "run") as run:
            response = self.client.get("/api/ai/models?provider=missing")
        self.assertEqual(response.status_code, 404)
        run.assert_not_called()

    def test_empty_or_malformed_catalog_is_an_explicit_error(self):
        for stdout in ["", "please sign in", "opencode/", "opencode/valid\nerror message"]:
            with self.subTest(stdout=stdout), patch.object(self.server.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout, "")):
                response = self.client.get("/api/ai/models")
            self.assertEqual(response.status_code, 502)
            self.assertIn("detail", response.json())

    def test_cli_failures_are_reported_without_exposing_stderr(self):
        with patch.object(self.server.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "private diagnostic")):
            response = self.client.get("/api/ai/models")
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("private diagnostic", response.text)
        for error, status in [(FileNotFoundError(), 502), (PermissionError(), 502), (subprocess.TimeoutExpired("opencode", 20), 504)]:
            with self.subTest(error=type(error).__name__), patch.object(self.server.subprocess, "run", side_effect=error):
                response = self.client.get("/api/ai/models")
            self.assertEqual(response.status_code, status)

    def test_missing_command_is_a_configuration_error(self):
        cfg = self.server.load_ai_config()
        for cmd in [None, [], "opencode", [""]]:
            cfg["providers"]["opencode"]["cmd"] = cmd
            with self.subTest(cmd=cmd), patch.object(self.server, "load_ai_config", return_value=cfg), patch.object(self.server.subprocess, "run") as run:
                response = self.client.get("/api/ai/models")
            self.assertEqual(response.status_code, 500)
            run.assert_not_called()

    def test_account_managed_model_is_not_editable(self):
        self.client.post("/api/ai/provider", json={"provider": "zcode"})
        self.assertFalse(self.client.get("/api/ai/model").json()["editable"])
        self.assertEqual(self.client.post("/api/ai/model", json={"model": "provider/chat"}).status_code, 400)

    def test_model_save_persists_and_is_used_for_chat(self):
        chosen = "provider/nested/chat"
        response = self.client.post("/api/ai/model", json={"model": chosen, "provider": "opencode"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/api/ai/model").json()["model"], chosen)
        proc = Mock(returncode=0)
        proc.communicate = AsyncMock(return_value=(b"answer", b""))
        with patch.object(self.server.asyncio, "create_subprocess_exec", new_callable=AsyncMock, return_value=proc) as start, patch.object(self.server.Path, "home", return_value=Path(self.temp.name) / "home"):
            response = self.client.post("/api/chat", json={"message": "hello"})
        self.assertEqual(response.status_code, 200)
        command = start.call_args.args
        self.assertEqual(command[command.index("--model") + 1], chosen)

    def test_provider_change_cannot_save_model_to_wrong_harness(self):
        before = self.server.AI_CONFIG_FILE.read_bytes()
        response = self.client.post("/api/ai/model", json={"model": "provider/chat", "provider": "openai"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.server.AI_CONFIG_FILE.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
