"""Memory Vault package."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("memory-vault")
except PackageNotFoundError:
    __version__ = "unknown"

__all__ = ["__version__"]
