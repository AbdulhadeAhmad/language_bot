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
import tempfile

ROOT = Path(__file__).resolve().parent


def text_cases(suite="basic"):
    cases = json.loads((ROOT / "fixtures/sentences.json").read_text())
    text = " ".join(case["german"] for case in cases)
    stress = ((text + " ") * 4)[:999] + "."
    assert len(stress) == 1000
    if suite == "extended":
        cases += json.loads((ROOT / "fixtures/tts_extended.json").read_text())
    return cases + [{"id": "limit_1000", "german": stress, "reference_english": None}]


def model_kind(key, model):
    return model.get("kind", "asr" if key.startswith("asr_") else "translation" if key == "translation" else "tts")


def select_models(models, only="all", candidates=None):
    if candidates:
        missing = set(candidates) - models.keys()
        if missing:
            raise ValueError(f"Candidates not prepared: {', '.join(sorted(missing))}")
    selected = {key: value for key, value in models.items() if (only == "all" or model_kind(key, value) == only) and (not candidates or key in candidates)}
    if not selected:
        raise ValueError("No candidates match the selection")
    return selected


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
    # Write logs directly so long CPU runs can be monitored and timeout logs survive.
    error_stream = (Path(config["output"]) / "stderr.log").open("w+b") if config.get("output") else tempfile.TemporaryFile()
    with error_stream:
        process = await asyncio.create_subprocess_exec(
            *(command or [sys.executable, str(ROOT / "worker.py")]),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=error_stream,
        )
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(json.dumps(config).encode()), timeout)
        except (TimeoutError, asyncio.CancelledError) as exc:
            if process.returncode is None:
                process.kill()
            await process.communicate()
            if isinstance(exc, asyncio.CancelledError):
                raise
            error_stream.seek(0)
            return {"status": "timeout", "timeout_seconds": timeout}, error_stream.read().decode(errors="replace")
        error_stream.seek(0)
        logs = error_stream.read().decode(errors="replace")
    try:
        result = json.loads(stdout)
        if not isinstance(result, dict) or "status" not in result:
            raise ValueError("Missing report status")
    except (ValueError, TypeError):
        result = {"status": "failed", "error": "Worker exited without a valid report", "stdout": stdout.decode(errors="replace")}
    if process.returncode != 0:
        result["status"] = "failed"
    result["exit_code"] = process.returncode
    return result, logs


async def run(args):
    suite = getattr(args, "suite", "basic")
    texts = text_cases(suite)
    audios = audio_cases(args.audio_manifest)
    validate_cases(texts)
    validate_cases(audios)
    models = select_models(json.loads(args.models.read_text()), getattr(args, "only", "all"), getattr(args, "candidates", None))
    qwen_python = getattr(args, "qwen_python", None)
    if any(model.get("provider") == "qwen3_tts" for model in models.values()):
        if qwen_python is None or not Path(qwen_python).is_file():
            raise ValueError("Selected Qwen candidates require --qwen-python benchmarks/qwen/.venv/bin/python (sync that project first)")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)  # Never overwrite earlier evidence.
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(), "platform": platform.platform(),
        "host_cpu_count": os.cpu_count(), "requested_cores": args.cores,
        "asr_samples_supplied": bool(audios), "quality_approved": False,
        "suite": suite, "selection": list(models), "completion_scope": "selected candidates only",
        "text_cases": texts, "audio_cases": audios, "results": {},
        "lock_sha256": hashlib.sha256((ROOT / "uv.lock").read_bytes()).hexdigest() if (ROOT / "uv.lock").exists() else None,
    }
    for key, model in models.items():
        kind = model_kind(key, model)
        is_qwen = model.get("provider") == "qwen3_tts"
        if kind == "asr" and not audios:
            report["results"][key] = {"status": "skipped", "reason": "No human audio manifest supplied"}
            continue
        validate_cases([{"id": key}])
        directory = output / key
        directory.mkdir()
        config = {
            "kind": kind, "model": model, "cores": args.cores, "runs": args.runs,
            "cases": audios if kind == "asr" else texts, "output": str(directory),
            "packages": ["qwen-tts", "torch", "transformers", "soundfile"] if is_qwen else {"asr": ["faster-whisper", "ctranslate2", "av"], "translation": ["torch", "transformers"], "tts": ["piper-tts", "onnxruntime"]}[kind],
        }
        print(f"Benchmarking {key} …", flush=True)
        command = [str(Path(qwen_python).absolute()), str(ROOT / "worker.py")] if is_qwen else None
        result, logs = await run_child(config, args.timeout, command=command)
        progress = directory / "progress.json"
        if result["status"] in {"timeout", "failed"} and progress.exists():
            try:
                result = {**json.loads(progress.read_text()), **result}
            except (ValueError, OSError):
                pass
        result["warning_count"] = len([line for line in logs.splitlines() if "warning" in line.lower() or "Missing phoneme" in line])
        if is_qwen:
            result["environment_lock_sha256"] = hashlib.sha256((ROOT / "qwen/uv.lock").read_bytes()).hexdigest()
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
    result.add_argument("--only", choices=["all", "asr", "translation", "tts"], default="all")
    result.add_argument("--candidates", nargs="+")
    result.add_argument("--suite", choices=["basic", "extended"], default="basic")
    result.add_argument("--qwen-python", type=Path)
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
