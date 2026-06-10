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
  /* Positional args are discarded per design (the name is the whole
     signal). Hand off to the embedded-Python serve loop. */
  (void) argc;
  (void) argv;
  return tai_embedded_serve_stdio ();
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
