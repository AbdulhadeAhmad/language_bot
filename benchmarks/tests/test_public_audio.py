import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
import wave
import sys
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("prepare_audio", ROOT / "prepare_audio.py")
audio = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audio)
with patch.dict(sys.modules, {"prepare_audio": audio}):
    spec = importlib.util.spec_from_file_location("run_all", ROOT / "run_all.py")
    run_all = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(run_all)


def wav_bytes(seconds=10):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(audio.RATE)
        output.writeframes(b"\0\0" * (seconds * audio.RATE))
    return buffer.getvalue()


def recordings(count=12):
    data = wav_bytes()
    return [{"row_idx": index, "raw_transcription": f"Das ist Satz {index}.", "audio_bytes": data} for index in range(count)]


class PublicAudioTests(unittest.TestCase):
    def test_wrapper_reuses_models_and_writes_summary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "artifacts").mkdir()
            (root / "artifacts/models.json").write_text("{}")
            output = root / "results"

            def execute(command, check):
                output.mkdir()
                (output / "report.json").write_text(json.dumps({"results": {"asr_base": {"status": "ok", "load_seconds": 2.0, "peak_rss_mib": 250}}}))
                self.assertIn("--audio-manifest", command)
                self.assertIn("--cores", command)
                return SimpleNamespace(returncode=0)

            with patch.object(run_all, "ROOT", root), patch.object(run_all, "prepare", return_value=root / "audio.json") as prepare, patch.object(run_all.subprocess, "run", side_effect=execute), patch.object(sys, "argv", ["run_all.py", "--output", str(output)]):
                self.assertEqual(run_all.main(), 0)
                prepare.assert_called_once()
            self.assertIn("asr_base | ok", (output / "summary.md").read_text())

    def test_wrapper_refuses_existing_output_before_preparing_audio(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "artifacts").mkdir()
            (root / "artifacts/models.json").write_text("{}")
            with patch.object(run_all, "ROOT", root), patch.object(run_all, "prepare") as prepare, patch.object(sys, "argv", ["run_all.py", "--output", str(root)]):
                with self.assertRaises(SystemExit):
                    run_all.main()
                prepare.assert_not_called()

    def test_creates_six_short_cases_and_exact_two_minute_case(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = audio.create_fixtures(root, recordings(), "a" * 40)
            rows = json.loads(manifest.read_text())
            self.assertEqual(len(rows), 7)
            stress = rows[-1]
            with wave.open(str(root / stress["path"]), "rb") as stream:
                self.assertEqual(stream.getnframes(), 120 * audio.RATE)
                self.assertEqual(stream.getnchannels(), 1)
            self.assertEqual(stress["reference_german"], " ".join(f"Das ist Satz {index}." for index in range(11)))
            self.assertAlmostEqual(stress["trailing_silence_seconds"], 7.5)
            self.assertEqual(audio.validate_cache(root), manifest)
            attribution = json.loads((root / "attribution.json").read_text())
            self.assertEqual(attribution["revision"], "a" * 40)
            self.assertEqual(attribution["license"], "CC-BY-4.0")

    def test_cached_audio_modification_is_detected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = audio.create_fixtures(root, recordings(), "b" * 40)
            rows = json.loads(manifest.read_text())
            (root / rows[0]["path"]).write_bytes(b"corrupted")
            with self.assertRaisesRegex(ValueError, "checksum"):
                audio.validate_cache(root)

    def test_insufficient_speech_does_not_publish_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(ValueError, "Not enough"):
                audio.create_fixtures(root, recordings(3), "c" * 40)
            self.assertFalse((root / "manifest.json").exists())

    def test_pcm_wave_decoding_preserves_frames(self):
        self.assertEqual(len(audio.pcm16(wav_bytes(2))), 2 * audio.RATE * 2)

    def test_fetch_rejects_non_https_before_network_access(self):
        with self.assertRaises(ValueError):
            audio.fetch("file:///etc/passwd")


if __name__ == "__main__":
    unittest.main()
