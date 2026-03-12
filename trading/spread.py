"""
Spread calculator for pair-trading baskets.

Uses the methodology from методика_парной_торговли.md:
  - Reference prices P₀ fixed at bot start (or restored from DB)
  - Per-instrument return: Δ = (P_now - P₀) / P₀ * 100
  - Basket return: R = mean(Δ for each instrument in basket)
  - Spread = R_basket1 - R_basket2
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

log = logging.getLogger("spread")


@dataclass
class BasketPair:
    index: int
    symbol_b1: str
    symbol_b2: str


class SpreadCalculator:

    def __init__(self, pairs: list[BasketPair]) -> None:
        self.pairs = pairs
        self.symbols_b1 = [p.symbol_b1 for p in pairs]
        self.symbols_b2 = [p.symbol_b2 for p in pairs]
        self.all_symbols = self.symbols_b1 + self.symbols_b2

        self.reference_prices: dict[str, float] = {}
        self.current_prices: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Prices
    # ------------------------------------------------------------------

    def update_price(self, symbol: str, price: float) -> None:
        self.current_prices[symbol] = price

    def fix_reference_prices(self) -> None:
        missing = [s for s in self.all_symbols if s not in self.current_prices]
        if missing:
            raise RuntimeError(f"Missing quotes for reference: {missing}")
        self.reference_prices = dict(self.current_prices)
        log.info("Reference prices fixed: %s", self.reference_prices)

    def refresh_reference_prices(self) -> None:
        """Reset reference prices to current values for all available symbols."""
        for s in self.all_symbols:
            if s in self.current_prices:
                self.reference_prices[s] = self.current_prices[s]
        log.info("Reference prices refreshed from current quotes")

    def has_all_quotes(self) -> bool:
        return all(s in self.current_prices for s in self.all_symbols)

    def has_reference(self) -> bool:
        """True only if we have reference prices for ALL current symbols."""
        return all(s in self.reference_prices for s in self.all_symbols)

    # ------------------------------------------------------------------
    # Spread calculation
    # ------------------------------------------------------------------

    def _return_pct(self, symbol: str) -> float:
        p0 = self.reference_prices.get(symbol)
        pnow = self.current_prices.get(symbol)
        if not p0 or not pnow:
            return 0.0
        return (pnow - p0) / p0 * 100.0

    def basket_return(self, basket: str) -> float:
        symbols = self.symbols_b1 if basket == "basket1" else self.symbols_b2
        if not symbols:
            return 0.0
        return sum(self._return_pct(s) for s in symbols) / len(symbols)

    def spread(self) -> float:
        return self.basket_return("basket1") - self.basket_return("basket2")

    def direction(self) -> tuple[str, str]:
        """Return (long_basket, short_basket) based on current spread sign."""
        s = self.spread()
        if s >= 0:
            return "basket2", "basket1"
        return "basket1", "basket2"

    def quotes_snapshot(self) -> dict[str, float]:
        return {s: self.current_prices.get(s, 0) for s in self.all_symbols}
