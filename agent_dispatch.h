/* agent_dispatch.h — tai agent-dispatch shell-side client.

   v1 wire format: see docs/dispatch-protocol.md.
   Language: see docs/pool-dispatch-operator.md. */

#if !defined (_AGENT_DISPATCH_H_)
#define _AGENT_DISPATCH_H_

#include "stdc.h"

#if defined (AGENT_DISPATCH)

/* Result of a dispatch call. The agent_dispatch client owns the buffers
   it returned non-NULL pointers in; the caller frees them with
   agent_dispatch_free_result. */
typedef struct {
  int ok;			/* 1 on success, 0 on failure (incl. transport). */
  char *error;			/* error code from holo, or NULL. */
  char *agent_instance;		/* agent that handled the dispatch, or NULL. */
  char *output;			/* captured slice when capture=1, or NULL. */
} agent_dispatch_result;

/* Issue a single dispatch. Caller-owned strings (selector / prompt /
   sentinel / block_id) may each be NULL when omitted. timeout_ms < 0
   means "use the holo pool default". capture is 0 or 1.

   Returns 0 on a successful round-trip with the daemon (regardless of
   ok/error inside the response — read result->ok). Returns -1 on a
   transport-level failure (no daemon, refused connection, malformed
   response). On -1 the result struct's ok field is 0 and error is set
   to a synthetic transport-error string. */
extern int agent_dispatch_call (const char *selector,
				int broadcast,
				const char *prompt,
				const char *sentinel,
				const char *block_id,
				int timeout_ms,
				int capture,
				agent_dispatch_result *result);

/* Release a block_id once a do…end block ends. Best-effort: errors
   are not propagated — the daemon's 5-min activity timeout is the
   real cleanup mechanism. */
extern void agent_dispatch_release (const char *block_id);

/* Free strings on a result struct. Safe to call on a zero-init result. */
extern void agent_dispatch_free_result (agent_dispatch_result *result);

/* Generate a fresh UUID-like block id (8 hex chars). Caller frees. */
extern char *agent_dispatch_new_block_id (void);

#endif /* AGENT_DISPATCH */

#endif /* _AGENT_DISPATCH_H_ */
