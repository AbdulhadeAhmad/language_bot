"""Download public speech as needed, run every model, and print a concise summary."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

from prepare_audio import prepare

ROOT = Path(__file__).resolve().parent


def summary(report: dict) -> str:
    lines = ["# Benchmark execution summary", "", "| Candidate | Status | Load (s) | Peak RSS (MiB) |", "|---|---|---|---|"]
    for name, result in report["results"].items():
        load = f"{result['load_seconds']:.2f}" if "load_seconds" in result else "—"
        peak = f"{result['peak_rss_mib']:.0f}" if "peak_rss_mib" in result else "—"
        lines.append(f"| {name} | {result['status']} | {load} | {peak} |")
        if result.get("error"):
            lines.extend(["", f"{name}: {result['error']}", ""])
    lines.extend(["", "See report.json for every transcription, translation, timing, and failure. Listen to the WAV files in each voice's folder. Successful execution is not automatic quality approval.", ""])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--cores", type=int, default=4)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=3600)
    args = parser.parse_args()
    if args.cores < 1 or args.runs < 2 or args.timeout <= 0:
        parser.error("Require positive cores/timeout and at least two runs")
    if not (ROOT / "artifacts/models.json").is_file():
        parser.error("Run benchmarks/prepare.py first to download the models")
    output = args.output or ROOT / "artifacts" / datetime.now(timezone.utc).strftime("results-%Y%m%d-%H%M%S-%f")
    if output.exists():
        parser.error("Output directory already exists; choose a new one to preserve previous results")
    manifest = prepare(ROOT / "artifacts/public-audio")
    result = subprocess.run([sys.executable, str(ROOT / "run.py"), "--audio-manifest", str(manifest), "--output", str(output), "--cores", str(args.cores), "--runs", str(args.runs), "--timeout", str(args.timeout)], check=False)
    report_path = output / "report.json"
    if report_path.exists():
        text = summary(json.loads(report_path.read_text()))
        (output / "summary.md").write_text(text)
        print(text)
        print(f"Results folder: {output.resolve()}")
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
