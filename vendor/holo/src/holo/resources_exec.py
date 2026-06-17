"""Per-resource shell exec primitive for Phase 2 of the resources feature.

This module is the load-bearing piece of `docs/resources.md` §Q2/§Q3 —
everything that makes the cross-host fan-out feature **safe** lives
here. It does three things, in this order, every call:

  1. **Static parse the body** (``validate_body``). Walks every command
     head out of the shell body, rejects any that isn't in the
     resource's ``caps=exec:NAME`` allowlist, and rejects any token that
     looks like an absolute path or a ``..``-bearing path. This is the
     loud-error layer — bodies that obviously violate intent get
     rejected with a specific message before anything spawns.

  2. **Build a PATH-pinning symlink farm** (``make_path_dir``). Per
     call, creates a fresh tmpdir containing symlinks ONLY for the
     allowed binaries. The body runs with ``PATH`` pinned to that
     tmpdir. Catches runtime composition (``$(echo ffprobe) "$f"``)
     that static parse misses — even if the body smuggles a command
     name through shell expansion, the dyld lookup fails for anything
     not on the symlink farm.

  3. **Spawn the body** (``exec_in_resource``). ``/bin/sh -c body``
     under ``cwd=resource.path``, with a stripped env that injects
     ``HOLO_HOST`` / ``HOLO_RESOURCE`` / ``HOLO_RESOURCE_PATH``. Output
     is line-buffered and streamed back as ``{fd, data}`` frames; a
     final ``{exit, duration_ms}`` frame closes the stream. Timeout =
     ``SIGKILL`` the process group.

Things that DON'T live here yet (deferred to Phase 2.B and v2):

- ACL via ``~/.holo/resources.yml`` (Phase 2.B). For now the cert chain
  on the MCP channel is the only access gate; multi-principal use needs
  the ACL.
- L2 path scoping (``uid:NAME`` cap): drop to a dedicated UID. Design
  hook only; not enforced here.
- L3 path scoping (``sandbox`` cap): sandbox-exec / bubblewrap.
  Deferred to v2.

The static parser is hand-rolled on top of stdlib ``shlex`` rather than
``bashlex`` — PATH pinning is the strong layer, so a sloppy static parse
(missing some shell edge cases) only loses defense-in-depth, not the
security floor. Avoiding the dep keeps the binary smaller for the
common-case desktop install.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from holo.announce import Resource

_log = logging.getLogger(__name__)


# Shell keywords / control structures that look like command tokens but
# aren't external commands the allowlist should gate. Kept deliberately
# conservative — false positives (treating a real command as a keyword)
# would bypass the allowlist, but PATH pinning catches that. False
# negatives (treating a keyword as a command) cause loud rejection the
# user can debug from the error message.
_SHELL_KEYWORDS: frozenset[str] = frozenset({
    # Control-flow keywords
    "for", "while", "until", "if", "then", "else", "elif", "fi",
    "do", "done", "case", "esac", "in", "function", "time", "select",
    "{", "}", "!", "[", "[[", "]]", "((", "))",
    # POSIX builtins that are always available regardless of PATH —
    # whitelist them so users don't have to ``caps=exec:cd``. None of
    # these can read or write files outside the resource without help
    # from a binary that the allowlist would gate separately.
    "cd", "echo", "exit", "export", "local", "read", "return",
    "set", "unset", "shift", "true", "false", ":", "alias", "unalias",
    "umask", "wait", "trap", "pwd", "type", "hash",
    # `test` and `[`-as-test are intentionally NOT here — they read
    # arbitrary paths and would defeat L1 scope-pin if allowed. Users
    # can declare ``caps=exec:test`` if they really need it; default-deny.
})


# Match a token that's an absolute path (starts with ``/``) or contains
# ``..`` as a path segment. Both forms break the L1 scope-pin contract:
# cwd-pinned execution loses meaning if args reach paths outside the
# announced resource.
_ABS_OR_TRAVERSAL_RE = re.compile(
    r"""
    ^/                              # absolute path
    | (?:^|/)\.\.(?:/|$)            # /.. or ../ or .. as a segment
    """,
    re.VERBOSE,
)


# A token is a variable assignment when it matches ``NAME=...`` at the
# start. Bash allows assignments to *precede* a command head, e.g.
# ``FOO=bar BAZ=qux ffmpeg ...`` — those tokens are env injections, not
# the command itself.
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


class BodyRejected(Exception):
    """The static parse rejected the body.

    The message names the specific token that triggered the rejection
    and the rule it violated, so the user can correct it.
    """


@dataclass(frozen=True)
class ExecResult:
    """Terminal status returned by :func:`exec_in_resource`.

    The streaming output (stdout/stderr frames) is delivered through
    the callback; this object carries the summary the caller needs
    after the stream closes.
    """

    exit_code: int
    duration_ms: int
    timed_out: bool


# --------------------------------------------------------------------- validate


def _strip_heredocs(body: str) -> str:
    """Replace heredoc bodies with blank lines so the static parser sees
    only the shell structure, not the embedded data.

    A heredoc ``<<DELIM`` (or ``<<-DELIM`` or ``<<"DELIM"`` / ``<<'DELIM'``)
    extends until a line containing just ``DELIM``. The content between is
    DATA — Python source, SQL, config text, anything — not shell commands.
    The static parser should not interpret it.

    We don't try to be cute about preserving exact line numbers in error
    messages — we just replace each heredoc body with empty lines (so the
    line count after stripping equals the line count before).

    Unterminated heredocs leave the rest of the body unmodified; ``/bin/sh
    -c`` will report a syntax error at exec time, which is the right
    failure mode (we're not the shell, we shouldn't second-guess it).
    """
    # Match the heredoc opener: <<, optional dash, optional quote, name,
    # optional matching quote. The delimiter NAME captures group 2.
    opener_re = re.compile(
        r'<<-?\s*(["\']?)(\w+)\1',
        re.MULTILINE,
    )
    out_chunks: list[str] = []
    pos = 0
    while True:
        m = opener_re.search(body, pos)
        if not m:
            out_chunks.append(body[pos:])
            return "".join(out_chunks)
        # Keep everything up through the heredoc-opener.
        out_chunks.append(body[pos:m.end()])
        delim = m.group(2)
        # Heredoc body starts on the line AFTER the opener line. Find the
        # newline that ends the opener line.
        after_opener = body.find("\n", m.end())
        if after_opener == -1:
            # Opener is the final line; no heredoc body to skip.
            return "".join(out_chunks)
        # Keep up through that newline so the line count is preserved.
        out_chunks.append(body[m.end():after_opener + 1])
        # Find the closing delimiter line.
        close_re = re.compile(
            rf"^\s*{re.escape(delim)}\s*$",
            re.MULTILINE,
        )
        close_m = close_re.search(body, after_opener + 1)
        if not close_m:
            # Unterminated heredoc — let /bin/sh catch this at exec time.
            return "".join(out_chunks)
        # Replace the heredoc body with the same number of newlines.
        between = body[after_opener + 1:close_m.start()]
        out_chunks.append("\n" * between.count("\n"))
        # Keep the closing delimiter line itself.
        out_chunks.append(body[close_m.start():close_m.end() + 1])
        pos = close_m.end() + 1


def _tokenize_for_validation(body: str) -> Iterator[list[str]]:
    """Yield per-command token lists from a shell body.

    Each yielded list is the tokens of one "simple command" — bounded
    by ``;``, ``&``, ``|``, ``||``, ``&&``, ``\\n``, or a subshell
    boundary ``(`` / ``)``. shlex is configured in POSIX mode with
    punctuation_chars so it returns operator tokens as their own
    pieces (rather than splitting on whitespace and losing them).

    Tokens inside quotes survive intact (``echo "a;b"`` is one command
    ``echo "a;b"``, not two). Comment lines (``#``) are stripped by
    shlex. Heredoc bodies are stripped by ``_strip_heredocs`` before we
    get here.

    Variable assignments at the head of a command are passed through
    in the token list — the caller skips them when looking for the
    command head.
    """
    cleaned = _strip_heredocs(body)
    if not cleaned.strip():
        return
    lex = shlex.shlex(cleaned, posix=True, punctuation_chars="|&;()<>")
    lex.whitespace_split = True
    lex.commenters = "#"
    current: list[str] = []
    for tok in lex:
        if tok in (";", "&", "|", "||", "&&", "(", ")", "\n"):
            if current:
                yield current
                current = []
            continue
        # `<` and `>` are redirection operators; the next token is a
        # path. We treat them as separators-of-a-sort: the command head
        # is everything before them, the redirection target is one
        # more token we still need to abs-path-check. Easiest: keep
        # everything in `current` so the abs-path scan covers the
        # target, and skip the operator itself.
        if tok in ("<", ">", "<<", ">>", "<<<", "<&", ">&"):
            current.append(tok)
            continue
        current.append(tok)
    if current:
        yield current


def validate_body(body: str, *, allowed_bins: frozenset[str] | set[str]) -> None:
    """Reject the body if it violates the allowlist or scope rules.

    Two checks per command in the body:

      1. **Command head allowlist.** The first non-assignment,
         non-keyword token of each command must be in ``allowed_bins``
         (or be a known shell keyword / builtin). ``allowed_bins`` is
         the bare names extracted from ``caps=exec:NAME`` — e.g.
         ``caps=exec:ffprobe,exec:python3`` → ``{"ffprobe", "python3"}``.

      2. **Path scope (L1).** No token may match the
         absolute-or-traversal pattern. So ``cat /etc/passwd`` is
         rejected (absolute path), ``ls ../parent`` is rejected
         (traversal), ``find . -name '*.mp4'`` is accepted (relative).

    Raises :class:`BodyRejected` with a specific message on failure —
    naming the rule violated and the offending token — so the user can
    fix and retry.
    """
    if not body or not body.strip():
        # An empty body is a no-op exec; the allowlist has nothing to
        # gate. Allow it.
        return

    allowed = frozenset(allowed_bins)
    for tokens in _tokenize_for_validation(body):
        # Skip leading variable assignments to find the real command head.
        head_idx = 0
        while head_idx < len(tokens) and _ASSIGNMENT_RE.match(tokens[head_idx]):
            head_idx += 1
        if head_idx >= len(tokens):
            # All-assignment "command" (e.g. `FOO=bar` on its own) — no
            # external invocation, nothing to gate.
            continue

        head = tokens[head_idx]
        # Allow shell keywords / POSIX builtins unconditionally — they
        # don't reach the allowlist.
        if head not in _SHELL_KEYWORDS and head not in allowed:
            raise BodyRejected(
                f"command {head!r} is not in this resource's caps "
                f"allowlist (allowed: {sorted(allowed) or '(none)'})"
            )

        # L1 path scope — scan EVERY token for absolute paths or
        # traversal. Includes the command head itself (no `caps=exec:`
        # value contains a slash, so a slashed head would also fail
        # the allowlist; checking abs-path here gives a clearer error).
        for tok in tokens:
            if _ABS_OR_TRAVERSAL_RE.search(tok):
                raise BodyRejected(
                    f"token {tok!r}: absolute paths and '..'-traversal "
                    "are rejected (paths must be relative to "
                    "$HOLO_RESOURCE_PATH)"
                )


# --------------------------------------------------------------------- PATH pinning


def caps_exec_bins(caps: Iterable[str]) -> frozenset[str]:
    """Extract bare binary names from a resource's ``caps`` list.

    ``caps=exec:ffprobe,exec:python3,readonly`` →
    ``frozenset({"ffprobe", "python3"})``. Non-exec caps (``readonly``,
    ``uid:NAME``, ``sandbox``) are skipped. A cap of the form
    ``exec:``-with-empty-name or ``exec:/abs/path`` is rejected at
    extraction time — caps are meant to declare bare names, not paths.
    """
    out: set[str] = set()
    for cap in caps:
        if not cap.startswith("exec:"):
            continue
        name = cap[len("exec:"):].strip()
        if not name:
            raise ValueError(
                f"resource cap {cap!r}: 'exec:' prefix with empty name"
            )
        if "/" in name:
            raise ValueError(
                f"resource cap {cap!r}: 'exec:' value must be a bare "
                "binary name (no '/'); the daemon resolves it via "
                "shutil.which at call time"
            )
        out.add(name)
    return frozenset(out)


@contextmanager
def pinned_path_dir(allowed_bins: Iterable[str]) -> Iterator[str]:
    """Yield a path to a tmpdir containing symlinks ONLY for ``allowed_bins``.

    The body runs with ``PATH=<this dir>``, so any command not symlinked
    fails dyld lookup. Structure::

        /tmp/holo-run-<uuid>/bin/ffprobe -> /usr/local/bin/ffprobe
        /tmp/holo-run-<uuid>/bin/python3 -> /usr/bin/python3

    The yielded path is the ``bin/`` directory, ready to drop into
    ``PATH`` as-is.

    Raises :class:`FileNotFoundError` if any declared binary isn't
    resolvable via :func:`shutil.which` at call time — announcer-side
    misconfiguration the caller should see right away, not later as a
    confusing "command not found" inside the body. The path-resolution
    happens BEFORE any directory is created, so a partial farm is never
    left around.

    The tmpdir is removed on exit. Caller owns the lifetime: while the
    contextmanager is open, the spawned process can use the path; once
    closed, the symlinks are gone (which is fine because the spawned
    process has already resolved its own argv[0] by then).
    """
    # Pre-resolve everything before touching the filesystem so we fail
    # cleanly on misconfigured caps.
    resolved: dict[str, str] = {}
    for name in allowed_bins:
        if "/" in name:
            raise ValueError(
                f"pinned_path_dir: binary name {name!r} must not "
                "contain '/' (use bare names; caller resolves via PATH)"
            )
        src = shutil.which(name)
        if src is None:
            raise FileNotFoundError(
                f"pinned_path_dir: binary {name!r} not on the daemon's "
                "PATH at call time — fix the resource's caps declaration "
                "or install the binary"
            )
        resolved[name] = src

    parent = tempfile.mkdtemp(prefix=f"holo-run-{uuid.uuid4().hex[:8]}-")
    try:
        bindir = os.path.join(parent, "bin")
        os.makedirs(bindir)
        for name, src in resolved.items():
            os.symlink(src, os.path.join(bindir, name))
        yield bindir
    finally:
        # rmtree handles the symlink farm + parent in one call; missing
        # children (e.g. cleanup raced with a Ctrl-C) shouldn't block
        # the unwind path.
        shutil.rmtree(parent, ignore_errors=True)


# --------------------------------------------------------------------- exec


# Default per-call timeout. The MCP tool surface accepts a per-call
# override; this is the floor for tools that don't specify.
DEFAULT_EXEC_TIMEOUT_S = 60

# Hard cap on the timeout a caller can request — prevents an MCP client
# from holding a slot open for hours via a runaway body.
MAX_EXEC_TIMEOUT_S = 30 * 60  # 30 minutes


def _build_env(
    resource: Resource,
    bindir: str,
    caller_env: dict[str, str],
) -> dict[str, str]:
    """Assemble the stripped-and-injected env for the spawned shell.

    Order of precedence (last writer wins):
      1. Baseline (PATH, HOME, TERM, locale) — minimal viable shell env
      2. Caller-supplied env — body-specific config
      3. Daemon-set vars (HOLO_HOST/RESOURCE/RESOURCE_PATH) — facts the
         body must trust, callers don't get to override
      4. PATH re-pin — the security boundary; even daemon code can't
         move this off the symlink farm

    The daemon's own env (containing ``$HOME``, ``$SSH_AUTH_SOCK``, the
    user's keychain paths, etc.) is NOT inherited. Bodies run with the
    minimum needed to be a usable shell, nothing more.
    """
    base: dict[str, str] = {
        "PATH": bindir,
        "HOME": resource.path,
        "TERM": "dumb",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    base.update(caller_env)
    base["HOLO_HOST"] = socket.gethostname()
    base["HOLO_RESOURCE"] = resource.name
    base["HOLO_RESOURCE_PATH"] = resource.path
    base["PATH"] = bindir
    return base


def _pump_lines(
    stream: object,
    fd_name: str,
    on_frame: Callable[[dict], None],
) -> None:
    """Read one line at a time off ``stream`` and emit ``{fd, data}`` frames.

    Runs on a worker thread. ``on_frame`` is invoked from this thread;
    callers that aggregate frames into shared structures should ensure
    thread safety themselves (list.append is GIL-safe; do the right
    thing for everything else).

    On any stream error (process died mid-write, pipe closed) the pump
    exits silently — the main thread's ``proc.wait()`` is the source of
    truth for completion, not the pumps.
    """
    try:
        for line in stream:  # type: ignore[attr-defined]
            on_frame({"fd": fd_name, "data": line.rstrip("\n")})
    except (OSError, ValueError):
        pass


def exec_in_resource(
    resource: Resource,
    body: str,
    *,
    env: dict[str, str] | None = None,
    timeout_s: int = DEFAULT_EXEC_TIMEOUT_S,
    on_frame: Callable[[dict], None],
) -> ExecResult:
    """Validate, prepare, and spawn ``/bin/sh -c body`` inside ``resource``.

    The order is non-negotiable: validate before symlink-farm, symlink-
    farm before spawn. Any failure in the first two stages is reported
    via raised exception (no process ever spawned); failures during
    exec come back via the return value's ``exit_code``.

    Streaming contract:
      - ``on_frame({"fd": "stdout"|"stderr", "data": str})`` is called
        per line of output as it arrives. Trailing newlines are stripped.
        Frames may interleave from the two streams as the kernel
        delivers them.
      - The frame callback is invoked from worker threads (one per fd);
        callers that aggregate state should be thread-safe.
      - A final ``{"exit": int, "duration_ms": int}`` frame is NOT
        emitted here — that's the MCP tool wrapper's job (it has the
        full per-call context). This function returns :class:`ExecResult`
        with the same data; wrapper code builds the terminal frame from it.

    Timeout: ``timeout_s`` is clamped to ``[1, MAX_EXEC_TIMEOUT_S]``.
    On expiry the process group receives SIGKILL (the body and any
    children — for/while loops, pipelines, subshells — all die in one
    syscall). ``ExecResult.timed_out`` is True; ``exit_code`` reflects
    the kill (typically -9 on POSIX or a positive 137).

    Raises :class:`BodyRejected` if the static parse fails.
    Raises :class:`FileNotFoundError` if any declared cap binary isn't
    on the daemon's PATH.
    Raises :class:`FileNotFoundError` if ``resource.path`` doesn't exist.
    """
    if not os.path.isdir(resource.path):
        raise FileNotFoundError(
            f"exec_in_resource: resource path {resource.path!r} does "
            "not exist on this host"
        )

    allowed = caps_exec_bins(resource.caps)
    validate_body(body, allowed_bins=allowed)

    timeout_s = max(1, min(MAX_EXEC_TIMEOUT_S, int(timeout_s)))
    caller_env = dict(env or {})

    with pinned_path_dir(allowed) as bindir:
        env_dict = _build_env(resource, bindir, caller_env)
        start = time.monotonic()
        proc = subprocess.Popen(
            ["/bin/sh", "-c", body],
            cwd=resource.path,
            env=env_dict,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,  # line-buffered
            start_new_session=True,
        )

        threads: list[threading.Thread] = []
        for fd_name, stream in (
            ("stdout", proc.stdout),
            ("stderr", proc.stderr),
        ):
            t = threading.Thread(
                target=_pump_lines,
                args=(stream, fd_name, on_frame),
                daemon=True,
                name=f"holo-exec-pump-{fd_name}",
            )
            t.start()
            threads.append(t)

        timed_out = False
        try:
            exit_code = proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            # SIGKILL the whole process group so children (subshells,
            # pipelines, loops) die with the parent. Wrap in try because
            # the process could exit between the timeout check and the
            # kill — that's fine, just claim the exit code.
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            exit_code = proc.wait()

        for t in threads:
            t.join(timeout=2.0)

        duration_ms = int((time.monotonic() - start) * 1000)
        return ExecResult(
            exit_code=exit_code,
            duration_ms=duration_ms,
            timed_out=timed_out,
        )
