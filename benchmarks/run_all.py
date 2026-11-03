try:
    from storage import ARTIFACTS, MODELS
except ModuleNotFoundError:
    from benchmarks.storage import ARTIFACTS, MODELS
"""Download public speech as needed, run every model, and print a concise summary."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import statistics

from prepare_audio import prepare
from run import select_models, model_kind
from review import write_review

ROOT = Path(__file__).resolve().parent


def summary(report: dict) -> str:
    lines = ["# Benchmark execution summary", "", "| Candidate | Status | Load (s) | Peak RSS (MiB) | Warm median (s) | Warnings |", "|---|---|---|---|---|---|"]
    for name, result in report["results"].items():
        load = f"{result['load_seconds']:.2f}" if "load_seconds" in result else "—"
        peak = f"{result['peak_rss_mib']:.0f}" if "peak_rss_mib" in result else "—"
        warm = [row["seconds"] for row in result.get("measurements", []) if row["status"] == "ok" and row["run"] > 0]
        median = f"{statistics.median(warm):.2f}" if warm else "—"
        lines.append(f"| {name} | {result['status']} | {load} | {peak} | {median} | {result.get('warning_count', 0)} |")
        if result.get("error"):
            lines.extend(["", f"{name}: {result['error']}", ""])
    lines.extend(["", "Warm median pools selected inputs after excluding repetition zero. Completion applies only to selected candidates. See report.json for every transcription, translation, timing, and failure. For TTS runs, open review.html for the listening comparison; scores go in listening_scores.csv. Successful execution is not automatic quality approval.", ""])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--cores", type=int, default=4)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=3600)
    parser.add_argument("--only", choices=["all", "asr", "translation", "tts"], default="all")
    parser.add_argument("--candidates", nargs="+")
    parser.add_argument("--suite", choices=["basic", "extended"], default="basic")
    parser.add_argument("--qwen-python", type=Path)
    args = parser.parse_args()
    if args.cores < 1 or args.runs < 2 or args.timeout <= 0:
        parser.error("Require positive cores/timeout and at least two runs")
    if not (MODELS / "models.json").is_file():
        parser.error("Run benchmarks/prepare.py first to download the models")
    output = args.output or ARTIFACTS / datetime.now(timezone.utc).strftime("results-%Y%m%d-%H%M%S-%f")
    if output.exists():
        parser.error("Output directory already exists; choose a new one to preserve previous results")
    models = select_models(json.loads((MODELS / "models.json").read_text()), args.only, args.candidates)
    if any(model.get("provider") == "qwen3_tts" for model in models.values()) and (args.qwen_python is None or not args.qwen_python.is_file()):
        parser.error("Qwen needs --qwen-python $LANGUAGE_BOT_DATA_DIR/venvs/qwen/bin/python; sync benchmarks/qwen first")
    command = [sys.executable, str(ROOT / "run.py"), "--output", str(output), "--cores", str(args.cores), "--runs", str(args.runs), "--timeout", str(args.timeout), "--only", args.only, "--suite", args.suite]
    if any(model_kind(key, model) == "asr" for key, model in models.items()):
        manifest = prepare(ARTIFACTS / "public-audio")
        command += ["--audio-manifest", str(manifest)]
    if args.candidates:
        command += ["--candidates", *args.candidates]
    if args.qwen_python:
        command += ["--qwen-python", str(args.qwen_python)]
    result = subprocess.run(command, check=False)
    report_path = output / "report.json"
    if report_path.exists():
        report = json.loads(report_path.read_text())
        text = summary(report)
        (output / "summary.md").write_text(text)
        if any(model_kind(key, model) == "tts" for key, model in models.items()):
            write_review(report, output)
        print(text)
        print(f"Results folder: {output.resolve()}")
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
