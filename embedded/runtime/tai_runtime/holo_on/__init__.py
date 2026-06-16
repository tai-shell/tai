"""Phase 3.A: standalone Python dispatch runtime for tai's ``on`` keyword.

The ``on`` keyword (planned for Phase 3.B in ``parse.y``) is bash-level
sugar for "run this shell body on every holo daemon matching this
selector." The dispatch logic — selector parsing, mDNS discovery, MCP
exec invocation, output collection — is all Python. This module is
that logic, exposed as a standalone CLI (``python -m
tai_runtime.holo_on``) so it can be validated end-to-end against a
live holo daemon BEFORE the bash-side keyword integration touches
``parse.y``.

Phase 3.B will wire the bash keyword to call into this module via the
embedded interpreter. Until then the CLI is the only entry point.

Spec: ``holo/docs/resources.md`` (the cross-system design doc).
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
