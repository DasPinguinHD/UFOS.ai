"""Risk Lab tests.

Path shim: puts ``RiskLab/src`` on ``sys.path`` so that
``python -m unittest discover -s tests`` works from ``RiskLab/`` without installing
the package or setting PYTHONPATH. unittest does not import this file when
``-s tests`` is used without ``-t .``, so every test module repeats the same
three lines before importing ``risklab``.
"""
import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:  # path shim: tests run without installing the package
    sys.path.insert(0, _SRC)
