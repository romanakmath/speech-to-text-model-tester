#!/usr/bin/env python3
"""CoreAudio input-device introspection via plain ctypes (no pyobjc marshaling).

Used by dictate-menubar.py to log which input device `rec` will capture from,
and to watch for default-input flips (Continuity iPhone mic <=> built-in).
"""
import ctypes
import ctypes.util

_ca = ctypes.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
_cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")

# --- FourCharCodes / constants (CoreAudio headers) ---
kAudioObjectSystemObject = 1
kAudioObjectPropertyName = 0x6C6E616D          # 'lnam'
kAudioObjectPropertyScopeGlobal = 0x676C6F62   # 'glob'
kAudioObjectPropertyElementMaster = 0
kAudioHardwarePropertyDefaultInputDevice = 0x64496E20  # 'dIn '
kAudioHardwarePropertyDevices = 0x64657623     # 'dev#'
kAudioDevicePropertyStreams = 0x7374726D       # 'strm'
kAudioObjectPropertyScopeInput = 0x696E7074    # 'inpt'


class AudioObjectPropertyAddress(ctypes.Structure):
    _fields_ = [("mSelector", ctypes.c_uint32),
                ("mScope", ctypes.c_uint32),
                ("mElement", ctypes.c_uint32)]


def _addr(sel, scope=kAudioObjectPropertyScopeGlobal):
    return AudioObjectPropertyAddress(sel, scope, kAudioObjectPropertyElementMaster)


_ca.AudioObjectGetPropertyData.argtypes = [
    ctypes.c_uint32, ctypes.POINTER(AudioObjectPropertyAddress),
    ctypes.c_uint32, ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
_ca.AudioObjectGetPropertyData.restype = ctypes.c_int32
_ca.AudioObjectHasProperty.argtypes = [
    ctypes.c_uint32, ctypes.POINTER(AudioObjectPropertyAddress)]
_ca.AudioObjectHasProperty.restype = ctypes.c_int32

_cf.CFStringGetCStringPtr.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
_cf.CFStringGetCStringPtr.restype = ctypes.c_char_p
_cf.CFStringGetCString.argtypes = [
    ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
_cf.CFStringGetCString.restype = ctypes.c_bool
_cf.CFRelease.argtypes = [ctypes.c_void_p]


def _cfstr_to_str(cf):
    if not cf:
        return None
    fast = _cf.CFStringGetCStringPtr(cf, 0)  # kCFStringEncodingUTF8 == 0
    if fast:
        return fast.decode()
    buf = ctypes.create_string_buffer(512)
    if _cf.CFStringGetCString(cf, buf, 512, 0):
        return buf.value.decode()
    return None


def _get_data(obj, sel, scope=kAudioObjectPropertyScopeGlobal, bufsize=1024):
    """Raw bytes for a property, or None. NOTE: scalar properties (e.g.
    DefaultInputDevice) reject oversized buffers with '!siz' — pass exact
    size via `bufsize` for those; arrays/pointers accept generous buffers."""
    a = _addr(sel, scope)
    if not _ca.AudioObjectHasProperty(obj, ctypes.byref(a)):
        return None
    size = ctypes.c_uint32(bufsize)
    buf = ctypes.create_string_buffer(bufsize)
    if _ca.AudioObjectGetPropertyData(obj, ctypes.byref(a), 0, None,
                                      ctypes.byref(size), buf) != 0:
        return None
    return buf.raw[:size.value]


def _get_name(dev_id):
    # Cached: device names are stable, and this dodges the murky CFString
    # ownership semantics of 'lnam' (release vs borrow) in a long-lived poller.
    if dev_id in _name_cache:
        return _name_cache[dev_id]
    raw = _get_data(dev_id, kAudioObjectPropertyName)
    if raw is None:
        return f"<dev {dev_id}>"
    ptr = int.from_bytes(raw[:8].ljust(8, b"\x00"), "little")
    name = _cfstr_to_str(ptr) or f"<dev {dev_id}>"
    _name_cache[dev_id] = name
    return name


_name_cache = {}


def _is_input(dev_id):
    return _get_data(dev_id, kAudioDevicePropertyStreams,
                     kAudioObjectPropertyScopeInput) is not None


def default_input_device():
    """(device_id, name) of the current default input, or (None, None)."""
    raw = _get_data(kAudioObjectSystemObject, kAudioHardwarePropertyDefaultInputDevice,
                    bufsize=4)
    if raw is None:
        return None, None
    dev_id = int.from_bytes(raw, "little")
    return dev_id, _get_name(dev_id)


def all_devices():
    """[(device_id, name), ...] of every audio device (input or output).
    Presence/absence of e.g. the Continuity iPhone mic in this list is the
    diagnostic signal — direction filtering is unreliable via streams."""
    raw = _get_data(kAudioObjectSystemObject, kAudioHardwarePropertyDevices)
    if raw is None:
        return []
    n = len(raw) // 4
    ids = [int.from_bytes(raw[i * 4:(i + 1) * 4], "little") for i in range(n)]
    return [(d, _get_name(d)) for d in ids]


if __name__ == "__main__":
    import sys
    d, name = default_input_device()
    print(f"DEFAULT INPUT: {d} -> {name!r}")
    for dev, nm in all_devices():
        print(f"  dev {dev}: {nm!r}", file=sys.stderr)
