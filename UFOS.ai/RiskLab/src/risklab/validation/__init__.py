"""Independent re-implementation of the UFOS strategy-platform validation gates (SPEC §9) and the
simulator of the gate overfitting study. Nothing is imported from the HedgeFund backend;
``HedgeFund/backend/tests/test_risklab_gate_parity.py`` checks the parity on fixed inputs."""
from . import folds, gates, grid, sim, stats

__all__ = ["folds", "gates", "grid", "sim", "stats"]
