"""Build a portable, offline listening comparison without changing benchmark data."""

import csv
import html
from pathlib import Path
from urllib.parse import quote


def write_review(report, output: Path):
    candidates = {name: result for name, result in report["results"].items() if any("audio_path" in row.get("result", {}) for row in result.get("measurements", []))}
    parts = ['<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>German voice comparison</title>', '<style>body{font:16px system-ui;max-width:1100px;margin:32px auto;padding:0 20px;color:#18212b}details{margin:20px 0;padding:16px;background:#f4f6f8;border-radius:10px}summary{cursor:pointer;font-weight:600}article{display:inline-block;vertical-align:top;margin:10px;padding:12px;background:white;border-radius:8px;max-width:310px}audio{max-width:100%}p{line-height:1.5}</style>', '<h1>German voice comparison</h1><p>Open a sentence and listen to each candidate. Judge pronunciation, naturalness, pace, and whether any words are omitted or repeated. The first successful repetition is shown. Compare Thorsten medium/high as two variants of the same speaker, not two distinct voices. Record scores in listening_scores.csv. Warnings and successful execution do not determine listening quality automatically.</p>']
    scores = []
    for case in report.get("text_cases", []):
        parts.append(f'<details><summary>{html.escape(case["id"])} — {html.escape(case["german"])}</summary>')
        parts.append(f'<p>{html.escape(case.get("focus", "General clarity and naturalness"))}</p>')
        for name, result in candidates.items():
            rows = [row for row in result.get("measurements", []) if row["case"] == case["id"] and row["status"] == "ok" and "audio_path" in row["result"]]
            parts.append(f'<article><h3>{html.escape(name)}</h3>')
            if rows:
                row = rows[0]
                path = f'{name}/{Path(row["result"]["audio_path"]).name}'
                parts.append(f'<audio controls preload="none" src="{quote(path)}"></audio><p>Generation: {row["seconds"]:.2f}s · Audio: {row["result"]["audio_seconds"]:.2f}s<br>Candidate warnings: {result.get("warning_count", 0)}</p>')
                scores.append([case["id"], name, "", "", "", ""])
            else:
                parts.append('<p>No completed audio for this sentence. See report.json.</p>')
            parts.append('</article>')
        parts.append('</details>')
    parts.append('</html>')
    (output / "review.html").write_text("\n".join(parts), encoding="utf-8")
    with (output / "listening_scores.csv").open("w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(["case", "candidate", "pronunciation_1_to_5", "naturalness_1_to_5", "complete_yes_no", "notes"])
        writer.writerows(scores)
