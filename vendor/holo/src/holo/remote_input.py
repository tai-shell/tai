"""Backward-compat shim. Re-exports from `holo.remote_backend`.

The class was renamed from `RemoteInputBackend` to `RemoteHoloBackend`
when capture-side proxying was added (the original name described only
the input side; the class now handles both). Existing imports of
`RemoteInputBackend` / `RemoteInputError` keep working via the aliases
below.
"""

from holo.remote_backend import RemoteHoloBackend as RemoteInputBackend
from holo.remote_backend import RemoteHoloError as RemoteInputError

__all__ = ["RemoteInputBackend", "RemoteInputError"]
