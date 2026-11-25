"""Transcribe a manifest with one engine and write hyp_<engine>.json.

Absolute paths only — several engine loaders reject relative paths.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from .engines import get_transcriber


def run(engine: str, manifest_path: str, out: str, model: str | None, data_root: str) -> None:
    manifest = json.loads(Path(manifest_path).read_text())
    transcribe = get_transcriber(engine, model)

    hyps: dict[str, str] = {}
    errors = 0
    t_all = time.time()
    for i, m in enumerate(manifest):
        t0 = time.time()
        path = Path(data_root) / m["path"] if not Path(m["path"]).is_absolute() else Path(m["path"])
        try:
            hyp = transcribe(str(path))
        except Exception as e:  # noqa: BLE001 — record, keep going
            hyp = f"__ERROR__ {type(e).__name__}"
            errors += 1
        hyps[m["utt"]] = hyp.strip()
        if i % 50 == 0:
            print(f"[{engine}] {i}/{len(manifest)}  last {time.time()-t0:.2f}s", flush=True)

    total = time.time() - t_all
    print(f"[{engine}] done: {len(manifest)-errors}/{len(manifest)} ok, {total:.0f}s total")
    Path(out).write_text(json.dumps(hyps, indent=0))
    print(f"hypotheses -> {out}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--engine",
        required=True,
        help="qwen3asr-mlx | oruk-fourier-mlx | musci-mps | oruk-fourier-mps | parakeet-mlx | voxtral-mlx",
    )
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--out", default=None, help="output JSON (default hyp_<engine>.json)")
    ap.add_argument("--model", default=None, help="override HF model id")
    ap.add_argument("--data-root", default=".", help="root the manifest's relative paths against")
    a = ap.parse_args()

    out = a.out or f"hyp_{a.engine}.json"
    run(a.engine, a.manifest, out, a.model, a.data_root)


if __name__ == "__main__":
    main()
