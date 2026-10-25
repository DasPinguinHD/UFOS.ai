# Council of AIs on the new platform — what will behave differently (for your approval)

When the Council of AIs moves from the old engine onto the platform, its decisions stay the same: the same agents,
the same AI judge, the same sizing rules. A side-by-side test checked this over 60 trading days (768 runs, 414
orders): every signal, verdict, position size, order and AI cost matched exactly.

A few things change on purpose. Please confirm this list (yes / no per point is fine).

1. **No stock orders while the market is closed.**
   - Old engine: it queued stock and ETF orders overnight or at the weekend, and they filled at the next open.
   - New: outside market hours, and in the first 5 minutes after the open, the Council only analyses stocks. The
     run records "market closed — no order placed".
   - Crypto trades around the clock as before.
   - Why: this is the platform's normal rule (you chose it as option B). Overnight orders fill at the opening
     price, which nobody has seen yet.

2. **The first run of the day starts 5 minutes after the open.**
   - Old engine: 5 seconds after the open. New: 5 minutes and 5 seconds after.
   - Why: this follows from point 1. The opening minutes are the most erratic.

3. **No waiting stock orders that get replaced.**
   - Old engine: it could replace a queued overnight stock order with a newer one.
   - New: there are no queued stock orders (point 1), so this cannot happen.

4. **Flipping a stock position while the market is closed.**
   - Old engine: it queued the first half of the flip (for example "sell the long") overnight.
   - New: it records the decision and places no order until the market is open.

5. **Unfilled crypto orders expire after a fixed 60 minutes.**
   - Old engine: this cleanup ran only in its overnight crypto check, and the limit depended on that check's interval.
   - New: always 60 minutes, at any time of day.

6. **No separate overnight crypto check.**
   - Old engine: a light crypto check ran every 30 minutes outside market hours.
   - New: the platform's background job covers margin, funding, fills and the drawdown stop for every account, all
     the time.

7. **Platform safety checks before every order.**
   - Old engine: no extra checks.
   - New: each order goes through the platform's pre-trade checks, the same ones every strategy gets. An order
     those checks block is recorded as "not executed" with the reason.

8. **Some status names change.**
   - The platform has its own names for order states, log entries and events.
   - For example, "autopilot switched off" becomes "auto trading is off" or "Council orders are off", and "engine
     halted" becomes "the account is halted".
   - The meaning stays the same.

9. **Features that are not carried over.**
   - Accepting a recommendation by hand, the CSV exports of the old engine, the old backtest button and the old
     pop-up notifications.
   - Notifications now come through the platform's alerts.
   - The old history is kept as a one-time export file.

Not a difference, but worth knowing: the Council is labelled **"not validated — AI strategy"** everywhere, because
it does not go through the platform's validation gates.
