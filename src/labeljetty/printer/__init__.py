"""TSPL printer library: hardware connection, command generation, headless render.

Self-contained (no dependency on the rest of the application) so it can be reused
or extracted as a standalone package.
"""

from labeljetty.printer.connection import TSPLPrinterConnectionUSB
from labeljetty.printer.network import TSPLPrinterConnectionNetwork
from labeljetty.printer.tspl import JobType, TSPLPrinter, TSPLPrinterStatusMessage

__all__ = [
    "TSPLPrinterConnectionUSB",
    "TSPLPrinterConnectionNetwork",
    "TSPLPrinter",
    "TSPLPrinterStatusMessage",
    "JobType",
]
