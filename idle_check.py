"""
idle_check.py — detects whether the Windows PC is "busy" (a fullscreen game
or an app hogging the GPU/CPU) so the night runner can skip the heavy local AI
pass when it would interfere with the user.

Used by night_runner.run_night(): if idle_check.is_machine_busy() returns True,
the runner skips the Ollama news pass (and optionally pauses) until it's free.

Everything here is read-only / measurement only — it never changes anything.
"""
import ctypes
import ctypes.wintypes
import time


_GPU_BUSY_THRESHOLD = 85.0   # % GPU utilization considered "busy"
_CPU_BUSY_THRESHOLD = 70.0   # % CPU utilization considered "busy"


def _fullscreen_window_title():
    """
    If the currently focused window covers the whole primary screen, treat it
    as a fullscreen app/game and return its title (else "").
    Uses Win32 GetForegroundWindow + GetWindowRect via ctypes (no extra deps).
    """
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return ""

        # title of the focused window
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value

        # bounding rect of the window
        rect = ctypes.wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return ""

        # primary screen work area (virtual desktop across monitors)
        sm_x = ctypes.windll.user32.GetSystemMetrics(0)   # SM_CXSCREEN
        sm_y = ctypes.windll.user32.GetSystemMetrics(1)   # SM_CYSCREEN

        w = rect.right - rect.left
        h = rect.bottom - rect.top

        # covers (almost) the whole primary screen -> fullscreen app
        if w >= sm_x * 0.98 and h >= sm_y * 0.98:
            return title
        return ""
    except Exception:
        return ""


def _gpu_utilization():
    """Return GPU utilization % via nvidia-smi, or None if unavailable."""
    try:
        import subprocess
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5)
        val = float(out.stdout.strip().splitlines()[0].strip())
        return val
    except Exception:
        return None


def _cpu_utilization():
    """Return overall CPU utilization % (sampled), or None if unavailable."""
    try:
        import psutil
        return psutil.cpu_percent(interval=0.5)
    except (ImportError, Exception):
        return None


def is_machine_busy(checks=None):
    """
    Return True if the PC looks busy (game fullscreen OR GPU/CPU hogged).

    checks: optional list controlling which signals to test from
            {"game", "gpu", "cpu"}. Default: all available.
    """
    if checks is None:
        checks = {"game", "gpu", "cpu"}

    reasons = []

    if "game" in checks:
        title = _fullscreen_window_title()
        if title:
            reasons.append(f"fullscreen app: '{title}'")

    if "gpu" in checks:
        g = _gpu_utilization()
        if g is not None and g >= _GPU_BUSY_THRESHOLD:
            reasons.append(f"GPU {g:.0f}% busy")

    if "cpu" in checks:
        c = _cpu_utilization()
        if c is not None and c >= _CPU_BUSY_THRESHOLD:
            reasons.append(f"CPU {c:.0f}% busy")

    return reasons


if __name__ == "__main__":
    print("machine busy reasons:", is_machine_busy())
    print("fullscreen window:", repr(_fullscreen_window_title() or ""))
