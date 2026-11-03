# Second target-server benchmark findings

Inspected 2026-11-04. Evidence: `benchmarks/artifacts/results-20261103-151109-663599/report.json` (ASR) and `results-20261103-152632-398569/report.json` (TTS), including candidate logs and WAV files. Both reports identify four CPU cores. Models were benchmarked sequentially; these measurements do not establish concurrent application throughput.

## ASR

Both candidates completed all seven recordings over three repetitions. The earlier decoding failure is resolved in this run.

| Candidate | Peak RSS MiB | Pooled warm median seconds | Six short clips: word errors / reference words | Two-minute clip warm median seconds | Two-minute WER |
|---|---:|---:|---:|---:|---:|
| Whisper base | 465 | 2.47 | 20 / 130 = 15.4% | 45.41 | 16.4% |
| Whisper small | 815 | 5.28 | 10 / 130 = 7.7% | 50.47 | 7.0% |

Short-clip WER pools word counts using repetition zero, not repeated copies of the same audio. The long fixture includes those same clips and is reported separately. Normalization ignores punctuation and case, but word splitting and spelling variants still count: `gegenüber liegenden` versus `gegenüberliegenden`, for example. This is a small human-read sample, not a learner-accent evaluation.

Small improves meaningful errors: it transcribes the T-Rex sentence correctly where base produces `beim Licht gewachsen`. Neither is error-free: small produces `völlig insudiert` for `völlig isoliert`, and `Darm` for `Damm` in the long fixture. User correction remains necessary.

The long reference ends in `deutet.x`, a visible reference-text anomaly retained from the supplied dataset. Treat its absolute WER cautiously; original evidence has not been edited.

Keep base as the previously authorized provisional speed-first option. Small is the stronger accuracy candidate: on the long fixture its additional warm latency is only about five seconds. Final selection remains open.

## TTS

All nine configurations completed 27 inputs with two repetitions each. All 486 copied WAVs match recorded SHA-256 hashes, open successfully, and match reported durations. This verifies transfer integrity and valid audio containers, not pronunciation or completeness.

| Candidate | Peak RSS MiB | Warm median seconds | Warm 1,000-character generation seconds | Warning count |
|---|---:|---:|---:|---:|
| Thorsten medium | 359 | 0.23 | 2.72 | 0 |
| Eva x-low | 300 | 0.18 | 1.71 | 144 |
| Karlsson low | 277 | 0.19 | 2.89 | 144 |
| Thorsten high | 403 | 1.18 | 14.81 | 0 |
| Kerstin low | 290 | 0.20 | 2.46 | 144 |
| Ramona low | 274 | 0.20 | 2.32 | 144 |
| Qwen Ryan | 6267 | 60.31 | 633.86 | 1 |
| Qwen Aiden | 6329 | 52.99 | 569.15 | 1 |
| Qwen Serena | 6212 | 58.38 | 622.77 | 1 |

Warm medians pool different input lengths; each TTS input has only one post-initial repetition. These are generation times excluding model load and Telegram delivery.

The four non-Thorsten Piper voices each log 144 instances of `Missing phoneme from id map: ̧` (U+0327, combining cedilla). This is a phoneme-map warning, not 144 failed files. Its audible effect needs investigation and listening; do not assume it is harmless or that these voices have inherently poor quality.

Qwen's warning concerns missing flash-attn and use of manual PyTorch inference. Logs also contain the automatic pad-token message. The observed CPU configuration completes successfully but generates the 1,000-character input in 9.5–10.6 minutes. Even the short everyday sentence takes 21–31 seconds warm. RAM fits the server, but this implementation is unsuitable as the default interactive generator on this CPU. These results do not measure alternative Qwen runtimes or GPU execution.

Thorsten medium is the strongest provisional balance of speed and clean logs; Thorsten high is about five times slower at the pooled median, with quality improvement still unestablished. They are variants of the same speaker, not two distinct voices. The application's planned three distinct voices still need selection.

## Outstanding quality review

All 243 listening-score rows are blank. No listening assessment was performed during this inspection. Open the copied `review.html` to compare pronunciation, missing words, numbers, umlauts, and long text. The stress text itself ends mid-word (`wei.`), so that ending is not evidence of model truncation.

Model/voice license review and owner quality approval remain pending. Translation was not rerun; the first report's translation issues remain relevant. No additional server files are needed for the current report inspection, and no application model settings were changed.
