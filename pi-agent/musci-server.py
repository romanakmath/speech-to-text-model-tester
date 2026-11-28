#!/usr/bin/env python3
"""Resident Musci-ASR transcription server (local, 127.0.0.1 only).

Model stays loaded; dictation latency after stop ≈ 1 s instead of 6–10 s
model-load-per-utterance. Managed by LaunchAgent com.roman.musci-server
(KeepAlive) so it is always warm for browser-side dictation.

  GET  /health      -> {"ok": true, "model": ...}
  POST /transcribe  (raw 16 kHz mono wav bytes) -> {"text": "..."}

CORS is open (bound to 127.0.0.1 only) so the pi-web page can reach it
directly; remote clients (iPhone) fall back to Deepgram instead.

Run with: ~/.pi/agent/venvs/asr/bin/python ~/.pi/agent/bin/musci-server.py
"""
import io
import json
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST, PORT = "127.0.0.1", 31046
MODEL_ID = "Musci-research/Musci-ASR-qwen-1019-hf"
KEEP_WARM_S = 240  # tiny self-transcription keeps weights + kernels resident
MIN_RETRY_DUR_S = 1.0  # only retry-empty when the user actually dictated

import numpy as np  # noqa: E402
import torch  # noqa: E402
import soundfile as sf  # noqa: E402
from transformers import AutoProcessor, Qwen3ASRForConditionalGeneration  # noqa: E402


def log(msg):
    """Timestamped logging — plain prints made wake/slow-request correlation
    impossible (e.g. tracing the 20-40 s cold-weight inferences after standby)."""
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


log("loading model…")
t0 = time.time()
_proc = AutoProcessor.from_pretrained(MODEL_ID)
_model = Qwen3ASRForConditionalGeneration.from_pretrained(
    MODEL_ID, dtype=torch.float16, attn_implementation="sdpa"
).to("mps").eval()
log(f"model ready in {time.time()-t0:.1f}s")

_lock = threading.Lock()


def _silence_wav() -> bytes:
    """0.5 s of silence as a 16 kHz mono PCM16 wav, in memory."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 8000)
    return buf.getvalue()


def _decode_wav(b: bytes):
    # soundfile fast path: dictation wavs are 16 kHz mono already (rec -r 16000,
    # browser worklet resamples). Falls back to librosa for anything else.
    try:
        wav, sr = sf.read(io.BytesIO(b), dtype="float32")
        if wav.ndim > 1:
            wav = wav.mean(axis=1)
        if sr != 16000:
            import librosa
            wav = librosa.resample(wav, orig_sr=sr, target_sr=16000)
        return wav, 16000
    except Exception:  # noqa: BLE001 — exotic container, let librosa try
        import librosa
        wav, sr = librosa.load(io.BytesIO(b), sr=16000, mono=True)
        return wav, sr


def transcribe_array(wav: "np.ndarray", sr: int) -> str:
    inputs = _proc.apply_transcription_request(
        audio=[wav], sampling_rate=sr
    ).to("mps", torch.float16)
    with torch.no_grad():
        out = _model.generate(**inputs, max_new_tokens=512, do_sample=False)
    gen = out[:, inputs["input_ids"].shape[1]:]
    parsed = _proc.decode(gen, return_format="parsed")[0]
    return parsed["transcription"] if isinstance(parsed, dict) else str(parsed)


def transcribe_wav(b: bytes) -> str:
    """Transcribe wav bytes, with an empty-result retry.

    The model (any device/dtype — verified CPU-f32 too) occasionally emits an
    EMPTY transcript for perfectly good speech: ~3 of ~100 dictations, healthy
    levels, mic present, deterministic on re-post. Tiny ε-perturbation of the
    samples (dither σ=1e-4, proven on all archived failures) flips it back to
    a full correct transcript — model edge case, not bad audio. Retry only
    when the clip is long enough that the user was really dictating."""
    wav, sr = _decode_wav(b)
    text = transcribe_array(wav, sr)
    if text.strip() or len(wav) / sr < MIN_RETRY_DUR_S:
        return text

    variants = [
        ("dither1e-4", wav + np.random.default_rng(42).normal(
            0, 1e-4, len(wav)).astype(np.float32)),
        ("dither1e-4+lead100ms", np.concatenate([
            np.zeros(int(0.1 * sr), np.float32),
            wav + np.random.default_rng(7).normal(
                0, 1e-4, len(wav)).astype(np.float32)])),
    ]
    for tag, v in variants:
        text = transcribe_array(v, sr)
        log(f"empty-asr retry ({tag}, {len(wav)/sr:.1f}s clip): "
            f"{'recovered' if text.strip() else 'still empty'}")
        if text.strip():
            return text
    return text


SILENCE = _silence_wav()


def _warm_once(reason: str):
    t0 = time.time()
    with _lock:
        transcribe_wav(SILENCE)
    log(f"keep-warm ok ({reason}, {time.time()-t0:.2f}s)")


def keep_warm():
    """Periodic tiny inference: macOS pages out the 4 GB of GPU-resident
    weights after long idle (server RSS is tiny — weights live in MPS memory),
    which made the first dictation after idle take seconds. A 0.2 s silent
    inference every KEEP_WARM_S keeps everything hot."""
    while True:
        time.sleep(KEEP_WARM_S)
        try:
            _warm_once("heartbeat")
        except Exception as e:  # noqa: BLE001
            log(f"keep-warm failed: {type(e).__name__}: {e}")


_obs_holder = []  # keeps the wake observer alive (see wake_watcher)


def wake_watcher():
    """Re-warm immediately on system wake: after standby the weights are
    paged out and the first inference pays 20-40 s (observed in keep-warm
    log). Warming at wake means the user's first dictation is fast."""
    try:
        from Foundation import NSObject
        from AppKit import NSWorkspace, NSWorkspaceDidWakeNotification

        class WakeObserver(NSObject):
            def didWake_(self, _note):
                def _go():
                    try:
                        _warm_once("wake")
                    except Exception as e:  # noqa: BLE001
                        log(f"wake warmup failed: {type(e).__name__}: {e}")
                threading.Thread(target=_go, daemon=True).start()

        obs = WakeObserver.new()
        _obs_holder.append(obs)  # belt & braces: never let the observer be collected
        center = NSWorkspace.sharedWorkspace().notificationCenter()
        center.addObserver_selector_name_object_(
            obs, "didWake:", NSWorkspaceDidWakeNotification, None)
        log("wake watcher armed")
    except Exception as e:  # noqa: BLE001
        log(f"wake watcher unavailable: {type(e).__name__}: {e}")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence request spam
        pass

    def _send(self, code: int, obj: dict):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"ok": True, "model": MODEL_ID})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path.split("?")[0] != "/transcribe":
            self._send(404, {"error": "not found"})
            return
        n = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(n)
        try:
            t0 = time.time()
            with _lock:
                text = transcribe_wav(body)
            log(f"transcribed {len(body)} bytes in {time.time()-t0:.2f}s -> {len(text)} chars")
            self._send(200, {"text": text})
        except Exception as e:  # noqa: BLE001
            log(f"transcribe failed: {type(e).__name__}: {e}")
            self._send(500, {"error": f"{type(e).__name__}: {e}"})


if __name__ == "__main__":
    # Compile MPS kernels + touch all weight pages NOW, so the first real
    # dictation after a (re)start is as fast as every other one.
    t0 = time.time()
    with _lock:
        transcribe_wav(SILENCE)
    log(f"warmup inference in {time.time()-t0:.2f}s")

    threading.Thread(target=keep_warm, daemon=True).start()
    log(f"keep-warm heartbeat: every {KEEP_WARM_S}s")
    threading.Thread(target=wake_watcher, daemon=True).start()
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
