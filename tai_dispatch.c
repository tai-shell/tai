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

#include "builtins.h"

#include "tai_dispatch.h"
#include "embedded/boot.h"

static const char *
tai_basename (const char *path)
{
  const char *p, *base;

  if (path == 0)
    return "";
  p = strrchr (path, '/');
  base = p ? p + 1 : path;
  /* Strip the leading '-' that login(1) prepends to argv[0] for a
     login shell, so `chsh -s /usr/local/bin/tai` (which lands with
     argv[0]="-tai") still routes through the user-shell branch. */
  if (base[0] == '-')
    base++;
  return base;
}

/* The control shell (MCP listener, `--listen` support) is reached by
   invoking the binary under a control-shell name. Two are recognized:

     tcsh — the original name.
     ksh  — the name the ssh-bootstrap remote deployment now installs
            under, so the listener process shows up as an ordinary
            `ksh` on Host B rather than the unusual `tcsh`. Same
            behaviour; only the name differs.

   Both map to tai_control_shell_main. The user shell keeps the name
   `tai`; a bare `bash`/`sh` invocation is unmodified upstream bash. */
int
tai_is_control_shell_name (const char *argv0)
{
  const char *base = tai_basename (argv0);
  return strcmp (base, "tcsh") == 0 || strcmp (base, "ksh") == 0;
}

int
tai_is_user_shell_name (const char *argv0)
{
  return strcmp (tai_basename (argv0), "tai") == 0;
}

/* `tcsh` accepts a small CLI surface:
     --listen PORT       — serve MCP over TCP instead of stdio
     --bind ADDR         — bind address for --listen (default 127.0.0.1)
     --token TOKEN       — require this token in the handshake
     --help              — print usage and exit

   These flags are PARSED ONLY IN tcsh MODE. argv[0]=tai never enters
   this function (it routes through shell.c → bash main), and any
   other name routes through plain bash; so the user shell and bash
   never see these flag names.

   Without --listen, behaviour is unchanged — stdio MCP for
   spawned-child use (Claude Code's `command` MCP server entry). */
int
tai_control_shell_main (int argc, char **argv)
{
  int port = 0;
  const char *bind_addr = NULL;	/* NULL → 127.0.0.1 in serve_tcp */
  const char *token = NULL;

  for (int i = 1; i < argc; i++)
    {
      const char *arg = argv[i];
      if (strcmp (arg, "--help") == 0 || strcmp (arg, "-h") == 0)
	{
	  fprintf (stderr,
		   "Usage: ksh [--listen PORT [--bind ADDR] [--token T]]\n"
		   "\n"
		   "With no flags: serve MCP over stdio (default).\n"
		   "With --listen PORT: serve a single TCP connection.\n"
		   "  --bind ADDR     bind address (default 127.0.0.1)\n"
		   "  --token TOKEN   require this token in the handshake\n"
		   "\n"
		   "Wire format for --listen connect:\n"
		   "  client → TAI/1\\n\n"
		   "  client → <token>\\n   (only when --token is set)\n"
		   "  server → OK\\n        (then MCP framing begins)\n");
	  return 0;
	}
      if (strcmp (arg, "--listen") == 0 && i + 1 < argc)
	{
	  port = atoi (argv[++i]);
	  continue;
	}
      if (strcmp (arg, "--bind") == 0 && i + 1 < argc)
	{
	  bind_addr = argv[++i];
	  continue;
	}
      if (strcmp (arg, "--token") == 0 && i + 1 < argc)
	{
	  token = argv[++i];
	  continue;
	}
      fprintf (stderr, "ksh: unknown argument '%s' (try --help)\n", arg);
      return 64;	/* EX_USAGE */
    }

  if (port == 0)
    return tai_embedded_serve_stdio ();

  return tai_embedded_serve_tcp (bind_addr, port, token);
}

/* Prefix-based identification of the holo-builtin family. Simpler
   and more robust than maintaining a hardcoded list — if a future
   commit adds a new browser_/screen_/ui_template_ tool, this picks
   it up for free. */
static int
tai_is_holo_builtin_name (const char *name)
{
  if (name == NULL)
    return 0;
  if (strcmp (name, "holo") == 0)
    return 1;
  if (strcmp (name, "app_activate") == 0)
    return 1;
  if (strncmp (name, "browser_", 8) == 0)
    return 1;
  if (strncmp (name, "screen_", 7) == 0)
    return 1;
  if (strncmp (name, "ui_template_", 12) == 0)
    return 1;
  return 0;
}

void
tai_disable_holo_builtins (void)
{
  /* shell_builtins is bash's runtime view of the builtin table; it
     starts as a pointer into static_shell_builtins, so mutating the
     entries via shell_builtins also mutates the static table. We
     clear BUILTIN_ENABLED — the same flag `enable -n NAME' clears.
     bash's lookup (find_shell_builtin) treats unmarked entries as
     not-installed, so `type browser_navigate' under bash returns
     `command not found' just like upstream. */
  for (int i = 0; i < num_shell_builtins; i++)
    if (tai_is_holo_builtin_name (shell_builtins[i].name))
      shell_builtins[i].flags &= ~BUILTIN_ENABLED;
}
