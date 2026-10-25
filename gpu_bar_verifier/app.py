"""GTK3 desktop application.

Layout: load controls and live GPU telemetry on the left, verification results
and the last captured screenshot on the right.
"""

from __future__ import annotations

import json
import os
import threading

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk  # noqa: E402

from . import __version__, nvidia  # noqa: E402
from .bars import measure_bars  # noqa: E402
from .glxload import GpuLoader  # noqa: E402
from .verify import SuiteResult, run_suite  # noqa: E402
from .xcapture import CaptureError, capture_by_title  # noqa: E402

APP_ID = "io.github.kram1nctrl.GpuBarVerifier"
APP_NAME = "GPU Bar Verifier"
ICON_NAME = "gpu-bar-verifier"
PRGNAME = "gpu-bar-verifier"
WM_CLASS = "GpuBarVerifier"

ASSETS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets")
CACHE_DIR = os.path.join(
    os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")),
    "gpu-bar-verifier")

CSS = b"""
.panel-title { font-weight: 600; font-size: 13px; }
.metric-value { font-weight: 600; font-size: 15px; }
.pass { color: #2ec27e; font-weight: 600; }
.fail { color: #e01b24; font-weight: 600; }
.dim { color: #888888; }
.big { font-size: 17px; font-weight: 700; }
.badge { font-size: 9px; font-weight: 700; padding: 1px 6px; border-radius: 8px;
         background: #555555; color: #ffffff; }
.badge-nvidia { background: #76b900; color: #102000; }
.badge-intel { background: #0071c5; color: #ffffff; }
.badge-amd { background: #ed1c24; color: #ffffff; }
"""


def app_icon(size: int = 128):
    """The bundled icon as a pixbuf, or None when running from a source tree."""
    path = os.path.join(ASSETS, "icons", f"gpu-bar-verifier-{size}.png")
    try:
        return GdkPixbuf.Pixbuf.new_from_file(path)
    except (GLib.Error, OSError):
        return None


def vendor_of(name: str) -> str:
    """Short vendor tag used for the badge next to the GPU name."""
    lowered = name.casefold()
    if "nvidia" in lowered or "geforce" in lowered or "quadro" in lowered:
        return "NVIDIA"
    if "intel" in lowered or "arc" in lowered or "iris" in lowered:
        return "INTEL"
    if "amd" in lowered or "radeon" in lowered:
        return "AMD"
    return ""


def _pil_to_pixbuf(image, max_w: int, max_h: int):
    """Scale a PIL image to fit and wrap it in a GdkPixbuf."""
    width, height = image.size
    scale = min(max_w / width, max_h / height, 1.0)
    if scale < 1.0:
        image = image.resize((max(1, int(width * scale)),
                              max(1, int(height * scale))))
    data = image.convert("RGB").tobytes()
    w, h = image.size
    # new_from_bytes copies nothing but keeps a reference to the buffer, which
    # is what we want: new_from_data would leave a dangling pointer.
    return GdkPixbuf.Pixbuf.new_from_bytes(
        GLib.Bytes.new(data), GdkPixbuf.Colorspace.RGB, False, 8, w, h, w * 3)


class VerifierWindow(Gtk.ApplicationWindow):
    def __init__(self, app: Gtk.Application, window_title: str):
        super().__init__(application=app,
                         title=f"GPU Bar Verifier {__version__}")
        self.window_title = window_title
        self.set_default_size(980, 620)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_border_width(0)

        self.loader = GpuLoader()
        self._verify_thread: threading.Thread | None = None
        self._last_result: SuiteResult | None = None
        self._last_image = None

        Gtk.Window.set_default_icon_name(ICON_NAME)
        icon = app_icon(128)
        if icon is not None:
            self.set_icon(icon)

        header = Gtk.HeaderBar(show_close_button=True)
        header.set_title(APP_NAME)
        header.set_subtitle("Prove the utilization bar follows real load")
        self.set_titlebar(header)

        logo = app_icon(48)
        if logo is not None:
            logo_image = Gtk.Image.new_from_pixbuf(logo)
            logo_image.set_tooltip_text(APP_NAME)
            header.pack_start(logo_image)

        about = Gtk.Button.new_from_icon_name("help-about-symbolic",
                                              Gtk.IconSize.BUTTON)
        about.set_tooltip_text("About")
        about.connect("clicked", self._on_about)
        header.pack_end(about)

        outer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        outer.set_border_width(12)
        self.add(outer)

        # The left column is fixed width and scrolls on short screens.
        left_scroll = Gtk.ScrolledWindow()
        left_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        left_scroll.set_size_request(340, -1)
        left_scroll.add(self._build_left())
        outer.pack_start(left_scroll, False, False, 0)
        outer.pack_start(self._build_right(), True, True, 0)

        GLib.timeout_add(1000, self._poll_stats)
        self.connect("destroy", self._on_destroy)
        self._set_status("Ready. Open the GPU page in the app under test, "
                         "then run a sweep.")

    # ------------------------------------------------------------------ UI
    def _build_left(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_size_request(330, -1)

        # -- target GPU summary
        gpu_frame = Gtk.Frame(label="GPU")
        gpu_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        gpu_box.set_border_width(10)
        name_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.gpu_name = Gtk.Label(label="detecting…", xalign=0)
        self.gpu_name.get_style_context().add_class("big")
        self.vendor_badge = Gtk.Label(label="", no_show_all=True)
        self.vendor_badge.get_style_context().add_class("badge")
        name_row.pack_start(self.gpu_name, False, False, 0)
        name_row.pack_start(self.vendor_badge, False, False, 0)

        self.gpu_driver = Gtk.Label(label="", xalign=0)
        self.gpu_driver.get_style_context().add_class("dim")
        gpu_box.pack_start(name_row, False, False, 0)
        gpu_box.pack_start(self.gpu_driver, False, False, 0)
        gpu_frame.add(gpu_box)
        box.pack_start(gpu_frame, False, False, 0)

        # -- load control
        load_frame = Gtk.Frame(label="Load generator")
        load_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        load_box.set_border_width(10)

        target_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        target_row.pack_start(Gtk.Label(label="Target", xalign=0), False, False, 0)
        self.target_spin = Gtk.SpinButton.new_with_range(0, 100, 5)
        self.target_spin.set_value(60)
        self.target_spin.connect("value-changed", self._on_target_changed)
        target_row.pack_end(self.target_spin, False, False, 0)
        load_box.pack_start(target_row, False, False, 0)

        self.target_scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, 0, 100, 5)
        self.target_scale.set_value(60)
        self.target_scale.set_draw_value(False)
        self.target_scale.connect("value-changed", self._on_scale_changed)
        load_box.pack_start(self.target_scale, False, False, 0)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.start_btn = Gtk.Button(label="Start load")
        self.start_btn.connect("clicked", self._on_start)
        self.stop_btn = Gtk.Button(label="Stop")
        self.stop_btn.connect("clicked", self._on_stop)
        self.stop_btn.set_sensitive(False)
        buttons.pack_start(self.start_btn, True, True, 0)
        buttons.pack_start(self.stop_btn, True, True, 0)
        load_box.pack_start(buttons, False, False, 0)

        grid = Gtk.Grid(column_spacing=12, row_spacing=6, margin_top=4)
        self.metrics: dict[str, Gtk.Label] = {}
        for row, (key, caption) in enumerate([
            ("util", "Utilization"), ("clock", "Clock"),
            ("temp", "Temperature"), ("vram", "VRAM"),
            ("fps", "Render rate"),
        ]):
            grid.attach(Gtk.Label(label=caption, xalign=0), 0, row, 1, 1)
            value = Gtk.Label(label="–", xalign=1)
            value.get_style_context().add_class("metric-value")
            grid.attach(value, 1, row, 1, 1)
            self.metrics[key] = value
        load_box.pack_start(grid, False, False, 0)

        self.renderer_label = Gtk.Label(label="", xalign=0)
        self.renderer_label.set_line_wrap(True)
        self.renderer_label.get_style_context().add_class("dim")
        load_box.pack_start(self.renderer_label, False, False, 0)

        load_frame.add(load_box)
        box.pack_start(load_frame, False, False, 0)

        # -- verification settings
        verify_frame = Gtk.Frame(label="Verification")
        verify_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        verify_box.set_border_width(10)

        win_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        win_row.pack_start(Gtk.Label(label="Window", xalign=0), False, False, 0)
        self.window_entry = Gtk.Entry()
        self.window_entry.set_text(self.window_title)
        win_row.pack_end(self.window_entry, True, True, 0)
        verify_box.pack_start(win_row, False, False, 0)

        tol_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        tol_row.pack_start(Gtk.Label(label="Tolerance (points)", xalign=0),
                           False, False, 0)
        self.tolerance_spin = Gtk.SpinButton.new_with_range(1, 100, 1)
        self.tolerance_spin.set_value(15)
        tol_row.pack_end(self.tolerance_spin, False, False, 0)
        verify_box.pack_start(tol_row, False, False, 0)

        targets_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        targets_row.pack_start(Gtk.Label(label="Targets", xalign=0), False, False, 0)
        self.targets_entry = Gtk.Entry()
        self.targets_entry.set_text("0, 30, 60, 90")
        targets_row.pack_end(self.targets_entry, True, True, 0)
        verify_box.pack_start(targets_row, False, False, 0)

        run_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.run_btn = Gtk.Button(label="Run test sweep")
        self.run_btn.get_style_context().add_class("suggested-action")
        self.run_btn.connect("clicked", self._on_run)
        self.capture_btn = Gtk.Button(label="Capture only")
        self.capture_btn.connect("clicked", self._on_capture)
        run_row.pack_start(self.run_btn, True, True, 0)
        run_row.pack_start(self.capture_btn, False, False, 0)
        verify_box.pack_start(run_row, False, False, 0)

        verify_frame.add(verify_box)
        box.pack_start(verify_frame, False, False, 0)
        return box

    def _build_right(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)

        self.verdict = Gtk.Label(label="Not run yet", xalign=0)
        self.verdict.get_style_context().add_class("big")
        box.pack_start(self.verdict, False, False, 0)

        # results table
        results_frame = Gtk.Frame(label="Results")
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_min_content_height(140)
        self.store = Gtk.ListStore(str, str, str, str, str, str)
        self.tree = Gtk.TreeView(model=self.store)
        for index, title in enumerate(
                ["Target", "nvidia-smi", "Integrated", "Dedicated", "Result", "Note"]):
            renderer = Gtk.CellRendererText()
            column = Gtk.TreeViewColumn(title, renderer, text=index)
            column.set_resizable(True)
            self.tree.append_column(column)
        scroll.add(self.tree)
        results_frame.add(scroll)
        box.pack_start(results_frame, True, True, 0)

        # screenshot preview
        shot_frame = Gtk.Frame(label="Last capture")
        self.preview = Gtk.Image()
        self.preview.set_size_request(-1, 170)
        shot_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        shot_box.set_border_width(8)
        shot_box.pack_start(self.preview, True, True, 0)
        shot_frame.add(shot_box)
        box.pack_start(shot_frame, True, True, 0)

        # status + export
        bottom = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.status = Gtk.Label(label="", xalign=0)
        self.status.set_line_wrap(True)
        self.status.get_style_context().add_class("dim")
        self.save_btn = Gtk.Button(label="Save report…")
        self.save_btn.set_sensitive(False)
        self.save_btn.connect("clicked", self._on_save)
        bottom.pack_start(self.status, True, True, 0)
        bottom.pack_start(self.save_btn, False, False, 0)
        box.pack_start(bottom, False, False, 0)
        return box

    # -------------------------------------------------------------- helpers
    def _set_status(self, text: str) -> None:
        self.status.set_text(text)

    def _parse_targets(self) -> list[float]:
        raw = self.targets_entry.get_text().replace(" ", "")
        targets = []
        for part in raw.split(","):
            if not part:
                continue
            try:
                value = float(part)
            except ValueError:
                raise ValueError(f"invalid target {part!r}")
            targets.append(max(0.0, min(100.0, value)))
        if not targets:
            raise ValueError("no targets given")
        return targets

    # ---------------------------------------------------------------- events
    def _on_target_changed(self, spin: Gtk.SpinButton) -> None:
        value = spin.get_value()
        if abs(self.target_scale.get_value() - value) > 0.01:
            self.target_scale.set_value(value)
        if self.loader.running:
            self.loader.target = value

    def _on_scale_changed(self, scale: Gtk.Scale) -> None:
        value = scale.get_value()
        if abs(self.target_spin.get_value() - value) > 0.01:
            self.target_spin.set_value(value)
        if self.loader.running:
            self.loader.target = value

    def _on_start(self, _button) -> None:
        self.loader.start(self.target_spin.get_value())
        self.start_btn.set_sensitive(False)
        self.stop_btn.set_sensitive(True)
        self._set_status("Load running. Watch the bar in the app under test.")

    def _on_stop(self, _button) -> None:
        self.loader.stop()
        self.start_btn.set_sensitive(True)
        self.stop_btn.set_sensitive(False)
        self._set_status("Load stopped.")

    def _on_capture(self, _button) -> None:
        title = self.window_entry.get_text().strip()
        try:
            image, info = capture_by_title(title)
        except CaptureError as exc:
            self._set_status(f"Capture failed: {exc}")
            return
        bars = measure_bars(image)
        stats = nvidia.query()
        util = stats.utilization if stats else None
        self._show_preview(image)
        if not bars:
            self._set_status("No bars found — is the app showing the GPU page?")
            return
        dedicated = bars[1].percent if len(bars) > 1 else None
        self._set_status(
            f"Captured {info.title!r}: {len(bars)} bar(s), "
            f"dedicated={'-' if dedicated is None else f'{dedicated:.0f}%'}, "
            f"nvidia-smi={'-' if util is None else f'{util:.0f}%'}")

    def _on_run(self, _button) -> None:
        if self._verify_thread and self._verify_thread.is_alive():
            return
        try:
            targets = self._parse_targets()
        except ValueError as exc:
            self._set_status(f"Invalid input: {exc}")
            return

        self.store.clear()
        self.save_btn.set_sensitive(False)
        self.run_btn.set_sensitive(False)
        self.verdict.set_text("Running…")
        for cls in ("pass", "fail"):
            self.verdict.get_style_context().remove_class(cls)
        shot_dir = os.path.join(CACHE_DIR, "last-run")
        self._verify_thread = threading.Thread(
            target=self._verify_worker,
            args=(targets, shot_dir), daemon=True)
        self._verify_thread.start()

    def _verify_worker(self, targets: list[float], shot_dir: str) -> None:
        def progress(index: int, total: int, message: str) -> None:
            GLib.idle_add(self._set_status,
                          f"[{index + 1}/{total}] {message}")

        try:
            result = run_suite(
                self.loader,
                window=self.window_entry.get_text().strip(),
                targets=targets,
                tolerance=self.tolerance_spin.get_value(),
                shot_dir=shot_dir,
                progress=progress,
            )
        except Exception as exc:  # keep the UI alive whatever happens
            GLib.idle_add(self._on_finished_error, str(exc))
            return
        GLib.idle_add(self._on_finished, result)

    def _on_finished_error(self, message: str) -> None:
        self.run_btn.set_sensitive(True)
        self.verdict.set_text("Error")
        self._set_status(message)

    def _on_finished(self, result: SuiteResult) -> None:
        self.run_btn.set_sensitive(True)
        self._last_result = result
        self.store.clear()
        for step in result.steps:
            self.store.append([
                f"{step.target:.0f}%",
                "-" if step.gpu_util is None else f"{step.gpu_util:.0f}%",
                "-" if step.integrated is None else f"{step.integrated:.0f}%",
                "-" if step.dedicated is None else f"{step.dedicated:.0f}%",
                "PASS" if step.ok else "FAIL",
                step.note,
            ])
        context = self.verdict.get_style_context()
        if result.error:
            self.verdict.set_text("Error")
            context.add_class("fail")
            self._set_status(result.error)
            return

        if result.passed:
            self.verdict.set_text(f"PASS — {len(result.steps)} steps, "
                                  f"{result.duration:.0f}s")
            context.add_class("pass")
            context.remove_class("fail")
        else:
            failed = sum(1 for s in result.steps if not s.ok)
            self.verdict.set_text(f"FAIL — {failed} of {len(result.steps)} steps")
            context.add_class("fail")
            context.remove_class("pass")
        self.save_btn.set_sensitive(True)
        last_shot = next((s.shot for s in reversed(result.steps) if s.shot), None)
        if last_shot:
            try:
                from PIL import Image
                self._show_preview(Image.open(last_shot))
            except Exception:
                pass
        self._set_status(f"Report ready. Screenshots in {os.path.dirname(last_shot) if last_shot else '-'}")

    def _on_save(self, _button) -> None:
        if self._last_result is None:
            return
        dialog = Gtk.FileChooserDialog(
            title="Save report", transient_for=self,
            action=Gtk.FileChooserAction.SAVE)
        dialog.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                           Gtk.STOCK_SAVE, Gtk.ResponseType.OK)
        dialog.set_current_name("gpu-bar-report.json")
        if dialog.run() == Gtk.ResponseType.OK:
            path = dialog.get_filename()
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(self._last_result.to_dict(), fh, indent=2)
            self._set_status(f"Saved {path}")
        dialog.destroy()

    def _show_preview(self, image) -> None:
        self._last_image = image
        try:
            allocation = self.preview.get_allocation()
            max_w = max(320, allocation.width or 600)
            max_h = 170
            pixbuf = _pil_to_pixbuf(image, max_w, max_h)
            self.preview.set_from_pixbuf(pixbuf)
        except Exception as exc:  # pragma: no cover - display dependent
            self._set_status(f"Preview failed: {exc}")

    def _poll_stats(self) -> bool:
        stats = nvidia.query()
        if stats is None:
            self.gpu_name.set_text("No NVIDIA GPU")
            self.gpu_driver.set_text("nvidia-smi not available")
        else:
            self.gpu_name.set_text(stats.name)
            self.gpu_driver.set_text(f"driver {stats.driver}")
            self._set_vendor(vendor_of(stats.name))
            self.metrics["util"].set_text(
                "–" if stats.utilization is None else f"{stats.utilization:.0f}%")
            self.metrics["clock"].set_text(
                "–" if stats.clock_mhz is None else f"{stats.clock_mhz:.0f} MHz")
            self.metrics["temp"].set_text(
                "–" if stats.temperature_c is None else f"{stats.temperature_c:.0f} °C")
            self.metrics["vram"].set_text(
                "–" if stats.memory_used_mib is None or stats.memory_total_mib is None
                else f"{stats.memory_used_mib:.0f} / {stats.memory_total_mib:.0f} MiB")
        self.metrics["fps"].set_text(
            f"{self.loader.fps:.0f} fps" if self.loader.running else "–")
        if self.loader.running and self.loader.renderer:
            self.renderer_label.set_text(f"Rendering on: {self.loader.renderer}")
        if self.loader.error:
            self._set_status(f"Load generator error: {self.loader.error}")
            self.loader.stop()
            self.start_btn.set_sensitive(True)
            self.stop_btn.set_sensitive(False)
        return True

    def _set_vendor(self, vendor: str) -> None:
        context = self.vendor_badge.get_style_context()
        for cls in ("badge-nvidia", "badge-intel", "badge-amd"):
            context.remove_class(cls)
        if not vendor:
            self.vendor_badge.set_no_show_all(True)
            self.vendor_badge.hide()
            return
        self.vendor_badge.set_text(vendor)
        context.add_class(f"badge-{vendor.casefold()}")
        self.vendor_badge.set_no_show_all(False)
        self.vendor_badge.show()

    def _on_destroy(self, _widget) -> None:
        self.loader.stop()

    def _on_about(self, _button) -> None:
        dialog = Gtk.AboutDialog(transient_for=self, modal=True)
        dialog.set_program_name(APP_NAME)
        logo = app_icon(128)
        if logo is not None:
            dialog.set_logo(logo)
        dialog.set_version(__version__)
        dialog.set_comments(
            "Generates a controllable GPU load and verifies that a utilization "
            "bar in another application follows the real hardware reading.")
        dialog.set_license_type(Gtk.License.MIT_X11)
        dialog.set_website("https://github.com/Kram1nctrl/gpu-bar-verifier")
        dialog.run()
        dialog.destroy()


class VerifierApp(Gtk.Application):
    def __init__(self, window_title: str):
        super().__init__(application_id=APP_ID,
                         flags=0)
        self.window_title = window_title
        self.win: VerifierWindow | None = None

    def do_startup(self) -> None:
        Gtk.Application.do_startup(self)
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        screen = Gdk.Screen.get_default()
        if screen is not None:
            Gtk.StyleContext.add_provider_for_screen(
                screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def do_activate(self) -> None:
        if self.win is None:
            self.win = VerifierWindow(self, self.window_title)
        self.win.show_all()
        self.win.present()


def main(window_title: str = "Performance Monitor") -> int:
    # Deterministic WM_CLASS / taskbar identity: the desktop entry names
    # GpuBarVerifier as StartupWMClass so the launcher and the window share
    # one icon and one entry instead of showing a generic "python3" window.
    GLib.set_prgname(PRGNAME)
    GLib.set_application_name(APP_NAME)
    Gtk.Window.set_default_icon_name(ICON_NAME)
    # The "class" half of WM_CLASS is what StartupWMClass in the .desktop file
    # matches; without this GDK derives it from argv[0] ("Cli.py") and the
    # launcher shows a second, icon-less entry.
    Gdk.set_program_class(WM_CLASS)

    return VerifierApp(window_title).run([])


if __name__ == "__main__":
    raise SystemExit(main())
