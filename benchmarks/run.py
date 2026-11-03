"""Async orchestration of sequential, process-isolated model benchmarks."""

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import sys

ROOT = Path(__file__).resolve().parent


def text_cases():
    cases = json.loads((ROOT / "fixtures/sentences.json").read_text())
    text = " ".join(case["german"] for case in cases)
    stress = ((text + " ") * 4)[:999] + "."
    assert len(stress) == 1000
    return cases + [{"id": "limit_1000", "german": stress, "reference_english": None}]


def audio_cases(path):
    if path is None:
        return []
    rows = json.loads(path.read_text())
    for row in rows:
        audio = (path.parent / row["path"]).resolve()
        if not audio.is_file() or not row.get("reference_german", "").strip():
            raise ValueError("Each ASR sample needs an existing file and reference_german")
        row["path"] = str(audio)
        row["sha256"] = hashlib.sha256(audio.read_bytes()).hexdigest()
    return rows


def validate_cases(cases):
    ids = [case["id"] for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("Case IDs must be unique")
    if any(not value or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in value) for value in ids):
        raise ValueError("Case IDs must contain only letters, digits, underscores, and hyphens")


async def run_child(config, timeout, command=None):
    process = await asyncio.create_subprocess_exec(
        *(command or [sys.executable, str(ROOT / "worker.py")]),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(json.dumps(config).encode()), timeout)
    except (TimeoutError, asyncio.CancelledError) as exc:
        if process.returncode is None:
            process.kill()
        await process.communicate()
        if isinstance(exc, asyncio.CancelledError):
            raise
        return {"status": "timeout", "timeout_seconds": timeout}, ""
    try:
        result = json.loads(stdout)
        if not isinstance(result, dict) or "status" not in result:
            raise ValueError("Missing report status")
    except (ValueError, TypeError):
        result = {"status": "failed", "error": "Worker exited without a valid report", "stdout": stdout.decode(errors="replace")}
    if process.returncode != 0:
        result["status"] = "failed"
    result["exit_code"] = process.returncode
    return result, stderr.decode(errors="replace")


async def run(args):
    texts = text_cases()
    audios = audio_cases(args.audio_manifest)
    validate_cases(texts)
    validate_cases(audios)
    models = json.loads(args.models.read_text())
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)  # Never overwrite earlier evidence.
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(), "platform": platform.platform(),
        "host_cpu_count": os.cpu_count(), "requested_cores": args.cores,
        "asr_samples_supplied": bool(audios), "quality_approved": False,
        "text_cases": texts, "audio_cases": audios, "results": {},
        "lock_sha256": hashlib.sha256((ROOT / "uv.lock").read_bytes()).hexdigest() if (ROOT / "uv.lock").exists() else None,
    }
    for key, model in models.items():
        kind = "asr" if key.startswith("asr_") else "translation" if key == "translation" else "tts"
        if kind == "asr" and not audios:
            report["results"][key] = {"status": "skipped", "reason": "No human audio manifest supplied"}
            continue
        validate_cases([{"id": key}])
        directory = output / key
        directory.mkdir()
        config = {
            "kind": kind, "model": model, "cores": args.cores, "runs": args.runs,
            "cases": audios if kind == "asr" else texts, "output": str(directory),
            "packages": {"asr": ["faster-whisper", "ctranslate2"], "translation": ["torch", "transformers"], "tts": ["piper-tts", "onnxruntime"]}[kind],
        }
        print(f"Benchmarking {key} …", flush=True)
        result, logs = await run_child(config, args.timeout)
        (directory / "stderr.log").write_text(logs)
        report["results"][key] = {"model": model, **result}
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    report["execution_complete"] = bool(report["results"]) and all(r["status"] == "ok" for r in report["results"].values())
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(f"Report: {output / 'report.json'}; quality approval remains pending.")
    return 0 if report["execution_complete"] else 1


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--models", type=Path, default=ROOT / "artifacts/models.json")
    result.add_argument("--audio-manifest", type=Path)
    result.add_argument("--output", type=Path, required=True)
    result.add_argument("--cores", type=int, default=4)
    result.add_argument("--runs", type=int, default=3)
    result.add_argument("--timeout", type=float, default=1800, help="Per-model timeout, including every case and repetition")
    return result


if __name__ == "__main__":
    args = parser().parse_args()
    if args.cores < 1 or args.runs < 2 or args.timeout <= 0:
        raise SystemExit("Require positive cores/timeout and at least two runs")
    sys.exit(asyncio.run(run(args)))
