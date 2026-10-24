"""Options Structure archive (SP §21, OS1): an independent data-collection process for SPY / QQQ options and the FRED
VIX series — point-in-time stamped, gaps recorded, never filled. Started by a Windows scheduled task the USER
registers (⚙ Keys & data → Data sources); no listening port, no trading, no AI.

Modules: ``config`` (every provisional number), ``transport`` (GET allow-list, shared with tools/verify_alpaca_options),
``db`` (``data/options_archive.db``), ``slots`` (NY slot plan on the NYSE calendar), ``collector`` (one run),
``report`` (gap report + status file), ``service`` (the long-running loop, single-instance lock, log).

Part of UFOS.ai, a proof-of-concept portfolio project; data collection only.
"""
