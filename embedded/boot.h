/* boot.h — Embedded Python boot/serve hook for the tai control shell.

   See docs/embedded-holo.md. Called from tai_control_shell_main()
   after argv[0] dispatch has selected the tcsh role. */

#if !defined (_TAI_EMBEDDED_BOOT_H_)
#define _TAI_EMBEDDED_BOOT_H_

/* Initialize the embedded Python runtime, hand stdin/stdout to the
   holo MCP server loop, and tear down on exit. Returns the desired
   process exit code. Safe to call only once per process.

   Current implementation is a smoke test: it brings up the interpreter,
   prints a marker line to stderr, and exits. A future commit replaces
   the body with HoloMCPServer.serve_stdio(). */
extern int tai_embedded_serve_stdio (void);

#endif /* _TAI_EMBEDDED_BOOT_H_ */
