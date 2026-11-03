import contextlib
import importlib.util
import json
import io
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
import wave

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner, worker, review = load("run"), load("worker"), load("review")


class SelectionTests(unittest.TestCase):
    def test_provider_startup_prints_do_not_corrupt_json_report(self):
        def noisy_provider(config):
            print("Provider startup warning")
            return {"status": "ok", "measurements": []}

        with tempfile.TemporaryDirectory() as temporary:
            stdout, stderr = io.StringIO(), io.StringIO()
            config = {"output": temporary}
            with patch.object(sys, "stdin", io.StringIO(json.dumps(config))), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), patch.object(worker, "measure", side_effect=noisy_provider):
                self.assertEqual(worker.main(), 0)
            self.assertEqual(json.loads(stdout.getvalue())["status"], "ok")
            self.assertIn("Provider startup warning", stderr.getvalue())

    def test_preparation_preserves_existing_models_and_only_adds_candidates(self):
        prepare = load("prepare")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifacts = root / "artifacts"
            artifacts.mkdir()
            models = {}
            for key in ("asr_base", "asr_small", "translation", "de_DE-thorsten-medium", "de_DE-eva_k-x_low", "de_DE-karlsson-low"):
                path = artifacts / key
                path.write_bytes(b"existing model")
                Path(str(path) + ".json").write_text("{}")
                models[key] = {"path": str(path), "revision": "old-revision"}
            original = json.loads(json.dumps(models))
            (artifacts / "models.json").write_text(json.dumps(models))

            def snapshot(repo, **kwargs):
                self.assertEqual(repo, "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice")
                directory = root / "qwen-snapshot"
                (directory / "speech_tokenizer").mkdir(parents=True)
                (directory / "speech_tokenizer/config.json").write_text("{}")
                return str(directory)

            def download(repo, filename, **kwargs):
                path = root / "download" / filename
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"candidate asset")
                return str(path)

            hub = SimpleNamespace(snapshot_download=MagicMock(side_effect=snapshot), hf_hub_download=download, HfApi=lambda: SimpleNamespace(model_info=lambda repo: SimpleNamespace(sha="a" * 40)))
            with patch.object(prepare, "MODELS", artifacts), patch.dict(sys.modules, {"huggingface_hub": hub}), patch.object(sys, "argv", ["prepare.py", "--extended-tts", "--qwen"]):
                prepare.main()
            prepared = json.loads((artifacts / "models.json").read_text())
            self.assertEqual(len(prepared), 12)
            for key in original:
                self.assertEqual(prepared[key], original[key])
            self.assertEqual(prepared["qwen_0_6b_ryan"]["speaker"], "Ryan")
            self.assertTrue(Path(prepared["de_DE-thorsten-high"]["path"]).is_file())
            hub.snapshot_download.assert_called_once()

    def test_extended_inputs_keep_baseline_and_are_unique(self):
        basic = runner.text_cases()
        extended = runner.text_cases("extended")
        runner.validate_cases(extended)
        self.assertEqual(len(extended), 27)
        self.assertEqual(extended[-1], basic[-1])
        self.assertTrue(all(len(row["german"]) <= 1000 for row in extended))

    def test_select_asr_without_qwen(self):
        models = {"asr_base": {}, "translation": {}, "qwen_0_6b_ryan": {"kind": "tts", "provider": "qwen3_tts"}}
        self.assertEqual(list(runner.select_models(models, "asr")), ["asr_base"])
        with self.assertRaises(ValueError):
            runner.select_models(models, "all", ["missing"])
        with self.assertRaises(ValueError):
            runner.select_models(models, "asr", ["translation"])

    def test_word_error_rate_counts_substitutions_deletions_and_insertions(self):
        self.assertEqual(worker.word_error_rate("Guten Morgen!", "guten morgen")["rate"], 0)
        self.assertEqual(worker.word_error_rate("Ich komme morgen", "Ich gehe morgen")["edits"], 1)
        self.assertEqual(worker.word_error_rate("Ich komme morgen", "Ich komme")["edits"], 1)
        self.assertEqual(worker.word_error_rate("Ich komme", "Ich komme heute")["edits"], 1)
        self.assertIsNone(worker.word_error_rate("", "Hallo"))

    def test_asr_adapter_consumes_lazy_segments_and_scores_reference(self):
        consumed = []

        def segments():
            consumed.append(True)
            yield SimpleNamespace(text=" Guten Morgen.")

        model = MagicMock()
        model.transcribe.return_value = (segments(), SimpleNamespace(duration=2.0))
        factory = MagicMock(return_value=model)
        with patch.dict(sys.modules, {"faster_whisper": SimpleNamespace(WhisperModel=factory)}):
            provider = worker.load_provider("asr", {"path": "/local/model"}, 4)
            result = provider({"path": "speech.wav", "reference_german": "Guten Morgen!"}, None)
        self.assertEqual(consumed, [True])
        self.assertEqual(result["wer"]["rate"], 0)
        self.assertEqual(factory.call_args.kwargs["device"], "cpu")

    def test_qwen_uses_separate_cpu_adapter_and_requested_speaker(self):
        torch = SimpleNamespace(float32="float32", set_num_threads=MagicMock(), set_num_interop_threads=MagicMock(), manual_seed=MagicMock(), inference_mode=contextlib.nullcontext)
        model = MagicMock()
        model.get_supported_speakers.return_value = ["ryan"]
        model.get_supported_languages.return_value = ["german"]
        model.generate_custom_voice.return_value = ([[0.0] * 240], 24000)
        factory = MagicMock(return_value=model)

        def write(path, data, rate, subtype):
            with wave.open(path, "wb") as stream:
                stream.setnchannels(1)
                stream.setsampwidth(2)
                stream.setframerate(rate)
                stream.writeframes(b"\0\0" * len(data))

        with tempfile.TemporaryDirectory() as temporary, patch.dict(sys.modules, {"torch": torch, "soundfile": SimpleNamespace(write=write), "qwen_tts": SimpleNamespace(Qwen3TTSModel=SimpleNamespace(from_pretrained=factory))}):
            provider = worker.load_provider("tts", {"path": "/model", "provider": "qwen3_tts", "speaker": "Ryan"}, 4)
            result = provider({"german": "Guten Morgen.", "_seed": 1001}, Path(temporary) / "sample.wav")
        self.assertEqual(factory.call_args.kwargs["device_map"], "cpu")
        self.assertEqual(factory.call_args.kwargs["attn_implementation"], "eager")
        self.assertEqual(model.generate_custom_voice.call_args.kwargs["language"], "German")
        self.assertEqual(model.generate_custom_voice.call_args.kwargs["speaker"], "Ryan")
        self.assertEqual(result["audio_seconds"], 0.01)
        torch.manual_seed.assert_called_once_with(1001)

    def test_review_is_portable_and_escapes_input(self):
        report = {"text_cases": [{"id": "sample", "german": "<script> & Hallo"}], "results": {"voice": {"measurements": [{"case": "sample", "run": 0, "status": "ok", "seconds": 1, "result": {"audio_path": "/server/voice/sample-0.wav", "audio_seconds": 2}}]}}}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            review.write_review(report, root)
            page = (root / "review.html").read_text()
            self.assertIn('src="voice/sample-0.wav"', page)
            self.assertNotIn("/server/", page)
            self.assertIn("&lt;script&gt; &amp; Hallo", page)
            self.assertNotIn("<script>", page)
            self.assertIn("sample,voice", (root / "listening_scores.csv").read_text())


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_preserves_stderr(self):
        command = [sys.executable, "-c", "import sys,time; print('decoder warning', file=sys.stderr, flush=True); time.sleep(10)"]
        result, logs = await runner.run_child({}, 0.3, command)
        self.assertEqual(result["status"], "timeout")
        self.assertIn("decoder warning", logs)

    async def test_timeout_recovers_completed_measurements(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            models = root / "models.json"
            models.write_text('{"voice": {"path": "fake"}}')
            args = SimpleNamespace(models=models, audio_manifest=None, output=root / "results", cores=4, runs=2, timeout=1)

            async def child(config, timeout, command=None):
                (Path(config["output"]) / "progress.json").write_text(json.dumps({"status": "running", "measurements": [{"case": "everyday", "status": "ok"}]}))
                return {"status": "timeout"}, "warning"

            with patch.object(runner, "run_child", side_effect=child):
                self.assertEqual(await runner.run(args), 1)
            result = json.loads((args.output / "report.json").read_text())["results"]["voice"]
            self.assertEqual(result["status"], "timeout")
            self.assertEqual(len(result["measurements"]), 1)
            self.assertEqual(result["warning_count"], 1)


if __name__ == "__main__":
    unittest.main()
