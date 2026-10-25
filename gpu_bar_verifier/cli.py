"""Command line entry point: `gpu-bar-verifier` with no arguments opens the GUI."""

from __future__ import annotations

import argparse
import json
import sys
import time

from . import __version__, nvidia
from .glxload import GpuLoader
from .verify import capture_once, run_suite


def _parse_targets(text: str) -> list[float]:
    try:
        targets = [float(part) for part in text.replace(" ", "").split(",") if part]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"bad target list: {text!r}") from exc
    if not targets:
        raise argparse.ArgumentTypeError("no targets given")
    for t in targets:
        if not 0 <= t <= 100:
            raise argparse.ArgumentTypeError(f"target {t} outside 0-100")
    return targets


def cmd_verify(args: argparse.Namespace) -> int:
    loader = GpuLoader()
    print(f"GPU Bar Verifier {__version__}")
    stats = nvidia.query()
    if stats is None:
        print("warning: nvidia-smi is not available; utilization will be unknown")
    else:
        print(f"GPU: {stats.name}  driver {stats.driver}")

    def progress(index: int, total: int, message: str) -> None:
        print(f"  [{index + 1}/{total}] {message}", flush=True)

    result = run_suite(
        loader,
        window=args.window,
        targets=args.targets,
        tolerance=args.tolerance,
        settle=args.settle,
        shot_dir=args.shots,
        progress=progress,
    )

    print()
    if result.error:
        print(f"error: {result.error}")
        return 2
    print(result.table())
    print()
    print(f"RESULT: {'PASS' if result.passed else 'FAIL'} "
          f"({len(result.steps)} steps, {result.duration:.0f}s)")
    if args.shots:
        print(f"screenshots: {args.shots}")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(result.to_dict(), fh, indent=2)
        print(f"report: {args.json}")
    return 0 if result.passed else 1


def cmd_load(args: argparse.Namespace) -> int:
    loader = GpuLoader()
    loader.start(args.target)
    print(f"loading {args.target:.0f}% for {args.duration:.0f}s "
          f"(renderer: {loader.renderer or 'starting...'})")
    deadline = time.time() + args.duration
    try:
        while time.time() < deadline:
            time.sleep(1.0)
            stats = nvidia.query()
            util = "-" if stats is None or stats.utilization is None else f"{stats.utilization:.0f}%"
            print(f"  util={util:>4}  fps={loader.fps:6.1f}", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        loader.stop()
    if loader.error:
        print(f"error: {loader.error}")
        return 2
    return 0


def cmd_capture(args: argparse.Namespace) -> int:
    bars, util = capture_once(args.window, args.out)
    print(f"nvidia-smi utilization: {'unknown' if util is None else f'{util:.0f}%'}")
    if not bars:
        print("no utilization bars found (is the app on the GPU page?)")
        return 1
    for index, bar in enumerate(bars):
        role = "integrated" if index == 0 else "dedicated" if index == 1 else f"bar{index}"
        print(f"{role:>10}: {bar.percent:5.1f}%  {bar}")
    if args.out:
        print(f"screenshot: {args.out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="gpu-bar-verifier",
        description="Prove that a GPU utilization bar follows real hardware load.")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="command")

    gui = sub.add_parser("gui", help="open the graphical app (default)")
    gui.add_argument("--window", default="Performance Monitor")

    verify = sub.add_parser("verify", help="run a load sweep and check the bars")
    verify.add_argument("--window", default="Performance Monitor")
    verify.add_argument("--targets", type=_parse_targets, default=[0.0, 40.0, 80.0],
                        help="comma separated load targets (default 0,40,80)")
    verify.add_argument("--tolerance", type=float, default=15.0,
                        help="allowed difference in percentage points")
    verify.add_argument("--settle", type=float, default=4.0,
                        help="seconds to wait per step for the UI to refresh")
    verify.add_argument("--shots", default=None, help="directory for screenshots")
    verify.add_argument("--json", default=None, help="write a JSON report here")

    load = sub.add_parser("load", help="only run the load generator")
    load.add_argument("--target", type=float, default=60.0)
    load.add_argument("--duration", type=float, default=30.0)

    cap = sub.add_parser("capture", help="measure the bars without loading the GPU")
    cap.add_argument("--window", default="Performance Monitor")
    cap.add_argument("--out", default=None)

    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    command = args.command or "gui"

    if command == "verify":
        return cmd_verify(args)
    if command == "load":
        return cmd_load(args)
    if command == "capture":
        return cmd_capture(args)

    window = getattr(args, "window", "Performance Monitor")
    try:
        from .app import main as gui_main
    except ImportError as exc:
        print(f"GTK is not available ({exc}). Use the CLI instead:\n"
              f"  gpu-bar-verifier verify --window {window!r}", file=sys.stderr)
        return 2
    return gui_main(window)


if __name__ == "__main__":
    raise SystemExit(main())
