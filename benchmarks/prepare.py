"""Download candidate snapshots once; record immutable revisions and voice hashes."""

import hashlib
import json
from pathlib import Path


def main() -> None:
    from huggingface_hub import hf_hub_download, snapshot_download

    root = Path(__file__).parent / "artifacts"
    root.mkdir(exist_ok=True)
    models = {}
    for key, repo in {
        "asr_base": "Systran/faster-whisper-base",
        "asr_small": "Systran/faster-whisper-small",
        "translation": "Helsinki-NLP/opus-mt-de-en",
    }.items():
        path = Path(snapshot_download(repo, cache_dir=root / "hub"))
        models[key] = {"path": str(path.resolve()), "repository": repo, "revision": path.name}

    # Resolve the voice repository once, then download every asset at that revision.
    from huggingface_hub import HfApi

    repo = "rhasspy/piper-voices"
    revision = HfApi().model_info(repo).sha
    if not revision:
        raise RuntimeError("Voice repository did not return an immutable revision")
    for name, quality in (("thorsten", "medium"), ("eva_k", "x_low"), ("karlsson", "low")):
        voice = f"de_DE-{name}-{quality}"
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
        models[voice] = {"path": str((voice_dir / f"{voice}.onnx").absolute()), "repository": repo, "revision": revision, "files": files}
    (root / "models.json").write_text(json.dumps(models, indent=2) + "\n")
    print(f"Candidate manifest: {root / 'models.json'}")


if __name__ == "__main__":
    main()
