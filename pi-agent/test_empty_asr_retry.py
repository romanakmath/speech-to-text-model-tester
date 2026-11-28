#!/usr/bin/env python3
"""Post-deploy verification for the musci-server empty-ASR retry fix.

Archived failed dictations (healthy speech, model returned '') must come
back with text through the fixed server. Run AFTER restarting musci-server:

  ~/.pi/agent/venvs/asr/bin/python test_empty_asr_retry.py

Exit 0 = all archived failures now transcribe (or none to test).
"""
import glob
import json
import sys
import urllib.request

MUSCI_URL = "http://127.0.0.1:31046/transcribe"


def main() -> int:
    files = sorted(glob.glob(
        "/Users/roman/.pi/agent/logs/dictate-failed/2026112*-*.wav"))
    if not files:
        print("no archived failed cycles to verify")
        return 0
    failures = 0
    for path in files:
        with open(path, "rb") as f:
            data = f.read()
        req = urllib.request.Request(MUSCI_URL, data=data,
                                     headers={"Content-Type": "audio/wav"},
                                     method="POST")
        with urllib.request.urlopen(req, timeout=300) as r:
            text = json.loads(r.read().decode()).get("text", "").strip()
        status = "PASS" if text else "FAIL"
        print(f"{status} {path.split('/')[-1]} -> {text[:80]!r}")
        failures += not bool(text)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
