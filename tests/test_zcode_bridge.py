import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from zcode_bridge import BridgeFailure, child_env, decode_output, run_async, run_sync


class BridgeTransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pdf-translate-bridge-tests-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def executable(self, source):
        path = self.root / "bridge"
        path.write_text(f"#!{sys.executable}\n" + source)
        path.chmod(0o755)
        return str(path)

    def assert_process_exited(self, pid):
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_environment_excludes_service_variables_and_credentials(self):
        with patch.dict(os.environ, {"INVOCATION_ID": "service", "API_KEY": "private"}):
            self.assertEqual(set(child_env()), {"HOME", "PATH"})

    def test_mixed_logs_and_nested_response_are_parsed_without_diagnostics(self):
        result = {"pdfTranslateZcodeBridge": 1, "response": 'text {"nested": true}', "extra": {"a": 1}}
        output = "startup {bad}\n" + json.dumps({"diagnostic": "private"}) + "\n" + json.dumps(result)
        self.assertEqual(decode_output(output, 0), result)
        for output, code in [("private diagnostic", 0), (json.dumps(result), 1)]:
            with self.subTest(code=code), self.assertRaises(BridgeFailure) as raised:
                decode_output(output, code)
            self.assertNotIn("private diagnostic", str(raised.exception))

    async def test_sync_and_async_transport_send_json_and_parse_native_result(self):
        executable = self.executable('import json, sys\ndata=json.load(sys.stdin)\nprint("startup")\nprint(json.dumps({"pdfTranslateZcodeBridge":1,"response":data["message"]}))\n')
        payload = {"message": "问答 payload"}
        self.assertEqual(run_sync(executable, payload)["response"], payload["message"])
        self.assertEqual((await run_async(executable, payload, 3))["response"], payload["message"])

    async def test_start_failure_and_safe_native_error(self):
        for run in [lambda: run_sync("/missing/pdf-translate-bridge", {})]:
            with self.assertRaises(BridgeFailure):
                run()
        with self.assertRaises(BridgeFailure):
            await run_async("/missing/pdf-translate-bridge", {}, 1)
        executable = self.executable('import sys\nprint(\'{"pdfTranslateZcodeBridge":1,"error":"model unavailable"}\')\nsys.stderr.write("private diagnostic")\nsys.exit(1)\n')
        with self.assertRaises(BridgeFailure) as raised:
            await run_async(executable, {}, 2)
        self.assertEqual(str(raised.exception), "model unavailable")

    async def test_timeout_kills_process_which_ignores_termination(self):
        pidfile = self.root / "pid"
        executable = self.executable(f'import os, signal, time\nfrom pathlib import Path\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nPath({str(pidfile)!r}).write_text(str(os.getpid()))\ntime.sleep(30)\n')
        with self.assertRaises(BridgeFailure) as raised:
            await run_async(executable, {}, 0.3)
        self.assertEqual(raised.exception.status, 504)
        self.assert_process_exited(int(pidfile.read_text()))

    async def test_cancellation_reaps_running_process(self):
        pidfile = self.root / "pid"
        executable = self.executable(f'import os, time\nfrom pathlib import Path\nPath({str(pidfile)!r}).write_text(str(os.getpid()))\ntime.sleep(30)\n')
        task = asyncio.create_task(run_async(executable, {}, 20))
        for _ in range(100):
            if pidfile.exists():
                break
            await asyncio.sleep(0.02)
        self.assertTrue(pidfile.exists())
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assert_process_exited(int(pidfile.read_text()))


if __name__ == "__main__":
    unittest.main()
