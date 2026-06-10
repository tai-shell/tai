"""tai control-shell runtime — the Python side of `tcsh` invocation.

Lives in embedded/runtime/ in the tai source tree. Added to sys.path
by embedded/boot.c at startup. Eventually frozen into the binary;
on-disk import is the dev path.
"""
