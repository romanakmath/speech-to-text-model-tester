#!/usr/bin/env python3
"""Global dictation hotkey — Musci local ASR, everywhere on macOS.

⌘⇧D (Cmd+Shift+D) toggles recording from ANY app. On stop, the transcript is
pasted at the cursor of the frontmost application (via clipboard swap + ⌘V).

Runs as a menu-bar app (LaunchAgent com.roman.dictate-menubar, KeepAlive).

Permissions (first run):
  - Microphone    -> for `rec` (sox); macOS prompts on first press, allow it
  - Accessibility -> for the global hotkey tap and the synthetic ⌘V paste
                     (grant to "Python" in System Settings → Privacy & Security)
  - Input Monitoring -> for the global hotkey listener, same place

Engine: resident musci-server on 127.0.0.1:31046 (LaunchAgent
com.roman.musci-server). Audio never leaves this machine.
"""
import json
import os
import queue
import shutil
import subprocess
import threading
import time
import urllib.request

import rumps
from pynput import keyboard

def log(msg):
    """Timestamped logging — the old log had no timestamps, which made it
    impossible to correlate failures with idle/sleep/device events."""
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


try:
    from Foundation import NSMakeRect
    from AppKit import (NSWindow, NSColor, NSTextField, NSFont,
                        NSScreen, NSWindowCollectionBehaviorCanJoinAllSpaces,
                        NSWindowCollectionBehaviorFullScreenAuxiliary,
                        NSCenterTextAlignment)
    HAS_APPKIT = True
except Exception as _e:
    log(f"AppKit overlay unavailable: {_e}")
    HAS_APPKIT = False

try:
    from Quartz import CGEventTapEnable
except Exception:
    CGEventTapEnable = None

MUSCI_URL = "http://127.0.0.1:31046/transcribe"
HOTKEY = "<cmd>+<shift>+d"
REC_BIN = shutil.which("rec") or "/opt/homebrew/bin/rec"  # launchd PATH has no /opt/homebrew/bin
REC_ERR_LOG = os.path.expanduser("~/.pi/agent/logs/dictate-menubar-rec.err")
WAV_PATH = os.path.expanduser("~/.pi/agent/logs/dictate-last.wav")
QUIET_RMS = 0.005  # failed cycles measured rms~0.002; working ones ~0.015

try:
    import audio_devices  # CoreAudio introspection, same directory
except Exception as _e:
    audio_devices = None
    print(f"audio_devices unavailable: {_e}", flush=True)


IDLE, REC, BUSY = "idle", "recording", "busy"

# live physical-modifier state (updated by the hotkey listener) — used to
# wait for ⌘⇧ release before typing, else every letter comes out SHIFTED
MODS = {"cmd": False, "shift": False}


def _wait_mods_released(timeout: float = 10.0) -> None:
    """Block until the user physically releases ⌘/⇧ (or timeout)."""
    t0 = time.time()
    while any(MODS.values()) and time.time() - t0 < timeout:
        time.sleep(0.05)
    log(f"mods at delivery: {dict(MODS)} (waited {time.time()-t0:.2f}s)")


def notify(title, text=""):
    log(f"notify: {title} | {text}")
    # osascript channel — the one proven to display banners on this machine
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification {json.dumps(str(text)[:200])} '
             f'with title "Dictation" subtitle {json.dumps(str(title)[:80])}'],
            capture_output=True, timeout=5)
    except Exception as e:
        log(f"notify failed: {type(e).__name__}: {e}")


# Chromium-family apps that swallow session-level synthetic ⌘V
STUBBORN = ("helium", "chrom", "brave", "vivaldi", "edge", "arc", "orbit")


def _sanitize_for_typing(text: str) -> str:
    """newlines/tabs would hit Enter/Tab — flatten to spaces."""
    return text.replace("\r", " ").replace("\n", " ").replace("\t", " ")


def _type_text_quartz(text: str) -> bool:
    """Type via CGEvent unicode injection: each event carries its own text
    with flags=0 — immune to caps-lock latches, held modifiers, and keyboard
    layout (the cause of the ALL-CAPS bug). Chunks of 20 chars per event."""
    try:
        from Quartz import (CGEventCreateKeyboardEvent,
                            CGEventKeyboardSetUnicodeString,
                            CGEventPost, kCGHIDEventTap)
        for i in range(0, len(text), 20):
            chunk = text[i:i + 20]
            for down in (True, False):
                ev = CGEventCreateKeyboardEvent(None, 0, down)
                CGEventKeyboardSetUnicodeString(ev, len(chunk), chunk)
                CGEventPost(kCGHIDEventTap, ev)
            time.sleep(0.02)
        log("deliver method: typing (quartz unicode)")
        return True
    except Exception as e:
        log(f"quartz typing failed: {type(e).__name__}: {e}")
        return False


def _type_text(text: str) -> bool:
    if _type_text_quartz(_sanitize_for_typing(text)):
        return True
    # fallback: System Events keystroke (layout+modifier sensitive)
    t = _sanitize_for_typing(text).replace("\\", "\\\\").replace('"', '\\"')
    r = subprocess.run(
        ["osascript", "-e",
         f'tell application "System Events" to keystroke "{t}"'],
        capture_output=True, timeout=15)
    if r.returncode == 0:
        log("deliver method: typing (System Events)")
        return True
    log(f"typing failed: {r.stderr.decode()[:120]}")
    return False


def _paste_keystroke_osascript() -> bool:
    r = subprocess.run(
        ["osascript", "-e",
         'tell application "System Events" to keystroke "v" using command down'],
        capture_output=True, timeout=5)
    if r.returncode == 0:
        log("deliver method: osascript ⌘V")
        return True
    log(f"osascript paste failed ({r.returncode}): {r.stderr.decode()[:120]}")
    return False


def _paste_keystroke_pynput() -> None:
    c = keyboard.Controller()
    c.press(keyboard.Key.cmd)
    c.tap("v")
    c.release(keyboard.Key.cmd)
    log("deliver method: pynput ⌘V")


def deliver_text(text: str) -> str:
    """Get the transcript into the focused field of the frontmost app.
    Returns the app name. Chromium forks: type directly (they block
    synthetic paste). Everyone else: clipboard swap + ⌘V."""
    _wait_mods_released()  # ⌘⇧ still physically held would SHIFT every letter
    target, bundle_id = "?", ""
    try:
        from AppKit import NSWorkspace
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        target = app.localizedName()
        bundle_id = app.bundleIdentifier() or ""
    except Exception as e:
        log(f"frontmost probe failed: {type(e).__name__}: {e}")

    if any(s in bundle_id.lower() for s in STUBBORN):
        if _type_text(text):
            return target
        # typing failed: leave transcript on the clipboard for a manual ⌘V
        subprocess.run(["pbcopy"], input=text.encode(), check=False)
        notify("press ⌘V to paste", text[:120])
        return target

    old = subprocess.run(["pbpaste"], capture_output=True, text=True).stdout
    subprocess.run(["pbcopy"], input=text.encode(), check=False)
    time.sleep(0.05)
    try:
        if not _paste_keystroke_osascript():
            _paste_keystroke_pynput()
        time.sleep(0.6)  # let the target app read the clipboard
    finally:
        subprocess.run(["pbcopy"], input=old.encode(), check=False)
    return target



class DictateApp(rumps.App):
    def __init__(self):
        super().__init__("🎙", quit_button=None)
        self.state = IDLE
        self.proc = None
        self.wav = None
        self.cancelled = False
        self.title = "🎙"
        self._overlay = None
        self._ovq = queue.Queue()  # overlay ops from any thread -> main thread
        self._mic = "?"  # cached default input name (mic poller)

        self.menu = [
            rumps.MenuItem("Start / Stop dictation"),
            rumps.MenuItem("Cancel recording"),
            None,
            rumps.MenuItem("Check server"),
            rumps.MenuItem("Quit"),
        ]

        threading.Thread(target=self._run_event_tap, daemon=True).start()
        log(f"global hotkey registered: {HOTKEY}")
        if audio_devices:
            threading.Thread(target=self._run_mic_poller, daemon=True).start()

        # main-thread pump that applies overlay changes
        self._timer = rumps.Timer(self._drain_overlay, 0.10)
        self._timer.start()
        # tap watchdog: re-enable the event tap if macOS disabled it (timeout)
        self._tap_holder = []
        self._watchdog = rumps.Timer(self._tap_watchdog, 5.0)
        self._watchdog.start()
        log(f"overlay pump started, AppKit: {HAS_APPKIT}")

    def _tap_watchdog(self, _sender=None):
        if self._tap_holder and CGEventTapEnable:
            CGEventTapEnable(self._tap_holder[0], True)  # no-op if already enabled

    # ---------------- visual overlay (main thread only) ----------------

    def _drain_overlay(self, _sender=None):
        try:
            while True:
                op, text, rgb = self._ovq.get_nowait()
                if op == "show":
                    self._overlay_show(text, rgb)
                else:
                    self._overlay_hide()
        except queue.Empty:
            pass

    def _overlay_show(self, text, rgb):
        if not HAS_APPKIT:
            return
        try:
            if self._overlay is None:
                win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                    NSMakeRect(0, 0, 260, 64), 0, 2, False)
                win.setLevel_(25)  # NSStatusWindowLevel — above all normal windows
                win.setOpaque_(False)
                win.setHasShadow_(True)
                win.setIgnoresMouseEvents_(True)
                win.setCollectionBehavior_(
                    NSWindowCollectionBehaviorCanJoinAllSpaces |
                    NSWindowCollectionBehaviorFullScreenAuxiliary)
                label = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 260, 64))
                label.setEditable_(False)
                label.setBordered_(False)
                label.setDrawsBackground_(False)
                label.setAlignment_(NSCenterTextAlignment)
                label.setFont_(NSFont.boldSystemFontOfSize_(24))
                label.setTextColor_(NSColor.whiteColor())
                win.contentView().addSubview_(label)
                self._overlay = (win, label)
            win, label = self._overlay
            win.setBackgroundColor_(NSColor.colorWithCalibratedRed_green_blue_alpha_(
                rgb[0], rgb[1], rgb[2], 0.94))
            label.setStringValue_(text)
            frame = NSScreen.mainScreen().frame()
            win.setFrameOrigin_((frame.origin.x + (frame.size.width - 260) / 2,
                                 frame.origin.y + frame.size.height - 140))
            win.orderFrontRegardless()
        except Exception as e:
            log(f"overlay show failed: {type(e).__name__}: {e}")

    def _overlay_hide(self):
        if not HAS_APPKIT or self._overlay is None:
            return
        try:
            self._overlay[0].orderOut_(None)
        except Exception as e:
            log(f"overlay hide failed: {type(e).__name__}: {e}")

    def _ov(self, op, text="", rgb=(1, 1, 1)):
        self._ovq.put((op, text, rgb))

    def _run_mic_poller(self):
        """Watch default input device + device set; log every change with a
        timestamp (no audio stream is opened — non-perturbing).
        Instrumentation for the after-idle 'no speech detected' bug: failed
        cycles captured input at ~10x lower level (rms ~0.002 vs ~0.015),
        flipping without app restarts — prime suspect is macOS switching the
        default input between the Continuity iPhone mic and the built-in
        mic (or the Continuity stream degrading when the iPhone locks)."""
        last_def = None
        last_set = None
        while True:
            try:
                dev, name = audio_devices.default_input_device()
                devs = [n for _, n in audio_devices.all_devices()]
                if (dev, name) != last_def:
                    log(f"mic-poller: default input -> {name!r} (id {dev})")
                    last_def = (dev, name)
                if devs != last_set:
                    log(f"mic-poller: device set -> {devs}")
                    last_set = devs
                if name:
                    self._mic = name
            except Exception as e:
                log(f"mic-poller failed: {type(e).__name__}: {e}")
            time.sleep(10)

    # ---------------- core toggle ----------------

    def _run_event_tap(self):
        """Suppressing CGEventTap: ⌘⇧D triggers dictation AND is swallowed —
        apps never see the combo, so no alert beep and no double-actions
        (FSNotes Duplicate, Chrome bookmark-all-tabs, …)."""
        try:
            from Quartz import (CGEventTapCreate, kCGSessionEventTap,
                                kCGHeadInsertEventTap, kCGEventTapOptionDefault,
                                CGEventMaskBit, kCGEventKeyDown, kCGEventFlagsChanged,
                                CGEventTapEnable,
                                CFMachPortCreateRunLoopSource, CFRunLoopGetCurrent,
                                CFRunLoopAddSource, CFRunLoopRun,
                                CGEventGetIntegerValueField, kCGKeyboardEventKeycode,
                                CGEventGetFlags, kCGEventFlagMaskCommand,
                                kCGEventFlagMaskShift)
            from Foundation import kCFRunLoopCommonModes
        except Exception as e:
            log(f"Quartz tap import failed: {e}")
            return
        # NOTE: do NOT add kCGEventTapDisabledBy* to the mask — their enum
        # values (0xFFFFFFFE…) make CGEventMaskBit overflow in pyobjc and
        # CGEventTapCreate rejects the mask. A watchdog timer re-enables the
        # tap instead (see self._tap_watchdog).
        mask = CGEventMaskBit(kCGEventKeyDown) | CGEventMaskBit(kCGEventFlagsChanged)
        tap_holder = []

        def callback(_proxy, etype, event, _refcon):
            flags = CGEventGetFlags(event)
            MODS["cmd"] = bool(flags & kCGEventFlagMaskCommand)
            MODS["shift"] = bool(flags & kCGEventFlagMaskShift)
            if etype == kCGEventKeyDown:
                code = CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode)
                if code == 2 and MODS["cmd"] and MODS["shift"]:  # keycode 2 = D (9 is V!)
                    try:
                        self.toggle()
                    except Exception as e:
                        log(f"toggle failed: {type(e).__name__}: {e}")
                    return None  # swallow — apps never see ⌘⇧D
            return event

        tap = CGEventTapCreate(kCGSessionEventTap, kCGHeadInsertEventTap,
                               kCGEventTapOptionDefault, mask, callback, None)
        if tap is None:
            log("event tap create FAILED (accessibility?) — hotkey dead")
            return
        tap_holder.append(tap)
        self._tap_holder = tap_holder
        source = CFMachPortCreateRunLoopSource(None, tap, 0)
        rl = CFRunLoopGetCurrent()
        CFRunLoopAddSource(rl, source, kCFRunLoopCommonModes)
        log("suppressing event tap active")
        try:
            CFRunLoopRun()
        except Exception as e:
            log(f"runloop died: {type(e).__name__}: {e}")

    def toggle(self, _=None):
        log(f"hotkey pressed, state={self.state}")
        if self.state == IDLE:
            self.start()
        elif self.state == REC:
            self.stop()

    def start(self):
        if self.state != IDLE:
            return
        self.cancelled = False
        self.wav = WAV_PATH
        try:
            os.remove(self.wav)
        except OSError:
            pass
        try:
            self._rec_err = open(REC_ERR_LOG, "ab")
            self.proc = subprocess.Popen(
                [REC_BIN, "-q", "-r", "16000", "-c", "1", self.wav],
                stdout=subprocess.DEVNULL, stderr=self._rec_err,
            )
        except FileNotFoundError:
            notify("sox missing", REC_BIN)
            return
        self.state = REC
        self.title = "●"
        self._ov("show", "● REC", (1.0, 0.22, 0.19))
        log(f"recording… [mic: {self._mic}]")

    def stop(self):
        if self.state != REC:
            return
        self.state = BUSY
        self.title = "⏳"
        self._ov("show", "⏳ transcribing…", (1.0, 0.60, 0.0))
        # proc teardown happens in _finish (worker thread) — the tap callback
        # must return in milliseconds or macOS disables the tap
        threading.Thread(target=self._finish, daemon=True).start()

    # ---------------- menu callbacks ----------------

    @rumps.clicked("Start / Stop dictation")
    def toggle_menu(self, _):
        self.toggle()

    @rumps.clicked("Cancel recording")
    def cancel(self, _=None):
        if self.state == REC:
            self.cancelled = True
            if self.proc:
                self.proc.terminate()
        self.state = IDLE
        self.title = "🎙"
        self._ov("hide")

    @rumps.clicked("Check server")
    def check_server(self, _=None):
        try:
            with urllib.request.urlopen("http://127.0.0.1:31046/health", timeout=2) as r:
                notify("musci-server", r.read().decode()[:80])
        except Exception as e:
            notify("musci-server down", str(e)[:80])

    @rumps.clicked("Quit")
    def quit(self, _=None):
        self.cancel()
        rumps.quit_application()

    # ---------------- transcribe + paste ----------------

    def _finish(self):
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except Exception:
                self.proc.kill()
            self.proc = None
        if getattr(self, "_rec_err", None):
            try:
                self._rec_err.close()
            except Exception:
                pass
            self._rec_err = None
        time.sleep(0.35)  # let sox finalize the wav header
        if self.cancelled:
            self._done()
            return
        try:
            audio = open(self.wav, "rb").read()
        except OSError as e:
            rec_err = ""
            try:
                rec_err = open(REC_ERR_LOG).read()[-120:]
            except OSError:
                pass
            notify("recording failed", (f"{e}"[:60] + " | " + rec_err)[-120:])
            self._done()
            return
        log(f"posting {len(audio)} bytes to musci-server… [mic: {self._mic}]")
        rms = peak = None
        try:
            import numpy as np
            import soundfile as sf
            w, sr = sf.read(self.wav, dtype="float32")
            rms = float(np.sqrt((w.astype(np.float64) ** 2).mean()))
            peak = float(np.abs(w).max())
            log(f"audio: sr={sr} dur={len(w)/sr:.1f}s rms={rms:.5f} peak={peak:.4f}")
            if rms < 1e-4:
                notify("recording is silent", "check microphone input device/level")
        except Exception as e:
            log(f"audio probe skipped: {type(e).__name__}: {e}")
        try:
            req = urllib.request.Request(MUSCI_URL, data=audio,
                headers={"Content-Type": "audio/wav"}, method="POST")
            with urllib.request.urlopen(req, timeout=120) as r:
                text = json.loads(r.read().decode()).get("text", "").strip()
        except Exception as e:
            notify("ASR failed", f"{type(e).__name__}: {str(e)[:80]}")
            self._done()
            return
        log(f"asr returned {len(text)} chars: {text[:100]!r}")

        # After-idle bug mitigation: after long idle the captured level can
        # collapse ~10x (rms ~0.002 vs ~0.015 when healthy — see mic-poller
        # notes). If ASR heard nothing in ultra-quiet audio, amplify and
        # retry once instead of losing the dictation.
        if not text and rms is not None and rms < QUIET_RMS and peak > 1e-4:
            try:
                import io
                import numpy as np
                import soundfile as sf
                w, sr = sf.read(self.wav, dtype="float32")
                gain = min(0.5 / peak, 50.0)
                log(f"amplify-rescue: gain {gain:.1f}x (rms {rms:.5f}, mic {self._mic})")
                w = np.clip(w * gain, -1.0, 1.0)
                buf = io.BytesIO()
                sf.write(buf, w, sr, subtype="PCM_16", format="WAV")
                req = urllib.request.Request(MUSCI_URL, data=buf.getvalue(),
                    headers={"Content-Type": "audio/wav"}, method="POST")
                with urllib.request.urlopen(req, timeout=120) as r:
                    text = json.loads(r.read().decode()).get("text", "").strip()
                log(f"amplify-rescue: asr returned {len(text)} chars: {text[:100]!r}")
            except Exception as e:
                log(f"amplify-rescue failed: {type(e).__name__}: {e}")

        # Ground truth for the diagnosis: keep the audio of every failed /
        # ultra-quiet cycle (bounded retention). Synthetic tests proved quiet
        # speech still transcribes — so a failed cycle means the user's voice
        # wasn't really IN the captured signal; these archives are how we
        # confirm which mic/mode produced it.
        if not text or (rms is not None and rms < QUIET_RMS):
            try:
                import glob
                arch_dir = os.path.join(os.path.dirname(WAV_PATH), "dictate-failed")
                os.makedirs(arch_dir, exist_ok=True)
                mic_tag = "".join(c if c.isalnum() else "_" for c in self._mic)[:32]
                dst = os.path.join(arch_dir, time.strftime("%Y%m%d-%H%M%S") +
                                   f"-rms{rms if rms is None else round(rms, 5)}-{mic_tag}.wav")
                shutil.copyfile(self.wav, dst)
                for old in sorted(glob.glob(os.path.join(arch_dir, "*.wav")))[:-10]:
                    os.remove(old)
                log(f"archived failed cycle -> {dst}")
            except Exception as e:
                log(f"archive failed: {type(e).__name__}: {e}")

        if text:
            try:
                target = deliver_text(text)
                log(f"delivered -> {target}")
                notify(f"✓ pasted to {target}", text[:120])
            except Exception as e:
                notify("delivery failed", f"{type(e).__name__}: {str(e)[:80]}")
        else:
            notify("no speech detected", f"mic: {self._mic}, rms {rms if rms is None else round(rms, 5)}")
        self._done()

    def _done(self):
        self.state = IDLE
        self.title = "🎙"
        self._ov("hide")
        log("cycle complete")


if __name__ == "__main__":
    DictateApp().run()
