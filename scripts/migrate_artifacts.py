"""Copy legacy benchmark artifacts to external storage, verify, then rewrite live paths.

Never removes sources or overwrites differing destination files. Historical reports
retain their original recorded server paths. Run before new downloads.
"""

import argparse
import hashlib
import json
import os
import shutil
from pathlib import Path


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def migrate(source, target):
    source, target = source.resolve(), target.expanduser().resolve()
    if target == source or source in target.parents:
        raise ValueError("Destination must be outside the source tree")

    def destination(path):
        relative = path.relative_to(source)
        group = "models" if relative.parts[0] in {"hub", "voices", "models.json"} else "benchmarks"
        return target / group / relative

    files = [p for p in source.rglob("*") if p.is_file()]
    manifest = source / "models.json"
    for path in files:
        if path == manifest:
            continue
        dest = destination(path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            if digest(path) != digest(dest):
                raise ValueError(f"Destination differs: {dest}")
        else:
            # Dereference cache/voice links so copied files never depend on the old repo.
            shutil.copy2(path, dest)
        if digest(path) != digest(dest):
            raise ValueError(f"Checksum mismatch: {dest}")
    if manifest.exists():

        def rewrite(value):
            if isinstance(value, dict):
                return {key: rewrite(item) for key, item in value.items()}
            if isinstance(value, list):
                return [rewrite(item) for item in value]
            if isinstance(value, str) and value.startswith(str(source) + "/"):
                return str(destination(Path(value)))
            return value

        dest = destination(manifest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        rewritten = json.dumps(rewrite(json.loads(manifest.read_text())), indent=2) + "\n"
        if dest.exists() and dest.read_text() != rewritten:
            raise ValueError(f"Destination manifest differs: {dest}")
        # Preserve the source manifest verbatim alongside the relocated version.
        original = dest.with_name("models.original.json")
        if original.exists() and digest(original) != digest(manifest):
            raise ValueError("Original manifest differs")
        shutil.copy2(manifest, original)
        dest.write_text(rewritten)
    print(f"Verified {len(files)} files. Sources retained at {source}. Destination: {target}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source", type=Path, default=Path(__file__).resolve().parents[1] / "benchmarks/artifacts"
    )
    parser.add_argument(
        "--destination",
        type=Path,
        default=Path(os.environ.get("LANGUAGE_BOT_DATA_DIR", "~/language_bot_data")).expanduser(),
    )
    args = parser.parse_args()
    migrate(args.source, args.destination)
