"""Single source of truth for the package version.

The release workflow checks the pushed tag against this value before publishing,
so a mismatched tag fails the build rather than shipping a mislabelled wheel.
"""

__version__ = "0.1.0"

#: Alias kept for readability at the call sites that build the ``User-Agent``.
VERSION = __version__
