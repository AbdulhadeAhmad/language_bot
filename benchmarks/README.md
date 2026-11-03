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

## Supply real German speech

Create `benchmarks/artifacts/audio/`, place recordings there, and create `benchmarks/artifacts/audio/manifest.json`:

```json
[
  {"id": "short", "path": "short.ogg", "reference_german": "Ich muss morgen früh arbeiten."},
  {"id": "two_minutes", "path": "two_minutes.ogg", "reference_german": "The actual complete German transcript of this recording"}
]
```

Use actual German reference text in both entries. Include clear speech, typical learner pronunciation, and a full two-minute recording; Telegram OGG/Opus is appropriate. Human recordings are required for quality acceptance: synthetic audio alone does not establish ASR quality. Paths are relative to the manifest. No recordings or transcripts are sent to an inference service; they remain local and ignored by git.

## Run

```bash
uv run --project benchmarks --no-sync python benchmarks/run.py \
  --audio-manifest benchmarks/artifacts/audio/manifest.json \
  --output benchmarks/artifacts/results-01 \
  --cores 4 --runs 3 --timeout 1800
```

Each candidate runs sequentially in a fresh subprocess constrained to up to four available CPUs. A per-candidate timeout kills and reaps that subprocess; other candidates still run. Existing output directories are refused to protect evidence. Omit the audio manifest to run translation/TTS only, but the report marks ASR skipped and the command exits nonzero because the suite is incomplete.

The script records peak Linux process RSS and flags the 10 GiB inference budget. This measures memory but **does not impose a hard memory limit**. Run without other model workloads. Models are loaded one at a time; these measurements do not establish the safety of keeping all models resident together.

## Read and evaluate results

`report.json` records host/runtime information, the lockfile hash, model revisions, input hashes, model-load time, every inference duration, output text or WAV paths, audio duration, peak RSS, and errors. Each candidate's `stderr.log` captures dependency diagnostics. Run 0 of the first case is first inference after loading; subsequent runs are warm. Loading is a fresh-process measurement with cached weights, **not** a guaranteed cold operating-system disk cache. Real-time factor can be calculated as inference seconds divided by audio seconds.

The suite includes six German text samples and an exact 1,000-character stress input. Inspect every translation against the supplied meaning references, and listen to the generated WAV files for all three voices. Compare ASR output against your human transcripts; the benchmark does not substitute string matching for quality judgment.

Complete this decision record after running:

| Acceptance item | Result / evidence |
|---|---|
| Target hardware confirmed | Pending |
| All dependencies install and providers execute | Pending |
| Two-minute human recording transcribed without failure | Pending |
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

These tests exercise subprocess failure/timeout handling, event-loop responsiveness, repeat measurement, fixture boundaries, and input validation. They are not real-model benchmarks or bot acceptance tests.
