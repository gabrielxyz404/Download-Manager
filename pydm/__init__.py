"""PyDM - a free multi-connection download manager."""

from .engine import DownloadTask, Status, probe
from .manager import DownloadManager

__all__ = ["DownloadTask", "DownloadManager", "Status", "probe"]
__version__ = "0.1.0"
