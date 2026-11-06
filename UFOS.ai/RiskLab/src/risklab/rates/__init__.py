"""Interest rate risk in the banking book (IRRBB): yield curves (``curve``), banking-book instruments and a
synthetic balance sheet (``instruments``), and the risk measures, standardised shocks and outlier tests
(``irrbb``). Synthetic data only."""
from . import curve, instruments, irrbb

__all__ = ["curve", "instruments", "irrbb"]
