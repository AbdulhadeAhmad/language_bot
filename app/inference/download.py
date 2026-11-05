"""Download only the selected runtime models into an external read-only inference mount."""

import argparse
import json
import os
import shutil
import time
from pathlib import Path

import requests
from huggingface_hub import hf_hub_download, snapshot_download


def download_with_retry(function, *args, **kwargs):
    for attempt in range(3):
        try:
            return function(*args, **kwargs)
        except (requests.RequestException, OSError) as exc:
            if attempt == 2:
                raise
            print(f"Download interrupted ({type(exc).__name__}); resuming cached files", flush=True)
            time.sleep(2 ** (attempt + 1))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--only",
        choices=["asr", "translation", "tts"],
        nargs="+",
        default=["asr", "translation", "tts"],
    )
    args = parser.parse_args()
    root = Path(os.environ.get("MODEL_DIR", "/models/runtime"))
    root.mkdir(parents=True, exist_ok=True)
    models = {
        "asr": {
            "repository": "Systran/faster-whisper-small",
            "revision": "536b0662742c02347bc0e980a01041f333bce120",
        },
        "translation": {
            "repository": "Helsinki-NLP/opus-mt-de-en",
            "revision": "1a922f3b32a8e809e17a47d4b32142d8105924e5",
        },
        "tts": {
            "repository": "rhasspy/piper-voices",
            "revision": "e7229f7d33a10e145e109b886d661984201ac3c4",
        },
    }
    for name in ("asr", "translation"):
        if name not in args.only:
            continue
        model = models[name]
        print(f"Preparing {name}: {model['repository']}", flush=True)
        download_with_retry(
            snapshot_download,
            model["repository"],
            revision=model["revision"],
            local_dir=root / name,
            allow_patterns=[
                "*.json", "*.txt", "*.spm", "*.bin", "*.safetensors", "README*", "LICENSE*"
            ],
        )
    if "tts" in args.only:
        model = models["tts"]
        directory = root / "tts"
        directory.mkdir(exist_ok=True)
        prefix = "de/de_DE/thorsten/medium/"
        for name in ("de_DE-thorsten-medium.onnx", "de_DE-thorsten-medium.onnx.json", "MODEL_CARD"):
            source = download_with_retry(
                hf_hub_download, model["repository"], prefix + name, revision=model["revision"]
            )
            shutil.copyfile(source, directory / name)
    for name in args.only:
        (root / name / "runtime-provenance.json").write_text(
            json.dumps(models[name], indent=2) + "\n"
        )
    print(f"Runtime models ready: {root}")


if __name__ == "__main__":
    main()
