"""Fetch public FLEURS German speech and build a reusable ASR fixture manifest."""

import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import tempfile
import tarfile
import time
import urllib.request
import wave

ROOT = Path(__file__).resolve().parent
DATASET = "https://huggingface.co/datasets/google/fleurs"
RATE = 16000
LICENSE = "https://creativecommons.org/licenses/by/4.0/"


def fetch(url: str, limit: int = 32 * 1024 * 1024) -> bytes:
    if not url.startswith("https://"):
        raise ValueError("Expected an HTTPS dataset asset")
    for attempt in range(3):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "language-bot-benchmark/0.1"})
            with urllib.request.urlopen(request, timeout=120) as response:
                data = response.read(limit + 1)
            if len(data) > limit:
                raise ValueError("Dataset response exceeds the expected size")
            return data
        except (OSError, TimeoutError):
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError("Download failed")


def pcm16(data: bytes) -> bytes:
    """Keep compatible WAVs unchanged; decode other formats using existing PyAV."""
    try:
        with wave.open(io.BytesIO(data), "rb") as source:
            if (source.getnchannels(), source.getsampwidth(), source.getframerate()) == (1, 2, RATE):
                return source.readframes(source.getnframes())
    except (wave.Error, EOFError):
        pass
    try:
        import av
    except ImportError:
        # Allows verification with system FFmpeg even without the benchmark venv.
        import shutil
        import subprocess

        if not shutil.which("ffmpeg"):
            raise RuntimeError("Audio decoding needs installed benchmark dependencies (PyAV) or ffmpeg")
        return subprocess.run(["ffmpeg", "-v", "error", "-i", "pipe:0", "-f", "s16le", "-ac", "1", "-ar", str(RATE), "pipe:1"], input=data, capture_output=True, check=True, timeout=60).stdout

    resampler = av.AudioResampler(format="s16", layout="mono", rate=RATE)
    parts = []
    with av.open(io.BytesIO(data)) as source:
        for frame in source.decode(audio=0):
            for converted in resampler.resample(frame):
                parts.append(converted.to_ndarray().tobytes())
        for converted in resampler.resample(None):
            parts.append(converted.to_ndarray().tobytes())
    return b"".join(parts)


def write_wav(path: Path, pcm: bytes) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(RATE)
        output.writeframes(pcm)


def validate_cache(directory: Path):
    manifest = directory / "manifest.json"
    if not manifest.exists():
        return None
    rows = json.loads(manifest.read_text())
    if not rows:
        raise ValueError("Cached audio manifest is empty")
    for row in rows:
        path = directory / row["path"]
        if path.parent != directory or not path.is_file():
            raise ValueError("Cached audio is missing; use --refresh to download it again")
        if hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]:
            raise ValueError("Cached audio checksum mismatch; use --refresh")
    return manifest


def original_recordings(revision: str):
    base = f"{DATASET}/resolve/{revision}/data/de_de"
    tsv = fetch(f"{base}/test.tsv").decode("utf-8")
    records = {}
    for index, row in enumerate(csv.reader(io.StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE)):
        if len(row) < 7 or not row[2].strip():
            raise ValueError("Unexpected FLEURS transcript format")
        records[row[1]] = {"row_idx": index, "id": row[0], "raw_transcription": row[2], "filename": row[1]}
    request = urllib.request.Request(f"{base}/audio/test.tar.gz", headers={"User-Agent": "language-bot-benchmark/0.1"})
    seen = set()
    # Stream only as far as needed. Never extract archive paths onto the filesystem.
    with urllib.request.urlopen(request, timeout=120) as response, tarfile.open(fileobj=response, mode="r|gz") as archive:
        for member in archive:
            name = Path(member.name).name
            record = records.get(name)
            if not member.isfile() or record is None or record["raw_transcription"] in seen:
                continue
            if member.size > 32 * 1024 * 1024:
                raise ValueError("Unexpectedly large speech sample")
            source = archive.extractfile(member)
            if source is None:
                raise ValueError("Could not read speech sample")
            with source:
                data = source.read()
            seen.add(record["raw_transcription"])
            yield {**record, "audio_bytes": data}


def create_fixtures(directory: Path, recordings, revision: str) -> Path:
    """Use complete utterances; never truncate speech or invent a transcript."""
    directory.mkdir(parents=True, exist_ok=True)
    individual = []
    source_records = []
    combined = bytearray()
    combined_text = []
    selected_ids = []
    silence = b"\0" * (RATE // 4 * 2)  # 250 ms between complete utterances.
    maximum_bytes = 120 * RATE * 2
    for row in recordings:
        transcript = row["raw_transcription"].strip()
        if not transcript:
            raise ValueError("Missing reference transcript")
        index = int(row["row_idx"])
        name = f"fleurs_de_{index:04d}"
        print(f"Downloading German recording {index + 1} …", flush=True)
        original = row["audio_bytes"]
        pcm = pcm16(original)
        if not pcm or len(pcm) % 2:
            raise ValueError(f"Invalid or empty decoded audio: {name}")
        duration = len(pcm) / (RATE * 2)
        source_records.append({"id": name, "dataset_row": index, "dataset_id": row.get("id"), "dataset_filename": row.get("filename"), "original_sha256": hashlib.sha256(original).hexdigest(), "reference_german": transcript, "duration_seconds": duration})
        if len(individual) < 6:
            path = directory / f"{name}.wav"
            write_wav(path, pcm)
            individual.append({"id": name, "path": path.name, "reference_german": transcript, "duration_seconds": duration, "source": DATASET, "license": LICENSE, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        gap = silence if combined else b""
        if len(combined) + len(gap) + len(pcm) <= maximum_bytes:
            combined.extend(gap)
            combined.extend(pcm)
            combined_text.append(transcript)
            selected_ids.append(name)
        if len(individual) == 6 and len(combined) >= 115 * RATE * 2:
            break
    if len(individual) != 6 or len(combined) < 105 * RATE * 2:
        raise ValueError("Not enough complete public speech for the two-minute sample; no manifest was published")
    padding = (maximum_bytes - len(combined)) / (RATE * 2)
    combined.extend(b"\0" * (maximum_bytes - len(combined)))
    path = directory / "two_minutes.wav"
    write_wav(path, bytes(combined))
    individual.append({"id": "two_minutes", "path": path.name, "reference_german": " ".join(combined_text), "duration_seconds": 120, "construction": "Complete human utterances concatenated with 250ms gaps; trailing silence to exactly 120s", "trailing_silence_seconds": padding, "source_ids": selected_ids, "source": DATASET, "license": LICENSE, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    attribution = {"dataset": "google/fleurs", "config": "de_de", "split": "test", "revision": revision, "publisher": "Google", "dataset_url": DATASET, "license": "CC-BY-4.0", "license_url": LICENSE, "changes": "Decoded/resampled to mono 16kHz PCM16; two-minute stress fixture concatenates complete utterances and adds silence", "sources": source_records}
    (directory / "attribution.json").write_text(json.dumps(attribution, ensure_ascii=False, indent=2) + "\n")
    manifest = directory / "manifest.json"
    manifest.write_text(json.dumps(individual, ensure_ascii=False, indent=2) + "\n")
    return manifest


def prepare(output: Path, refresh: bool = False) -> Path:
    output = output.resolve()
    if not refresh:
        cached = validate_cache(output)
        if cached:
            print(f"Reusing verified German audio: {cached}")
            return cached
    print("Fetching public Google FLEURS German test speech (CC-BY-4.0) …", flush=True)
    revision = json.loads(fetch("https://huggingface.co/api/datasets/google/fleurs"))["sha"]
    if len(revision) != 40 or any(c not in "0123456789abcdef" for c in revision):
        raise ValueError("Dataset did not return an immutable revision")
    output.parent.mkdir(parents=True, exist_ok=True)
    # Failed downloads never publish a partially prepared manifest.
    with tempfile.TemporaryDirectory(dir=output.parent, prefix="audio-download-") as temporary:
        staging = Path(temporary)
        from contextlib import closing

        with closing(original_recordings(revision)) as recordings:
            create_fixtures(staging, recordings, revision)
        output.mkdir(exist_ok=True)
        for path in staging.iterdir():
            if path.name != "manifest.json":
                path.replace(output / path.name)
        (staging / "manifest.json").replace(output / "manifest.json")
    return output / "manifest.json"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/public-audio")
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    print(f"Audio manifest: {prepare(args.output, args.refresh)}")
