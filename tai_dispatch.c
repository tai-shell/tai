/* tai_dispatch.c — argv[0] dispatch for the tai multi-call binary.

   This is the entry-point hook called from shell.c's main() before any
   bash-specific initialization runs. See tai_dispatch.h and
   docs/embedded-holo.md for the design. */

#include "config.h"

#include <stdio.h>
#include <string.h>

#include "bashtypes.h"
#include "bashansi.h"
#include "shell.h"

#include "tai_dispatch.h"

static const char *
tai_basename (const char *path)
{
  const char *p;

  if (path == 0)
    return "";
  p = strrchr (path, '/');
  return p ? p + 1 : path;
}

int
tai_is_control_shell_name (const char *argv0)
{
  return strcmp (tai_basename (argv0), "tcsh") == 0;
}

int
tai_is_user_shell_name (const char *argv0)
{
  return strcmp (tai_basename (argv0), "tai") == 0;
}

int
tai_control_shell_main (int argc, char **argv)
{
  /* Stub. The real implementation will:
       - Py_InitializeEx(0)
       - import the frozen holo.mcp_server
       - construct HoloMCPServer(embedded=True)
       - call serve_stdio() until EOF on stdin
       - Py_Finalize() and exit with the loop's return code.
     For now we just announce ourselves on stderr so callers can verify
     the dispatch is wired up end-to-end, then exit cleanly. Positional
     args are discarded per design (the name is the whole signal). */
  (void) argc;
  fprintf (stderr,
	   "tai: control shell stub (argv[0]=%s)\n"
	   "tai: real MCP server is not implemented yet; exiting.\n",
	   argv[0] ? argv[0] : "(null)");
  return 0;
}
