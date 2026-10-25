"""Run a load sweep and check the rendered bars against nvidia-smi.

A step passes when, for the same moment in time:
  * the target window still draws the bar (a trough exists at all), and
  * the painted fill matches what nvidia-smi reports, within a tolerance.

That combination is what "the bar works" means in practice: the number is real
*and* it reaches the screen.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Callable

from . import nvidia
from .bars import Bar, measure_bars
from .glxload import GpuLoader
from .xcapture import CaptureError, capture_by_title

Progress = Callable[[int, int, str], None]


@dataclass
class StepResult:
    target: float
    gpu_util: float | None
    integrated: float | None
    dedicated: float | None
    bar_drawn: bool
    delta: float | None
    ok: bool
    note: str = ""
    shot: str | None = None

    def row(self) -> str:
        gpu = "-" if self.gpu_util is None else f"{self.gpu_util:.0f}%"
        ig = "-" if self.integrated is None else f"{self.integrated:.0f}%"
        dg = "-" if self.dedicated is None else f"{self.dedicated:.0f}%"
        verdict = "PASS" if self.ok else "FAIL"
        return (f"{self.target:>6.0f}%  {gpu:>6}  {ig:>8}  {dg:>9}  "
                f"{verdict:>4}  {self.note}")


@dataclass
class SuiteResult:
    window: str
    tolerance: float
    steps: list[StepResult] = field(default_factory=list)
    error: str | None = None
    started: float = field(default_factory=time.time)

    @property
    def passed(self) -> bool:
        return self.error is None and bool(self.steps) and all(s.ok for s in self.steps)

    @property
    def duration(self) -> float:
        return time.time() - self.started

    def to_dict(self) -> dict:
        return {
            "window": self.window,
            "tolerance": self.tolerance,
            "passed": self.passed,
            "error": self.error,
            "duration_s": round(self.duration, 1),
            "steps": [
                {
                    "target": s.target,
                    "gpu_util": s.gpu_util,
                    "integrated": s.integrated,
                    "dedicated": s.dedicated,
                    "bar_drawn": s.bar_drawn,
                    "delta": s.delta,
                    "ok": s.ok,
                    "note": s.note,
                    "shot": s.shot,
                }
                for s in self.steps
            ],
        }

    def table(self) -> str:
        head = ("target  nvidia  integrated  dedicated  result  note\n"
                "------  ------  ----------  ---------  ------  ----")
        return "\n".join([head] + [s.row() for s in self.steps])


def _pick(bars: list[Bar]) -> tuple[float | None, float | None, bool]:
    """Left bar is the integrated GPU, right bar the dedicated one."""
    if len(bars) >= 2:
        return bars[0].percent, bars[1].percent, True
    if len(bars) == 1:
        return bars[0].percent, None, True
    return None, None, False


def run_suite(
    loader: GpuLoader,
    window: str = "Performance Monitor",
    targets: list[float] | None = None,
    tolerance: float = 15.0,
    settle: float = 4.0,
    shot_dir: str | None = None,
    progress: Progress | None = None,
) -> SuiteResult:
    """Sweep `targets`, loading the GPU at each step and checking the UI."""
    targets = targets if targets is not None else [0, 40, 80]
    result = SuiteResult(window=window, tolerance=tolerance)
    total = len(targets)

    try:
        # Fail fast if the window is not reachable.
        capture_by_title(window)
    except CaptureError as exc:
        result.error = str(exc)
        return result

    for index, target in enumerate(targets):
        if progress:
            progress(index, total, f"loading GPU to {target:.0f}%")
        loader.start(target)
        time.sleep(settle)

        stats = nvidia.query()
        util = stats.utilization if stats else None

        if progress:
            progress(index, total, "capturing window")
        try:
            image, _info = capture_by_title(window)
        except CaptureError as exc:
            result.error = str(exc)
            return result

        shot = None
        if shot_dir:
            os.makedirs(shot_dir, exist_ok=True)
            shot = os.path.join(shot_dir, f"target-{int(target):03d}.png")
            image.save(shot)

        integrated, dedicated, drawn = _pick(measure_bars(image))

        note = ""
        ok = drawn and dedicated is not None
        delta = None
        if dedicated is None:
            note = "dedicated bar not found (is the GPU page open?)"
        elif util is None:
            note = "nvidia-smi returned no utilization"
            ok = False
        else:
            delta = dedicated - util
            if abs(delta) > tolerance:
                ok = False
                note = f"bar off by {delta:+.0f} pts (tol {tolerance:.0f})"
            elif util >= 5 and dedicated < 3:
                ok = False
                note = "GPU busy but bar stays empty"
            elif util < 5 and dedicated > 5:
                ok = False
                note = "GPU idle but bar is painted"
            else:
                note = "bar follows nvidia-smi"

        result.steps.append(StepResult(
            target=target,
            gpu_util=util,
            integrated=integrated,
            dedicated=dedicated,
            bar_drawn=drawn,
            delta=delta,
            ok=ok,
            note=note,
            shot=shot,
        ))

    loader.stop()
    _check_monotonic(result)
    return result


def _check_monotonic(result: SuiteResult) -> None:
    """A rising load must not produce a falling bar."""
    busy = [s for s in result.steps
            if s.gpu_util is not None and s.dedicated is not None and s.gpu_util >= 10]
    busy.sort(key=lambda s: s.gpu_util)
    for lower, higher in zip(busy, busy[1:]):
        if higher.gpu_util - lower.gpu_util >= 10 and higher.dedicated < lower.dedicated - 5:
            higher.ok = False
            higher.note = (f"bar went down ({lower.dedicated:.0f}% -> "
                           f"{higher.dedicated:.0f}%) while load rose")


def capture_once(window: str = "Performance Monitor",
                 shot: str | None = None) -> tuple[list[Bar], float | None]:
    """Measure the current state without generating any load."""
    image, _info = capture_by_title(window)
    if shot:
        image.save(shot)
    stats = nvidia.query()
    return measure_bars(image), (stats.utilization if stats else None)
