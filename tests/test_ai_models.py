import importlib.util
import asyncio
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
        self.server.save_sessions({})
        self.server.zcode_chat_locks.clear()
        home = patch.object(self.server.Path, "home", return_value=Path(self.temp.name) / "home")
        home.start()
        self.addCleanup(home.stop)
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
            response = self.client.get("/api/ai/models?provider=claude")
        self.assertEqual(response.json(), {"provider": "claude", "models": []})
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

    def test_zcode_without_bridge_cannot_select_a_model(self):
        with patch.dict(self.server.DEFAULT_AI_PROVIDERS["zcode"], {"model_bridge": ""}):
            self.client.post("/api/ai/provider", json={"provider": "zcode"})
            self.assertFalse(self.client.get("/api/ai/model").json()["editable"])
            self.assertFalse(self.client.get("/api/ai/model").json()["catalog"])
            self.assertEqual(self.client.get("/api/ai/models?provider=zcode").status_code, 400)
            self.assertEqual(self.client.post("/api/ai/model", json={"model": "provider/chat"}).status_code, 400)

    def test_account_managed_model_is_not_editable(self):
        self.client.post("/api/ai/provider", json={"provider": "claude"})
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

    def configure_zcode(self, **options):
        self.server.AI_CONFIG_FILE.write_text(json.dumps({
            "provider": "zcode", "providers": {"zcode": {"model_bridge": "/configured/bridge", **options}},
        }))

    def native_catalog(self):
        return {"current": "native/fast", "models": [
            {"id": "native/fast", "label": "Fast", "providerLabel": "Native"},
            {"id": "native/deep", "label": "Deep", "providerLabel": "Native"},
        ], "credentials": "private diagnostic"}

    def test_zcode_catalog_and_selection_use_native_available_models(self):
        self.configure_zcode()
        self.assertTrue(self.client.get("/api/ai/model").json()["editable"])
        self.assertTrue(self.client.get("/api/ai/model").json()["catalog"])
        with patch.object(self.server, "read_zcode_bridge", return_value=self.native_catalog()) as read:
            response = self.client.get("/api/ai/models?provider=zcode")
            saved = self.client.post("/api/ai/model", json={"provider": "zcode", "model": "native/deep"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["models"], ["native/deep", "native/fast"])
        self.assertEqual(response.json()["labels"]["native/deep"], "Deep")
        self.assertEqual(response.json()["providerLabels"], {"native": "Native"})
        self.assertNotIn("private diagnostic", response.text)
        self.assertEqual(read.call_args.args[0], "/configured/bridge")
        self.assertEqual(read.call_args.args[1]["action"], "models")
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(self.client.get("/api/ai/model").json()["model"], "native/deep")

    def test_unavailable_zcode_model_does_not_change_configuration(self):
        self.configure_zcode()
        before = self.server.AI_CONFIG_FILE.read_bytes()
        with patch.object(self.server, "read_zcode_bridge", return_value=self.native_catalog()):
            response = self.client.post("/api/ai/model", json={"model": "native/unavailable"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.server.AI_CONFIG_FILE.read_bytes(), before)

    def test_zcode_catalog_failure_is_explicit(self):
        self.configure_zcode()
        for models in [[], [{"id": "invalid"}], [None]]:
            with self.subTest(models=models), patch.object(self.server, "read_zcode_bridge", return_value={"models": models}):
                self.assertEqual(self.client.get("/api/ai/models?provider=zcode").status_code, 502)
        with patch.object(self.server, "read_zcode_bridge", side_effect=self.server.BridgeFailure("safe error", 504)):
            response = self.client.get("/api/ai/models?provider=zcode")
        self.assertEqual(response.status_code, 504)
        self.assertEqual(response.json()["detail"], "safe error")

    def test_zcode_chat_switches_model_in_the_same_session(self):
        self.configure_zcode(model="native/deep")
        native = AsyncMock(return_value={"response": "answer", "sessionId": "native-session", "model": "native/deep"})
        with patch.object(self.server, "run_zcode_bridge", native):
            first = self.client.post("/api/chat", json={"message": "hello"})
            self.configure_zcode(model="native/fast")
            native.return_value["model"] = "native/fast"
            second = self.client.post("/api/chat", json={"message": "continue", "continue": True})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(native.call_args_list[0].args[1]["model"], "native/deep")
        self.assertNotIn("sessionId", native.call_args_list[0].args[1])
        self.assertEqual(native.call_args_list[1].args[1]["sessionId"], "native-session")
        self.assertEqual(native.call_args_list[1].args[1]["model"], "native/fast")
        self.assertEqual(second.json()["model"], "native/fast")

    def test_zcode_fresh_session_attaches_paper_even_with_continue_requested(self):
        self.configure_zcode()
        paper = self.server.UPLOADS / "test-paper.pdf"
        paper.write_bytes(b"synthetic fixture")
        self.addCleanup(paper.unlink)
        native = AsyncMock(return_value={"response": "answer", "sessionId": "paper-session", "model": "native/fast"})
        with patch.object(self.server, "run_zcode_bridge", native):
            first = self.client.post("/api/chat", json={"message": "read", "taskId": "test-paper", "continue": True})
            second = self.client.post("/api/chat", json={"message": "follow up", "taskId": "test-paper", "continue": True})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(native.call_args_list[0].args[1]["attach"], str(paper))
        self.assertNotIn("attach", native.call_args_list[1].args[1])
        self.assertEqual(native.call_args_list[1].args[1]["sessionId"], "paper-session")

    def test_wrong_model_or_failed_chat_does_not_save_a_session(self):
        self.configure_zcode(model="native/deep")
        for result in [{"response": "answer", "sessionId": "sid", "model": "native/fast"}, {"response": "", "sessionId": "sid", "model": "native/deep"}]:
            with self.subTest(result=result), patch.object(self.server, "run_zcode_bridge", AsyncMock(return_value=result)):
                self.assertEqual(self.client.post("/api/chat", json={"message": "hello"}).status_code, 502)
            self.assertEqual(self.server.load_sessions(), {})
        with patch.object(self.server, "run_zcode_bridge", AsyncMock(side_effect=self.server.BridgeFailure("safe error", 504))):
            self.assertEqual(self.client.post("/api/chat", json={"message": "hello"}).status_code, 504)
        self.assertEqual(self.server.load_sessions(), {})

    def test_zcode_selected_model_requires_bridge_instead_of_using_default(self):
        self.configure_zcode(model_bridge="", model="native/deep")
        with patch.object(self.server.asyncio, "create_subprocess_exec", new_callable=AsyncMock) as start:
            response = self.client.post("/api/chat", json={"message": "hello"})
        self.assertEqual(response.status_code, 400)
        start.assert_not_called()

    def test_same_paper_concurrent_chats_resume_in_order(self):
        self.configure_zcode()
        requests = []
        async def native(executable, payload, timeout):
            requests.append(dict(payload))
            await asyncio.sleep(0.02)
            return {"response": "answer", "sessionId": "ordered-session", "model": "native/fast"}
        async def exercise():
            with patch.object(self.server, "run_zcode_bridge", native):
                return await asyncio.gather(*[self.server.chat({"message": message, "continue": True}) for message in ["first", "second"]])
        answers = asyncio.run(exercise())
        self.assertEqual(len(answers), 2)
        self.assertNotIn("sessionId", requests[0])
        self.assertEqual(requests[1]["sessionId"], "ordered-session")


if __name__ == "__main__":
    unittest.main()
