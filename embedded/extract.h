/* extract.h — Extract the appended payload into a cached directory.

   Called from embedded/boot.c after embedded/payload.c has confirmed
   the binary is bundled and located its payload. Writes the extracted
   tree to a versioned cache dir under $HOME/Library/Caches/tai/ and
   returns the resolved path in `cache_dir_out`. Subsequent invocations
   of the same binary skip the extraction step (stamp file check).
   See docs/embedded-holo.md and embedded/payload.h. */

#if !defined (_TAI_EMBEDDED_EXTRACT_H_)
#define _TAI_EMBEDDED_EXTRACT_H_

#include <limits.h>

#include "payload.h"

/* Ensure the payload at offset/length inside `exe_path` has been
   extracted to a per-binary cache dir, and write the cache dir's
   absolute path into `cache_dir_out` (which must have room for
   PATH_MAX bytes).

   On first invocation: atomically creates the cache dir, pipes the
   appended payload through `tar -xz`, writes a stamp file, and
   returns 0. On subsequent invocations: notices the stamp, returns
   0 without re-extracting (typically <2 ms).

   Returns 0 on success, -1 on failure. Failure modes include: $HOME
   unset, disk full, `tar` missing from PATH, payload truncated/
   corrupt. On failure the function leaves a diagnostic on stderr —
   the caller's job is just to propagate the error code. */
extern int tai_payload_ensure_extracted (
    const char *exe_path,
    const tai_payload_trailer_t *trailer,
    char *cache_dir_out);

#endif /* _TAI_EMBEDDED_EXTRACT_H_ */
