"""Quickstart: a static normal VaR vs GARCH(1,1)-t on synthetic GARCH-t data, four backtests each.
One synthetic path (seed 1) for illustration; the rejection RATES are in studies/var_backtest_power."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from risklab.backtest import acerbi_szekely_z2, christoffersen_cc, kupiec_pof, traffic_light  # noqa: E402
from risklab.data import garch11  # noqa: E402
from risklab.engine import walk_forward  # noqa: E402
from risklab.models import GarchTModel, NormalModel  # noqa: E402

N_TEST = int(os.environ.get("RISKLAB_QUICKSTART_DAYS", "1000"))  # forecast days (the test uses fewer)
WINDOW, A_VAR, A_ES = 500, 0.99, 0.975

series = garch11(WINDOW + N_TEST, 2e-6, 0.08, 0.90, innovations="t", nu=6.0, seed=1)
print(f"GARCH(1,1)-t6 losses: {WINDOW}-day window, {N_TEST} forecast days, VaR {A_VAR}, ES {A_ES}")
for name, factory in (("static normal", NormalModel), ("GARCH(1,1)-t", GarchTModel)):
    f = walk_forward(series.losses, factory, WINDOW, (A_VAR, A_ES))
    exc = f.exceedance[A_VAR]
    tests = (kupiec_pof(exc, A_VAR), christoffersen_cc(exc, A_VAR),
             acerbi_szekely_z2(f.loss, f.var[A_ES], f.es[A_ES], f.dists, A_ES, seed=1, m_sim=500),
             traffic_light(exc, A_VAR, min(250, len(exc))))
    names = [t.name for t in tests[:3]] + [f"traffic_light (last {min(250, len(exc))} days)"]
    print(f"\n{name}: {int(exc.sum())} exceedances of {len(exc)} (expected {len(exc) * (1 - A_VAR):.0f})")
    for name, t in zip(names, tests):
        p = "" if t.p_value is None else f"p = {t.p_value:.3f}  "
        print(f"  {name:<32} {p}-> {t.decision}")
