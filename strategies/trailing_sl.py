"""
Trailing Stop Loss Engine

For BUY trades:
  - As price moves up, SL moves up by the trailing amount
  - SL never moves down
  - Trade closes when LTP drops to trailing SL level

For SELL trades:
  - As price moves down, SL moves down
  - SL never moves up
  - Trade closes when LTP rises to trailing SL level
"""

from loguru import logger


class TrailingStopLoss:

    @staticmethod
    def update(action: str, ltp: float, current_sl: float,
               entry_price: float, trailing_points: float,
               highest_price: float | None = None,
               lowest_price: float | None = None) -> tuple[float, float, float, bool]:
        """
        Returns: (new_sl, new_highest, new_lowest, sl_triggered)
        """
        if action.upper() == "BUY":
            return TrailingStopLoss._update_long(
                ltp, current_sl, entry_price, trailing_points, highest_price or entry_price
            )
        else:
            return TrailingStopLoss._update_short(
                ltp, current_sl, entry_price, trailing_points, lowest_price or entry_price
            )

    @staticmethod
    def _update_long(ltp: float, current_sl: float, entry_price: float,
                     trailing_points: float, highest_price: float) -> tuple[float, float, float, bool]:
        new_high = max(highest_price, ltp)
        new_sl = new_high - trailing_points

        # SL should never go below initial SL
        new_sl = max(new_sl, current_sl)

        triggered = ltp <= new_sl
        if triggered:
            logger.info(f"Trailing SL triggered | LTP={ltp} | TSL={new_sl:.2f}")

        return new_sl, new_high, highest_price, triggered

    @staticmethod
    def _update_short(ltp: float, current_sl: float, entry_price: float,
                      trailing_points: float, lowest_price: float) -> tuple[float, float, float, bool]:
        new_low = min(lowest_price, ltp)
        new_sl = new_low + trailing_points

        # SL should never go above initial SL
        new_sl = min(new_sl, current_sl)

        triggered = ltp >= new_sl
        if triggered:
            logger.info(f"Trailing SL triggered (short) | LTP={ltp} | TSL={new_sl:.2f}")

        return new_sl, lowest_price, new_low, triggered

    @staticmethod
    def calculate_initial_trailing_points(entry: float, sl: float,
                                          method: str = "fixed",
                                          value: float = None) -> float:
        """
        method='fixed': trailing by exact points (value=50 means 50 pts)
        method='percent': trailing by % of entry (value=2 means 2%)
        method='sl_distance': trail by same distance as entry-SL gap
        """
        if method == "fixed" and value:
            return value
        elif method == "percent" and value:
            return entry * (value / 100)
        elif method == "sl_distance":
            return abs(entry - sl)
        else:
            # Default: trail by entry-SL distance
            return abs(entry - sl)
