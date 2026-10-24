"""Context-specific notices (SPEC §15 "Stage 10 addition", stage 10 S10-H; the WPF counterpart is
``Shared/AiContent.cs``). They replace the blanket "not investment advice" in user-facing backend texts. The three
AI-context notices are word-for-word the AiContent texts, because the app shows payload notices verbatim:

- (a) ``ANALYSIS``: AI analyses / explanations / commentaries;
- (b) ``RESEARCH``: AI research output (Studio replies, paper → strategy, committee memos, loop finalists);
- (c) ``PAPER_EXECUTION``: the automated paper trading views (portfolio, brokers, monitoring);
- (d) a live-trading notice exists only if live trading is ever enabled (stage 12) — deliberately not defined here.

Short context lines for event / alert bodies: ``PAPER`` (paper contexts), ``PAPER_DEMO`` (broker / demo contexts),
``HYPOTHETICAL`` (backtests, validations, shadow rule portfolios), ``REVIEW`` (Shadow statistics of your own trades).

Part of UFOS.ai, a proof-of-concept portfolio project.
"""
ANALYSIS = "AI-generated analysis — may be inaccurate; information, not a recommendation."
RESEARCH = ("AI-generated research output for your own decision. Not a personal recommendation by a licensed advisor; "
            "you decide and carry the risk. Backtests are hypothetical.")
PAPER_EXECUTION = ("Automated trading on your own paper account under the limits you set. Paper only — no real "
                   "money.")
PAPER = "Paper trading · simulated money."
PAPER_DEMO = "Paper/demo trading · simulated money."
HYPOTHETICAL = "Hypothetical, simulated results — they do not indicate future results."
REVIEW = "For your own review."
