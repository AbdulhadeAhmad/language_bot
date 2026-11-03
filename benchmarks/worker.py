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


def load_provider(kind: str, model_path: str, cores: int):
    if kind == "asr":
        from faster_whisper import WhisperModel

        model = WhisperModel(model_path, device="cpu", compute_type="int8", cpu_threads=cores, num_workers=1, local_files_only=True)

        def transcribe(case, output):
            segments, info = model.transcribe(case["path"], language="de", beam_size=5)
            # faster-whisper performs inference while iterating, not at transcribe().
            text = "".join(segment.text for segment in segments).strip()
            if not text:
                raise ValueError("ASR returned empty text")
            return {"text": text, "audio_seconds": info.duration}

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
    provider = provider_loader(config["kind"], config["model"]["path"], len(cpus))
    load_seconds = time.perf_counter() - start
    rows = []
    for case in config["cases"]:
        for run in range(config["runs"]):
            output = Path(config["output"]) / f"{case['id']}-{run}.wav"
            start = time.perf_counter()
            try:
                result = provider(case, output)
                status = "ok"
            except Exception as exc:
                output.unlink(missing_ok=True)
                result = {"error_type": type(exc).__name__, "error": str(exc)}
                status = "failed"
            elapsed = time.perf_counter() - start
            rows.append({"case": case["id"], "run": run, "status": status, "seconds": elapsed, "result": result})
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
    try:
        result = measure(config)
    except Exception as exc:
        result = {"status": "failed", "error_type": type(exc).__name__, "error": str(exc), "peak_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
