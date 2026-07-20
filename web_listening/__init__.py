"""Web Listening - monitor websites for changes and download documents."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("web-listening")
except PackageNotFoundError:  # Source tree imported before installation.
    __version__ = "0+unknown"
