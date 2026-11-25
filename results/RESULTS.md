# Results — LibriSpeech test-clean, 202-utterance deterministic subset

Run date: 2026-11-15 · Apple M4 Pro, 24 GB · Python 3.13
Subset: every 13th of 2,620 sorted utterances → 202 utts, 4,027 reference words
Scoring: `jiwer.process_words` + Whisper `EnglishTextNormalizer`, greedy decoding
Raw hypotheses in this directory (`hyp_*.json`), identical manifest for all engines.

| Engine (runtime) | **Measured WER (subset)** | Published WER (full set) | Wall time, 202 utts | Raw file |
|---|---:|---:|---:|---|
| **Oruk-Fourier-2B** (PyTorch MPS, bf16, 3-beam)* | **1.50%** | 1.04% (test-clean, 3-beam) | 202 s (M5 Max) | `hyp_oruk_fourier_mps.json` |
| **Oruk-Fourier-2B-8bit** (MLX via mlx-audio, greedy)* | **1.55%** | 1.40% (test-clean, Q8_0 GGUF) | **48 s** (M5 Max) | `hyp_oruk_fourier_mlx.json` |
| **Musci-ASR-qwen-1019** (PyTorch MPS, fp16) | **1.63%** | 1.03% (test-clean, model card) | 329 s | `hyp_musci.json` |
| **Qwen3-ASR-1.7B-8bit** (MLX via mlx-audio) | **1.82%** | — | **81 s** | `hyp_qwen.json` |
| Parakeet-TDT-0.6B-v2 (MLX bf16) | 2.05% | ~1.6–1.7% (test-clean) | 515 s | `hyp_parakeet.json` |
| Voxtral-Mini-4B-Realtime-2602 (MLX 4bit) | 2.51% | — | 1184 s | `hyp_vox.json` |
| Deepgram Nova-3 (cloud batch API) | 3.11% | 5.2% (AA aggregate, harder mix) | 209 s | `hyp_deepgram-nova3.json` |

*`oruk/oruk-fourier-2b` (`CC BY-NC 4.0`, English-specialized post-training of Qwen3-ASR 1.7B) evaluated on the identical 202-utt manifest (2026-11-25, Apple M5 Max 128 GB, Python 3.12). `oruk-fourier-mps` uses the checkpoint's default 3-beam decoding (`generation_config.json`: `num_beams=3, do_sample=False`), whereas `oruk-fourier-mlx` uses greedy MLX 8-bit decoding. Wall times for the M5 Max rows are not cross-host comparable to M4 Pro; WER/subs/del/ins are host-independent.

Sub/Del/Ins detail:

| Engine | subs | del | ins |
|---|---:|---:|---:|
| Oruk-Fourier-2B (MPS bf16) | 47 | 8 | 6 |
| Oruk-Fourier-2B (MLX 8bit) | 48 | 8 | 7 |
| Musci | 49 | 9 | 8 |
| Qwen3-ASR | 56 | 12 | 6 |
| Parakeet | 63 | 12 | 8 |
| Voxtral | 78 | 12 | 12 |
| Deepgram Nova-3 | 95 | 22 | 9 |

## Validation

Musci's model card reports **1.03%** on the *full* test-clean; Parakeet's card
reports ~**1.6–1.7%**. Measured values sit 0.3–0.5 pt above on a 202-utt
subset — the expected sampling offset (95% CI ≈ ±0.5 pt) plus minor
normalizer differences. The measurement pipeline is therefore anchored to
published reality.

## Interpretation

- **Musci ≈ Qwen3-ASR** statistically (CI overlap) on clean read speech.
  Qwen3-ASR is **2–4× faster** (81 s vs 329 s incl. load) and MLX-native.
- **Deepgram Nova-3 (3.11%) trails all four local models** on clean speech,
  with the highest substitution and deletion counts. It was evaluated via
  the batch REST API (`model=nova-3&smart_format=true&punctuate=true`),
  the only cloud engine here — audio leaves the machine. The 5.2% AA
  leaderboard number is on a harder aggregate; relative order (behind the
  Qwen3-ASR family) is consistent.
- Voxtral-Mini-4B-Realtime is last among locals on both axes here; note
  Mistral's higher-accuracy "Voxtral Small/Mini Transcribe 3" models are
  **API-only** (no published weights).
- `test-clean` is the easy end of ASR. Leaderboard averages (e.g. Musci
  4.36% over 9 datasets) include noise/accent/spontaneous-speech sets.
  Run `test-other` for adverse-condition tie-breaks.

## Non-WER properties observed (synthetic numbers/code samples)

On spoken-form numbers/tickers/code prompts (not part of the WER corpus):

- **Qwen3-ASR-1.7B**: recovers camelCase (`fetchRawSignal`), formats numbers
  (`12.4%`, `$92,000`), resolves spelled tickers (`B T C U S D T` → `BTC USDT`).
- **Musci**: similar formatting; slightly weaker on code identifiers
  (`speakbrief. ts` spacing glitch).
- **Parakeet v2**: strongest ticker spelling (`ETHUSDT`, no space), numbers
  formatted, but merges some code identifiers into prose.
- **Voxtral**: good `$92,000`-style formatting, occasional word substitution.
