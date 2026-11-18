# Musci dictation: after-idle "no speech detected" bug

## Symptom

After a long idle period, pressing ⌘⇧D shows the ● REC overlay and records
fine, but on stop the notification says **"no speech detected"** — even though
the user was speaking into the microphone.

## Evidence from the pre-fix log (no timestamps existed yet)

```
posting 1695132 bytes to musci-server…
audio: sr=16000 dur=26.5s rms=0.00196 peak=0.0193
asr returned 0 chars: ''
notify: no speech detected
```

- ~13 failed cycles: **rms ≈ 0.001–0.002, peak ≈ 0.01–0.03** → ASR empty.
- Working cycles: **rms ≈ 0.014–0.018, peak ≈ 0.10–0.13** — ~10× louder.
- Levels flip between quiet/loud across consecutive recordings **without
  app restarts** → the changing state is in macOS, not in our code.
- musci-server was healthy throughout (keep-warm OK, 200 responses).
- sox stderr shows only the usual 16k→48k rate warning + one rare
  `coreaudio: unhandled buffer overrun`.

## Root cause (leading, pending confirmation)

The machine has a **Continuity mic — "iPhone de Roman Microphone"** — plus
"MacBook Pro Microphone" and a virtual Teams device. The prime suspects:

- **H1**: after idle the iPhone locks/leaves range → macOS switches the
  default input device under `rec` (sox records from *whatever is default*),
  and the user's voice lands on a far/dead mic at ~10× lower level.
- **H2**: the Continuity mic stays default but delivers a degraded
  ultra-quiet stream when the iPhone locks.

### Falsified sub-hypothesis (important)

"Quiet speech fails ASR" is **false**: scaling a known-good recording down to
rms 0.002 still transcribes perfectly (`test_idle_bug_rescue.py`). Therefore
in the failed cycles the user's voice was (near-)absent from the captured
signal — a wrong or dead source, not a mere level drop.

## What was deployed (2026-11-18)

In `pi-agent/dictate-menubar.py` + `pi-agent/audio_devices.py`:

1. **Timestamped logging** for every line.
2. **Mic poller** (CoreAudio, no audio stream opened — non-perturbing):
   logs every default-input-device change and device-set change with a
   timestamp, continuously, even while idle. This is the instrument that
   will name the culprit on the next occurrence.
3. **Failed-cycle audio archive**: every cycle that ends empty or below
   `QUIET_RMS = 0.005` is copied to `~/.pi/agent/logs/dictate-failed/`
   (last 10 kept) — ground-truth audio for analysis.
4. **Amplify-rescue**: if ASR returns nothing and rms < 0.005, the audio is
   amplified (peak → 0.5, gain capped 50×) and re-posted once. Cheap
   insurance for the borderline case.
5. "no speech detected" notification now names the mic + rms.
6. Synced the hotkey keycode fix (2 = D; 9 is V) into the repo, fixed an
   fd leak on the sox stderr handle.

## How to read the next occurrence

1. `grep mic-poller ~/.pi/agent/logs/dictate-menubar.log` — was there a
   default-input flip right before the failed cycle?
2. The failed cycle's own line shows `[mic: <name>]` — which device was it?
3. `~/.pi/agent/logs/dictate-failed/*.wav` — listen / inspect; does it
   contain the user's voice at all?

If H1 is confirmed (default flips away from the intended mic), the fix is to
stop relying on the default input: record from an explicitly chosen device
(ffmpeg `-f avfoundation -i :<idx>` supports device selection; sox coreaudio
does not) with a preference list, e.g. iPhone mic when present, else built-in.

## Harness

`pi-agent/test_idle_bug_rescue.py` — reproduces the quiet-audio signature
against the live musci-server and verifies the rescue path (also proved the
model's level-robustness that falsified the simple theory).
