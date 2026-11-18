#!/usr/bin/env python3
"""Repro + verify harness for the after-idle 'no speech detected' bug.

Failure signature (from dictate-menubar.log): after long idle the captured
audio level collapses ~10x (rms ~0.002 vs ~0.015 healthy) and the ASR
correctly returns ''. This harness reproduces that signature offline by
scaling a known-good recording down to the failure level, then verifies the
amplify-rescue mitigation recovers the transcript through the live
musci-server.

Usage:  ~/.pi/agent/venvs/asr/bin/python test_idle_bug_rescue.py [good.wav]
Exit 0 = rescue verified; exit 1 = reproduced but NOT rescued; 2 = no input.
"""
import io
import json
import sys
import urllib.request

import numpy as np
import soundfile as sf

MUSCI_URL = "http://127.0.0.1:31046/transcribe"
QUIET_RMS = 0.005   # as in dictate-menubar.py
GOOD_WAV = sys.argv[1] if len(sys.argv) > 1 else \
    __import__("os").path.expanduser("~/.pi/agent/logs/dictate-last.wav")


def post(wav_bytes: bytes) -> str:
    req = urllib.request.Request(MUSCI_URL, data=wav_bytes,
                                 headers={"Content-Type": "audio/wav"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode()).get("text", "").strip()


def to_wav_bytes(w: np.ndarray, sr: int) -> bytes:
    buf = io.BytesIO()
    sf.write(buf, w, sr, subtype="PCM_16", format="WAV")
    return buf.getvalue()


def main() -> int:
    w, sr = sf.read(GOOD_WAV, dtype="float32")
    good_rms = float(np.sqrt((w.astype(np.float64) ** 2).mean()))
    print(f"source: {GOOD_WAV} sr={sr} dur={len(w)/sr:.1f}s rms={good_rms:.5f}")

    # 1) baseline: known-good audio transcribes
    base = post(to_wav_bytes(w, sr))
    print(f"baseline asr: {base!r}")
    if not base:
        print("FAIL: source wav itself doesn't transcribe — need a valid sample")
        return 2

    # 2) reproduce: scale down to the failure signature (~10x quieter)
    quiet = w * (0.002 / max(good_rms, 1e-9))
    q_rms = float(np.sqrt((quiet.astype(np.float64) ** 2).mean()))
    q_peak = float(np.abs(quiet).max())
    print(f"quiet: rms={q_rms:.5f} peak={q_peak:.5f} (failure signature)")
    quiet_text = post(to_wav_bytes(quiet, sr))
    print(f"quiet asr: {quiet_text!r}")
    if quiet_text:
        print("NOTE: ASR transcribed the quiet audio anyway (model robustness) — bug not reproducible at this level")

    # 3) rescue: same logic as dictate-menubar._finish amplify-rescue
    if not quiet_text and q_rms < QUIET_RMS and q_peak > 1e-4:
        gain = min(0.5 / q_peak, 50.0)
        amp = np.clip(quiet * gain, -1.0, 1.0)
        rescued = post(to_wav_bytes(amp, sr))
        print(f"rescue (gain {gain:.1f}x) asr: {rescued!r}")
        if rescued:
            print("PASS: amplify-rescue recovered the dictation")
            return 0
        print("FAIL: rescue did not recover the dictation")
        return 1

    return 0 if quiet_text else 1


if __name__ == "__main__":
    sys.exit(main())
