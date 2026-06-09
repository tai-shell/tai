/* boot.c — Embedded Python boot/serve for the tai control shell.

   See docs/embedded-holo.md. Currently a smoke test that proves the
   CPython static link and Py_InitializeEx path work end-to-end. The
   next iteration replaces the inline Python snippet with the actual
   MCP server bootstrap (import holo.mcp_server, construct an
   embedded HoloMCPServer, call serve_stdio()). */

/* Python.h must come first per CPython's documented requirement. */
#define PY_SSIZE_T_CLEAN
#include <Python.h>

#include <stdio.h>

#include "boot.h"

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
#ifndef TAI_VENDOR_HOLO_PATH
#  error "TAI_VENDOR_HOLO_PATH must be -D'd at build time (see Makefile.in)"
#endif
#ifndef TAI_EMBED_SITE_PKGS
#  error "TAI_EMBED_SITE_PKGS must be -D'd at build time (see Makefile.in)"
#endif
#ifndef TAI_EMBED_RUNTIME
#  error "TAI_EMBED_RUNTIME must be -D'd at build time (see Makefile.in)"
#endif

  /* Wire sys.path so the tai_runtime package, the dep closure, and the
     vendored holo tree are all reachable. Order matters: vendored holo
     must come BEFORE any site-packages copy so a system-wide pip-installed
     holo (if any) doesn't shadow our pin. */
  if (PyRun_SimpleString (
	"import sys\n"
	"sys.path.insert(0, '" TAI_EMBED_SITE_PKGS "')\n"
	"sys.path.insert(0, '" TAI_EMBED_RUNTIME "')\n"
	"sys.path.insert(0, '" TAI_VENDOR_HOLO_PATH "')\n") != 0)
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
