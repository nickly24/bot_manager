"""
Position manager — entry, DCA, exit logic for pair-trading.

Coordinates with OKXClient to place orders and with SpreadCalculator
for trading signals.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from trading.okx_client import OKXClient
from trading.spread import SpreadCalculator

log = logging.getLogger("position")


@dataclass
class PositionState:
    is_open: bool = False
    long_basket: str | None = None
    short_basket: str | None = None
    entry_spread: float = 0.0
    entry_time: datetime | None = None
    dca_count: int = 0
    positions: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "is_open": self.is_open,
            "long_basket": self.long_basket,
            "short_basket": self.short_basket,
            "entry_spread": self.entry_spread,
            "entry_time": self.entry_time.isoformat() if self.entry_time else None,
            "dca_count": self.dca_count,
            "positions": self.positions,
        }


class PositionManager:
    """
    Decides when to enter/exit/DCA based on bot_configs parameters.
    Executes trades via OKXClient.
    """

    def __init__(
        self,
        okx: OKXClient,
        spread_calc: SpreadCalculator,
        config: dict,
        log_event_cb: Callable[[str, str, dict | None], None] | None = None,
    ) -> None:
        self.okx = okx
        self.sc = spread_calc
        self.cfg = config
        self.log_event = log_event_cb
        self.state = PositionState()
        self.entry_cooldown = False

    # ------------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------------

    @property
    def entry_threshold(self) -> float:
        return float(self.cfg.get("entry_spread_pct", 2.0))

    @property
    def take_profit_pct(self) -> float:
        return float(self.cfg.get("take_profit_pct", 0.8))

    @property
    def dca_max(self) -> int:
        return int(self.cfg.get("dca_count", 3))

    @property
    def dca_step(self) -> float:
        return float(self.cfg.get("dca_step_pct", 3.0))

    @property
    def stop_loss_pct(self) -> float:
        return float(self.cfg.get("stop_loss_pct", 4.0))

    @property
    def stop_loss_enabled(self) -> bool:
        return bool(self.cfg.get("stop_loss_enabled", False))

    @property
    def leverage(self) -> int:
        return int(self.cfg.get("leverage", 20))

    @property
    def position_size_pct(self) -> float:
        return float(self.cfg.get("position_size_pct", 200.0))

    @property
    def no_new_position(self) -> bool:
        return bool(self.cfg.get("no_new_position", True))

    @property
    def simulation_mode(self) -> bool:
        return bool(self.cfg.get("simulation_mode", False))

    # ------------------------------------------------------------------
    # Restore state from DB
    # ------------------------------------------------------------------

    def restore(self, db_state: dict) -> None:
        self.state.is_open = bool(db_state.get("position_open"))
        self.state.long_basket = db_state.get("long_basket")
        self.state.short_basket = db_state.get("short_basket")
        self.state.entry_spread = float(db_state.get("entry_spread_pct") or 0)
        self.state.dca_count = int(db_state.get("dca_count_current") or 0)
        et = db_state.get("entry_time")
        self.state.entry_time = et if isinstance(et, datetime) else None

        pd = db_state.get("positions_data")
        if pd:
            self.state.positions = json.loads(pd) if isinstance(pd, str) else pd
        log.info("Position state restored: %s", self.state.to_dict())

    # ------------------------------------------------------------------
    # Trading signals evaluation (called every tick)
    # ------------------------------------------------------------------

    def evaluate(self) -> str | None:
        """
        Check spread against thresholds and return action taken:
        'entry', 'dca', 'take_profit', 'stop_loss', or None.
        """
        spread = self.sc.spread()
        abs_spread = abs(spread)

        if not self.state.is_open:
            if abs_spread >= self.entry_threshold:
                if self.no_new_position or self.entry_cooldown:
                    return None
                return self._open_position(spread)
            return None

        pnl = self._unrealised_pnl()
        tp = self.take_profit_pct

        if pnl >= tp:
            snapshot = self.state.to_dict()
            self._close_position("take_profit", pnl, spread)
            return ("take_profit", snapshot)

        if self.stop_loss_enabled and pnl <= -self.stop_loss_pct:
            snapshot = self.state.to_dict()
            self._close_position("stop_loss", pnl, spread)
            return ("stop_loss", snapshot)

        dca_threshold = self.entry_threshold + (self.state.dca_count + 1) * self.dca_step
        if abs_spread >= dca_threshold and self.state.dca_count < self.dca_max:
            return self._dca(spread)

        return None

    # ------------------------------------------------------------------
    # Unrealised P&L — приоритет: реальный upl с биржи, fallback: spread
    # ------------------------------------------------------------------

    def _unrealised_pnl_from_exchange(self) -> float | None:
        """
        Реальный unrealized PnL в % из суммы upl по позициям.
        Returns None если нет данных (позиции не синхронизированы).
        """
        if not self.state.positions:
            return None
        total_upl = sum(float(p.get("upl", 0)) for p in self.state.positions.values())
        exposure = 0.0
        for inst_id, pos in self.state.positions.items():
            try:
                qty = float(pos.get("qty", 0))
                avg_px = float(pos.get("avg_price", 0))
                ct_val = self.okx.get_ct_val(inst_id)
                exposure += qty * ct_val * avg_px
            except (ValueError, KeyError):
                continue
        if exposure <= 0:
            return None
        return 100.0 * total_upl / exposure

    def _unrealised_pnl(self) -> float:
        """TP/SL: реальный upl с биржи (приоритет), иначе spread-based fallback."""
        if not self.state.is_open:
            return 0.0

        real = self._unrealised_pnl_from_exchange()
        if real is not None:
            return real

        current_spread = self.sc.spread()
        spread_pnl = (
            self.state.entry_spread - current_spread
            if self.state.long_basket == "basket2"
            else current_spread - self.state.entry_spread
        )
        if abs(spread_pnl) > 0.1:
            log.debug("Using spread-based PnL (%.4f%%), no upl data yet", spread_pnl)
        return spread_pnl

    def get_pnl_breakdown(self) -> dict:
        """Spread-based PnL (fallback, не учитывает slippage/комиссии)."""
        r_b1 = self.sc.basket_return("basket1")
        r_b2 = self.sc.basket_return("basket2")

        if self.state.long_basket == "basket1":
            pnl_long = r_b1
            pnl_short = -r_b2
        else:
            pnl_long = r_b2
            pnl_short = -r_b1

        return {
            "pnl_long_pct": round(pnl_long, 4),
            "pnl_short_pct": round(pnl_short, 4),
            "pnl_total_pct": round(pnl_long + pnl_short, 4),
            "pnl_total_usdt": None,
        }

    def get_pnl_for_dashboard(self) -> dict:
        """
        Реальный PnL с биржи (upl) для отображения в админке.
        При открытой позиции: upl из positions. Иначе — spread-based.
        """
        fallback = self.get_pnl_breakdown()
        if not self.state.is_open or not self.state.positions:
            return fallback

        total_upl = sum(float(p.get("upl", 0)) for p in self.state.positions.values())
        exposure = 0.0
        for inst_id, pos in self.state.positions.items():
            try:
                qty = float(pos.get("qty", 0))
                avg_px = float(pos.get("avg_price", 0))
                ct_val = self.okx.get_ct_val(inst_id)
                exposure += qty * ct_val * avg_px
            except (ValueError, KeyError):
                continue
        if exposure <= 0:
            return fallback

        pnl_total_pct = 100.0 * total_upl / exposure
        long_syms = set(
            self.sc.symbols_b2 if self.state.long_basket == "basket2" else self.sc.symbols_b1
        )
        upl_long = sum(
            float(p.get("upl", 0))
            for inst, p in self.state.positions.items()
            if inst in long_syms
        )
        upl_short = total_upl - upl_long
        half_exp = exposure / 2.0
        pnl_long_pct = 100.0 * upl_long / half_exp if half_exp else 0
        pnl_short_pct = 100.0 * upl_short / half_exp if half_exp else 0

        return {
            "pnl_long_pct": round(pnl_long_pct, 4),
            "pnl_short_pct": round(pnl_short_pct, 4),
            "pnl_total_pct": round(pnl_total_pct, 4),
            "pnl_total_usdt": round(total_upl, 2),
        }

    # ------------------------------------------------------------------
    # Open / Close / DCA
    # ------------------------------------------------------------------

    def _open_position(self, spread: float) -> str:
        long_basket, short_basket = self.sc.direction()
        log.info("ENTRY: spread=%.4f long=%s short=%s", spread, long_basket, short_basket)

        if not self.simulation_mode:
            try:
                self._execute_entry(long_basket, short_basket)
            except Exception:
                log.exception(
                    "Failed to execute entry orders — position tracked logically"
                )

        self.state.is_open = True
        self.state.long_basket = long_basket
        self.state.short_basket = short_basket
        self.state.entry_spread = spread
        self.state.entry_time = datetime.utcnow()
        self.state.dca_count = 0
        return "entry"

    def _dca(self, spread: float) -> str:
        self.state.dca_count += 1
        log.info(
            "DCA #%d: spread=%.4f long=%s short=%s",
            self.state.dca_count, spread,
            self.state.long_basket, self.state.short_basket,
        )
        if not self.simulation_mode:
            try:
                self._execute_entry(self.state.long_basket, self.state.short_basket)
            except Exception:
                log.exception("Failed to execute DCA orders")
        return "dca"

    def _close_position(self, reason: str, pnl: float, spread: float) -> str:
        log.info("CLOSE (%s): spread=%.4f pnl=%.4f%%", reason, spread, pnl)
        if not self.simulation_mode:
            try:
                self._execute_close()
            except Exception:
                log.exception("Failed to execute close orders")

        self.state.is_open = False
        self.state.long_basket = None
        self.state.short_basket = None
        self.state.entry_spread = 0
        self.state.entry_time = None
        self.state.dca_count = 0
        self.state.positions = {}
        return reason

    def close_all(self) -> bool:
        """
        Force-close all positions (called on SIGUSR1 or manual command).
        Returns True if close succeeded (or was simulation), False if OKX API failed.
        """
        if self.state.is_open and not self.simulation_mode:
            try:
                self._execute_close()
            except Exception:
                log.exception("Failed to execute close orders in close_all — positions may still be open on exchange")
                return False
        self.state.is_open = False
        self.state.long_basket = None
        self.state.short_basket = None
        self.state.entry_spread = 0
        self.state.entry_time = None
        self.state.dca_count = 0
        self.state.positions = {}
        return True

    # ------------------------------------------------------------------
    # Execution helpers
    # ------------------------------------------------------------------

    def _get_price(self, sym: str) -> float:
        price = self.sc.current_prices.get(sym, 0)
        if price <= 0:
            price = self.sc.reference_prices.get(sym, 0)
        return price

    def _execute_entry(self, long_basket: str, short_basket: str) -> None:
        required = len(self.sc.all_symbols)
        balance = self.okx.get_balance()
        avail = balance["avail_eq"]
        per_pair_usdt = (avail * self.position_size_pct / 100.0) / len(self.sc.pairs)
        log.info(
            "Entry calc: avail=%.2f, size_pct=%.0f%%, pairs=%d, per_pair=%.2f USDT, required=%d",
            avail, self.position_size_pct, len(self.sc.pairs), per_pair_usdt, required,
        )

        # Ждём ВСЕ цены — без ограничения попыток, до последнего
        attempt = 0
        while True:
            missing = [s for s in self.sc.all_symbols if self.sc.current_prices.get(s, 0) <= 0]
            if not missing:
                break
            attempt += 1
            log.info("Price fetch attempt %d: missing %s", attempt, missing)
            fetched = self.okx.fetch_ticker_prices(missing)
            for sym, px in fetched.items():
                if px > 0:
                    self.sc.current_prices[sym] = px
                    log.info("REST price for %s: %.4f", sym, px)
            time.sleep(1.5)

        orders = []
        long_symbols = self.sc.symbols_b1 if long_basket == "basket1" else self.sc.symbols_b2
        short_symbols = self.sc.symbols_b2 if long_basket == "basket1" else self.sc.symbols_b1

        for sym in long_symbols:
            price = self._get_price(sym)
            sz = self.okx.usdt_to_contracts(sym, per_pair_usdt, price)
            if sz <= 0:
                lot = self.okx.get_lot_sz(sym)
                sz = max(1, int(round(lot))) if lot > 0 else 1
                log.warning("LONG %s: sz was 0, using min %d", sym, sz)
            orders.append({
                "instId": sym, "tdMode": "cross",
                "side": "buy", "ordType": "market", "sz": str(sz),
            })

        for sym in short_symbols:
            price = self._get_price(sym)
            sz = self.okx.usdt_to_contracts(sym, per_pair_usdt, price)
            if sz <= 0:
                lot = self.okx.get_lot_sz(sym)
                sz = max(1, int(round(lot))) if lot > 0 else 1
                log.warning("SHORT %s: sz was 0, using min %d", sym, sz)
            orders.append({
                "instId": sym, "tdMode": "cross",
                "side": "sell", "ordType": "market", "sz": str(sz),
            })

        # Всегда строим required ордеров (sz=0 → min lot)
        assert len(orders) == required, f"orders {len(orders)} != required {required}"

        results = self.okx.place_batch_orders(orders)
        failed = []
        for i, r in enumerate(results):
            s_code = r.get("sCode", "0")
            if s_code != "0":
                o = orders[i] if i < len(orders) else {}
                failed.append((o, r.get("sMsg", ""), s_code))

        # Ретрай проваленных — БЕЗ ЛИМИТА, до последнего, пока все не разместим
        retry_round = 0
        while failed:
            retry_round += 1
            delay = min(2.0 + retry_round * 0.5, 15.0)
            log.warning("Batch entry: %d failed, retry #%d in %.1fs", len(failed), retry_round, delay)
            time.sleep(delay)
            still_failed = []
            for order, msg, code in failed:
                try:
                    r = self.okx.place_batch_orders([order])
                    if r and r[0].get("sCode") == "0":
                        log.info("Retry OK: %s", order.get("instId"))
                    else:
                        err_msg = r[0].get("sMsg", msg) if r else msg
                        err_code = r[0].get("sCode", code) if r else code
                        still_failed.append((order, err_msg, err_code))
                        log.error("Retry failed %s: sCode=%s %s", order.get("instId"), err_code, err_msg)
                except Exception as e:
                    still_failed.append((order, str(e), "exception"))
                    log.exception("Retry exception for %s: %s", order.get("instId"), e)
            failed = still_failed

        ok_count = len(orders)
        log.info("Batch entry: %d OK, %d failed (after retries)", ok_count, len(failed))

        if self.log_event:
            details = {
                "total": len(orders),
                "ok": ok_count,
                "failed": len(failed),
                "failed_symbols": [o.get("instId") for o, _, _ in failed],
                "failed_codes": [(o.get("instId"), c, m) for o, m, c in failed],
            }
            self.log_event(
                "trade" if len(failed) == 0 else "warning",
                f"Batch entry: {ok_count}/{len(orders)} OK" + (f", {len(failed)} failed" if failed else ""),
                details,
            )

        self._update_positions_from_exchange()

    def _execute_close(self) -> None:
        positions = self.okx.get_positions()
        our_symbols = set(self.sc.all_symbols)
        orders = []
        for pos in positions:
            if pos["instId"] not in our_symbols:
                continue
            qty = float(pos.get("pos", 0))
            if qty == 0:
                continue
            side = "sell" if qty > 0 else "buy"
            orders.append({
                "instId": pos["instId"], "tdMode": "cross",
                "side": side, "ordType": "market",
                "sz": str(abs(int(float(pos["pos"])))),
                "reduceOnly": True,
            })

        if not orders:
            log.info("No open positions to close on exchange")
            return

        results = self.okx.place_batch_orders(orders)
        failed_orders = []
        for i, r in enumerate(results):
            s_code = r.get("sCode", "0")
            if s_code != "0":
                failed_orders.append((orders[i], r.get("sMsg", ""), s_code, r.get("instId", "?")))
                log.error("Close failed instId=%s sCode=%s %s", r.get("instId"), s_code, r.get("sMsg", ""))
            else:
                log.info("Close order OK: instId=%s ordId=%s", r.get("instId", "?"), r.get("ordId", "?"))

        if failed_orders:
            log.warning("Retrying %d failed close orders one-by-one", len(failed_orders))
            for order, msg, code, inst_id in failed_orders:
                try:
                    time.sleep(0.6)
                    rr = self.okx.place_batch_orders([order])
                    if rr and rr[0].get("sCode") == "0":
                        log.info("Retry close OK: %s", inst_id)
                    else:
                        log.error("Retry close failed %s: %s", inst_id, msg)
                except Exception as e:
                    log.exception("Retry close exception %s: %s", inst_id, e)

        log.info("Batch close: %d orders sent, %d failed", len(results), len(failed_orders))

        time.sleep(2)
        remaining = self.okx.get_positions()
        still_open = [p for p in remaining if p["instId"] in our_symbols and float(p.get("pos", 0)) != 0]
        if still_open:
            syms = [p["instId"] for p in still_open]
            log.warning("Positions still open after close attempt: %s — retrying one-by-one", syms)
            for pos in still_open:
                qty = float(pos["pos"])
                sz = max(1, abs(int(round(qty))))
                side = "sell" if qty > 0 else "buy"
                ro = {"instId": pos["instId"], "tdMode": "cross", "side": side, "ordType": "market", "sz": str(sz), "reduceOnly": True}
                try:
                    time.sleep(0.6)
                    rr = self.okx.place_batch_orders([ro])
                    if rr and rr[0].get("sCode") == "0":
                        log.info("Retry close OK: %s", pos["instId"])
                    else:
                        log.error("Retry close failed %s: %s", pos["instId"], rr[0].get("sMsg", "") if rr else "no response")
                except Exception as e:
                    log.exception("Retry close exception %s: %s", pos["instId"], e)
            time.sleep(2)
            final = self.okx.get_positions()
            still_open_final = [p for p in final if p["instId"] in our_symbols and float(p.get("pos", 0)) != 0]
            if still_open_final:
                syms_final = [p["instId"] for p in still_open_final]
                raise RuntimeError(f"Failed to close positions after retry: {syms_final}")

    def _update_positions_from_exchange(self) -> None:
        positions = self.okx.get_positions()
        self.state.positions = {}
        for pos in positions:
            qty = float(pos.get("pos", 0))
            if qty == 0:
                continue
            self.state.positions[pos["instId"]] = {
                "side": "long" if qty > 0 else "short",
                "qty": abs(qty),
                "avg_price": float(pos.get("avgPx", 0)),
                "upl": float(pos.get("upl", 0)),
            }

    def sync_with_exchange(self) -> None:
        self._update_positions_from_exchange()
        log.info("Positions synced with exchange: %d", len(self.state.positions))
