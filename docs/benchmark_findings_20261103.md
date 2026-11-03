# First target-server benchmark: findings

Source: `benchmarks/artifacts/results-20261103-143937-431134/report.json`.
Report SHA-256: `f128a8b3cef9c693929ab492e3075c065d9aa3570f38cf63ef910270503e1a23`.
Reported environment: Linux x86_64, Python 3.12.3, four logical CPUs, affinity restricted to all four. Total system RAM is not recorded in this report.

## Transfer and execution checks

- Report, summary, and all six candidate logs are present.
- All 63 generated WAVs match their report checksums. WAV headers are readable and durations agree with the report.
- Translation and each TTS voice completed all 21 measurements: seven inputs, three repetitions each.
- Both ASR candidates failed all 21 measurements before transcription. Their reported memory figures reflect loading and failure, not successful ASR inference.

## Performance

Warm medians exclude repetition zero and use the remaining two repetitions. Short-input medians pool the six ordinary sentences. These are inference times; model loading is separate.

| Candidate | Short input median | 1,000-character median | Load | Peak process RSS |
|---|---:|---:|---:|---:|
| OPUS-MT translation | 0.309 s | 9.272 s | 5.44 s | 731 MiB |
| Piper Thorsten medium | 0.152 s | 2.693 s | 1.97 s | 283 MiB |
| Piper Eva x-low | 0.101 s | 1.594 s | 1.93 s | 225 MiB |
| Piper Karlsson low | 0.128 s | 2.248 s | 1.92 s | 262 MiB |

Translation/TTS fit comfortably within the per-process 10 GiB inference budget on this sample. This does not measure concurrent model residency, overall application memory, or bot responsiveness.

## ASR failure: incompatible decoder dependency

Every failure is `TypeError: open() got an unexpected keyword argument 'metadata_errors'`. faster-whisper 1.2.1 is recorded in the report; the benchmark lock resolves PyAV 19.0.2. PyAV 19 removed this argument, while faster-whisper 1.2.1 still passes it. This exact incompatibility is documented in [upstream issue 1589](https://github.com/SYSTRAN/faster-whisper/issues/1589).

Next change: constrain PyAV to `<19`, regenerate the dependency lock, synchronize the server environment, and rerun ASR. Prefer a released compatible dependency set over patching installed packages. Record PyAV's installed version in future ASR reports as well. No ASR throughput or accuracy conclusion can be drawn from this run.

## Quality observations

- OPUS-MT consistently translated `Ich muss morgen früh arbeiten.` as `I have to work in the morning.` in all three repetitions. This loses explicit tomorrow/early meaning. Technical success is not translation-quality approval.
- `Termin` became `date` in the appointment example, which is less precise in context. Several other short translations preserve the intended meaning. The 1,000-character stress input itself ends mid-phrase, so its incomplete final English phrase is not by itself evidence of model truncation.
- Eva and Karlsson each logged 84 warnings: `Missing phoneme from id map: ̧` (U+0327). Thorsten's log is empty. The warnings require pronunciation review and investigation before approving those voices; valid WAVs alone cannot establish spoken accuracy.
- The two-minute reference transcript ends in `deutet.x`. Verify that suffix against the original dataset/audio before using it for accuracy scoring. It did not cause the ASR failures, which happened during audio decoding.
- This inspection verifies file integrity, runtime outputs, timing, and logs. It does not claim human listening approval of pronunciation or naturalness.

## Milestone decision

M1 remains **In progress**. Translation/TTS execution and resource feasibility have positive evidence. ASR needs a compatible dependency rerun; translation and voice quality, voice licenses, and final provider selection still need review. No additional result files are needed to diagnose this run.
