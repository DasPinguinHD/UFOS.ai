"""Data sources of the Risk Lab. In the repo: synthetic data only."""

from risklab.data.synthetic import (SyntheticSeries, garch11, iid_normal, iid_student_t,
                                    regime_switching)

__all__ = ["SyntheticSeries", "iid_normal", "iid_student_t", "garch11", "regime_switching"]
