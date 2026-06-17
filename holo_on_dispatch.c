/* holo_on_dispatch.c -- C bridge from tai's `on' keyword to the
   Python dispatch runtime at tai_runtime.holo_on.dispatch.

   When execute_on_dispatch_command (execute_cmd.c) fires for a
   cm_on_dispatch AST node, it calls holo_on_dispatch_call with the
   parsed selector + body + (optional) sentinel. This file is the
   thin C->Python bridge: lazy-init the embedded CPython, import
   the dispatch module, call dispatch_from_c (which uses sys.stdout
   / sys.stderr for output), unwrap the int return value back to
   the shell as the command's exit code.

   The pattern mirrors builtins/holo.c which already does C->Python
   for the holo MCP tools, so error handling, refcounting, and the
   sys.path setup are reused via the same tai_embedded_ensure_ready
   entry point.
*/

#include <config.h>

#include <stdio.h>
#include <string.h>

#if defined (AGENT_DISPATCH)

#define PY_SSIZE_T_CLEAN
#include <Python.h>

#include "bashansi.h"
#include "shell.h"
#include "embedded/boot.h"
#include "holo_on_dispatch.h"

int
holo_on_dispatch_call (const char *selector_str,
		       const char *body,
		       const char *sentinel)
{
  /* sentinel is captured by the parser (parse.y) and would carry
     the /done semantic, but the Python dispatch doesn't act on it
     yet -- that lands in Phase 3.D alongside modes and $ON_HOSTS.
     Accept it for forward-compat and silence unused warnings. */
  (void)sentinel;

  if (selector_str == NULL)
    selector_str = "";
  if (body == NULL)
    body = "";

  /* ~80 ms on first call (interpreter init, sys.path setup, etc.);
     ~0 ms thereafter. Same lazy-init pattern as builtins/holo.c. */
  if (tai_embedded_ensure_ready () != 0)
    {
      fprintf (stderr,
	       "tai: `on': embedded CPython init failed\n");
      return EXECUTION_FAILURE;
    }

  PyObject *mod = PyImport_ImportModule (
      "tai_runtime.holo_on.dispatch");
  if (mod == NULL)
    {
      PyErr_Print ();
      fprintf (stderr,
	       "tai: `on': failed to import "
	       "tai_runtime.holo_on.dispatch\n");
      return EXECUTION_FAILURE;
    }

  /* dispatch_from_c (selector_str: str, body: str) -> int.
     Format spec "(ss)" packs the two strings into a positional-args
     tuple and PyObject_CallMethod takes care of decref. */
  PyObject *result = PyObject_CallMethod (
      mod, "dispatch_from_c", "(ss)", selector_str, body);
  Py_DECREF (mod);

  if (result == NULL)
    {
      PyErr_Print ();
      fprintf (stderr,
	       "tai: `on': dispatch_from_c raised\n");
      return EXECUTION_FAILURE;
    }

  long rc = PyLong_AsLong (result);
  Py_DECREF (result);
  if (rc == -1 && PyErr_Occurred ())
    {
      PyErr_Print ();
      return EXECUTION_FAILURE;
    }

  /* Python returns 0 / 1 / 2 -- map directly to shell exit codes. */
  return (int) rc;
}

#endif /* AGENT_DISPATCH */
