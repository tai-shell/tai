/* payload.c — Trailer lookup for the bundled tai binary.

   The extraction logic (Part B) and path-resolution switch (Part C)
   live in separate translation units. This file only does the read +
   validate step so it can be safely linked into both the bundled and
   non-bundled binary — a plain dev build invokes it, the magic
   mismatches, and the dev-tree code path fires unchanged. */

#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#include "payload.h"

int
tai_payload_read_trailer (const char *exe_path,
			  tai_payload_trailer_t *trailer)
{
  FILE *f = fopen (exe_path, "rb");
  if (f == NULL)
    return -1;

  /* Determine file size — needed to validate the trailer's offset
     and length point at bytes that actually exist in the file. */
  struct stat st;
  if (fstat (fileno (f), &st) != 0)
    {
      fclose (f);
      return -1;
    }
  off_t file_size = st.st_size;
  if (file_size < (off_t) sizeof (tai_payload_trailer_t))
    {
      fclose (f);
      return -1;
    }

  /* Read the last 24 bytes. */
  if (fseeko (f, file_size - (off_t) sizeof (tai_payload_trailer_t),
	      SEEK_SET) != 0)
    {
      fclose (f);
      return -1;
    }
  tai_payload_trailer_t t;
  if (fread (&t, sizeof t, 1, f) != 1)
    {
      fclose (f);
      return -1;
    }
  fclose (f);

  /* Magic + version checks. A non-bundled binary fails here. */
  if (memcmp (t.magic, TAI_PAYLOAD_MAGIC, TAI_PAYLOAD_MAGIC_LEN) != 0)
    return -1;
  if (t.version != TAI_PAYLOAD_VERSION)
    return -1;

  /* Internal consistency: trailer's claimed payload region must
     fit before the trailer itself. Catches a torn append where the
     trailer was written but the payload wasn't, and catches an
     obviously-bogus offset (e.g. an attacker fudging it past EOF). */
  off_t payload_end = (off_t) t.offset + (off_t) t.length;
  off_t trailer_start = file_size - (off_t) sizeof (tai_payload_trailer_t);
  if (payload_end != trailer_start)
    return -1;
  if ((off_t) t.offset < 0 || (off_t) t.offset >= trailer_start)
    return -1;

  *trailer = t;
  return 0;
}
