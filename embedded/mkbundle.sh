#!/usr/bin/env bash
# mkbundle.sh — Produce a tai binary with its runtime payload appended.
#
# Usage: mkbundle.sh <input-bash> <output-bash> <payload-staging-dir>
#
# The staging dir is expected to have already been laid out by the
# Makefile `bundle` target — this script just tars it, appends to a
# copy of the input binary, and writes the 24-byte trailer.
#
# Trailer layout matches embedded/payload.h:
#   bytes 0-6 : magic "TAIPYLD"  (7 bytes, no terminator)
#   byte  7   : version          (1 byte, currently 1)
#   bytes 8-15: payload offset   (uint64 LE)
#   bytes 16-23: payload length  (uint64 LE)

set -euo pipefail

input_bin="$1"
output_bin="$2"
staging_dir="$3"

if [ ! -f "$input_bin" ]; then
    echo "mkbundle.sh: input binary not found: $input_bin" >&2
    exit 1
fi
if [ ! -d "$staging_dir" ]; then
    echo "mkbundle.sh: staging dir not found: $staging_dir" >&2
    exit 1
fi

# Step 1: build the tarball from the staging dir.
# -C cd's into the dir so paths in the archive are relative (no
# absolute paths leaking from the dev machine). gzip -1 is fast and
# the resulting file is already large; -9 would save ~10% at a
# noticeable wallclock cost.
tarball="$(mktemp -t tai-bundle-XXXXXX.tar.gz)"
trap 'rm -f "$tarball"' EXIT
(cd "$staging_dir" && tar -czf "$tarball" .)

payload_bytes=$(stat -f %z "$tarball" 2>/dev/null \
    || stat -c %s "$tarball")
input_bytes=$(stat -f %z "$input_bin" 2>/dev/null \
    || stat -c %s "$input_bin")

# Step 2: assemble output = input + payload + trailer.
cp "$input_bin" "$output_bin"
cat "$tarball" >> "$output_bin"

# Step 3: trailer. Use printf for the 8 bytes of magic + version, and
# perl one-liners to emit two little-endian uint64s. (`printf' with
# `%016x' would give us hex, not raw bytes; this is the cleanest
# portable way without writing a small C program for it.)
{
    printf 'TAIPYLD\1'                   # 7 magic bytes + 1 version byte
    perl -e 'print pack("Q<", '"$input_bytes"')'    # payload offset
    perl -e 'print pack("Q<", '"$payload_bytes"')'  # payload length
} >> "$output_bin"

output_bytes=$(stat -f %z "$output_bin" 2>/dev/null \
    || stat -c %s "$output_bin")
expected=$((input_bytes + payload_bytes + 24))
if [ "$output_bytes" -ne "$expected" ]; then
    echo "mkbundle.sh: output size $output_bytes != expected $expected" >&2
    exit 1
fi

cat <<EOF
==> Bundled: $output_bin
    input  : $input_bytes bytes
    payload: $payload_bytes bytes  (tarball of $staging_dir)
    trailer: 24 bytes
    total  : $output_bytes bytes
EOF
