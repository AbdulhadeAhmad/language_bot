import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner = load("run")
worker = load("worker")


class CasesTests(unittest.TestCase):
    def test_stress_case_is_exact_limit(self):
        cases = runner.text_cases()
        runner.validate_cases(cases)
        self.assertEqual(len(cases[-1]["german"]), 1000)

    def test_reject_unsafe_or_duplicate_ids(self):
        for ids in (("../escape",), ("",), ("same", "same")):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                runner.validate_cases([{"id": value} for value in ids])

    def test_audio_paths_relative_to_manifest_and_hashed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sample.wav").write_bytes(b"test fixture")
            manifest = root / "audio.json"
            manifest.write_text(json.dumps([{"id": "sample", "path": "sample.wav", "reference_german": "Hallo."}]))
            rows = runner.audio_cases(manifest)
            self.assertEqual(rows[0]["path"], str(root / "sample.wav"))
            self.assertEqual(len(rows[0]["sha256"]), 64)

    def test_audio_requires_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audio.json"
            path.write_text('[{"id":"bad", "path":"missing.wav"}]')
            with self.assertRaises(ValueError):
                runner.audio_cases(path)


class ProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_suite_records_missing_asr_without_claiming_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "models.json"
            manifest.write_text(json.dumps({"asr_base": {"path": "unused"}, "translation": {"path": "fake"}}))
            args = SimpleNamespace(audio_manifest=None, models=manifest, output=root / "results", cores=4, runs=3, timeout=3)
            with patch.object(runner, "run_child", return_value=({"status": "ok"}, "")):
                code = await runner.run(args)
            report = json.loads((args.output / "report.json").read_text())
            self.assertEqual(code, 1)
            self.assertFalse(report["execution_complete"])
            self.assertFalse(report["quality_approved"])
            self.assertEqual(report["results"]["asr_base"]["status"], "skipped")

    async def test_suite_continues_after_failure_and_preserves_existing_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "models.json"
            manifest.write_text(json.dumps({"translation": {"path": "fake"}, "voice": {"path": "fake"}}))
            args = SimpleNamespace(audio_manifest=None, models=manifest, output=root / "results", cores=4, runs=3, timeout=3)
            with patch.object(runner, "run_child", side_effect=[({"status": "failed"}, "failure detail"), ({"status": "ok"}, "")]) as child:
                self.assertEqual(await runner.run(args), 1)
                self.assertEqual(child.call_count, 2)
            original = (args.output / "report.json").read_text()
            with self.assertRaises(FileExistsError):
                await runner.run(args)
            self.assertEqual((args.output / "report.json").read_text(), original)

    async def test_success_includes_exit_code(self):
        result, logs = await runner.run_child({}, 3, [sys.executable, "-c", 'print(\'{"status":"ok"}\')'])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["exit_code"], 0)

    async def test_crash_does_not_become_success(self):
        result, _ = await runner.run_child({}, 3, [sys.executable, "-c", 'import sys; print(\'{"status":"ok"}\'); sys.exit(9)'])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["exit_code"], 9)

    async def test_malformed_report(self):
        result, _ = await runner.run_child({}, 3, [sys.executable, "-c", 'print("not json")'])
        self.assertEqual(result["status"], "failed")

    async def test_timeout_reaps_worker(self):
        start = time.monotonic()
        result, _ = await runner.run_child({}, 0.1, [sys.executable, "-c", "import time; time.sleep(30)"])
        self.assertEqual(result["status"], "timeout")
        self.assertLess(time.monotonic() - start, 3)

    async def test_orchestration_does_not_block_event_loop(self):
        ticks = []

        async def heartbeat():
            for _ in range(3):
                await asyncio.sleep(0.02)
                ticks.append(True)

        await asyncio.gather(heartbeat(), runner.run_child({}, 3, [sys.executable, "-c", 'import time; time.sleep(0.2); print(\'{"status":"ok"}\')']))
        self.assertEqual(len(ticks), 3)


class MeasurementTests(unittest.TestCase):
    def test_failed_case_keeps_other_measurements_and_removes_partial_audio(self):
        def loader(kind, model, cores):
            def infer(case, output):
                if case["id"] == "broken":
                    output.write_bytes(b"incomplete")
                    raise ValueError("synthetic model failure")
                return {"text": "Hello."}
            return infer

        with tempfile.TemporaryDirectory() as directory, patch.object(worker, "configure_cpu", return_value=[0]):
            result = worker.measure({"cores": 1, "kind": "tts", "model": {"path": "fake"}, "cases": [{"id": "broken"}, {"id": "good"}], "runs": 2, "output": directory}, loader)
            self.assertEqual(list(Path(directory).glob("*.wav")), [])
        self.assertEqual(result["status"], "failed")
        self.assertEqual([row["status"] for row in result["measurements"]], ["failed", "failed", "ok", "ok"])

    def test_all_repeats_execute_and_results_are_recorded(self):
        calls = []

        def provider_loader(kind, model, cores):
            def infer(case, output):
                calls.append(case["id"])
                return {"text": "Hello."}
            return infer

        with tempfile.TemporaryDirectory() as directory, patch.object(worker, "configure_cpu", return_value=[0, 1]):
            result = worker.measure({"cores": 2, "kind": "translation", "model": {"path": "fake"}, "cases": [{"id": "one"}], "runs": 3, "output": directory}, provider_loader)
        self.assertEqual(calls, ["one", "one", "one"])
        self.assertEqual(len(result["measurements"]), 3)
        self.assertGreater(result["peak_rss_mib"], 0)


if __name__ == "__main__":
    unittest.main()
