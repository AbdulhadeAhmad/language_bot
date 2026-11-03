try:
    from storage import ARTIFACTS, MODELS
except ModuleNotFoundError:
    from benchmarks.storage import ARTIFACTS, MODELS
"""Download candidate snapshots once; record immutable revisions and voice hashes."""

import hashlib
import argparse
import json
from pathlib import Path


def main() -> None:
    from huggingface_hub import hf_hub_download, snapshot_download

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extended-tts", action="store_true", help="Add Thorsten high, Kerstin low, and Ramona low")
    parser.add_argument("--qwen", action="store_true", help="Add Qwen3-TTS 0.6B with Ryan, Aiden, and Serena")
    args = parser.parse_args()
    root = MODELS
    root.mkdir(parents=True, exist_ok=True)
    manifest = root / "models.json"
    models = json.loads(manifest.read_text()) if manifest.exists() else {}

    def save():
        temporary = manifest.with_suffix(".tmp")
        temporary.write_text(json.dumps(models, indent=2) + "\n")
        temporary.replace(manifest)

    def available(key):
        return key in models and Path(models[key]["path"]).exists()

    for key, repo in {
        "asr_base": "Systran/faster-whisper-base",
        "asr_small": "Systran/faster-whisper-small",
        "translation": "Helsinki-NLP/opus-mt-de-en",
    }.items():
        if available(key):
            print(f"Reusing {key}", flush=True)
            continue
        path = Path(snapshot_download(repo, cache_dir=root / "hub"))
        models[key] = {"path": str(path.resolve()), "repository": repo, "revision": path.name}
        save()

    # Resolve the voice repository once, then download every asset at that revision.
    from huggingface_hub import HfApi

    repo = "rhasspy/piper-voices"
    voices = [("thorsten", "medium"), ("eva_k", "x_low"), ("karlsson", "low")]
    if args.extended_tts:
        voices += [("thorsten", "high"), ("kerstin", "low"), ("ramona", "low")]
    revision = None
    for name, quality in voices:
        voice = f"de_DE-{name}-{quality}"
        if available(voice) and Path(models[voice]["path"] + ".json").exists():
            print(f"Reusing {voice}", flush=True)
            continue
        if revision is None:
            revision = HfApi().model_info(repo).sha
            if not revision:
                raise RuntimeError("Voice repository did not return an immutable revision")
        prefix = f"de/de_DE/{name}/{quality}"
        files = {}
        for filename in (f"{voice}.onnx", f"{voice}.onnx.json", "MODEL_CARD"):
            path = Path(hf_hub_download(repo, f"{prefix}/{filename}", revision=revision, cache_dir=root / "hub"))
            files[filename] = {"path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        # Keep adjacent ONNX + JSON names for Piper, without duplicating model bytes.
        voice_dir = root / "voices"
        voice_dir.mkdir(exist_ok=True)
        for filename in (f"{voice}.onnx", f"{voice}.onnx.json"):
            link = voice_dir / filename
            link.unlink(missing_ok=True)
            link.symlink_to(files[filename]["path"])
        models[voice] = {"path": str((voice_dir / f"{voice}.onnx").absolute()), "repository": repo, "revision": revision, "files": files, "kind": "tts", "provider": "piper", "speaker": name}
        save()
    if args.qwen:
        repo = "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"
        keys = [f"qwen_0_6b_{speaker.lower()}" for speaker in ("Ryan", "Aiden", "Serena")]
        if not all(available(key) for key in keys):
            path = Path(snapshot_download(repo, cache_dir=root / "hub"))
            if not (path / "speech_tokenizer/config.json").is_file():
                raise RuntimeError("Qwen snapshot is missing its speech tokenizer")
            for key, speaker in zip(keys, ("Ryan", "Aiden", "Serena")):
                models[key] = {"path": str(path.resolve()), "repository": repo, "revision": path.name, "kind": "tts", "provider": "qwen3_tts", "speaker": speaker}
            save()
        else:
            print("Reusing Qwen3-TTS 0.6B and all three speaker configurations", flush=True)
    print(f"Candidate manifest: {root / 'models.json'}")


if __name__ == "__main__":
    main()
