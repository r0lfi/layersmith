"""Terminal client for LayerSmith: the same server, through a CLI and a TUI.

Everything here talks to the LayerSmith HTTP API. Nothing builds, scans or
stores images locally; the server owns projects, builds, history and files.
"""

from layersmith_client._version import __version__  # noqa: F401
