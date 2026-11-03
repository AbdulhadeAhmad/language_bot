"""One model per fresh Linux process; dependencies are imported after CPU limiting."""

import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import resource
import sys
import time
import wave
from contextlib import redirect_stdout
import re
import unicodedata


def word_error_rate(reference: str, hypothesis: str):
    def words(text):
        return re.findall(r"\w+", unicodedata.normalize("NFC", text).casefold())

    expected, actual = words(reference), words(hypothesis)
    if not expected:
        return None
    previous = list(range(len(actual) + 1))
    for i, token in enumerate(expected, 1):
        current = [i]
        for j, other in enumerate(actual, 1):
            current.append(min(current[-1] + 1, previous[j] + 1, previous[j - 1] + (token != other)))
        previous = current
    return {"edits": previous[-1], "reference_words": len(expected), "rate": previous[-1] / len(expected), "normalization": "NFC, casefold, Unicode word tokens; punctuation ignored"}


def configure_cpu(cores: int) -> list[int]:
    if cores < 1:
        raise ValueError("cores must be positive")
    available = sorted(os.sched_getaffinity(0))
    selected = available[:cores]
    os.sched_setaffinity(0, selected)
    for variable in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
        os.environ[variable] = str(len(selected))
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    return selected


def load_provider(kind: str, model_config: dict, cores: int):
    model_path = model_config["path"]
    if kind == "asr":
        from faster_whisper import WhisperModel

        model = WhisperModel(model_path, device="cpu", compute_type="int8", cpu_threads=cores, num_workers=1, local_files_only=True)

        def transcribe(case, output):
            segments, info = model.transcribe(case["path"], language="de", beam_size=5)
            # faster-whisper performs inference while iterating, not at transcribe().
            text = "".join(segment.text for segment in segments).strip()
            if not text:
                raise ValueError("ASR returned empty text")
            return {"text": text, "audio_seconds": info.duration, "wer": word_error_rate(case["reference_german"], text)}

        return transcribe
    if kind == "translation":
        import torch
        from transformers import MarianMTModel, MarianTokenizer

        torch.set_num_threads(cores)
        torch.set_num_interop_threads(1)
        tokenizer = MarianTokenizer.from_pretrained(model_path, local_files_only=True)
        model = MarianMTModel.from_pretrained(model_path, local_files_only=True).eval()

        def translate(case, output):
            inputs = tokenizer(case["german"], return_tensors="pt", truncation=False)
            if inputs["input_ids"].shape[1] > model.config.max_position_embeddings:
                raise ValueError("Input exceeds model token capacity; needs explicit chunking before production")
            with torch.inference_mode():
                tokens = model.generate(**inputs, max_new_tokens=512, num_beams=4)
            if tokens[0, -1].item() != model.config.eos_token_id:
                raise ValueError("Translation hit output limit without EOS")
            text = tokenizer.decode(tokens[0], skip_special_tokens=True)
            if not text.strip():
                raise ValueError("Translation returned empty text")
            return {"text": text}

        return translate
    if kind == "tts" and model_config.get("provider") == "qwen3_tts":
        import torch
        import soundfile as sf
        from qwen_tts import Qwen3TTSModel

        torch.set_num_threads(cores)
        torch.set_num_interop_threads(1)
        model = Qwen3TTSModel.from_pretrained(model_path, device_map="cpu", dtype=torch.float32, attn_implementation="eager", local_files_only=True)
        speaker = model_config["speaker"]
        if speaker.lower() not in {value.lower() for value in model.get_supported_speakers()}:
            raise ValueError(f"Qwen model does not support speaker {speaker}")
        if "german" not in {value.lower() for value in model.get_supported_languages()}:
            raise ValueError("Qwen model does not support German")

        def synthesize_qwen(case, output):
            seed = case.get("_seed", 0)
            torch.manual_seed(seed)
            with torch.inference_mode():
                wavs, sample_rate = model.generate_custom_voice(text=case["german"], language="German", speaker=speaker, max_new_tokens=2048)
            if len(wavs) != 1 or sample_rate <= 0 or len(wavs[0]) == 0:
                raise ValueError("Qwen returned empty or unexpected audio")
            sf.write(str(output), wavs[0], sample_rate, subtype="PCM_16")
            return {"audio_seconds": len(wavs[0]) / sample_rate, "audio_path": str(output), "sha256": hashlib.sha256(output.read_bytes()).hexdigest(), "seed": seed, "max_new_tokens": 2048, "completion_requires_listening_review": True}

        return synthesize_qwen
    if kind == "tts":
        from piper import PiperVoice

        model = PiperVoice.load(model_path, use_cuda=False)

        def synthesize(case, output):
            with wave.open(str(output), "wb") as stream:
                model.synthesize_wav(case["german"], stream)
            with wave.open(str(output), "rb") as stream:
                duration = stream.getnframes() / stream.getframerate()
            if duration <= 0:
                raise ValueError("TTS returned empty audio")
            return {"audio_seconds": duration, "audio_path": str(output), "sha256": hashlib.sha256(output.read_bytes()).hexdigest()}

        return synthesize
    raise ValueError(f"Unknown provider: {kind}")


def measure(config, provider_loader=load_provider):
    cpus = configure_cpu(config["cores"])
    start = time.perf_counter()
    provider = provider_loader(config["kind"], config["model"], len(cpus))
    load_seconds = time.perf_counter() - start
    rows = []
    for case in config["cases"]:
        for run in range(config["runs"]):
            output = Path(config["output"]) / f"{case['id']}-{run}.wav"
            start = time.perf_counter()
            try:
                result = provider({**case, "_seed": 1000 + run}, output)
                status = "ok"
            except Exception as exc:
                output.unlink(missing_ok=True)
                result = {"error_type": type(exc).__name__, "error": str(exc)}
                status = "failed"
            elapsed = time.perf_counter() - start
            rows.append({"case": case["id"], "run": run, "status": status, "seconds": elapsed, "result": result})
            progress = Path(config["output"]) / "progress.json"
            temporary = progress.with_suffix(".tmp")
            temporary.write_text(json.dumps({"status": "running", "load_seconds": load_seconds, "peak_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, "measurements": rows}, ensure_ascii=False))
            temporary.replace(progress)
            print(f"{case['id']} run {run + 1}/{config['runs']}: {status} ({elapsed:.2f}s)", file=sys.stderr, flush=True)
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    return {
        "status": "ok" if rows and all(row["status"] == "ok" for row in rows) else "failed",
        "python": platform.python_version(), "cpus": cpus,
        "load_seconds": load_seconds, "peak_rss_mib": peak,
        "within_10_gib_budget": peak <= 10 * 1024, "measurements": rows,
        "packages": {name: importlib.metadata.version(name) for name in config.get("packages", [])},
    }


def main():
    config = json.loads(sys.stdin.read())
    packages = {}
    for name in config.get("packages", []):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = "not installed"
    try:
        # Some providers print startup notices to stdout; keep the report channel JSON-only.
        with redirect_stdout(sys.stderr):
            result = measure(config)
    except Exception as exc:
        result = {"status": "failed", "error_type": type(exc).__name__, "error": str(exc), "peak_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024}
    result["packages"] = packages
    progress = Path(config["output"]) / "progress.json"
    temporary = progress.with_suffix(".tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False))
    temporary.replace(progress)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
