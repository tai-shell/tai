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

/* Same loop, but reachable over a single TCP connection on
   bind_addr:port. Single-shot: accept one client, run the magic-
   prefix + optional-token handshake, dup2 the connection over
   fd 0/1, hand off to tai_embedded_serve_stdio(). When the client
   disconnects the process exits.

   Used only by `tcsh --listen PORT [--bind ADDR] [--token T]`.
   bind_addr defaults to "127.0.0.1" at the caller. token may be
   NULL — in which case the handshake only checks the magic
   prefix. The wire format on connect:

       client → TAI/1\n
       client → <token>\n          (only when token != NULL)
       server → OK\n               (or "ERR <reason>\n" + close)
       … MCP framing continues as if it were stdio …

   Returns the same exit code shape as tai_embedded_serve_stdio
   (70 on infrastructure errors, otherwise the Python loop's
   return). */
extern int tai_embedded_serve_tcp (const char *bind_addr,
				   int port,
				   const char *token);

/* Initialize the embedded Python runtime if it isn't already up.
   Returns 0 on success (Python ready, tai_runtime importable),
   -1 on failure (diagnostic already written to stderr). Idempotent
   and cheap to call after the first success (~0 ms). First call
   pays the full bringup cost (~80 ms Py_InitializeEx + payload
   extraction on cold cache). */
extern int tai_embedded_ensure_ready (void);

#endif /* _TAI_EMBEDDED_BOOT_H_ */
