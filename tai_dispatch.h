/* tai_dispatch.h — argv[0] dispatch for the tai multi-call binary.

   See docs/embedded-holo.md for the full design. In short:

     basename(argv[0]) == "tcsh"   -> tai_control_shell_main()
                                      (eager MCP server over stdio)
     basename(argv[0]) == "tai"    -> fall through to bash main with
                                      embedded holo available
     anything else                 -> fall through to bash main as
                                      plain bash, no holo. */

#if !defined (_TAI_DISPATCH_H_)
#define _TAI_DISPATCH_H_

#include "stdc.h"

/* Returns 1 if basename(argv0) == "tcsh", else 0. NULL-safe. */
extern int tai_is_control_shell_name PARAMS((const char *argv0));

/* Returns 1 if basename(argv0) == "tai", else 0. NULL-safe. */
extern int tai_is_user_shell_name PARAMS((const char *argv0));

/* Enters the control shell. Discards positional args (per design). Will
   eventually Py_InitializeEx + HoloMCPServer.serve_stdio(); current
   implementation is a stub that prints a diagnostic and exits 0.

   Returns the process exit code. Does not return to bash main. */
extern int tai_control_shell_main PARAMS((int argc, char **argv));

/* Walk the static shell-builtins table and clear BUILTIN_ENABLED on
   every holo* / app_activate / browser_* / screen_* / ui_template_*
   entry. Called from shell.c's main when argv[0] is not "tai" or
   "tcsh" so a bash invocation of the multi-call binary stays
   indistinguishable from upstream bash. The builtin C functions are
   still linked in (link-time-only would require conditional .o),
   but bash's lookup ignores them once the flag is cleared. */
extern void tai_disable_holo_builtins PARAMS((void));

#endif /* _TAI_DISPATCH_H_ */
