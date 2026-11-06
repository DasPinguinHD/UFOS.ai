"""Options Structure computations (OS2, SP §21.12): quote quality, own IV / greeks (``bs``), GEX under three dealer
assumptions (``gex``), skew / expected move / term structure / VRP (``surface``), the expiration and event calendar
(``calendar``) and the point-in-time regime service (``regime``) with its own store ``data/options_structure.db``
(``store``). Reads the OS1 archive read-only; nothing here trades or feeds a strategy (OS4 comes later)."""
