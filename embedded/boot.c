/* boot.c — Embedded Python boot/serve for the tai control shell.

   See docs/embedded-holo.md. Currently a smoke test that proves the
   CPython static link and Py_InitializeEx path work end-to-end. The
   next iteration replaces the inline Python snippet with the actual
   MCP server bootstrap (import holo.mcp_server, construct an
   embedded HoloMCPServer, call serve_stdio()). */

/* Python.h must come first per CPython's documented requirement. */
#define PY_SSIZE_T_CLEAN
#include <Python.h>

#include <arpa/inet.h>
#include <errno.h>
#include <libgen.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <sys/wait.h>
#include <unistd.h>

#include "boot.h"
#include "extract.h"
#include "payload.h"

/* Locate the directory containing the running binary, resolving any
   symlinks (so /tmp/tcsh -> /usr/local/bin/tai gives /usr/local/bin).
   On success returns 0 and writes the directory into `out` (which
   must have room for PATH_MAX bytes). Returns -1 on failure. */
static int
tai_resolve_exe_dir (char *out)
{
  char raw[PATH_MAX];
  uint32_t size = sizeof raw;

  if (_NSGetExecutablePath (raw, &size) != 0)
    return -1;

  char resolved[PATH_MAX];
  if (realpath (raw, resolved) == NULL)
    return -1;

  /* dirname() may modify its input on some platforms; give it a copy. */
  char copy[PATH_MAX];
  strncpy (copy, resolved, sizeof copy - 1);
  copy[sizeof copy - 1] = '\0';

  const char *dir = dirname (copy);
  if (dir == NULL)
    return -1;

  strncpy (out, dir, PATH_MAX - 1);
  out[PATH_MAX - 1] = '\0';
  return 0;
}

/* Same as tai_resolve_exe_dir but writes the full resolved path of
   the executable file (not its parent directory). Used by the
   payload-trailer reader. */
static int
tai_resolve_exe_path (char *out)
{
  char raw[PATH_MAX];
  uint32_t size = sizeof raw;

  if (_NSGetExecutablePath (raw, &size) != 0)
    return -1;

  if (realpath (raw, out) == NULL)
    return -1;
  return 0;
}

/* The seven filesystem locations the embedded interpreter needs.
   Filled from the bundled-payload cache when present, otherwise
   from the dev-tree layout next to the running binary.

   cpython_home is the prefix Python uses to find its stdlib
   (encodings/, lib-dynload/, etc.) — must be set via config.home
   BEFORE Py_InitializeFromConfig, otherwise init fails at the
   "Failed to import encodings module" bootstrap step. The string
   convention: cpython_home contains a `lib/python3.13/` subtree.

   sikuli_jar is the *API* jar now (sikulixapi-2.0.5-macos.jar)
   paired with jython_jar on the JVM classpath — see holo v0.1.0a33
   bridge.py. The legacy IDE jar wedged on macOS 15 via jkeymaster's
   Carbon hotkey provider; the API + Jython invocation skips that. */
typedef struct {
  char cpython_home[PATH_MAX];	/* CPython prefix; has lib/python3.13/ */
  char holo_src[PATH_MAX];	/* parent of the `holo` package      */
  char runtime[PATH_MAX];	/* parent of the `tai_runtime` package*/
  char site_pkgs[PATH_MAX];	/* pip-installed dep closure          */
  char sikuli_jar[PATH_MAX];	/* sikulixapi-*.jar absolute path     */
  char jython_jar[PATH_MAX];	/* jython-standalone-*.jar abs path   */
  char bridge_script[PATH_MAX];	/* bridge.py absolute path            */
} tai_runtime_paths_t;

static void
tai_paths_from_cache_dir (const char *cache_dir,
			  tai_runtime_paths_t *out)
{
  snprintf (out->cpython_home,  sizeof out->cpython_home,
	    "%s/cpython",                        cache_dir);
  snprintf (out->holo_src,      sizeof out->holo_src,
	    "%s/holo/src",                       cache_dir);
  snprintf (out->runtime,       sizeof out->runtime,
	    "%s/runtime",                        cache_dir);
  snprintf (out->site_pkgs,     sizeof out->site_pkgs,
	    "%s/site-packages",                  cache_dir);
  snprintf (out->sikuli_jar,    sizeof out->sikuli_jar,
	    "%s/sikuli/sikulixapi-2.0.5-macos.jar", cache_dir);
  snprintf (out->jython_jar,    sizeof out->jython_jar,
	    "%s/sikuli/jython-standalone-2.7.4.jar", cache_dir);
  snprintf (out->bridge_script, sizeof out->bridge_script,
	    "%s/holo/bridge/bridge.py",          cache_dir);
}

static void
tai_paths_from_dev_tree (const char *exe_dir,
			 tai_runtime_paths_t *out)
{
  snprintf (out->cpython_home,  sizeof out->cpython_home,
	    "%s/build/cpython-install",          exe_dir);
  snprintf (out->holo_src,      sizeof out->holo_src,
	    "%s/vendor/holo/src",                exe_dir);
  snprintf (out->runtime,       sizeof out->runtime,
	    "%s/embedded/runtime",               exe_dir);
  snprintf (out->site_pkgs,     sizeof out->site_pkgs,
	    "%s/build/site-packages",            exe_dir);
  snprintf (out->sikuli_jar,    sizeof out->sikuli_jar,
	    "%s/build/sikuli/sikulixapi-2.0.5-macos.jar", exe_dir);
  snprintf (out->jython_jar,    sizeof out->jython_jar,
	    "%s/build/sikuli/jython-standalone-2.7.4.jar", exe_dir);
  snprintf (out->bridge_script, sizeof out->bridge_script,
	    "%s/vendor/holo/bridge/bridge.py",   exe_dir);
}

/* Resolve all six runtime paths. Returns 0 on success and fills
   *out; -1 on failure (diagnostic already on stderr). Bundled
   binary: extracts the payload to ~/Library/Caches/tai/payload-
   <hex>/ on first call, points paths at it. Dev-tree binary:
   points at sibling dirs of the executable. */
static int
tai_resolve_runtime_paths (tai_runtime_paths_t *out)
{
  char exe_path[PATH_MAX];
  if (tai_resolve_exe_path (exe_path) == 0)
    {
      tai_payload_trailer_t trailer;
      if (tai_payload_read_trailer (exe_path, &trailer) == 0)
	{
	  char cache_dir[PATH_MAX];
	  if (tai_payload_ensure_extracted (exe_path, &trailer,
					    cache_dir) == 0)
	    {
	      tai_paths_from_cache_dir (cache_dir, out);
	      return 0;
	    }
	  /* Falls through to dev-tree if extraction failed; the
	     caller will see a clear error there. */
	}
    }
  char exe_dir[PATH_MAX];
  if (tai_resolve_exe_dir (exe_dir) != 0)
    {
      fprintf (stderr, "tai: could not resolve executable path\n");
      return -1;
    }
  tai_paths_from_dev_tree (exe_dir, out);
  return 0;
}

/* Prepend the three runtime paths to sys.path via the C API.

   Done with PySys_GetObject + PyList_Insert (instead of building a
   Python source snippet and PyRun_SimpleString'ing it) so a path
   containing a single quote, backslash, or other PEP-3120 quoting
   hazard can't turn into a SyntaxError at startup. Order matches
   the precedence rule: vendored holo wins over the runtime package
   wins over the pip dep closure. Returns 0 on success, -1 on
   failure (with a Python error indicator set). */
static int
tai_apply_path_setup (const tai_runtime_paths_t *paths)
{
  /* sys.path is a borrowed reference owned by sys; don't DECREF it. */
  PyObject *sys_path = PySys_GetObject ("path");
  if (sys_path == NULL || !PyList_Check (sys_path))
    return -1;

  /* Inserts happen at position 0, so insert in REVERSE priority order
     to end up with [holo_src, runtime, site_pkgs, ...] in front. */
  const char *entries[] = {
    paths->site_pkgs,
    paths->runtime,
    paths->holo_src,
  };
  for (size_t i = 0; i < sizeof entries / sizeof entries[0]; i++)
    {
      PyObject *s = PyUnicode_FromString (entries[i]);
      if (s == NULL)
	return -1;
      int rc = PyList_Insert (sys_path, 0, s);
      Py_DECREF (s);
      if (rc != 0)
	return -1;
    }
  return 0;
}

/* Idempotency guard. Once a successful init has happened in this
   process, subsequent calls to tai_embedded_ensure_ready() are
   O(1) no-ops. */
static bool _py_ready = false;

static void
_tai_finalize_atexit (void)
{
  /* Drives Python's own atexit handlers (which include
     tai_runtime.server's bridge.stop), then tears down the
     interpreter. Fires on normal exit; SIGKILL bypasses it but
     the SikuliX bridge has its own parent-death watchdog. */
  if (_py_ready)
    Py_FinalizeEx ();
}

static int
_tai_embedded_init (void)
{
  /* Resolve filesystem locations BEFORE Py_InitializeFromConfig.
     CPython's init bootstraps `encodings` to set up stdio — it
     needs to find the stdlib via config.home, which we compute
     from either the extracted bundle cache or the dev tree. */
  tai_runtime_paths_t paths;
  if (tai_resolve_runtime_paths (&paths) != 0)
    return -1;

  /* Tell holo.bridge where to find the SikuliX jar + Jython bridge
     script, and holo.templates where its on-disk cache lives.
     Setting these from C means a user invoking the binary doesn't
     need anything in their shell environment for screen_* tools
     to work. overwrite=0: the user's explicit env wins. */
  /* HOLO_SIKULI_JAR is the legacy env var name; vendored holo
     v0.1.0a33+ reads it as a fallback for the new
     HOLO_SIKULI_API_JAR. Set both to be unambiguous. */
  setenv ("HOLO_SIKULI_JAR",     paths.sikuli_jar,    /*overwrite=*/0);
  setenv ("HOLO_SIKULI_API_JAR", paths.sikuli_jar,    /*overwrite=*/0);
  setenv ("HOLO_JYTHON_JAR",     paths.jython_jar,    /*overwrite=*/0);
  setenv ("HOLO_BRIDGE_SCRIPT",  paths.bridge_script, /*overwrite=*/0);
  {
    const char *home = getenv ("HOME");
    if (home != NULL)
      {
	char tmpl_dir[PATH_MAX];
	snprintf (tmpl_dir, sizeof tmpl_dir,
		  "%s/Library/Caches/tai/templates", home);
	setenv ("HOLO_TEMPLATE_DIR", tmpl_dir, /*overwrite=*/0);
      }
  }

  /* Now configure + init Python. config.home points at the
     extracted (or dev-tree) CPython prefix so encodings/
     lib-dynload/ are findable. Isolated config:
       - no sys.path manipulation from environment
       - no user site-packages
       - no SIGINT handler installation (bash owns signals)
     Plus unbuffered stdio so MCP framing works line-by-line. */
  PyStatus status;
  PyConfig config;

  PyConfig_InitIsolatedConfig (&config);
  config.buffered_stdio = 0;
  config.parse_argv = 0;

  status = PyConfig_SetBytesString (&config, &config.home,
				    paths.cpython_home);
  if (PyStatus_Exception (status))
    {
      fprintf (stderr, "tai: PyConfig_SetBytesString(home) failed: %s\n",
	       status.err_msg ? status.err_msg : "(no detail)");
      PyConfig_Clear (&config);
      return -1;
    }

  status = Py_InitializeFromConfig (&config);
  PyConfig_Clear (&config);

  if (PyStatus_Exception (status))
    {
      fprintf (stderr, "tai: Py_InitializeFromConfig failed: %s\n",
	       status.err_msg ? status.err_msg : "(no detail)");
      return -1;
    }

  if (tai_apply_path_setup (&paths) != 0)
    {
      PyErr_Print ();
      fprintf (stderr, "tai: sys.path setup failed\n");
      Py_FinalizeEx ();
      return -1;
    }

  _py_ready = true;
  atexit (_tai_finalize_atexit);
  return 0;
}

int
tai_embedded_ensure_ready (void)
{
  if (_py_ready)
    return 0;
  return _tai_embedded_init ();
}

int
tai_embedded_serve_stdio (void)
{
  if (tai_embedded_ensure_ready () != 0)
    return 70;	/* EX_SOFTWARE — diagnostic already on stderr */

  /* Import tai_runtime.server and call its serve() entry point.
     Using the C API (rather than PyRun_SimpleString) so the int return
     value flows back to the caller as a real exit code. */
  PyObject *server_mod = PyImport_ImportModule ("tai_runtime.server");
  if (server_mod == NULL)
    {
      PyErr_Print ();
      fprintf (stderr, "tai: failed to import tai_runtime.server\n");
      return 70;
    }

  PyObject *result = PyObject_CallMethod (server_mod, "serve", NULL);
  Py_DECREF (server_mod);
  if (result == NULL)
    {
      PyErr_Print ();
      fprintf (stderr, "tai: tai_runtime.server.serve() raised\n");
      return 70;
    }

  long exit_code = PyLong_AsLong (result);
  Py_DECREF (result);
  if (exit_code == -1 && PyErr_Occurred ())
    {
      PyErr_Print ();
      exit_code = 70;
    }

  return (int) exit_code;
}

/* ----------- --listen support (TCP transport for tcsh) ------------ */

/* Read a single newline-terminated line into `buf`. Caps at `max - 1`
   bytes (always leaves room for a trailing NUL). Returns the number
   of bytes read NOT including the newline, or -1 on EOF / error /
   line too long. */
static ssize_t
_read_line (int fd, char *buf, size_t max)
{
  size_t i = 0;
  while (i < max - 1)
    {
      char c;
      ssize_t r = read (fd, &c, 1);
      if (r <= 0)
	return -1;
      if (c == '\n')
	{
	  buf[i] = '\0';
	  return (ssize_t) i;
	}
      buf[i++] = c;
    }
  /* Line too long — bail out before the client can fill memory. */
  return -1;
}

/* Drain "TAI/1\n[<token>\n]" from the client. Returns 0 on success,
   -1 on any deviation. Sends a short reason to stderr on failure
   so the operator can see why a connection was rejected without
   needing to attach a debugger. */
static int
_validate_handshake (int fd, const char *expected_token)
{
  char buf[256];

  ssize_t n = _read_line (fd, buf, sizeof buf);
  if (n < 0 || strcmp (buf, "TAI/1") != 0)
    {
      fprintf (stderr, "tai: tcsh --listen: bad magic prefix\n");
      return -1;
    }

  if (expected_token != NULL)
    {
      n = _read_line (fd, buf, sizeof buf);
      if (n < 0)
	{
	  fprintf (stderr, "tai: tcsh --listen: missing token line\n");
	  return -1;
	}
      if (strcmp (buf, expected_token) != 0)
	{
	  fprintf (stderr, "tai: tcsh --listen: token mismatch\n");
	  return -1;
	}
    }
  return 0;
}

/* How long a freshly-accepted client has to complete the
   "TAI/1\n<token>\n" handshake before we drop it. Without this a
   client that connects and then says nothing (a port scanner, a
   half-open TCP from a dying tunnel) blocks the accept loop
   forever — which, in a serial single-session listener, is a
   denial of service against the next real reconnect. */
#define TAI_HANDSHAKE_TIMEOUT_S 10

/* Idle seconds before the kernel starts probing a silent peer, then
   probe spacing and how many unanswered probes kill the connection.
   60 + 5*10 => a dead peer is reaped in ~110s instead of macOS's
   2-hour SO_KEEPALIVE default. */
#define TAI_KEEPALIVE_IDLE_S  60
#define TAI_KEEPALIVE_INTVL_S 10
#define TAI_KEEPALIVE_CNT     5

/* Arm TCP keepalive on an accepted connection.

   This covers the case where the peer dies without ever sending a
   FIN — the client host panics, is force-slept, or the network
   partitions — and the socket would otherwise sit in an
   indefinitely-blocked read(). That is the "listener still running
   on the remote hours later, must be killed by hand" failure.

   Caveat worth knowing: when reached through an `ssh -L` tunnel the
   peer of this socket is the local sshd, not the far-end client, so
   keepalive only fires if sshd itself dies hard. sshd noticing its
   own dead client (and thus closing this forwarded channel) is
   governed by ClientAliveInterval in the remote sshd_config. The
   accept loop below is what makes that recoverable either way.

   All three of these are best-effort: a setsockopt failure means
   slightly worse dead-peer detection, never a broken session, so
   the return values are deliberately unchecked. */
static void
_tai_arm_keepalive (int fd)
{
  int on = 1;
  setsockopt (fd, SOL_SOCKET, SO_KEEPALIVE, &on, sizeof on);

  /* Darwin spells the idle time TCP_KEEPALIVE (seconds); the
     INTVL/CNT knobs match Linux's names and exist on 10.9+. */
#ifdef TCP_KEEPALIVE
  int idle = TAI_KEEPALIVE_IDLE_S;
  setsockopt (fd, IPPROTO_TCP, TCP_KEEPALIVE, &idle, sizeof idle);
#endif
#ifdef TCP_KEEPINTVL
  int intvl = TAI_KEEPALIVE_INTVL_S;
  setsockopt (fd, IPPROTO_TCP, TCP_KEEPINTVL, &intvl, sizeof intvl);
#endif
#ifdef TCP_KEEPCNT
  int cnt = TAI_KEEPALIVE_CNT;
  setsockopt (fd, IPPROTO_TCP, TCP_KEEPCNT, &cnt, sizeof cnt);
#endif

  /* Writing to a socket the client already closed must return EPIPE,
     not raise SIGPIPE and take the whole listener down with it. */
#ifdef SO_NOSIGPIPE
  setsockopt (fd, SOL_SOCKET, SO_NOSIGPIPE, &on, sizeof on);
#endif
}

/* Set (secs > 0) or clear (secs == 0) a receive timeout on fd.

   Used to bound the handshake read. It MUST be cleared before the
   fd becomes the session's stdin: a socket carrying SO_RCVTIMEO
   surfaces expiries as EAGAIN from read(2), which Python raises as
   an OSError in the middle of the MCP loop. */
static void
_tai_set_rcvtimeo (int fd, int secs)
{
  struct timeval tv;
  tv.tv_sec = secs;
  tv.tv_usec = 0;
  setsockopt (fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
}

int
tai_embedded_serve_tcp (const char *bind_addr, int port,
			const char *token)
{
  if (port <= 0 || port > 65535)
    {
      fprintf (stderr, "tai: tcsh --listen: invalid port %d\n", port);
      return 70;
    }
  if (bind_addr == NULL)
    bind_addr = "127.0.0.1";

  int listen_fd = socket (AF_INET, SOCK_STREAM, 0);
  if (listen_fd < 0)
    {
      fprintf (stderr, "tai: tcsh --listen: socket: %s\n",
	       strerror (errno));
      return 70;
    }

  int yes = 1;
  setsockopt (listen_fd, SOL_SOCKET, SO_REUSEADDR, &yes, sizeof yes);

  struct sockaddr_in addr;
  memset (&addr, 0, sizeof addr);
  addr.sin_family = AF_INET;
  addr.sin_port = htons ((uint16_t) port);
  if (inet_pton (AF_INET, bind_addr, &addr.sin_addr) != 1)
    {
      fprintf (stderr, "tai: tcsh --listen: invalid bind address %s\n",
	       bind_addr);
      close (listen_fd);
      return 70;
    }

  if (bind (listen_fd, (struct sockaddr *) &addr, sizeof addr) < 0)
    {
      fprintf (stderr, "tai: tcsh --listen: bind %s:%d: %s\n",
	       bind_addr, port, strerror (errno));
      close (listen_fd);
      return 70;
    }
  if (listen (listen_fd, 4) < 0)
    {
      fprintf (stderr, "tai: tcsh --listen: listen: %s\n",
	       strerror (errno));
      close (listen_fd);
      return 70;
    }

  /* Diagnostic only — written to stderr so it doesn't contaminate
     the stdio MCP channel a parent (e.g. an SSH session shovelling
     bytes to/from a remote nc) might be reading.

     mcp-bridge.py's wait_for_listener greps this line for the
     literal "tcsh listening on" to know the bind() → listen()
     chain has completed, so that substring is load-bearing. */
  fprintf (stderr,
	   "tai: tcsh listening on %s:%d (serial accept loop)%s\n",
	   bind_addr, port,
	   token ? " — token required" : "");
  fflush (stderr);

  /* Serial accept-and-fork loop.
     ---------------------------------------------------------------
     This used to be a single accept() followed by close(listen_fd),
     which made a dropped connection terminal: the ssh tunnel
     hiccups, the client goes away, and the only way back was to
     kill the process on the remote host and re-run the whole
     bootstrap. Now the listening socket outlives each session, so a
     reconnecting bridge is simply the next accept().

     Why fork per session rather than looping back into
     tai_embedded_serve_stdio() in-process: that function
     initializes CPython exactly once (the _py_ready guard) and
     hands off to tai_runtime.server.serve(), which runs FastMCP's
     stdio transport. That transport builds a fresh TextIOWrapper
     over sys.stdin.buffer on every run; a second run against a
     re-dup2'd fd 0 races the first wrapper's finalizer, which
     closes the buffer it wrapped — i.e. closes our new connection
     out from under us. Forking sidesteps the entire question: the
     parent never touches Python, and each session gets a pristine
     interpreter.

     Serial (waitpid before the next accept) rather than concurrent
     because a session drives singleton resources — the SikuliX JVM,
     the mouse, the keyboard, the front window. Two live sessions
     would fight over them. A second client that connects mid-session
     simply waits in the backlog. */
  for (;;)
    {
      int conn_fd = accept (listen_fd, NULL, NULL);
      if (conn_fd < 0)
	{
	  if (errno == EINTR || errno == ECONNABORTED)
	    continue;
	  fprintf (stderr, "tai: tcsh --listen: accept: %s\n",
		   strerror (errno));
	  close (listen_fd);
	  return 70;
	}

      _tai_arm_keepalive (conn_fd);

      /* Bound the handshake so a silent client can't wedge the loop,
	 then clear the timeout before the fd becomes stdin. */
      _tai_set_rcvtimeo (conn_fd, TAI_HANDSHAKE_TIMEOUT_S);
      int ok = _validate_handshake (conn_fd, token);
      _tai_set_rcvtimeo (conn_fd, 0);

      if (ok != 0)
	{
	  /* A rejected client is no longer fatal: log it, drop that
	     connection, keep listening. Previously a stray probe
	     (or one bad token) killed the listener outright. */
	  const char *err = "ERR bad handshake\n";
	  (void) write (conn_fd, err, strlen (err));
	  close (conn_fd);
	  continue;
	}
      (void) write (conn_fd, "OK\n", 3);

      pid_t pid = fork ();
      if (pid < 0)
	{
	  fprintf (stderr, "tai: tcsh --listen: fork: %s\n",
		   strerror (errno));
	  close (conn_fd);
	  continue;
	}

      if (pid == 0)
	{
	  /* Child — this connection's session. */
	  close (listen_fd);

	  /* dup2 the connection over fd 0 and fd 1. Python's
	     stdin/stdout are bound during Py_InitializeFromConfig
	     (called later by tai_embedded_serve_stdio →
	     tai_embedded_ensure_ready), so this MUST happen first or
	     the interpreter will grab the original terminal/pipe and
	     the dup happens too late. */
	  if (dup2 (conn_fd, 0) < 0 || dup2 (conn_fd, 1) < 0)
	    {
	      fprintf (stderr, "tai: tcsh --listen: dup2: %s\n",
		       strerror (errno));
	      _exit (70);
	    }
	  if (conn_fd > 2)
	    close (conn_fd);

	  /* exit(), not _exit(): tai_embedded_serve_stdio registers
	     _tai_finalize_atexit, which drives Python's atexit
	     handlers — including tai_runtime.server's bridge.stop,
	     which reaps the SikuliX JVM. Skipping those would leak a
	     JVM per session. The parent had registered no atexit
	     handlers at fork time, so running them here is safe. */
	  exit (tai_embedded_serve_stdio ());
	}

      /* Parent — hold the listening socket, wait out the session. */
      close (conn_fd);

      int status = 0;
      while (waitpid (pid, &status, 0) < 0)
	{
	  if (errno != EINTR)
	    break;
	}

      if (WIFSIGNALED (status))
	fprintf (stderr,
		 "tai: session %d killed by signal %d; "
		 "listening for reconnect on %s:%d\n",
		 (int) pid, WTERMSIG (status), bind_addr, port);
      else
	fprintf (stderr,
		 "tai: session %d ended (exit %d); "
		 "listening for reconnect on %s:%d\n",
		 (int) pid, WEXITSTATUS (status), bind_addr, port);
      fflush (stderr);
    }
}
