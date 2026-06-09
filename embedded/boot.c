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

  /* Smoke test: prove the interpreter is up, version is sane, and
     stderr passthrough works. Goes to stderr so it doesn't pollute
     stdout (which will carry MCP frames once we have a real server). */
  if (PyRun_SimpleString (
	"import sys\n"
	"print('tai: embedded Python', sys.version.split()[0], "
	"'is up (smoke test, no MCP yet)', file=sys.stderr)\n") != 0)
    {
      fprintf (stderr, "tai: embedded Python smoke test failed\n");
      Py_Finalize ();
      return 70;
    }

  if (Py_FinalizeEx () < 0)
    {
      fprintf (stderr, "tai: Py_FinalizeEx reported a clean-up error\n");
      return 70;
    }

  return 0;
}
