/* boot.h — Embedded Python boot for the tai control shell + builtins.

   See docs/embedded-holo.md. Two entry points share one Python
   bringup:

   - tai_embedded_serve_stdio() runs the MCP server loop, called from
     tai_control_shell_main() (argv[0]=tcsh).

   - tai_embedded_ensure_ready() lazy-initializes Python without
     entering any loop, called from holo shell builtins (argv[0]=tai).

   Both ultimately call the internal _tai_embedded_init() that does
   Py_InitializeFromConfig + payload cache extraction + sys.path
   setup. It's idempotent — calling it twice is a no-op after the
   first success. */

#if !defined (_TAI_EMBEDDED_BOOT_H_)
#define _TAI_EMBEDDED_BOOT_H_

/* Initialize the embedded Python runtime if it isn't already up,
   then enter HoloMCPServer.serve_stdio() until stdin EOFs. Returns
   the desired process exit code. */
extern int tai_embedded_serve_stdio (void);

/* Initialize the embedded Python runtime if it isn't already up.
   Returns 0 on success (Python ready, tai_runtime importable),
   -1 on failure (diagnostic already written to stderr). Idempotent
   and cheap to call after the first success (~0 ms). First call
   pays the full bringup cost (~80 ms Py_InitializeEx + payload
   extraction on cold cache). */
extern int tai_embedded_ensure_ready (void);

#endif /* _TAI_EMBEDDED_BOOT_H_ */
