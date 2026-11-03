# M1: target-server CPU benchmarks

This is an isolated experiment, not the bot implementation. Candidates are **not approved production choices**. Run on the planned Linux server (4 cores, 16 GB RAM, approximately 400 GB SSD). Waiting for inference is acceptable; the future bot must use a separate worker.

## Candidates and compatibility

- ASR: faster-whisper `base` and `small`, CPU INT8, beam size 5. [Upstream](https://github.com/SYSTRAN/faster-whisper) documents CPU quantization and lazy transcription; the benchmark consumes all segments inside the measured interval.
- Translation: `Helsinki-NLP/opus-mt-de-en`, Marian with CPU PyTorch. [Model card](https://huggingface.co/Helsinki-NLP/opus-mt-de-en) declares Apache-2.0. The benchmark rejects token overflow instead of silently truncating input.
- TTS: Piper voices `de_DE-thorsten-medium`, `de_DE-eva_k-x_low`, and `de_DE-karlsson-low`. These are candidate distinct voices, subject to listening approval. [Piper](https://github.com/OHF-Voice/piper1-gpl) is GPL-3.0; voice licenses must be checked separately using the downloaded MODEL_CARD files before selection. [Python API](https://github.com/OHF-Voice/piper1-gpl/blob/main/docs/API_PYTHON.md).
- Python 3.12 is the experiment runtime. `uv.lock` locks dependencies, including CPU-only Torch. The application's Python pin has not changed; final runtime selection awaits M1 results.

## Prepare on the target server

Install Python 3.12 and uv, copy this repository to the server, then run from its root:

```bash
uv sync --project benchmarks --python python3.12 --locked
uv run --project benchmarks --no-sync python -c 'from faster_whisper import WhisperModel; from transformers import MarianMTModel, MarianTokenizer; from piper import PiperVoice; print("Provider imports OK")'
uv run --project benchmarks --no-sync python benchmarks/prepare.py
```

Preparation requires internet and downloads model weights, tokenizer files, and voice cards into ignored `benchmarks/artifacts/`. It records resolved model revisions and voice checksums in `artifacts/models.json`. Keep that manifest with your results. Preparation may download several GB and is not included in inference timing. Do not rerun preparation between comparisons without preserving the previous manifest. Runtime inference uses local files only.

If dependency downloads time out on a slow connection, retry the sync command with `UV_HTTP_TIMEOUT=120` before it. Local preparation verified dependency resolution, but package downloads timed out before installation completed; provider imports and actual model execution must still be verified on the target server.

## Run everything with supplied German speech

If you already installed dependencies and ran `prepare.py`, do not repeat those steps. Upload the updated benchmark scripts (or extract the supplied `benchmark-update.zip` from the repository root), then run:

```bash
uv run --project benchmarks --no-sync python benchmarks/run_all.py
```

This reuses `artifacts/models.json`, prepares or verifies the public German audio automatically, runs all six model candidates, and prints the results directory. No recordings, transcripts, manifest editing, new packages, or account are required. Results get a new timestamped directory on each run. The default is four CPUs, three repetitions, and a one-hour timeout per candidate.

The ready-made audio is from [Google FLEURS](https://huggingface.co/datasets/google/fleurs), German (`de_de`) test split, licensed [CC-BY-4.0](https://creativecommons.org/licenses/by/4.0/). The downloader streams the original archive at a resolved immutable revision, bypassing the dataset preview service. It selects six distinct human utterances with their provided transcripts, converts them to mono 16 kHz PCM WAV, and creates a two-minute stress sample from complete utterances with short gaps and trailing silence. No words are cut or invented. `attribution.json` records the source revision, utterances, hashes, license, and transformations.

The two-minute sample tests long-input processing; it is assembled speech, not a continuous natural conversation or a learner-pronunciation evaluation. The first six files remain separate quality examples. Public fixtures suffice to run this benchmark; personal recordings are optional for later real-world evaluation.

The supplied update ZIP includes verified audio in `benchmarks/artifacts/public-audio/`. If you upload only the scripts, the first run downloads the audio automatically. Cached audio is checksum-verified on later runs. If files are missing or modified, repair them explicitly:

```bash
uv run --project benchmarks --no-sync python benchmarks/prepare_audio.py --refresh
```

For custom limits or a specific new output directory:

```bash
uv run --project benchmarks --no-sync python benchmarks/run_all.py \
  --output benchmarks/artifacts/results-01 \
  --cores 4 --runs 3 --timeout 3600
```

The wrapper produces `summary.md` alongside `report.json`. To use your own optional recordings instead, use the lower-level `run.py --audio-manifest PATH --output NEW_DIRECTORY`; its manifest is a JSON list of `{ "id": "sample", "path": "sample.ogg", "reference_german": "Actual German transcript" }` objects, with audio paths relative to that manifest.

Each candidate runs sequentially in a fresh subprocess constrained to up to four available CPUs. A per-candidate timeout kills and reaps that subprocess; other candidates still run. Existing output directories are refused to protect evidence. Omit the audio manifest to run translation/TTS only, but the report marks ASR skipped and the command exits nonzero because the suite is incomplete.

The script records peak Linux process RSS and flags the 10 GiB inference budget. This measures memory but **does not impose a hard memory limit**. Run without other model workloads. Models are loaded one at a time; these measurements do not establish the safety of keeping all models resident together.

## Read and evaluate results

`report.json` records host/runtime information, the lockfile hash, model revisions, input hashes, model-load time, every inference duration, output text or WAV paths, audio duration, peak RSS, and errors. Each candidate's `stderr.log` captures dependency diagnostics. Run 0 of the first case is first inference after loading; subsequent runs are warm. Loading is a fresh-process measurement with cached weights, **not** a guaranteed cold operating-system disk cache. Real-time factor can be calculated as inference seconds divided by audio seconds.

The suite includes six German text samples and an exact 1,000-character stress input. Inspect every translation against the supplied meaning references, and listen to the generated WAV files for all three voices. Compare ASR output against the supplied reference transcripts; the benchmark does not substitute string matching for quality judgment.

Complete this decision record after running:

| Acceptance item | Result / evidence |
|---|---|
| Target hardware confirmed | Pending |
| All dependencies install and providers execute | Pending |
| Two-minute assembled human-speech stress fixture transcribed without failure | Pending |
| 1,000-character translation and all three TTS outputs complete | Pending |
| Each inference process stays within 10 GiB RSS | Pending |
| ASR quality accepted by owner | Pending |
| Translation quality accepted by owner | Pending |
| Three distinct voices accepted by owner | Pending |
| Voice/model licenses reviewed | Pending |
| Final Python, model revisions, voices, thread counts selected | Pending |

`execution_complete` means only that all requested candidates executed successfully. It does not mean M1 passed. `quality_approved` remains false until the owner reviews samples and the decision is recorded in `implementation_plan.md`. Failure or excessive memory requires another candidate/configuration and another measured run before M2.

Benchmark audio is deliberately retained for listening review, unlike production temporary audio. Delete unwanted local benchmark artifacts after review. Do not commit model weights, personal audio, or generated samples.

## Test the harness without model downloads

```bash
python3 -m unittest discover -s benchmarks/tests -v
```

These tests exercise subprocess failure/timeout handling, event-loop responsiveness, repeat measurement, fixture boundaries, public-audio assembly, checksum verification, and input validation. They are not real-model benchmarks or bot acceptance tests.
