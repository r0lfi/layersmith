"""Client version.

Like the server, the version is not maintained by hand: a release build
stamps it from the tag (scripts/build-release.sh). A source checkout reports a
development version so it can never pass for a release.
"""

__version__ = "0.0.0.dev0"
