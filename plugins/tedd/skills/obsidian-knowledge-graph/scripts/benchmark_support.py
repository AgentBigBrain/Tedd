#!/usr/bin/env python3
"""Small cross-platform helpers shared by Tedd's opt-in benchmarks."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def private_temporary_parent() -> str | None:
    """Choose disposable-state placement; callers still validate private paths."""

    if "TEDD_PRIVATE_TEMP_PARENT" in os.environ:
        value = os.environ["TEDD_PRIVATE_TEMP_PARENT"].strip()
        parent = Path(value)
        if not value or not parent.is_absolute() or not parent.is_dir():
            raise RuntimeError("TEDD_PRIVATE_TEMP_PARENT must be an existing absolute directory")
        return str(parent)

    if os.name != "nt":
        return None
    value = os.environ.get("LOCALAPPDATA", "").strip()
    if not value:
        raise RuntimeError("LOCALAPPDATA is required for private Windows benchmarks")
    parent = Path(value)
    if not parent.is_dir():
        raise RuntimeError(f"Windows private temporary parent does not exist: {parent}")
    return str(parent)


def peak_rss_kib() -> float:
    """Return this process's peak resident set size in KiB."""

    if os.name != "nt":
        import resource

        peak = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        return peak / 1024 if sys.platform == "darwin" else peak

    import ctypes
    from ctypes import wintypes

    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(ProcessMemoryCounters),
        wintypes.DWORD,
    ]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL

    counters = ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    if not psapi.GetProcessMemoryInfo(
        kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return float(counters.PeakWorkingSetSize) / 1024
