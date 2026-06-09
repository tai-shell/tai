/* boot.c — Embedded Python boot/serve for the tai control shell.

   See docs/embedded-holo.md. Currently a smoke test that proves the
   CPython static link and Py_InitializeEx path work end-to-end. The
   next iteration replaces the inline Python snippet with the actual
   MCP server bootstrap (import holo.mcp_server, construct an
   embedded HoloMCPServer, call serve_stdio()). */

/* Python.h must come first per CPython's documented requirement. */
#define PY_SSIZE_T_CLEAN
#include <Python.h>

#include <libgen.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "boot.h"

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

/* Build a sys.path-setup Python snippet that prepends three paths
   computed from `exe_dir`. The order matches the precedence rule:
   vendored holo wins over the runtime package wins over the pip
   dep closure. Returns a malloc'd string the caller must free.

   We don't quote-escape exe_dir for the embedded Python string
   literal because realpath() output won't contain quote chars on
   any plausible filesystem we care about. */
static char *
tai_format_path_setup (const char *exe_dir)
{
  /* Liberal upper bound: 3 path strings (~PATH_MAX each) +
     boilerplate. PATH_MAX is 1024 on macOS, so ~4 KiB worst case. */
  size_t cap = (PATH_MAX + 64) * 3 + 128;
  char *buf = malloc (cap);
  if (buf == NULL)
    return NULL;

  /* Dev-tree layout (matches `make` output):
       <exe_dir>/vendor/holo/src         vendored holo
       <exe_dir>/embedded/runtime        tai_runtime package
       <exe_dir>/build/site-packages     pip-installed deps
     For an installed binary, the post-install hook should drop the
     same three sibling dirs next to the executable, or eventually
     freeze them into the binary so this lookup becomes a no-op. */
  snprintf (buf, cap,
	    "import sys\n"
	    "sys.path.insert(0, '%s/build/site-packages')\n"
	    "sys.path.insert(0, '%s/embedded/runtime')\n"
	    "sys.path.insert(0, '%s/vendor/holo/src')\n",
	    exe_dir, exe_dir, exe_dir);
  return buf;
}

int
tai_embedded_serve_stdio (void)
{
  PyStatus status;
  PyConfig config;

  PyConfig_InitIsolatedConfig (&config);

  /* Isolated config defaults:
       - no sys.path manipulation from environment
       - no user site-packages
       - no SIGINT handler installation (tai owns signals)
     But we DO want stdio to be unbuffered so MCP framing works
     line-by-line without extra fflush() dances. */
  config.buffered_stdio = 0;
  config.parse_argv = 0;

  status = Py_InitializeFromConfig (&config);
  PyConfig_Clear (&config);

  if (PyStatus_Exception (status))
    {
      fprintf (stderr, "tai: Py_InitializeFromConfig failed: %s\n",
	       status.err_msg ? status.err_msg : "(no detail)");
      return 70;	/* EX_SOFTWARE */
    }

  /* Smoke test: prove the interpreter is up, vendored holo is
     importable from sys.path, and the pure-Python browser_chrome
     module loads without pyobjc. Goes to stderr so stdout stays
     clean for the eventual MCP frame stream.

     TAI_VENDOR_HOLO_PATH is the absolute path to vendor/holo/src/,
     baked in at build time via -D in EMBED_CFLAGS. We prepend it
     to sys.path so `import holo` finds the vendored tree before any
     other site-packages copy. */
  /* Compute the three sys.path entries from the binary's location at
     runtime (not from build-time -D macros, so the binary relocates
     cleanly). Order matters: vendored holo must come BEFORE any
     site-packages copy so a system-wide pip-installed holo (if any)
     doesn't shadow our pin. */
  char exe_dir[PATH_MAX];
  if (tai_resolve_exe_dir (exe_dir) != 0)
    {
      fprintf (stderr, "tai: could not resolve executable path\n");
      Py_Finalize ();
      return 70;
    }

  /* Tell holo.bridge where to find the SikuliX jar + Jython bridge
     script. Both live next to the binary in the dev tree (and in the
     install tree once the post-install hook lays them out). Setting
     the env vars from C means a user invoking `tcsh` doesn't need
     anything in their shell environment for screen_* tools to work. */
  {
    char sikuli_jar[PATH_MAX];
    char bridge_script[PATH_MAX];
    snprintf (sikuli_jar, sizeof sikuli_jar,
	      "%s/build/sikuli/sikulixide-2.0.5.jar", exe_dir);
    snprintf (bridge_script, sizeof bridge_script,
	      "%s/vendor/holo/bridge/bridge.py", exe_dir);
    setenv ("HOLO_SIKULI_JAR", sikuli_jar, /*overwrite=*/0);
    setenv ("HOLO_BRIDGE_SCRIPT", bridge_script, /*overwrite=*/0);
  }

  char *path_setup = tai_format_path_setup (exe_dir);
  if (path_setup == NULL)
    {
      fprintf (stderr, "tai: out of memory composing sys.path setup\n");
      Py_Finalize ();
      return 70;
    }
  int path_rc = PyRun_SimpleString (path_setup);
  free (path_setup);
  if (path_rc != 0)
    {
      fprintf (stderr, "tai: sys.path setup failed\n");
      Py_Finalize ();
      return 70;
    }

  /* Import tai_runtime.server and call its serve() entry point.
     Using the C API (rather than PyRun_SimpleString) so the int return
     value flows back to the caller as a real exit code. */
  PyObject *server_mod = PyImport_ImportModule ("tai_runtime.server");
  if (server_mod == NULL)
    {
      PyErr_Print ();
      fprintf (stderr, "tai: failed to import tai_runtime.server\n");
      Py_Finalize ();
      return 70;
    }

  PyObject *result = PyObject_CallMethod (server_mod, "serve", NULL);
  Py_DECREF (server_mod);
  if (result == NULL)
    {
      PyErr_Print ();
      fprintf (stderr, "tai: tai_runtime.server.serve() raised\n");
      Py_Finalize ();
      return 70;
    }

  long exit_code = PyLong_AsLong (result);
  Py_DECREF (result);
  if (exit_code == -1 && PyErr_Occurred ())
    {
      PyErr_Print ();
      exit_code = 70;
    }

  if (Py_FinalizeEx () < 0)
    {
      fprintf (stderr, "tai: Py_FinalizeEx reported a clean-up error\n");
      if (exit_code == 0)
	exit_code = 70;
    }

  return (int) exit_code;
}
