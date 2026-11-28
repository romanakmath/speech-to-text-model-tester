# Musci dictation: after-standby bugs — full diagnosis (2026-11-28)

Three distinct root causes were confirmed for the user-visible symptoms
("after reopening from standby I wait unnecessarily long", "recording starts
but repressing does nothing, nothing gets pasted", intermittent
"no speech detected").

## 1. App crash mid-dictation → dictation silently lost

**Symptom**: press ⌘⇧D → "● REC" appears → press to stop → *nothing happens*,
nothing pasted (app crashes; launchd KeepAlive relaunches instantly, so it
*looks* like the stop press did nothing).

**Root cause (corrected after a wrong first diagnosis)**: AppKit UI access
from non-main threads. The overlay NSWindow was correctly marshaled to the
main thread via a queue, but rumps' `self.title = "●"` setter — called from
the **event-tap callback thread** (`toggle → start/stop`) and from the
**`_finish` worker thread** (`_done`) — mutates the NSStatusItem UI and
creates CoreAnimation transactions on those threads' runloops. Those CA
transactions race the main-thread overlay transactions on shared layer
state → CG display-list resource double-free.

**Evidence**: crash reports 11-23 (SIGSEGV), 11-24 & 14:28 (SIGABRT): both
the event-tap thread's runloop and the main runloop caught in
`CA::Transaction::flush_as_runloop_observer → CA::Layer::display_if_needed →
dl_Finalize → POINTER_BEING_FREED_WAS_NOT_ALLOCATED`. The tap runloop only
has CA observers because the title setter created transactions there. The
standby correlation was a red herring — post-wake CoreAnimation rebuild
widens the race window.

**Wrong first fix (kept for the record)**: "recreate the overlay window per
show + close()" — diagnosed as stale display-lists across standby. The
window-per-show part was harmless, but `win.close()` from pyobjc
**over-releases** the NSWindow (close() releases the ObjC object, the
Python wrapper releases it again → zombie → SIGSEGV in
`objc_autoreleasePoolPop`, crash 2026-11-28 17:02, introduced by that fix).

**Correct fix**: (a) ALL AppKit mutations go through the single main-thread
pump — the title is now set via the overlay queue ("title" op) drained by
the 0.10 s rumps timer; (b) persistent overlay window, created lazily on
the main thread, never destroyed (orderOut to hide; no close/release
anywhere). Plus: kill orphaned `rec` children at startup (a crashed parent
leaves sox recording forever), and a "still transcribing…" notification
when the hotkey is pressed during BUSY (previously silently ignored).

## 2. First transcription after standby takes ~20–40 s ("wait unnecessarily long")

**Evidence**: musci-server keep-warm log: normal inference 0.83 s, but
periodically 21–43 s — the 4 GB of MPS weights get paged out around standby;
`time.sleep(240)` in the keep-warm thread freezes during sleep, and the
first inference after wake pays the full page-in cost. If that first
inference is the user's dictation, they wait.

**Fix** (`musci-server.py`): NSWorkspaceDidWakeNotification observer
(verified working) triggers an immediate warm-up inference at wake, so the
cost is paid before the first dictation. Keep-warm heartbeat unchanged.
Also: timestamped server logs + per-request duration logging.

## 3. Intermittent empty transcription on healthy audio → "no speech detected"

**Evidence**: archived failed cycles (2026-11-28 00:23, 14:57, 15:01) show
healthy levels (rms 0.008–0.010, voiced/syllabic statistics identical to
known-good recordings, mic = MacBook Pro Microphone, no default-input flip
in the mic-poller log), yet the ASR returns '' — **deterministically**, on
re-post, AND on CPU+float32 (not an MPS/f16 numerics issue — it's a model
failure mode). Perturbation experiments: the exact same samples with
ε-dither (σ=1e-4) or lead-in silence transcribe perfectly; gain/DC-removal/
truncation do not. 3 occurrences in ~100 dictations.

**Fix** (`musci-server.py`): on empty result for clips ≥ 1 s, retry with
(1) dither σ=1e-4 (seed 42 — recovered all three archived failures),
(2) dither σ=1e-4 + 100 ms leading silence. Costs an extra 1–3 s only in
the rare failing case. The client-side amplify-rescue from 2026-11-18
remains as additional insurance.

## Earlier hypothesis (2026-11-18) — status

The "default input device flip after idle" hypothesis was **not confirmed**:
the mic-poller has logged zero default-input flips across 10 days; failures
showed the expected mic with healthy levels. The original pre-fix log's
quiet failures (rms ~0.002) may have been the same model issue combined
with far-field speech; the instrumentation deployed then (timestamps,
mic-poller, archives) is what made today's full diagnosis possible.

## Verification

- `test_empty_asr_retry.py` — post-deploy: all archived failed cycles must
  transcribe through the fixed server.
- `test_idle_bug_rescue.py` — quiet-audio level robustness (2026-11-18).
- Wake observer wiring verified standalone (manual notification post).
- Crash fix verified by code + compile; real-world confirmation = absence of
  new crash reports in `~/Library/Logs/DiagnosticReports/` after standby
  cycles.
