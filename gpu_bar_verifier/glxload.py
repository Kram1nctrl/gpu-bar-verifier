"""Controllable GPU load generator built on GLX + a heavy fragment shader.

The point is not to benchmark: it is to put a *known, adjustable* amount of work
on a specific GPU so a monitoring UI can be checked against reality.

On hybrid laptops the discrete GPU only renders when an application asks for it,
so the environment is pre-seeded with the usual offload switches:

    __NV_PRIME_RENDER_OFFLOAD=1     NVIDIA PRIME render offload
    __GLX_VENDOR_LIBRARY_NAME=nvidia
    DRI_PRIME=1                     Mesa (AMD / Intel) equivalent

No OpenGL headers are required: everything is loaded with ctypes.
"""

from __future__ import annotations

import ctypes
import os
import threading
import time

# ---------------------------------------------------------------- GLX / GL consts
GLX_DRAWABLE_TYPE = 0x8010
GLX_RENDER_TYPE = 0x8011
GLX_X_RENDERABLE = 0x8012
GLX_RGBA_BIT = 0x00000001
GLX_PBUFFER_BIT = 0x00000004
GLX_PBUFFER_WIDTH = 0x8041
GLX_PBUFFER_HEIGHT = 0x8040
GLX_CONTEXT_MAJOR_VERSION_ARB = 0x2091
GLX_CONTEXT_MINOR_VERSION_ARB = 0x2092
GLX_CONTEXT_PROFILE_MASK_ARB = 0x9126
GLX_CONTEXT_CORE_PROFILE_BIT_ARB = 0x00000001

GL_VENDOR = 0x1F00
GL_RENDERER = 0x1F01
GL_VERSION = 0x1F02
GL_FRAGMENT_SHADER = 0x8B30
GL_VERTEX_SHADER = 0x8B31
GL_COMPILE_STATUS = 0x8B81
GL_LINK_STATUS = 0x8B82
GL_ARRAY_BUFFER = 0x8892
GL_STATIC_DRAW = 0x88E4
GL_FLOAT = 0x1406
GL_TRIANGLES = 0x0004
GL_COLOR_BUFFER_BIT = 0x4000

VERTEX_SRC = b"""#version 330 core
layout(location = 0) in vec2 pos;
out vec2 uv;
void main() {
    uv = pos * 0.5 + 0.5;
    gl_Position = vec4(pos, 0.0, 1.0);
}
"""

# A deliberately ALU-heavy shader: enough work that the GPU cannot idle between
# frames, and cheap to scale by changing `iterations`.
FRAGMENT_SRC = b"""#version 330 core
in vec2 uv;
out vec4 frag_color;
uniform float t;
uniform float iterations;
void main() {
    vec2 p = uv * 8.0;
    vec3 c = vec3(0.0);
    for (int i = 0; i < 512; i++) {
        if (float(i) >= iterations) break;
        float f = float(i) + 1.0;
        c += vec3(sin(p.x * f + t), cos(p.y * f - t), sin((p.x + p.y) * f + t * 0.5));
    }
    frag_color = vec4(abs(c / max(iterations, 1.0)), 1.0);
}
"""


class GlxError(RuntimeError):
    """Raised when a GL context cannot be created."""


def _offload_env() -> dict:
    env = dict(os.environ)
    env.setdefault("__NV_PRIME_RENDER_OFFLOAD", "1")
    env.setdefault("__GLX_VENDOR_LIBRARY_NAME", "nvidia")
    env.setdefault("DRI_PRIME", "1")
    return env


class _GlContext:
    """A 3.3 core-profile context on a pbuffer, plus one heavy draw call.

    Rendering into a pbuffer means nothing appears on screen: the display server
    keeps running on the integrated GPU while the discrete GPU is saturated.
    """

    def __init__(self, size: int = 1024, iterations: int = 300):
        self.size = size
        self.iterations = iterations
        self.renderer = ""
        self.vendor = ""
        self.gl_version = ""
        self._frames = 0
        self._open()

    # -- setup ---------------------------------------------------------------
    def _open(self) -> None:
        try:
            self._x11 = ctypes.CDLL("libX11.so.6")
            self._gl = ctypes.CDLL("libGL.so.1")
        except OSError as exc:  # pragma: no cover - depends on host
            raise GlxError(f"libGL/libX11 not available: {exc}") from exc

        x11, gl = self._x11, self._gl
        x11.XOpenDisplay.restype = ctypes.c_void_p
        x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        self._dpy = x11.XOpenDisplay(os.environ.get("DISPLAY", ":0").encode())
        if not self._dpy:
            raise GlxError("cannot open X display (is DISPLAY set?)")

        attribs = (ctypes.c_int * 13)(
            GLX_DRAWABLE_TYPE, GLX_PBUFFER_BIT,
            GLX_RENDER_TYPE, GLX_RGBA_BIT,
            GLX_X_RENDERABLE, 1,
            8, 8, 9, 8, 10, 8, 0,
        )
        gl.glXChooseFBConfig.restype = ctypes.POINTER(ctypes.c_void_p)
        gl.glXChooseFBConfig.argtypes = [
            ctypes.c_void_p, ctypes.c_int,
            ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
        ]
        count = ctypes.c_int(0)
        configs = gl.glXChooseFBConfig(self._dpy, 0, attribs, ctypes.byref(count))
        if not configs or count.value == 0:
            raise GlxError("no GLX framebuffer config with a pbuffer")
        self._config = configs[0]

        pb_attribs = (ctypes.c_int * 5)(GLX_PBUFFER_WIDTH, self.size,
                                        GLX_PBUFFER_HEIGHT, self.size, 0)
        gl.glXCreatePbuffer.restype = ctypes.c_void_p
        gl.glXCreatePbuffer.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                        ctypes.POINTER(ctypes.c_int)]
        self._pbuffer = gl.glXCreatePbuffer(self._dpy, self._config, pb_attribs)
        if not self._pbuffer:
            raise GlxError("cannot create GLX pbuffer")

        gl.glXCreateContextAttribsARB.restype = ctypes.c_void_p
        gl.glXCreateContextAttribsARB.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_int, ctypes.POINTER(ctypes.c_int),
        ]
        ctx_attribs = (ctypes.c_int * 7)(
            GLX_CONTEXT_MAJOR_VERSION_ARB, 3,
            GLX_CONTEXT_MINOR_VERSION_ARB, 3,
            GLX_CONTEXT_PROFILE_MASK_ARB, GLX_CONTEXT_CORE_PROFILE_BIT_ARB, 0,
        )
        self._ctx = gl.glXCreateContextAttribsARB(
            self._dpy, self._config, None, 1, ctx_attribs)
        if not self._ctx:
            raise GlxError("cannot create a GL 3.3 core context")

        gl.glXMakeContextCurrent.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        if not gl.glXMakeContextCurrent(self._dpy, self._pbuffer,
                                        self._pbuffer, self._ctx):
            raise GlxError("glXMakeContextCurrent failed")

        gl.glGetString.restype = ctypes.c_char_p
        self.vendor = (gl.glGetString(GL_VENDOR) or b"").decode(errors="replace")
        self.renderer = (gl.glGetString(GL_RENDERER) or b"").decode(errors="replace")
        self.gl_version = (gl.glGetString(GL_VERSION) or b"").decode(errors="replace")

        self._program = self._build_program()
        self._t_loc = gl.glGetUniformLocation(self._program, b"t")
        self._iter_loc = gl.glGetUniformLocation(self._program, b"iterations")
        gl.glUseProgram(self._program)
        gl.glUniform1f.argtypes = [ctypes.c_int, ctypes.c_float]
        gl.glUniform1f(self._iter_loc, ctypes.c_float(self.iterations))
        self._setup_quad()
        gl.glViewport.argtypes = [ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int]
        gl.glViewport(0, 0, self.size, self.size)
        gl.glClearColor.argtypes = [ctypes.c_float] * 4
        gl.glDrawArrays.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_int]
        gl.glFinish.argtypes = []

    def _shader(self, kind: int, source: bytes) -> int:
        gl = self._gl
        shader = gl.glCreateShader(kind)
        buf = ctypes.c_char_p(source)
        gl.glShaderSource.argtypes = [ctypes.c_uint, ctypes.c_int,
                                      ctypes.POINTER(ctypes.c_char_p),
                                      ctypes.POINTER(ctypes.c_int)]
        gl.glShaderSource(shader, 1, ctypes.byref(buf), None)
        gl.glCompileShader.argtypes = [ctypes.c_uint]
        gl.glCompileShader(shader)
        ok = ctypes.c_int(0)
        gl.glGetShaderiv.argtypes = [ctypes.c_uint, ctypes.c_uint,
                                     ctypes.POINTER(ctypes.c_int)]
        gl.glGetShaderiv(shader, GL_COMPILE_STATUS, ctypes.byref(ok))
        if not ok.value:
            log = ctypes.create_string_buffer(4096)
            gl.glGetShaderInfoLog.argtypes = [ctypes.c_uint, ctypes.c_int,
                                              ctypes.POINTER(ctypes.c_int),
                                              ctypes.c_char_p]
            gl.glGetShaderInfoLog(shader, 4096, None, log)
            raise GlxError("shader compile failed: " + log.value.decode(errors="replace"))
        return shader

    def _build_program(self) -> int:
        gl = self._gl
        vert = self._shader(GL_VERTEX_SHADER, VERTEX_SRC)
        frag = self._shader(GL_FRAGMENT_SHADER, FRAGMENT_SRC)
        gl.glCreateProgram.restype = ctypes.c_uint
        program = gl.glCreateProgram()
        gl.glAttachShader.argtypes = [ctypes.c_uint, ctypes.c_uint]
        gl.glAttachShader(program, vert)
        gl.glAttachShader(program, frag)
        gl.glLinkProgram.argtypes = [ctypes.c_uint]
        gl.glLinkProgram(program)
        ok = ctypes.c_int(0)
        gl.glGetProgramiv.argtypes = [ctypes.c_uint, ctypes.c_uint,
                                      ctypes.POINTER(ctypes.c_int)]
        gl.glGetProgramiv(program, GL_LINK_STATUS, ctypes.byref(ok))
        if not ok.value:
            raise GlxError("shader link failed")
        return program

    def _setup_quad(self) -> None:
        gl = self._gl
        # One oversized triangle covers the viewport with no vertex buffer churn.
        verts = (ctypes.c_float * 6)(-1.0, -1.0, 3.0, -1.0, -1.0, 3.0)
        gl.glGenVertexArrays.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint)]
        vao = ctypes.c_uint(0)
        gl.glGenVertexArrays(1, ctypes.byref(vao))
        gl.glBindVertexArray.argtypes = [ctypes.c_uint]
        gl.glBindVertexArray(vao.value)
        gl.glGenBuffers.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint)]
        vbo = ctypes.c_uint(0)
        gl.glGenBuffers(1, ctypes.byref(vbo))
        gl.glBindBuffer.argtypes = [ctypes.c_uint, ctypes.c_uint]
        gl.glBindBuffer(GL_ARRAY_BUFFER, vbo.value)
        gl.glBufferData.argtypes = [ctypes.c_uint, ctypes.c_ssize_t,
                                    ctypes.c_void_p, ctypes.c_uint]
        gl.glBufferData(GL_ARRAY_BUFFER, ctypes.sizeof(verts), verts, GL_STATIC_DRAW)
        gl.glEnableVertexAttribArray.argtypes = [ctypes.c_uint]
        gl.glEnableVertexAttribArray(0)
        gl.glVertexAttribPointer.argtypes = [
            ctypes.c_uint, ctypes.c_int, ctypes.c_uint,
            ctypes.c_ubyte, ctypes.c_int, ctypes.c_void_p]
        gl.glVertexAttribPointer(0, 2, GL_FLOAT, 0, 0, None)
        self._vao, self._vbo = vao, vbo

    # -- work ----------------------------------------------------------------
    def draw(self, t: float) -> None:
        gl = self._gl
        gl.glClear(GL_COLOR_BUFFER_BIT)
        gl.glUniform1f(self._t_loc, ctypes.c_float(t))
        gl.glDrawArrays(GL_TRIANGLES, 0, 3)
        gl.glFinish()  # block until the GPU really finished this frame
        self._frames += 1

    @property
    def frames(self) -> int:
        return self._frames


class GpuLoader:
    """Runs a GL context on a worker thread and duty-cycles it to a target.

    `target` is the percentage of wall time the GPU should spend rendering.
    nvidia-smi reports utilization as a time average, so a 100 ms duty cycle
    lands within a few points of the requested value.
    """

    def __init__(self, size: int = 256, iterations: int = 300,
                 period: float = 0.02):
        self.size = size
        self.iterations = iterations
        self.period = period
        self._target = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._error: str | None = None
        self._renderer = ""
        self._fps = 0.0

    # -- public API ----------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def target(self) -> float:
        with self._lock:
            return self._target

    @target.setter
    def target(self, value: float) -> None:
        with self._lock:
            self._target = max(0.0, min(100.0, float(value)))

    @property
    def renderer(self) -> str:
        return self._renderer

    @property
    def fps(self) -> float:
        return self._fps

    @property
    def error(self) -> str | None:
        return self._error

    def start(self, target: float = 100.0) -> None:
        if self.running:
            self.target = target
            return
        self._stop.clear()
        self._error = None
        self.target = target
        self._thread = threading.Thread(target=self._run, name="gpu-loader",
                                        daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    # -- worker --------------------------------------------------------------
    def _run(self) -> None:
        try:
            os.environ.update(_offload_env())
            ctx = _GlContext(self.size, self.iterations)
        except Exception as exc:  # surfaced in the UI instead of crashing
            self._error = str(exc)
            return

        self._renderer = ctx.renderer
        start = time.perf_counter()
        frames_at_start = 0
        carry = 0.0  # unused budget from the previous cycle (negative = overshoot)
        try:
            while not self._stop.is_set():
                target = self.target
                if target <= 0:
                    carry = 0.0
                    time.sleep(min(self.period, 0.05))
                    continue

                cycle_start = time.perf_counter()
                # One frame can cost 20 ms on a small GPU, so a plain "render for
                # budget ms" loop overshoots badly at low targets. Carrying the
                # error into the next cycle converges on the requested average.
                budget = max(0.0, self.period * (target / 100.0) + carry)
                while not self._stop.is_set():
                    ctx.draw(cycle_start)
                    if time.perf_counter() - cycle_start >= budget:
                        break
                busy = time.perf_counter() - cycle_start
                carry = max(-self.period, min(self.period, budget - busy))
                idle = self.period - busy
                if idle > 0:
                    time.sleep(idle)

                elapsed = time.perf_counter() - start
                if elapsed >= 1.0:
                    self._fps = (ctx.frames - frames_at_start) / elapsed
                    start, frames_at_start = time.perf_counter(), ctx.frames
        finally:
            self._fps = 0.0


def main(argv: list[str] | None = None) -> int:
    """Small self-check: python -m gpu_bar_verifier.glxload --target 60"""
    import argparse
    from . import nvidia

    ap = argparse.ArgumentParser(description="Run a controllable GPU load")
    ap.add_argument("--target", type=float, default=100.0,
                    help="percent of wall time to render (0-100)")
    ap.add_argument("--duration", type=float, default=10.0)
    ap.add_argument("--size", type=int, default=256)
    args = ap.parse_args(argv)

    loader = GpuLoader(size=args.size)
    loader.start(args.target)
    deadline = time.time() + args.duration
    print(f"target {args.target:.0f}%  (renderer: {loader.renderer or 'starting...'})")
    try:
        while time.time() < deadline:
            time.sleep(1.0)
            stats = nvidia.query()
            if stats is None:
                print(f"  fps={loader.fps:7.1f}  nvidia-smi unavailable")
            else:
                print(f"  fps={loader.fps:7.1f}  util={stats.utilization:3.0f}%  "
                      f"clock={stats.clock_mhz}MHz  temp={stats.temperature_c}C")
    finally:
        loader.stop()
    if loader.error:
        print("error:", loader.error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
