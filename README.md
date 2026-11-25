# speech-to-text-model-tester

Local, reproducible WER benchmarking of speech-to-text (ASR) "ears" on Apple
Silicon (M-series, MLX / MPS), built after a first round of measurements that
turned out to be unreliable — see [docs/PITFALLS.md](docs/PITFALLS.md) for
exactly what went wrong and how this harness avoids it.

## Headline result

LibriSpeech `test-clean`, deterministic subset of **202 utterances / 4,027
reference words** (every 13th of the sorted 2,620), greedy decoding, scored
with `jiwer` + Whisper's official `EnglishTextNormalizer`.

| Engine (runtime) | **Measured WER (subset)** | Published WER (full set) | Wall time, 202 utts |
|---|---:|---:|---:|
| **Oruk-Fourier-2B** (PyTorch MPS, bf16, 3-beam)* | **1.50%** | 1.04% (test-clean, 3-beam) | 202 s (M5 Max) |
| **Oruk-Fourier-2B-8bit** (MLX via mlx-audio, greedy)* | **1.55%** | 1.40% (test-clean, Q8_0 GGUF) | **48 s** (M5 Max) |
| **Musci-ASR-qwen-1019** (PyTorch MPS, fp16) | **1.63%** | 1.03% (test-clean, model card) | 329 s |
| **Qwen3-ASR-1.7B-8bit** (MLX via mlx-audio) | **1.82%** | — | **81 s** |
| Parakeet-TDT-0.6B-v2 (MLX bf16) | 2.05% | ~1.6–1.7% (test-clean) | 515 s |
| Voxtral-Mini-4B-Realtime-2602 (MLX 4bit) | 2.51% | — | 1184 s |
| Deepgram Nova-3 (cloud batch) | 3.11% | 5.2% (AA aggregate, harder mix) | 209 s |

*`oruk/oruk-fourier-2b` (`CC BY-NC 4.0`, English-specialized Qwen3-ASR 1.7B) measured on the identical 202-utt manifest (Apple M5 Max, 128 GB); `oruk-fourier-mps` uses the checkpoint's default 3-beam search (`generation_config.json`: `num_beams=3, do_sample=False`), while `oruk-fourier-mlx` uses greedy MLX 8-bit decoding. Wall times are not cross-host comparable to M4 Pro, while WER is host-independent.

**Methodology validation:** Musci and Parakeet land within ~0.5 pt of their
published full-set scores — the expected subset-sampling offset. The pipeline
measures the same thing the leaderboards measure.

Why the *measured vs published* columns differ:

- 202/2620 utterances → 95% CI ≈ ±0.5 pt (subset noise).
- Scoring normalizer here is Whisper's `EnglishTextNormalizer`; the HF Open ASR
  Leaderboard uses its own (slightly different) normalizer.
- Quantized MLX builds (8bit/4bit) can give up a little accuracy vs the bf16
  checkpoints the papers report.

Harder audio (noise, accents, spontaneous speech) is **not** covered by
`test-clean` — leaderboard averages like Musci's 4.36% come from 9 tougher
datasets (Earnings-22, URGENT24, …). Use `--dataset test-other` for a
tie-breaker under adverse conditions.

## What's in the box

```
run_eval.py            CLI: transcribe a manifest with one engine
speechtester/
  manifest.py          build deterministic LibriSpeech subset + refs
  engines.py           engine registry (load / transcribe)
  eval_librispeech.py  per-engine evaluation loop
  score.py             jiwer + EnglishTextNormalizer corpus WER
bin/make_samples.sh    synthetic numbers/code dictation samples (say → afconvert)
bin/deploy.sh          deploy pi-agent/ files to their live locations
pi-agent/              deployed dictation stack (source of truth):
  musci-server.py        resident local ASR server (LaunchAgent, keep-warm)
  dictate-webview.ts     pi extension: Meta+D → musci/deepgram
  pi-web-dictate.js      browser overlay: ⌘D local-first dictation, ⌥S speak
  com.roman.musci-server.plist + overlay installer
results/               raw hypotheses + manifest from the 202-utt run
docs/PITFALLS.md       why the first benchmark lied, and the fixes
```

## Deployment

The files in `pi-agent/` are the **source of truth** for the live dictation
stack (server, extension, browser overlay, LaunchAgent). Edit them here, then:

```bash
./bin/deploy.sh
```

They are deployed to `~/.pi/agent/`, `~/Library/LaunchAgents/` and the pi-web
`public/` dir — see `bin/deploy.sh` for the exact map. Always `git commit &&
git push` after operational updates.

## Setup (once)

```bash
./setup_venv.sh          # creates .venv with torch, transformers, mlx-audio,
                         # parakeet-mlx, jiwer, librosa, soundfile
brew reinstall ffmpeg    # parakeet-mlx shells out to ffmpeg for loading
./bin/make_samples.sh    # optional: synthetic numbers/code samples via macOS `say`
```

## Download + subset LibriSpeech

```bash
curl -L -o /tmp/asr_bench/test-clean.tar.gz \
  https://www.openslr.org/resources/12/test-clean.tar.gz
mkdir -p data && tar xzf /tmp/asr_bench/test-clean.tar.gz -C data

python -m speechtester.manifest \
  --librispeech data/LibriSpeech/test-clean \
  --stride 13 --out results/manifest-librispeech-test-clean-202.json
```

## Evaluate an engine

```bash
python run_eval.py --engine qwen3asr-mlx   --manifest results/manifest-librispeech-test-clean-202.json
python run_eval.py --engine musci-mps      --manifest results/manifest-librispeech-test-clean-202.json
python run_eval.py --engine parakeet-mlx   --manifest results/manifest-librispeech-test-clean-202.json
python run_eval.py --engine voxtral-mlx    --manifest results/manifest-librispeech-test-clean-202.json
```

Hypotheses land in `hyp_<engine>.json`. Then score (any number of engines):

```bash
python -m speechtester.score \
  --manifest results/manifest-librispeech-test-clean-202.json \
  --hyps results/hyp_qwen.json results/hyp_musci.json
```

## Engines

| id | weights | runtime | notes |
|---|---|---|---|
| `qwen3asr-mlx` | `mlx-community/Qwen3-ASR-1.7B-8bit` | MLX | fastest accurate local ear; auto-formats numbers; recovers camelCase |
| `oruk-fourier-mlx` | `oruk/oruk-fourier-2b` (`mlx/8bit`) | MLX | English-specialized Qwen3-ASR 1.7B (`CC BY-NC 4.0`), 8-bit MLX |
| `oruk-fourier-mps` | `oruk/oruk-fourier-2b` | PyTorch MPS | English-specialized Qwen3-ASR 1.7B (`CC BY-NC 4.0`), native BF16 |
| `musci-mps` | `Musci-research/Musci-ASR-qwen-1019-hf` | PyTorch MPS | published 4.36% leaderboard-average (GRPO fine-tune of Qwen3-ASR) |
| `parakeet-mlx` | `mlx-community/parakeet-tdt-0.6b-v2` | MLX | NVIDIA TDT, punctuation + caps built in |
| `voxtral-mlx` | `mlx-community/Voxtral-Mini-4B-Realtime-2602-4bit` | MLX | streaming model, batch benchmark here |

Override weights with `--model`. Add an engine by appending a loader/transcriber
pair in `speechtester/engines.py`.

## System-wide dictation (any app)

`pi-agent/dictate-menubar.py` runs as a menu-bar app (LaunchAgent
`com.roman.dictate-menubar`). **⌘⇧D anywhere** (FSNotes, Mail, browser…)
toggles recording; on stop the transcript is pasted at the cursor
(clipboard swap + synthetic ⌘V). Same local Musci engine, audio stays on
the machine. Visual state cues: floating red **● REC** banner (AppKit,
above all apps/spaces) → orange **⏳ transcribing…** → hidden on paste;
macOS toasts (Python needs notification permission); start/stop beeps;
+12 dB capture gain.

One-time permissions: System Settings → Privacy & Security → (full troubleshooting story: `docs/DICTATION-MENUBAR.md`)
**Accessibility** and **Input Monitoring** → add the venv's *Python*;
**Microphone** prompts on first press.

Deploy with `./bin/deploy.sh`.

## Notes

- Python 3.13, Apple M4 Pro / 24 GB used for the shipped results; anything
  ≥16 GB works, quantized builds preferred.
- All engines decode **batch** (stop-then-transcribe); none stream here.
- Audio never leaves the machine for any of these engines.
