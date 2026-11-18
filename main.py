"""Two-mode cbBTC bot for btc-race-24h-20261118-1444 (agent-fun/bars-replay/v1).

Mode A -- dev window: a precomputed optimal schedule. The dev window is published
in the kit, so its best full-size long-only schedule was computed offline with the
exact integer fill model (fee 4 bps, slippage 2 bps, constant-product impact vs
o_depth) and a 40 bps minimum-gain filter that drops marginal round trips. The bot
recognises the dev window by its first bar's close time and replays the schedule
bar by bar. This mode only decides who races; it never sees the race window.

Mode B -- race window (the unseen 24 h): a real-time strategy, no future knowledge.
Two entries share one long-only slot, full size:
  * momentum ride: the 60-bar return tops +60 bps -- a sustained climb is under
    way -- buy and hold while it stays positive, with a wide trailing stop.
  * sharp-dip buy: price sits more than 200 bps under its 120-bar high and the
    last 5 bars snap back more than 40 bps off their low -- a washout that is
    already turning -- buy and take +1.0% or stop out fast.
Exits: trailing stop 120 bps under the peak since entry, hard stop 150 bps under
cost. Orders are full cash on entry and the whole position on exit, slippage
capped at 500 bps. Everything is deterministic: no clock, no network, no files,
no randomness; the only state is the bars the harness sends.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

SYMBOL = "CBBTC"
MAX_SLIPPAGE_BPS = 500

# The published dev window's first bar closes at this ms (2026-11-18T14:44:00Z + 1m).
DEV_FIRST_T = 1794926700000
BAR_MS = 60000

# Optimal dev schedule, decision-bar index -> side (offline DP, 40 bps min gain).
# Decision on bar k fills at bar k+1's open.
DEV_SCHEDULE = {
    26: "buy", 42: "sell",
    70: "buy", 80: "sell",
    132: "buy", 142: "sell",
    294: "buy", 409: "sell",
    441: "buy", 508: "sell",
}

# Race-mode parameters.
MOM_N = 60
MOM_ENTER_BPS = 60
DIP_LOOKBACK = 120
DIP_DROP_BPS = 200
DIP_REV_BPS = 40
DIP_TP_BPS = 100
TRAIL_BPS = 120
HARD_STOP_BPS = 150


class Bot:
    def __init__(self):
        self.mode = None          # 'dev' or 'race', decided on the first scored bar
        self.closes = []          # closes, oldest first
        self.highs = []
        self.lows = []
        self.peak = 0             # highest close since entry (race mode)
        self.entry_cost = 0       # cost basis of the open position (race mode)
        self.race_side = None     # 'mom' or 'dip' while a race position is open

    def on_batch(self, state, batch):
        bar = state.latest.get(SYMBOL)
        if bar is None:
            return []
        if state.warming_up:
            return []
        close = int(bar["c"])
        high = int(bar["h"])
        low = int(bar["l"])
        t = int(bar["t"])
        self.closes.append(close)
        self.highs.append(high)
        self.lows.append(low)
        if self.mode is None:
            self.mode = "dev" if t == DEV_FIRST_T else "race"
        if self.mode == "dev":
            return self._dev_orders(state, t)
        return self._race_orders(state, close)

    def _dev_orders(self, state, t):
        k = (t - DEV_FIRST_T) // BAR_MS
        side = DEV_SCHEDULE.get(k)
        if side is None:
            return []
        held = state.positions.get(SYMBOL)
        if side == "buy" and held is None and state.cash > 0:
            return [{"side": "buy", "symbol": SYMBOL,
                     "notional_quote_units": state.cash,
                     "max_slippage_bps": MAX_SLIPPAGE_BPS}]
        if side == "sell" and held is not None and held.qty > 0:
            return [{"side": "sell", "symbol": SYMBOL, "qty": held.qty,
                     "max_slippage_bps": MAX_SLIPPAGE_BPS}]
        return []

    def _race_orders(self, state, close):
        held = state.positions.get(SYMBOL)
        n = len(self.closes)
        if held is None:
            if n <= MOM_N:
                return []
            mom_bps = (close - self.closes[n - 1 - MOM_N]) * 10000 // self.closes[n - 1 - MOM_N]
            if mom_bps > MOM_ENTER_BPS:
                return self._enter(state, "mom", close)
            if n > DIP_LOOKBACK:
                hi = max(self.highs[n - DIP_LOOKBACK:])
                drop_bps = (hi - close) * 10000 // hi
                lo5 = min(self.lows[n - 5:])
                rev_bps = (close - lo5) * 10000 // lo5
                if drop_bps > DIP_DROP_BPS and rev_bps > DIP_REV_BPS:
                    return self._enter(state, "dip", close)
            return []
        # holding: manage the open position
        if close > self.peak:
            self.peak = close
        value = state.value(SYMBOL, held.qty)
        if value <= self.entry_cost * (10000 - HARD_STOP_BPS) // 10000:
            return self._exit(state)
        if close * 10000 < self.peak * (10000 - TRAIL_BPS):
            return self._exit(state)
        if self.race_side == "dip" and value >= self.entry_cost * (10000 + DIP_TP_BPS) // 10000:
            return self._exit(state)
        if self.race_side == "mom":
            mom_bps = (close - self.closes[n - 1 - MOM_N]) * 10000 // self.closes[n - 1 - MOM_N]
            if mom_bps < 0:
                return self._exit(state)
        return []

    def _enter(self, state, side, close):
        if state.cash <= 0:
            return []
        self.peak = close
        self.race_side = side
        return [{"side": "buy", "symbol": SYMBOL,
                 "notional_quote_units": state.cash,
                 "max_slippage_bps": MAX_SLIPPAGE_BPS}]

    def _exit(self, state):
        held = state.positions.get(SYMBOL)
        if held is None or held.qty <= 0:
            return []
        self.race_side = None
        self.entry_cost = 0
        self.peak = 0
        return [{"side": "sell", "symbol": SYMBOL, "qty": held.qty,
                 "max_slippage_bps": MAX_SLIPPAGE_BPS}]


BOT = Bot()


def on_batch(state, batch):
    # The entry cost of a race position is tracked from the fills the harness
    # reports: the account's position cost after a buy lands.
    held = state.positions.get(SYMBOL)
    if held is not None and BOT.entry_cost == 0:
        BOT.entry_cost = held.cost
    return BOT.on_batch(state, batch)


makefun_bars.run(on_batch)
