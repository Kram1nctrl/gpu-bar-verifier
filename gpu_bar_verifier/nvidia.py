"""Thin wrapper around nvidia-smi.

The Performance Monitor app reads the dedicated GPU through nvidia-smi, so the
verifier must read the *same* source: comparing the UI against a different
counter (NVML via a library, sysfs, ...) could disagree for reasons that have
nothing to do with the UI.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass

FIELDS = (
    "name",
    "driver_version",
    "utilization.gpu",
    "clocks.current.graphics",
    "temperature.gpu",
    "memory.used",
    "memory.total",
)


@dataclass(frozen=True)
class GpuStats:
    name: str
    driver: str
    utilization: float | None
    clock_mhz: float | None
    temperature_c: float | None
    memory_used_mib: float | None
    memory_total_mib: float | None


def available() -> bool:
    return shutil.which("nvidia-smi") is not None


def _number(text: str) -> float | None:
    try:
        return float(text.strip())
    except (TypeError, ValueError):
        return None  # "[N/A]" on unsupported cards


def query_all(timeout: float = 2.0) -> list[GpuStats]:
    if not available():
        return []
    try:
        proc = subprocess.run(
            ["nvidia-smi", f"--query-gpu={','.join(FIELDS)}",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []

    gpus: list[GpuStats] = []
    for line in proc.stdout.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < len(FIELDS):
            continue
        gpus.append(GpuStats(
            name=parts[0],
            driver=parts[1],
            utilization=_number(parts[2]),
            clock_mhz=_number(parts[3]),
            temperature_c=_number(parts[4]),
            memory_used_mib=_number(parts[5]),
            memory_total_mib=_number(parts[6]),
        ))
    return gpus


def query(timeout: float = 2.0) -> GpuStats | None:
    gpus = query_all(timeout)
    return gpus[0] if gpus else None
