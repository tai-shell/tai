/* agent_dispatch.c — tai shell-side client for the dispatch protocol.

   Sends one HTTP/1.0 POST to the holo daemon (default
   http://localhost:7082, override via $TAI_DISPATCH_URL) per `@'
   invocation, parses the JSON response, and returns the result.

   v1 implementation choices:

     - Pure C, no external library dependencies. Uses bash's
       netopen("/dev/tcp/host/port") for the socket, hand-rolled
       string concat for the request body, hand-rolled JSON scanning
       for the response.
     - HTTP/1.0 with Connection: close. One round-trip per call.
       Avoids keep-alive / chunked-encoding parsing.
     - Response parsing is shape-aware, not a general JSON parser:
       holo's response shape is fixed (see docs/dispatch-protocol.md).
       Robust to whitespace and field reordering; not robust to
       weirdly-encoded escape sequences in field values. Holo's
       implementation must keep responses clean — this is the
       protocol contract.

   See docs/dispatch-protocol.md for the wire format and the
   accompanying language spec at docs/pool-dispatch-operator.md. */

#include "config.h"

#include "bashtypes.h"
#include "bashansi.h"

#if defined (HAVE_UNISTD_H)
#  include <unistd.h>
#endif

#include <stdio.h>
#include <errno.h>
#include <ctype.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "shell.h"
#include "xmalloc.h"
#include "externs.h"
#include "agent_dispatch.h"

#if defined (AGENT_DISPATCH)

/* Default holo dispatch endpoint. Override with $TAI_DISPATCH_URL. */
#define DEFAULT_DISPATCH_HOST "127.0.0.1"
#define DEFAULT_DISPATCH_PORT "7082"

/* --------------------------------------------------------------- */
/* small expandable string helper                                  */
/* --------------------------------------------------------------- */

typedef struct {
  char *buf;
  size_t len;
  size_t cap;
} sbuf;

static void
sbuf_init (sbuf *s)
{
  s->cap = 256;
  s->len = 0;
  s->buf = (char *)xmalloc (s->cap);
  s->buf[0] = '\0';
}

static void
sbuf_grow (sbuf *s, size_t need)
{
  if (s->len + need + 1 > s->cap)
    {
      while (s->len + need + 1 > s->cap)
	s->cap *= 2;
      s->buf = (char *)xrealloc (s->buf, s->cap);
    }
}

static void
sbuf_putc (sbuf *s, int c)
{
  sbuf_grow (s, 1);
  s->buf[s->len++] = (char)c;
  s->buf[s->len] = '\0';
}

static void
sbuf_puts (sbuf *s, const char *p)
{
  size_t n = strlen (p);
  sbuf_grow (s, n);
  memcpy (s->buf + s->len, p, n);
  s->len += n;
  s->buf[s->len] = '\0';
}

static void
sbuf_free (sbuf *s)
{
  if (s->buf) free (s->buf);
  s->buf = NULL;
  s->len = s->cap = 0;
}

/* --------------------------------------------------------------- */
/* JSON                                                            */
/* --------------------------------------------------------------- */

static void
json_emit_string (sbuf *out, const char *s)
{
  sbuf_putc (out, '"');
  if (s)
    {
      const unsigned char *p = (const unsigned char *)s;
      for (; *p; p++)
	{
	  switch (*p)
	    {
	    case '"':  sbuf_puts (out, "\\\""); break;
	    case '\\': sbuf_puts (out, "\\\\"); break;
	    case '\n': sbuf_puts (out, "\\n"); break;
	    case '\r': sbuf_puts (out, "\\r"); break;
	    case '\t': sbuf_puts (out, "\\t"); break;
	    case '\b': sbuf_puts (out, "\\b"); break;
	    case '\f': sbuf_puts (out, "\\f"); break;
	    default:
	      if (*p < 0x20)
		{
		  char esc[8];
		  snprintf (esc, sizeof esc, "\\u%04x", *p);
		  sbuf_puts (out, esc);
		}
	      else
		sbuf_putc (out, *p);
	    }
	}
    }
  sbuf_putc (out, '"');
}

static void
json_emit_field (sbuf *out, const char *name, const char *value, int comma_before)
{
  if (comma_before) sbuf_putc (out, ',');
  json_emit_string (out, name);
  sbuf_putc (out, ':');
  if (value)
    json_emit_string (out, value);
  else
    sbuf_puts (out, "null");
}

static void
json_emit_bool (sbuf *out, const char *name, int value, int comma_before)
{
  if (comma_before) sbuf_putc (out, ',');
  json_emit_string (out, name);
  sbuf_putc (out, ':');
  sbuf_puts (out, value ? "true" : "false");
}

static void
json_emit_int_or_null (sbuf *out, const char *name, int value, int has_value, int comma_before)
{
  if (comma_before) sbuf_putc (out, ',');
  json_emit_string (out, name);
  sbuf_putc (out, ':');
  if (has_value)
    {
      char num[24];
      snprintf (num, sizeof num, "%d", value);
      sbuf_puts (out, num);
    }
  else
    sbuf_puts (out, "null");
}

/* Shape-aware response scanner. Looks for `"<key>":<value>' where
   <value> is either a quoted string (decoded) or true/false/null.
   Returns 1 if found, 0 otherwise. For string fields, *out is
   xmalloc'd; caller frees. For boolean fields *out is unchanged and
   *bool_out is set. */
static int
json_find_value (const char *body, const char *key, char **str_out, int *bool_out)
{
  size_t klen = strlen (key);
  const char *p = body;

  while ((p = strchr (p, '"')) != NULL)
    {
      const char *q = p + 1;
      const char *qend = strchr (q, '"');
      if (!qend) break;
      if ((size_t)(qend - q) == klen && memcmp (q, key, klen) == 0)
	{
	  /* Found "<key>". Skip whitespace and `:'. */
	  const char *r = qend + 1;
	  while (*r == ' ' || *r == '\t') r++;
	  if (*r != ':') { p = qend + 1; continue; }
	  r++;
	  while (*r == ' ' || *r == '\t' || *r == '\n') r++;
	  if (*r == '"' && str_out)
	    {
	      /* Decode a string. */
	      sbuf decoded; sbuf_init (&decoded);
	      r++;
	      while (*r && *r != '"')
		{
		  if (*r == '\\' && r[1])
		    {
		      switch (r[1])
			{
			case '"':  sbuf_putc (&decoded, '"'); r += 2; break;
			case '\\': sbuf_putc (&decoded, '\\'); r += 2; break;
			case '/':  sbuf_putc (&decoded, '/'); r += 2; break;
			case 'n':  sbuf_putc (&decoded, '\n'); r += 2; break;
			case 'r':  sbuf_putc (&decoded, '\r'); r += 2; break;
			case 't':  sbuf_putc (&decoded, '\t'); r += 2; break;
			case 'b':  sbuf_putc (&decoded, '\b'); r += 2; break;
			case 'f':  sbuf_putc (&decoded, '\f'); r += 2; break;
			case 'u':
			  /* Best effort: skip \uXXXX; tai doesn't use
			     wide-char fields in v1. Emits a `?' so the
			     value isn't silently empty. */
			  if (isxdigit ((unsigned char)r[2]) &&
			      isxdigit ((unsigned char)r[3]) &&
			      isxdigit ((unsigned char)r[4]) &&
			      isxdigit ((unsigned char)r[5]))
			    { sbuf_putc (&decoded, '?'); r += 6; }
			  else
			    { sbuf_putc (&decoded, '\\'); r++; }
			  break;
			default:
			  sbuf_putc (&decoded, r[1]); r += 2;
			}
		    }
		  else
		    sbuf_putc (&decoded, *r++);
		}
	      *str_out = decoded.buf;	/* hand off ownership */
	      return 1;
	    }
	  if (bool_out)
	    {
	      if (strncmp (r, "true", 4) == 0) { *bool_out = 1; return 1; }
	      if (strncmp (r, "false", 5) == 0) { *bool_out = 0; return 1; }
	    }
	  return 0;
	}
      p = qend + 1;
    }
  return 0;
}

/* --------------------------------------------------------------- */
/* HTTP                                                            */
/* --------------------------------------------------------------- */

/* Read the entire fd into an sbuf. Returns 0 on EOF, -1 on read
   error. */
static int
read_all (int fd, sbuf *out)
{
  char chunk[4096];
  ssize_t n;
  while ((n = read (fd, chunk, sizeof chunk)) > 0)
    {
      sbuf_grow (out, (size_t)n);
      memcpy (out->buf + out->len, chunk, (size_t)n);
      out->len += (size_t)n;
      out->buf[out->len] = '\0';
    }
  return (n < 0) ? -1 : 0;
}

/* Build the bash-netopen path /dev/tcp/<host>/<port> from the
   $TAI_DISPATCH_URL env var (or fall back to defaults). Caller
   frees. */
static char *
build_netopen_path (void)
{
  const char *url = getenv ("TAI_DISPATCH_URL");
  const char *host = DEFAULT_DISPATCH_HOST;
  const char *port = DEFAULT_DISPATCH_PORT;
  char *host_buf = NULL, *port_buf = NULL;
  char *path;

  if (url && *url)
    {
      /* Accept http://HOST:PORT or HOST:PORT. */
      const char *p = url;
      if (strncmp (p, "http://", 7) == 0) p += 7;
      const char *colon = strchr (p, ':');
      const char *slash = strchr (p, '/');
      const char *end = slash ? slash : p + strlen (p);
      if (colon && colon < end)
	{
	  host_buf = (char *)xmalloc ((size_t)(colon - p) + 1);
	  memcpy (host_buf, p, colon - p);
	  host_buf[colon - p] = '\0';
	  port_buf = (char *)xmalloc ((size_t)(end - colon - 1) + 1);
	  memcpy (port_buf, colon + 1, end - colon - 1);
	  port_buf[end - colon - 1] = '\0';
	  host = host_buf; port = port_buf;
	}
      else if (end > p)
	{
	  host_buf = (char *)xmalloc ((size_t)(end - p) + 1);
	  memcpy (host_buf, p, end - p);
	  host_buf[end - p] = '\0';
	  host = host_buf;
	}
    }

  size_t n = strlen ("/dev/tcp/") + strlen (host) + 1 + strlen (port) + 1;
  path = (char *)xmalloc (n);
  snprintf (path, n, "/dev/tcp/%s/%s", host, port);

  if (host_buf) free (host_buf);
  if (port_buf) free (port_buf);
  return path;
}

/* POST `body' to `endpoint' (e.g. "/dispatch") on the configured
   holo daemon. Returns 0 on transport success (response stored in
   *response_body, caller frees), -1 on transport failure. */
static int
http_post (const char *endpoint, const char *body, char **response_body)
{
  char *path = build_netopen_path ();
  int fd = netopen (path);
  free (path);
  if (fd < 0)
    return -1;

  size_t body_len = strlen (body);
  sbuf req; sbuf_init (&req);
  sbuf_puts (&req, "POST ");
  sbuf_puts (&req, endpoint);
  sbuf_puts (&req, " HTTP/1.0\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: ");
  {
    char num[24];
    snprintf (num, sizeof num, "%zu", body_len);
    sbuf_puts (&req, num);
  }
  sbuf_puts (&req, "\r\nConnection: close\r\n\r\n");
  sbuf_grow (&req, body_len);
  memcpy (req.buf + req.len, body, body_len);
  req.len += body_len;
  req.buf[req.len] = '\0';

  size_t off = 0;
  while (off < req.len)
    {
      ssize_t n = write (fd, req.buf + off, req.len - off);
      if (n < 0)
	{
	  if (errno == EINTR) continue;
	  sbuf_free (&req);
	  close (fd);
	  return -1;
	}
      off += (size_t)n;
    }
  sbuf_free (&req);

  sbuf resp; sbuf_init (&resp);
  if (read_all (fd, &resp) < 0)
    {
      sbuf_free (&resp);
      close (fd);
      return -1;
    }
  close (fd);

  /* Find end of headers. */
  char *body_start = strstr (resp.buf, "\r\n\r\n");
  if (body_start) body_start += 4;
  else
    {
      body_start = strstr (resp.buf, "\n\n");
      if (body_start) body_start += 2;
    }
  if (!body_start)
    {
      sbuf_free (&resp);
      return -1;
    }

  *response_body = savestring (body_start);
  sbuf_free (&resp);
  return 0;
}

/* --------------------------------------------------------------- */
/* Public API                                                      */
/* --------------------------------------------------------------- */

void
agent_dispatch_free_result (agent_dispatch_result *result)
{
  if (!result) return;
  FREE (result->error);
  FREE (result->agent_instance);
  FREE (result->output);
  result->error = result->agent_instance = result->output = NULL;
}

char *
agent_dispatch_new_block_id (void)
{
  /* 8 hex chars from a mix of time + pid + getpid-relative counter.
     Collision-resistant within one shell. */
  static unsigned long counter = 0;
  unsigned long stamp = (unsigned long)time (NULL) ^ (unsigned long)getpid ()
    ^ (counter++ << 8);
  char *out = (char *)xmalloc (9);
  snprintf (out, 9, "%08lx", stamp & 0xffffffffUL);
  return out;
}

int
agent_dispatch_call (const char *selector,
		     int broadcast,
		     const char *prompt,
		     const char *sentinel,
		     const char *block_id,
		     int timeout_ms,
		     int capture,
		     agent_dispatch_result *result)
{
  sbuf body; sbuf_init (&body);
  sbuf_putc (&body, '{');
  json_emit_field (&body, "selector", selector, 0);
  json_emit_bool (&body, "broadcast", broadcast, 1);
  json_emit_field (&body, "prompt", prompt ? prompt : "", 1);
  json_emit_field (&body, "sentinel", sentinel, 1);
  json_emit_field (&body, "block_id", block_id, 1);
  json_emit_int_or_null (&body, "timeout_ms", timeout_ms, timeout_ms >= 0, 1);
  json_emit_bool (&body, "capture", capture, 1);
  /* version */
  sbuf_puts (&body, ",\"v\":1}");

  char *resp_body = NULL;
  int rc = http_post ("/dispatch", body.buf, &resp_body);
  sbuf_free (&body);

  if (rc < 0)
    {
      result->ok = 0;
      result->error = savestring ("transport");
      result->agent_instance = NULL;
      result->output = NULL;
      return -1;
    }

  /* Parse response. */
  result->ok = 0;
  result->error = NULL;
  result->agent_instance = NULL;
  result->output = NULL;

  int ok_val = 0;
  json_find_value (resp_body, "ok", NULL, &ok_val);
  result->ok = ok_val;

  json_find_value (resp_body, "error", &result->error, NULL);
  json_find_value (resp_body, "agent_instance", &result->agent_instance, NULL);
  if (capture)
    json_find_value (resp_body, "output", &result->output, NULL);

  free (resp_body);
  return 0;
}

void
agent_dispatch_release (const char *block_id)
{
  if (!block_id) return;
  sbuf body; sbuf_init (&body);
  sbuf_puts (&body, "{\"v\":1,\"block_id\":");
  json_emit_string (&body, block_id);
  sbuf_putc (&body, '}');
  char *resp_body = NULL;
  (void)http_post ("/dispatch/release", body.buf, &resp_body);
  sbuf_free (&body);
  FREE (resp_body);
}

#endif /* AGENT_DISPATCH */
