"""Main package for the Flockwave GPS module."""

from .configurator import Configuration
from .daemon_app import DaemonApp
from .version import __version__, __version_info__

__all__ = (
    "__version__",
    "__version_info__",
    "Configuration",
    "DaemonApp",
)
