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
#include "variables.h"
#include "arrayfunc.h"
#include "array.h"
#include "embedded/boot.h"
#include "holo_on_dispatch.h"

/* Build a `host:exit_code' string for one entry of the $ON_HOSTS
   array. Returns a freshly xmalloc'd buffer the caller frees. */
static char *
format_on_hosts_entry (const char *host, long exit_code)
{
  size_t hlen = host ? strlen (host) : 1;
  /* worst-case int decimal + sign + ':' + NUL */
  size_t buflen = hlen + 16;
  char *buf = (char *)xmalloc (buflen);
  snprintf (buf, buflen, "%s:%ld", host ? host : "?", exit_code);
  return buf;
}

/* Walk the "hosts" key of the dispatch result dict and bind the
   per-host (host:exit) entries as the $ON_HOSTS bash array. The
   array is overwritten if it already exists -- each `on' call
   replaces it, mirroring how $PIPESTATUS replaces itself per
   pipeline (see variables.c:6299). Silent on any per-entry
   malformation; bash array remains the partial result. */
static void
bind_on_hosts_array (PyObject *hosts_list)
{
  if (hosts_list == NULL || !PyList_Check (hosts_list))
    return;

  SHELL_VAR *v = find_variable ("ON_HOSTS");
  if (v != NULL && array_p (v) == 0)
    {
      /* User had a non-array ON_HOSTS in scope -- unset it so we
	 can replace with an array. */
      unbind_variable ("ON_HOSTS");
      v = NULL;
    }
  if (v == NULL)
    v = make_new_array_variable ("ON_HOSTS");
  if (v == NULL || array_p (v) == 0)
    return;

  ARRAY *a = array_cell (v);
  array_flush (a);

  Py_ssize_t n = PyList_Size (hosts_list);
  for (Py_ssize_t i = 0; i < n; i++)
    {
      PyObject *entry = PyList_GetItem (hosts_list, i);	/* borrowed */
      if (entry == NULL || !PyDict_Check (entry))
	continue;
      PyObject *py_host = PyDict_GetItemString (entry, "host");
      PyObject *py_exit = PyDict_GetItemString (entry, "exit");
      if (py_host == NULL || py_exit == NULL)
	continue;
      const char *host_s = PyUnicode_AsUTF8 (py_host);
      long exit_l = PyLong_AsLong (py_exit);
      if (host_s == NULL || (exit_l == -1 && PyErr_Occurred ()))
	{
	  PyErr_Clear ();
	  continue;
	}
      char *entry_str = format_on_hosts_entry (host_s, exit_l);
      array_insert (a, (arrayind_t) i, entry_str);
      free (entry_str);
    }
}

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

  /* dispatch_from_c (selector_str: str, body: str) -> dict.
     Returns {"exit": int, "hosts": [{"host", "resource", "exit"}, ...]}.
     The "exit" maps to the shell command's $?; "hosts" gets bound
     as the $ON_HOSTS bash array (Phase 3.D.3) so scripts can
     introspect per-target outcomes. */
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

  if (!PyDict_Check (result))
    {
      Py_DECREF (result);
      fprintf (stderr,
	       "tai: `on': dispatch_from_c returned non-dict\n");
      return EXECUTION_FAILURE;
    }

  PyObject *py_exit = PyDict_GetItemString (result, "exit");
  PyObject *py_hosts = PyDict_GetItemString (result, "hosts");
  long rc = py_exit ? PyLong_AsLong (py_exit) : -1;
  if (rc == -1 && PyErr_Occurred ())
    {
      PyErr_Print ();
      Py_DECREF (result);
      return EXECUTION_FAILURE;
    }

  bind_on_hosts_array (py_hosts);

  Py_DECREF (result);

  /* Python returns 0 / 1 / 2 -- map directly to shell exit codes. */
  return (int) rc;
}

#endif /* AGENT_DISPATCH */
