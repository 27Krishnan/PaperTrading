"""
Trailing Profit (Trail Target) Engine

Once price hits target1, move SL to breakeven (entry).
Once price hits target2, move SL to target1.
Optionally apply a trailing profit mechanism above target.
"""
from loguru import logger


class TrailingProfit:

    @staticmethod
    def check_targets(action: str, ltp: float, entry_price: float,
                      targets: list[float], current_sl: float,
                      current_target_idx: int) -> tuple[float, int, str | None]:
        """
        Returns: (new_sl, new_target_idx, exit_reason or None)
        new_target_idx: which target level we're at now (0=none hit, 1=t1 hit, etc.)
        """
        targets_sorted = sorted(targets, reverse=(action.upper() == "SELL"))

        new_sl = current_sl
        new_idx = current_target_idx
        exit_reason = None

        for i, target in enumerate(targets_sorted):
            if i < current_target_idx:
                continue  # Already hit this target

            if action.upper() == "BUY" and ltp >= target:
                new_idx = i + 1
                if i == 0:
                    # T1 hit → move SL to entry (breakeven)
                    new_sl = max(current_sl, entry_price)
                    logger.info(f"Target 1 hit @ {ltp:.2f} | SL moved to breakeven {entry_price:.2f}")
                elif i == 1:
                    # T2 hit → move SL to T1
                    new_sl = max(current_sl, targets_sorted[0])
                    logger.info(f"Target 2 hit @ {ltp:.2f} | SL moved to T1 {targets_sorted[0]:.2f}")
                elif i >= 2:
                    # T3+ hit → close trade
                    exit_reason = f"TARGET_{i+1}_HIT"
                    logger.info(f"Target {i+1} hit @ {ltp:.2f} | Closing trade")
                    break

            elif action.upper() == "SELL" and ltp <= target:
                new_idx = i + 1
                if i == 0:
                    new_sl = min(current_sl, entry_price)
                    logger.info(f"Target 1 hit (short) @ {ltp:.2f} | SL to breakeven")
                elif i == 1:
                    new_sl = min(current_sl, targets_sorted[0])
                    logger.info(f"Target 2 hit (short) @ {ltp:.2f} | SL to T1")
                elif i >= 2:
                    exit_reason = f"TARGET_{i+1}_HIT"
                    break

        return new_sl, new_idx, exit_reason

    @staticmethod
    def check_sl(action: str, ltp: float, stop_loss: float) -> bool:
        """Check if stop loss is triggered"""
        if action.upper() == "BUY":
            return ltp <= stop_loss
        else:
            return ltp >= stop_loss

    @staticmethod
    def check_entry_trigger(action: str, ltp: float, entry_price: float,
                             entry_type: str) -> bool:
        """
        Check if entry condition is met:
        ABOVE → buy when LTP crosses above entry price
        BELOW → buy when LTP drops below entry price
        LIMIT / MARKET → triggered immediately
        """
        if entry_type == "ABOVE":
            return ltp >= entry_price
        elif entry_type == "BELOW":
            return ltp <= entry_price
        else:
            return True  # LIMIT or MARKET - triggered at creation
