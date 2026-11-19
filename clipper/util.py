"""Subprocess helpers shared by the agents."""
from __future__ import annotations

import asyncio
import atexit
import os
import re
import shutil
import subprocess
import sys
import unicodedata

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
CHILDREN: set[subprocess.Popen] = set()

# Run the tools through the same interpreter so the venv copies are always used.
STREAMLINK = [sys.executable, "-m", "streamlink"]
YTDLP = [sys.executable, "-m", "yt_dlp"]


def ffmpeg_bin() -> str:
    return shutil.which("ffmpeg") or "ffmpeg"


def ffprobe_bin() -> str:
    return shutil.which("ffprobe") or "ffprobe"


def _make_kill_on_exit_job():
    """Windows only: a Job Object that kills every process in it when this app dies —
    even if the app is force-closed — so no recorder is ever left running in the background."""
    if os.name != "nt":
        return None, None
    try:
        import ctypes
        from ctypes import wintypes

        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class IoCounters(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in ("ReadOps", "WriteOps", "OtherOps", "ReadBytes", "WriteBytes", "OtherBytes")]

        class Extended(ctypes.Structure):
            _fields_ = [("Basic", Basic), ("Io", IoCounters), ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        job = k32.CreateJobObjectW(None, None)
        info = Extended()
        info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not job or not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            return None, None
        return k32, job
    except Exception:
        return None, None


_K32, _JOB = _make_kill_on_exit_job()


def spawn(args, **kw) -> subprocess.Popen:
    """Start a long-running child process that is killed when the app exits."""
    kw.setdefault("creationflags", NO_WINDOW)
    proc = subprocess.Popen(args, **kw)
    CHILDREN.add(proc)
    if _JOB:
        try:
            _K32.AssignProcessToJobObject(_JOB, int(proc._handle))
        except Exception:
            pass
    return proc


def kill(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)
    except Exception:
        pass
    CHILDREN.discard(proc)


@atexit.register
def _kill_children() -> None:
    for proc in list(CHILDREN):
        kill(proc)


async def run_cmd(args, timeout: float = 120, cwd=None) -> tuple[int, str, str]:
    """Run a short command off the event loop. Returns (returncode, stdout, stderr)."""

    def _run():
        try:
            p = subprocess.run(args, capture_output=True, timeout=timeout, cwd=cwd,
                               creationflags=NO_WINDOW)
            return (p.returncode, p.stdout.decode("utf-8", "replace"),
                    p.stderr.decode("utf-8", "replace"))
        except subprocess.TimeoutExpired:
            return -1, "", "timed out"
        except OSError as exc:
            return -2, "", str(exc)

    return await asyncio.to_thread(_run)


def slug(text: str, limit: int = 40) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:limit].strip("-") or "clip"
