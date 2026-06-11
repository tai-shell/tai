"""Python client for the SikuliX Jython bridge.

Spawns `java -jar sikulixapi.jar -r bridge/bridge.py` as a subprocess
(stdio transport) and exchanges line-delimited JSON-RPC over its
stdin/stdout. Methods on `BridgeClient` map to the bridge's handlers:
`activate`, `click`, `key`, `type_text`, `screenshot`, `find_image`.

A second transport — connecting to a remote bridge over TCP — is
designed in but not yet implemented; it'll land alongside the cross-
host work in Phase 3. The `BridgeClient` interface is independent of
transport so the caller doesn't have to know which is in use.

Resource resolution (where to find the SikuliX jar and `bridge.py`):

    1. Explicit kwargs (`jar_path=`, `script_path=`)
    2. Env vars `HOLO_SIKULI_JAR`, `HOLO_BRIDGE_SCRIPT`
    3. PyInstaller's `sys._MEIPASS` (release builds bundle both)
    4. Repo-root fallback: `<repo>/vendor/sikulix*.jar` and
       `<repo>/bridge/bridge.py` (development)
    5. User cache dir: `~/Library/Caches/holo` (macOS) or
       `~/.cache/holo` (Linux). `holo install-bridge` populates this
       from the pinned GitHub Release; `BridgeClient` will also
       auto-download on first start unless `HOLO_BRIDGE_NO_DOWNLOAD=1`.

If the jar can't be found and auto-download is disabled, `start()`
raises `BridgeMissingError` so callers can surface a clean
diagnostic instead of an opaque `FileNotFoundError`.
"""

from __future__ import annotations

import hashlib
import json
import os
import ssl
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# --- Pinned SikuliX IDE release (used by `holo ide`) ---------------------
# Mirror of the upstream SikuliX 2.0.5 IDE jar. The full IDE distribution
# bundles Jython 2.7 and a GUI; `holo ide` invokes it as
# `java -jar sikulixide.jar` to give the user the SikuliX IDE window.
# NOT used by BridgeClient anymore (see SIKULI_API_JAR_* below) — the
# IDE main triggers HotkeyManager init which hangs on macOS 15+ Carbon.
SIKULI_VERSION: str = "2.0.5"
SIKULI_JAR_NAME: str = "sikulixide-2.0.5.jar"
SIKULI_JAR_URL: str = (
    "https://github.com/nospaceleftondevice/holo/releases/download/"
    "vendored-sikulix-2.0.5/sikulixide-2.0.5.jar"
)
SIKULI_JAR_SHA256: str = (
    "f4b0b50c8e413094e78cd1d8fed02ae65f62f8c53ed00da0562fdedf4acff729"
)
SIKULI_JAR_BYTES: int = 128_949_200

# --- Pinned SikuliX API release (used by BridgeClient) -------------------
# The macOS-specific API-only jar from the oculix-org fork — same SikuliX
# 2.0.5 code as upstream, repackaged per-platform. NO IDE classes, NO
# HotkeyManager, NO Carbon dependency. Driven directly by standalone
# Jython (see JYTHON_JAR_* below). This is what `BridgeClient.start()`
# spawns.
#
# Path A in the v0.1.0a33 macOS-15 debugging session: oculix-org's IDE
# repack hung the same way. Path B — Jython-direct against the API jar
# — proved out cleanly (ping roundtrip <1 s, zero stderr, zero
# HotkeyManager init). See the `fix/sikuli-api-bridge` PR for the
# debugging story. URLs point at upstream releases for now; mirror to
# holo's own release surface as a follow-up.
# TODO: linux/windows variants — sikulixapi-2.0.5-linux.jar and
# sikulixapi-2.0.5-windows.jar exist at the same release.
SIKULI_API_JAR_NAME: str = "sikulixapi-2.0.5-macos.jar"
SIKULI_API_JAR_URL: str = (
    "https://github.com/oculix-org/SikuliX1/releases/download/"
    "v2.0.5/sikulixapi-2.0.5-macos.jar"
)
SIKULI_API_JAR_SHA256: str = (
    "6a486167696280b4601bd7cf1ec7c4669da696403c05cc9bda7f478507aac769"
)
SIKULI_API_JAR_BYTES: int = 83_177_290

# --- Pinned standalone Jython (used by BridgeClient) ---------------------
# Jython 2.7.4 standalone jar from Maven Central. Self-contained Python
# 2.7 interpreter that runs on the JVM. Pairs with sikulixapi.jar on the
# classpath; together they replicate what `sikulixide.jar -r script.py`
# does, minus the IDE main and HotkeyManager.
JYTHON_VERSION: str = "2.7.4"
JYTHON_JAR_NAME: str = "jython-standalone-2.7.4.jar"
JYTHON_JAR_URL: str = (
    "https://repo1.maven.org/maven2/org/python/jython-standalone/"
    "2.7.4/jython-standalone-2.7.4.jar"
)
JYTHON_JAR_SHA256: str = (
    "1fba1769effcc8b19f5e10436bc8274a158ce988559f257927c24c73bb137f3c"
)
JYTHON_JAR_BYTES: int = 50_453_449


class BridgeError(RuntimeError):
    """Raised when the bridge returns a JSON-RPC error envelope."""

    def __init__(self, code: int, message: str, trace: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.trace = trace


class BridgeMissingError(RuntimeError):
    """Raised when sikulixapi.jar / bridge.py cannot be located."""


@dataclass
class BridgeClient:
    """Synchronous client for the Jython bridge over stdio.

    One process per `BridgeClient`. Requests are serialised by an
    internal lock — concurrent callers wait their turn. JVM startup
    is slow (~2–5 s) so callers should keep the same client around
    for the life of the daemon.
    """

    # Per-instance overrides. `api_jar_path` + `jython_jar_path` are
    # the bridge's actual jars (used together on the classpath). The
    # legacy `jar_path` kwarg is accepted for back-compat but ONLY as
    # a fallback for `api_jar_path` if the latter isn't set; callers
    # that explicitly want the API jar should use the new field.
    api_jar_path: Path | None = None
    jython_jar_path: Path | None = None
    jar_path: Path | None = None  # legacy alias for api_jar_path
    script_path: Path | None = None
    java_path: str = "java"
    extra_jvm_args: tuple[str, ...] = ()
    default_timeout: float = 10.0

    _proc: subprocess.Popen[bytes] | None = field(default=None, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def start(self) -> None:
        """Spawn the JVM subprocess and run a `ping` to confirm liveness.

        Invocation: standalone Jython on the classpath with sikulixapi —
        NOT the IDE jar's `-r script.py` flag. The IDE main triggers
        HotkeyManager init which hangs on macOS 15+ (jkeymaster's
        Carbon hotkey provider blocks forever during cleanup).
        Direct-Jython skips that whole chain.

            java -cp api.jar:jython.jar org.python.util.jython bridge.py

        bridge.py defaults its transport to stdio, so no extra args.
        """
        if self._proc is not None:
            return
        api_jar = self._resolve_api_jar()
        jython_jar = self._resolve_jython_jar()
        script = self._resolve_script()
        # `:` is the JVM's classpath separator on POSIX. Windows would
        # use `;`; not relevant until we ship a non-macOS API jar
        # (see TODO on SIKULI_API_JAR_NAME).
        classpath = f"{api_jar}{os.pathsep}{jython_jar}"
        cmd = [
            self.java_path,
            *self.extra_jvm_args,
            "-cp",
            classpath,
            "org.python.util.jython",
            str(script),
        ]
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        # Sanity check — first request after spawn must succeed, otherwise
        # the JVM is wedged and we want to know now rather than at first use.
        self.request("ping", timeout=max(self.default_timeout, 30.0))

    def stop(self) -> None:
        if self._proc is None:
            return
        try:
            if self._proc.stdin is not None:
                self._proc.stdin.close()
            self._proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait()
        finally:
            self._proc = None

    # ---- request/response ------------------------------------------------

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Send `method`/`params`, block for the response, return `result`."""
        if self._proc is None:
            self.start()
        assert self._proc is not None and self._proc.stdin is not None
        assert self._proc.stdout is not None

        rid = uuid.uuid4().hex
        envelope = {"id": rid, "method": method, "params": params or {}}
        line = (json.dumps(envelope) + "\n").encode("utf-8")

        with self._lock:
            try:
                self._proc.stdin.write(line)
                self._proc.stdin.flush()
            except (BrokenPipeError, OSError) as e:
                raise BridgeError(-32000, "bridge stdin closed: " + str(e)) from e

            # Skip stdout chatter from SikuliX/JVM that bypassed the
            # bridge's silencer (action logs, JVM warnings, etc.). Look
            # for the first line that parses as a JSON object — that's
            # our response. Bound the skip so a dead bridge fails fast.
            response: dict[str, Any] | None = None
            skipped: list[str] = []
            for _ in range(32):
                raw = self._proc.stdout.readline()
                if not raw:
                    # Popen with `bufsize=0` gives raw FileIO streams, which
                    # implement `read()` but not `read1()`. Use plain `read`
                    # so the diagnostic itself doesn't crash with
                    # AttributeError and bury the real cause of the
                    # stdout-closed condition.
                    stderr_tail = b""
                    if self._proc.stderr is not None:
                        try:
                            stderr_tail = self._proc.stderr.read(4096) or b""
                        except (ValueError, OSError):
                            pass
                    raise BridgeError(
                        -32001,
                        "bridge stdout closed; stderr tail: "
                        + stderr_tail.decode("utf-8", errors="replace"),
                    )
                try:
                    decoded = raw.decode("utf-8")
                except UnicodeDecodeError:
                    skipped.append(repr(raw))
                    continue
                stripped = decoded.lstrip()
                if not stripped.startswith("{"):
                    skipped.append(decoded.rstrip("\n"))
                    continue
                try:
                    response = json.loads(decoded)
                except json.JSONDecodeError:
                    skipped.append(decoded.rstrip("\n"))
                    continue
                break
            if response is None:
                raise BridgeError(
                    -32002,
                    "no JSON response after 32 lines; skipped: "
                    + " | ".join(skipped[-5:]),
                )

        if response.get("id") != rid:
            raise BridgeError(
                -32003,
                "id mismatch: expected " + rid + ", got " + str(response.get("id")),
            )

        if "error" in response:
            err = response["error"]
            raise BridgeError(
                err.get("code", -32603),
                err.get("message", "unknown error"),
                err.get("trace"),
            )
        return response.get("result", {})

    # ---- convenience verbs ----------------------------------------------

    def ping(self) -> dict[str, Any]:
        return self.request("ping")

    def activate(self, name: str) -> dict[str, Any]:
        return self.request("app.activate", {"name": name})

    def click(
        self,
        x: float,
        y: float,
        *,
        modifiers: list[str] | None = None,
    ) -> dict[str, Any]:
        return self.request(
            "screen.click",
            {"x": int(x), "y": int(y), "modifiers": modifiers or []},
        )

    def key(self, combo: str) -> dict[str, Any]:
        return self.request("screen.key", {"combo": combo})

    def type_text(self, text: str) -> dict[str, Any]:
        # Avoid clashing with the `type` builtin in callers' namespaces.
        return self.request("screen.type", {"text": text})

    def mouse_move(self, x: float, y: float) -> dict[str, Any]:
        """Move the cursor to (x, y) without clicking or scrolling.

        Useful for hover-only UI (tooltips, dropdown menus that open
        on mouseenter, reveal-on-hover toolbars) where you want the
        cursor positioned before deciding whether to click. Returns
        ``{"moved": True, "x", "y"}``.
        """
        return self.request(
            "screen.move",
            {"x": int(x), "y": int(y)},
        )

    def scroll(
        self,
        x: float,
        y: float,
        *,
        direction: str = "down",
        steps: int = 3,
    ) -> dict[str, Any]:
        """Move to (x, y) and emit `steps` mouse-wheel events.

        `direction` is "up" or "down". Each step is one wheel "click"
        — exact pixel delta is OS / app-defined. Scroll-by-wheel
        works in apps that don't accept keyboard scroll (panel that
        doesn't have keyboard focus).
        """
        return self.request(
            "screen.scroll",
            {
                "x": int(x),
                "y": int(y),
                "direction": direction,
                "steps": int(steps),
            },
        )

    def screenshot(
        self,
        *,
        region: dict[str, int] | None = None,
        timeout: float = 15.0,
    ) -> bytes:
        """Capture the screen (or a region) and return raw PNG bytes."""
        import base64 as _b64

        params: dict[str, Any] = {}
        if region is not None:
            params["region"] = region
        result = self.request("screen.shot", params, timeout=timeout)
        return _b64.b64decode(result["image"])

    def find_image(
        self,
        needle: bytes,
        *,
        region: dict[str, int] | None = None,
        score: float = 0.7,
        timeout: float = 15.0,
    ) -> dict[str, Any] | None:
        """Find `needle` (PNG bytes) on screen. Returns coords/score or None."""
        import base64 as _b64

        params: dict[str, Any] = {
            "needle": _b64.b64encode(needle).decode("ascii"),
            "score": score,
        }
        if region is not None:
            params["region"] = region
        return self.request("screen.find_image", params, timeout=timeout)

    def find_image_path(
        self,
        path: str,
        *,
        region: dict[str, int] | None = None,
        score: float = 0.7,
        timeout: float = 15.0,
    ) -> dict[str, Any] | None:
        """Same as `find_image` but the needle is a JVM-side filesystem path.

        Used by the template cache so we don't re-encode the same small
        PNGs on every find. The path must be readable by the JVM
        process — the daemon and the bridge share a filesystem.
        """
        params: dict[str, Any] = {"path": str(path), "score": score}
        if region is not None:
            params["region"] = region
        return self.request("screen.find_image_path", params, timeout=timeout)

    def user_capture(
        self,
        *,
        prompt: str = "",
        timeout: float = 60.0,
    ) -> dict[str, Any]:
        """Block until the user drag-selects a screen rect (or cancels).

        Returns either:
          - {"image": "<base64 PNG>", "x", "y", "width", "height"} on success
          - {"cancelled": True, "reason": str} on Esc/timeout

        The transport timeout is set generously to cover the user's
        thinking time; the bridge itself uses Sikuli's blocking
        `userCapture` and returns whenever the user finishes.
        """
        params: dict[str, Any] = {"timeout": timeout}
        if prompt:
            params["prompt"] = prompt
        # Add slack on top of the user-facing timeout for SikuliX overhead.
        return self.request(
            "screen.user_capture", params, timeout=timeout + 5.0
        )

    # ---- resource resolution --------------------------------------------

    def _resolve_jar(self) -> Path:
        if self.jar_path is not None:
            return _require(Path(self.jar_path), "SikuliX jar")
        env = os.environ.get("HOLO_SIKULI_JAR")
        if env:
            return _require(Path(env), "SikuliX jar (HOLO_SIKULI_JAR)")
        for candidate in _candidate_jar_paths():
            if candidate.exists():
                return candidate
        if os.environ.get("HOLO_BRIDGE_NO_DOWNLOAD") == "1":
            raise BridgeMissingError(
                "SikuliX jar not found and HOLO_BRIDGE_NO_DOWNLOAD=1. "
                "Drop sikulixide-*.jar in vendor/ or set HOLO_SIKULI_JAR."
            )
        # Last resort: download from the pinned release into the cache.
        return ensure_jar()

    def _resolve_api_jar(self) -> Path:
        """Locate the SikuliX *API* jar — the one BridgeClient actually
        spawns. Search order mirrors `_resolve_jar`:

            1. Explicit `api_jar_path` kwarg
            2. Legacy `jar_path` kwarg (back-compat with pre-fix callers
               that didn't know about the api/ide split)
            3. `HOLO_SIKULI_API_JAR` env var, else legacy `HOLO_SIKULI_JAR`
            4. PyInstaller / repo-root / user-cache filesystem search for
               sikulixapi*.jar
            5. Download from the pinned URL into the user cache
        """
        if self.api_jar_path is not None:
            return _require(Path(self.api_jar_path), "sikulixapi jar")
        if self.jar_path is not None:
            return _require(Path(self.jar_path), "sikulixapi jar (legacy `jar_path` kwarg)")
        env = (
            os.environ.get("HOLO_SIKULI_API_JAR")
            or os.environ.get("HOLO_SIKULI_JAR")
        )
        if env:
            return _require(Path(env), "sikulixapi jar (env)")
        for candidate in _candidate_api_jar_paths():
            if candidate.exists():
                return candidate
        if os.environ.get("HOLO_BRIDGE_NO_DOWNLOAD") == "1":
            raise BridgeMissingError(
                "sikulixapi jar not found and HOLO_BRIDGE_NO_DOWNLOAD=1. "
                "Drop sikulixapi-*.jar in vendor/ or set HOLO_SIKULI_API_JAR."
            )
        return ensure_api_jar()

    def _resolve_jython_jar(self) -> Path:
        """Locate the standalone Jython jar (paired with sikulixapi on
        the classpath). Same search order shape as `_resolve_api_jar`."""
        if self.jython_jar_path is not None:
            return _require(Path(self.jython_jar_path), "jython-standalone jar")
        env = os.environ.get("HOLO_JYTHON_JAR")
        if env:
            return _require(Path(env), "jython-standalone jar (HOLO_JYTHON_JAR)")
        for candidate in _candidate_jython_jar_paths():
            if candidate.exists():
                return candidate
        if os.environ.get("HOLO_BRIDGE_NO_DOWNLOAD") == "1":
            raise BridgeMissingError(
                "jython-standalone jar not found and HOLO_BRIDGE_NO_DOWNLOAD=1. "
                "Drop jython-standalone-*.jar in vendor/ or set HOLO_JYTHON_JAR."
            )
        return ensure_jython_jar()

    def _resolve_script(self) -> Path:
        if self.script_path is not None:
            return _require(Path(self.script_path), "bridge.py")
        env = os.environ.get("HOLO_BRIDGE_SCRIPT")
        if env:
            return _require(Path(env), "bridge.py (HOLO_BRIDGE_SCRIPT)")
        for candidate in _candidate_script_paths():
            if candidate.exists():
                return candidate
        raise BridgeMissingError("bridge.py not found among PyInstaller / repo paths")


def _require(path: Path, label: str) -> Path:
    if not path.exists():
        raise BridgeMissingError(label + " not found at " + str(path))
    return path


def _bundle_root() -> Path | None:
    """PyInstaller's `_MEIPASS` if the daemon is running from a frozen build."""
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) if base else None


def _repo_root() -> Path:
    # Walk up from this file looking for the repo's pyproject.toml.
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").exists():
            return parent
    return here.parent


def _candidate_jar_paths() -> list[Path]:
    """Search order for any SikuliX jar (legacy resolver used by `holo
    ide` etc., which doesn't care which jar shape it gets).

    Both `sikulixapi-*.jar` (headless API) and `sikulixide-*.jar` (full
    IDE) are accepted here. Prefer the slimmer api jar when both are
    present.
    """
    out: list[Path] = []
    for base in _jar_search_dirs():
        for pattern in ("sikulixapi*.jar", "sikulixide*.jar"):
            out.extend(sorted(base.glob(pattern)))
    return out


def _candidate_api_jar_paths() -> list[Path]:
    """Search order specifically for the SikuliX API jar that
    `BridgeClient.start()` spawns. Filters to `sikulixapi*.jar` so the
    bridge never tries to run the IDE jar (which triggers the macOS-15
    Carbon hang). If only the IDE jar is on disk, the user gets a
    clean BridgeMissingError + download fallback instead of a
    confusing runtime hang.
    """
    out: list[Path] = []
    for base in _jar_search_dirs():
        out.extend(sorted(base.glob("sikulixapi*.jar")))
    return out


def _candidate_jython_jar_paths() -> list[Path]:
    out: list[Path] = []
    for base in _jar_search_dirs():
        out.extend(sorted(base.glob("jython-standalone-*.jar")))
    return out


def _jar_search_dirs() -> list[Path]:
    """Where to look for the SikuliX jar, in priority order.

    1. The PyInstaller bundle root (release builds bundle the jar in).
    2. `<repo>/vendor/` (development).
    3. `~/.cache/holo/` (downloaded-on-demand from a GitHub Release;
       see `holo install-bridge`).
    """
    out: list[Path] = []
    bundle = _bundle_root()
    if bundle is not None:
        out.append(bundle)
        out.append(bundle / "vendor")
    out.append(_repo_root() / "vendor")
    out.append(_user_cache_dir())
    return [d for d in out if d.exists()]


def _user_cache_dir() -> Path:
    """Best-effort XDG-style cache path for downloaded jars.

    Matches `~/.cache/holo` on Linux / unset-XDG macOS; respects
    `XDG_CACHE_HOME` if set; falls back to `~/Library/Caches/holo`
    on macOS when `XDG_CACHE_HOME` isn't set and we're on a path
    where the Apple convention is more idiomatic. We keep this
    simple — the install command writes here, and the resolver
    reads here.
    """
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        return Path(xdg) / "holo"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "holo"
    return Path.home() / ".cache" / "holo"


def _candidate_script_paths() -> list[Path]:
    out: list[Path] = []
    bundle = _bundle_root()
    if bundle is not None:
        out.append(bundle / "bridge.py")
        out.append(bundle / "bridge" / "bridge.py")
    repo = _repo_root()
    out.append(repo / "bridge" / "bridge.py")
    return out


# ---- jar download -----------------------------------------------------


def ensure_jar(
    *,
    cache_dir: Path | None = None,
    on_progress: Any = None,
) -> Path:
    """Return the cached SikuliX *IDE* jar path, downloading if missing.

    Used by `holo ide` (which launches the SikuliX IDE GUI).
    BridgeClient uses `ensure_api_jar` + `ensure_jython_jar` instead.
    """
    return _ensure_artifact(
        name=SIKULI_JAR_NAME,
        url=SIKULI_JAR_URL,
        sha256=SIKULI_JAR_SHA256,
        cache_dir=cache_dir,
        on_progress=on_progress,
    )


def ensure_api_jar(
    *,
    cache_dir: Path | None = None,
    on_progress: Any = None,
) -> Path:
    """Return the cached SikuliX *API* jar path, downloading if missing.

    Paired with `ensure_jython_jar` — together they're what
    BridgeClient spawns. NOT a substitute for `ensure_jar` (the IDE
    jar): the API jar has no main class.
    """
    return _ensure_artifact(
        name=SIKULI_API_JAR_NAME,
        url=SIKULI_API_JAR_URL,
        sha256=SIKULI_API_JAR_SHA256,
        cache_dir=cache_dir,
        on_progress=on_progress,
    )


def ensure_jython_jar(
    *,
    cache_dir: Path | None = None,
    on_progress: Any = None,
) -> Path:
    """Return the cached standalone Jython jar path, downloading if missing."""
    return _ensure_artifact(
        name=JYTHON_JAR_NAME,
        url=JYTHON_JAR_URL,
        sha256=JYTHON_JAR_SHA256,
        cache_dir=cache_dir,
        on_progress=on_progress,
    )


def _ensure_artifact(
    *,
    name: str,
    url: str,
    sha256: str,
    cache_dir: Path | None,
    on_progress: Any,
) -> Path:
    """Generic cached-download-with-digest-verify. Idempotent."""
    cache = cache_dir if cache_dir is not None else _user_cache_dir()
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / name

    if target.exists():
        if _sha256(target) == sha256:
            return target
        target.unlink(missing_ok=True)

    tmp = target.with_suffix(target.suffix + ".part")
    try:
        _download(url, tmp, on_progress=on_progress)
        digest = _sha256(tmp)
        if digest != sha256:
            raise BridgeMissingError(
                f"SHA-256 mismatch for {url}: "
                f"expected {sha256}, got {digest}"
            )
        tmp.replace(target)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
    return target


def _ssl_context() -> ssl.SSLContext:
    """SSL context with a CA bundle that exists on the user's machine.

    PyInstaller bundles the Python interpreter built on the GHA runner.
    That interpreter's OpenSSL has the runner's CA paths compiled in
    (e.g. `/etc/ssl/cert.pem`), which don't exist on a fresh user
    machine — `urllib.request.urlopen` then fails with
    `CERTIFICATE_VERIFY_FAILED`. Use `certifi`'s bundled CA store
    explicitly to sidestep that.
    """
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        # Dev install without certifi — fall back to system default.
        return ssl.create_default_context()


def _download(url: str, dest: Path, *, on_progress: Any = None) -> None:
    try:
        ctx = _ssl_context()
        with urllib.request.urlopen(url, context=ctx) as response:  # noqa: S310 (pinned URL)
            # Falls back to 0 when the server omits Content-Length;
            # on_progress callers all handle a zero total gracefully.
            total = int(response.headers.get("Content-Length") or 0)
            with open(dest, "wb") as out:
                read = 0
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
                    read += len(chunk)
                    if on_progress is not None:
                        on_progress(read, total)
    except urllib.error.URLError as e:
        raise BridgeMissingError(f"download from {url} failed: {e}") from e


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()
