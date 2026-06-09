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
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

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

/* The five filesystem locations the embedded interpreter needs.
   Filled from the bundled-payload cache when present, otherwise
   from the dev-tree layout next to the running binary. */
typedef struct {
  char holo_src[PATH_MAX];	/* parent of the `holo` package      */
  char runtime[PATH_MAX];	/* parent of the `tai_runtime` package*/
  char site_pkgs[PATH_MAX];	/* pip-installed dep closure          */
  char sikuli_jar[PATH_MAX];	/* SikuliX jar absolute path          */
  char bridge_script[PATH_MAX];	/* bridge.py absolute path            */
} tai_runtime_paths_t;

static void
tai_paths_from_cache_dir (const char *cache_dir,
			  tai_runtime_paths_t *out)
{
  snprintf (out->holo_src,      sizeof out->holo_src,
	    "%s/holo/src",                       cache_dir);
  snprintf (out->runtime,       sizeof out->runtime,
	    "%s/runtime",                        cache_dir);
  snprintf (out->site_pkgs,     sizeof out->site_pkgs,
	    "%s/site-packages",                  cache_dir);
  snprintf (out->sikuli_jar,    sizeof out->sikuli_jar,
	    "%s/sikuli/sikulixide-2.0.5.jar",    cache_dir);
  snprintf (out->bridge_script, sizeof out->bridge_script,
	    "%s/holo/bridge/bridge.py",          cache_dir);
}

static void
tai_paths_from_dev_tree (const char *exe_dir,
			 tai_runtime_paths_t *out)
{
  snprintf (out->holo_src,      sizeof out->holo_src,
	    "%s/vendor/holo/src",                exe_dir);
  snprintf (out->runtime,       sizeof out->runtime,
	    "%s/embedded/runtime",               exe_dir);
  snprintf (out->site_pkgs,     sizeof out->site_pkgs,
	    "%s/build/site-packages",            exe_dir);
  snprintf (out->sikuli_jar,    sizeof out->sikuli_jar,
	    "%s/build/sikuli/sikulixide-2.0.5.jar", exe_dir);
  snprintf (out->bridge_script, sizeof out->bridge_script,
	    "%s/vendor/holo/bridge/bridge.py",   exe_dir);
}

/* Build a sys.path-setup Python snippet that prepends three paths
   from `paths`. The order matches the precedence rule: vendored
   holo wins over the runtime package wins over the pip dep closure.
   Returns a malloc'd string the caller must free.

   We don't quote-escape because realpath() / cache-dir paths won't
   contain quote chars on any plausible filesystem we care about. */
static char *
tai_format_path_setup (const tai_runtime_paths_t *paths)
{
  size_t cap = (PATH_MAX + 64) * 3 + 128;
  char *buf = malloc (cap);
  if (buf == NULL)
    return NULL;

  snprintf (buf, cap,
	    "import sys\n"
	    "sys.path.insert(0, '%s')\n"
	    "sys.path.insert(0, '%s')\n"
	    "sys.path.insert(0, '%s')\n",
	    paths->site_pkgs, paths->runtime, paths->holo_src);
  return buf;
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
  PyStatus status;
  PyConfig config;

  PyConfig_InitIsolatedConfig (&config);

  /* Isolated config defaults:
       - no sys.path manipulation from environment
       - no user site-packages
       - no SIGINT handler installation (bash owns signals)
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
      return -1;
    }

  /* Pick the runtime support layout. Bundled binary (produced by
     `make -C embedded bundle`) has the runtime appended as a gzip
     tarball with a 24-byte trailer at EOF; we extract once into a
     per-binary cache dir and point at it. A plain dev build has no
     trailer and falls back to the sibling-dirs-of-the-binary
     layout that `make` produces. */
  tai_runtime_paths_t paths;
  bool resolved = false;
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
	      tai_paths_from_cache_dir (cache_dir, &paths);
	      resolved = true;
	    }
	  /* Falls through to dev-tree below if extraction failed; the
	     caller will see a clearer ModuleNotFoundError there if the
	     binary is bundled but the dev tree isn't present either. */
	}
    }
  if (!resolved)
    {
      char exe_dir[PATH_MAX];
      if (tai_resolve_exe_dir (exe_dir) != 0)
	{
	  fprintf (stderr, "tai: could not resolve executable path\n");
	  Py_FinalizeEx ();
	  return -1;
	}
      tai_paths_from_dev_tree (exe_dir, &paths);
    }

  /* Tell holo.bridge where to find the SikuliX jar + Jython bridge
     script, and holo.templates where its on-disk cache lives.
     Setting the env vars from C means a user invoking the binary
     doesn't need anything in their shell environment for screen_*
     tools to work. overwrite=0: the user's explicit env wins. */
  setenv ("HOLO_SIKULI_JAR",    paths.sikuli_jar,    /*overwrite=*/0);
  setenv ("HOLO_BRIDGE_SCRIPT", paths.bridge_script, /*overwrite=*/0);
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

  char *path_setup = tai_format_path_setup (&paths);
  if (path_setup == NULL)
    {
      fprintf (stderr, "tai: out of memory composing sys.path setup\n");
      Py_FinalizeEx ();
      return -1;
    }
  int path_rc = PyRun_SimpleString (path_setup);
  free (path_setup);
  if (path_rc != 0)
    {
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
