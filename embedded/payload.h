/* payload.h — Trailer format for the tai single-file bundled binary.

   The "bundled" form of tai has its runtime payload (vendored holo,
   site-packages, SikuliX jar, tai_runtime) appended to the end of
   the executable file as a gzip-compressed tarball, followed by a
   24-byte trailer that lets the binary find its own payload at
   startup. See docs/embedded-holo.md and embedded/Makefile's
   `bundle' target.

   File layout:

       [ ... Mach-O / ELF sections ... ]      ← regular executable
       [ payload bytes (tar.gz) ]             ← appended
       [ TAI_PAYLOAD_TRAILER (24 bytes) ]     ← last bytes of file

   The trailer is at a known offset from EOF, so locating the payload
   is a single fseek + fread. A binary that has *not* been bundled
   (e.g. a plain `make' output during dev) will fail the magic check
   and the code path falls back to dev-tree path resolution.

   Layout choice: explicit length even though we could derive it from
   trailer_offset - payload_offset, because (a) it documents intent
   and (b) it catches a torn append (write that got truncated). */

#if !defined (_TAI_PAYLOAD_H_)
#define _TAI_PAYLOAD_H_

#include <stdint.h>

#define TAI_PAYLOAD_MAGIC      "TAIPYLD"
#define TAI_PAYLOAD_MAGIC_LEN  7
#define TAI_PAYLOAD_VERSION    1

/* Total size on disk: exactly 24 bytes. The 8 reserved bytes round
   the trailer up to a multiple of 8 for any aligned-fetch tooling
   and leave room to add a sha256 of the payload later without
   changing the trailer size. */
typedef struct {
  char     magic[TAI_PAYLOAD_MAGIC_LEN]; /*  7  "TAIPYLD" no terminator */
  uint8_t  version;			 /*  1  TAI_PAYLOAD_VERSION    */
  uint64_t offset;			 /*  8  byte offset of payload */
  uint64_t length;			 /*  8  payload length         */
} __attribute__((packed)) tai_payload_trailer_t;

_Static_assert (sizeof (tai_payload_trailer_t) == 24,
		"tai_payload_trailer_t must be exactly 24 bytes");

/* Read the trailer from `exe_path`. Returns 0 on success and fills
   *trailer; returns -1 on any failure (file unreadable, too small,
   magic mismatch, version mismatch, internal consistency check
   fails). Suitable to call before Py_Initialize. */
extern int tai_payload_read_trailer (const char *exe_path,
				     tai_payload_trailer_t *trailer);

#endif /* _TAI_PAYLOAD_H_ */
