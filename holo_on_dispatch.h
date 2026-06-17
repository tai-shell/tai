/* holo_on_dispatch.h -- C bridge to tai_runtime.holo_on.dispatch.

   Header for the Phase 3.B.3 Python bridge invoked by
   execute_on_dispatch_command (execute_cmd.c) when an `on'
   command fires. See holo/docs/resources.md for the full
   semantics.
*/

#ifndef _HOLO_ON_DISPATCH_H_
#define _HOLO_ON_DISPATCH_H_

#if defined (AGENT_DISPATCH)

/* Dispatch a single `on' command body to the holo daemons matching
   selector_str. Output streams from the dispatched body are written
   to the tai process's stdout / stderr by the Python runtime (which
   uses sys.stdout / sys.stderr). selector_str / body / sentinel are
   the parsed pieces from the AST node.

   Returns the resolved exit code (max of per-host exits, or 2 on
   selector parse failure, etc.). Lazy-initialises the embedded
   CPython interpreter on first call.

   sentinel is currently informational only -- the Python dispatcher
   doesn't yet act on /done semantics, that's Phase 3.D. */
extern int holo_on_dispatch_call (const char *selector_str,
				  const char *body,
				  const char *sentinel);

#endif /* AGENT_DISPATCH */

#endif /* _HOLO_ON_DISPATCH_H_ */
