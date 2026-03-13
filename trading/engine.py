"""
TradingEngine — async main loop of a single bot worker.

Responsibilities:
  - Connect to OKX WebSocket (tickers)
  - Recalculate spread on every tick
  - Delegate entry/exit decisions to PositionManager
  - Periodically write state to MySQL
  - Log spread to spread_log
  - Record trades
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime

from config import Config
from crypto.encryption import decrypt
from db.connection import Database
from db import queries as Q
from trading.okx_client import OKXClient
from trading.spread import SpreadCalculator, BasketPair
from trading.position import PositionManager

log = logging.getLogger("engine")


class TradingEngine:

    def __init__(self, user_id: int) -> None:
        self.user_id = user_id
        self.db = Database()
        self.okx: OKXClient | None = None
        self.sc: SpreadCalculator | None = None
        self.pm: PositionManager | None = None

        self._running = True
        self._close_requested = False
        self._last_state_update = 0.0
        self._last_spread_log = 0.0
        self._last_instrument_log = 0.0
        self._tick_count = 0
        self._config: dict = {}
        self._rest_api_ok = True
        self._rest_fail_until = 0.0
        self._entry_blocked_until = 0.0

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        self._config = self._load_config()
        pairs = self._load_pairs()
        api_key, secret, passphrase = self._load_keys()

        self.okx = OKXClient(
            api_key=api_key,
            secret_key=secret,
            passphrase=passphrase,
            demo=(Config.OKX_DEMO == "1"),
        )
        instruments = self.okx.load_instruments()
        self._log_event(
            "info",
            f"OKX инструменты загружены: {len(instruments)} всего, "
            f"{len([i for i in instruments.values() if i.get('settleCcy') == 'USDT'])} USDT-SWAP",
        )

        self.sc = SpreadCalculator(pairs)
        self.pm = PositionManager(self.okx, self.sc, self._config)

        self.okx.set_position_mode_net()
        lever = int(self._config.get("leverage", 20))
        for sym in self.sc.all_symbols:
            try:
                self.okx.set_leverage(sym, lever)
            except Exception:
                log.warning("Failed to set leverage for %s", sym, exc_info=True)

        self._restore_state()
        log.info(
            "Config: entry=%.2f%%, TP=%.2f%%, SL=%.2f%% (enabled=%s)",
            self._config.get("entry_spread_pct", 0),
            self._config.get("take_profit_pct", 0),
            self._config.get("stop_loss_pct", 0),
            self._config.get("stop_loss_enabled", False),
        )
        # #region agent log
        try:
            import json
            with open("/Users/nickly/Desktop/robot/.cursor/debug-7b318a.log", "a") as f:
                f.write(json.dumps({"sessionId":"7b318a","hypothesisId":"A","location":"engine.py:init","message":"config_loaded","data":{"cfg_keys":list(self._config.keys())[:20],"take_profit_pct":self._config.get("take_profit_pct"),"entry_spread_pct":self._config.get("entry_spread_pct"),"stop_loss_pct":self._config.get("stop_loss_pct"),"raw_tp":repr(self._config.get("take_profit_pct"))},"timestamp":int(__import__("time").time()*1000)}) + "\n")
        except Exception:
            pass
        # #endregion
        self._log_event("info", "Бот инициализирован")

    def _load_config(self) -> dict:
        rows = self.db.execute(Q.SELECT_CONFIG, (self.user_id,))
        if not rows:
            raise RuntimeError(f"No bot_config for user_id={self.user_id}")
        return rows[0]

    def _load_pairs(self) -> list[BasketPair]:
        cfg = self._load_config()
        config_id = cfg["id"]
        rows = self.db.execute(Q.SELECT_BASKET_PAIRS, (config_id,))
        if not rows:
            raise RuntimeError(f"No basket_pairs for config_id={config_id}")
        return [
            BasketPair(
                index=r["pair_index"],
                symbol_b1=r["symbol_basket1"],
                symbol_b2=r["symbol_basket2"],
            )
            for r in rows
        ]

    def _load_keys(self) -> tuple[str, str, str]:
        rows = self.db.execute(Q.SELECT_USER_KEYS, (self.user_id,))
        if not rows:
            raise RuntimeError(f"No API keys for user_id={self.user_id}")
        r = rows[0]
        return (
            decrypt(r["okx_api_key"]),
            decrypt(r["okx_secret_key"]),
            decrypt(r["okx_passphrase"]),
        )

    def _restore_state(self) -> None:
        rows = self.db.execute(Q.SELECT_STATE, (self.user_id,))
        if not rows:
            return
        st = rows[0]

        rp = st.get("reference_prices")
        if rp:
            prices = json.loads(rp) if isinstance(rp, str) else rp
            # Restore only if symbols match (user didn't change pairs)
            if all(s in prices for s in self.sc.all_symbols):
                self.sc.reference_prices = {s: prices[s] for s in self.sc.all_symbols}
                log.info("Restored reference prices from DB")
            else:
                log.info("Reference prices ignored: pairs changed, waiting for fresh quotes")

        if st.get("position_open"):
            self.pm.restore(st)

        if self.pm.state.is_open:
            try:
                self.pm.sync_with_exchange()
            except Exception:
                log.warning("Could not sync positions with exchange — using saved state")
                self._log_event("warning", "Не удалось синхронизировать позиции с биржей — используется сохранённое состояние")

    # ------------------------------------------------------------------
    # Async main loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        ws = await self.okx.subscribe_tickers(
            self.sc.all_symbols,
            callback=self._on_tick,
        )
        self._log_event(
            "info",
            "Подписка на OKX tickers установлена для: "
            + ", ".join(self.sc.all_symbols),
        )

        # Bootstrap: fetch prices via REST (WS may be slow for low-volume pairs like BONK)
        rest_prices = self.okx.fetch_ticker_prices(self.sc.all_symbols)
        for sym, price in rest_prices.items():
            self.sc.update_price(sym, price)
        if rest_prices and not self.sc.has_reference():
            if self.sc.has_all_quotes():
                self.sc.fix_reference_prices()
                self._log_event("info", "Базовые цены загружены через REST")

        self._update_db_state("running")

        try:
            while self._running:
                if self._close_requested:
                    try:
                        was_open = self.pm.state.is_open
                        saved_state = self.pm.state.to_dict() if was_open else None
                        if was_open:
                            spread = self.sc.spread()
                            pnl = self.pm.get_pnl_breakdown()
                        ok = self.pm.close_all()
                        if not ok:
                            self._log_event(
                                "error",
                                "Не удалось закрыть позицию на бирже (API ошибка). "
                                "Позиции могут оставаться открытыми — проверьте OKX и попробуйте снова.",
                            )
                        elif was_open and saved_state:
                            self._entry_blocked_until = time.time() + 30
                            log.info("Entry blocked for 30s after manual close")
                            self._record_trade_from_snapshot(
                                "manual", spread, pnl, saved_state
                            )
                            self._log_event("trade", f"Позиция закрыта вручную: PnL={pnl['pnl_total_pct']:.4f}%")
                            self.sc.refresh_reference_prices()
                            self._log_event("info", "Базовые цены пересчитаны после закрытия позиции")
                        else:
                            self._log_event("info", "Команда закрытия: позиция не была открыта")
                    except Exception as exc:
                        log.exception("Error handling close request")
                        self._log_event("error", f"Ошибка при закрытии позиции: {exc}")
                    finally:
                        self._close_requested = False

                now = time.time()
                if now - self._last_state_update >= 3.0:
                    self._update_db_state("running")
                    self._last_state_update = now

                await asyncio.sleep(0.5)
        finally:
            await self.okx.close_ws()

    # ------------------------------------------------------------------
    # Tick handler (called from WebSocket thread — schedule on loop)
    # ------------------------------------------------------------------

    def _on_tick(self, raw_msg) -> None:
        try:
            if isinstance(raw_msg, str):
                msg = json.loads(raw_msg)
            else:
                msg = raw_msg

            data_list = msg.get("data")
            if not data_list:
                return
            data = data_list[0]
            inst_id = data.get("instId")
            last_price = data.get("last")
            if not inst_id or not last_price:
                return

            self._tick_count += 1
            price = float(last_price)
            self.sc.update_price(inst_id, price)

            if self._tick_count == 1:
                log.info("First tick: %s = %s", inst_id, price)
                self._log_event("info", f"Первый тик получен: {inst_id} = {price}")

            if not self.sc.has_reference():
                if self.sc.has_all_quotes():
                    self.sc.fix_reference_prices()
                    self._log_event(
                        "info",
                        f"Базовые цены зафиксированы (после {self._tick_count} тиков)",
                    )
                elif self._tick_count % 50 == 0:
                    filled = len(self.sc.current_prices)
                    total = len(self.sc.all_symbols)
                    log.info(
                        "Tick #%d: quotes %d/%d filled",
                        self._tick_count, filled, total,
                    )
                return

            self.pm.entry_cooldown = time.time() < self._entry_blocked_until

            action = self.pm.evaluate()
            if action:
                self._handle_action(action)

            now = time.time()
            if now - self._last_state_update >= 2.0:
                self._update_db_state("running")
                self._last_state_update = now

            if now - self._last_spread_log >= 5.0:
                self._write_spread_log()
                self._last_spread_log = now

            if now - self._last_instrument_log >= 30.0:
                self._write_chart_instrument_points()
                self._last_instrument_log = now

        except Exception as exc:
            log.exception("Error in tick handler")
            try:
                self._log_event("error", f"Ошибка в обработчике тика: {exc}")
            except Exception:
                pass

    def _handle_action(self, action: str | tuple[str, dict]) -> None:
        if isinstance(action, tuple):
            action, snapshot = action
        else:
            snapshot = None

        spread = self.sc.spread()
        pnl = self.pm.get_pnl_breakdown()

        if action in ("take_profit", "stop_loss"):
            self._record_trade(action, spread, pnl, snapshot)
            self._log_event("trade", f"Позиция закрыта ({action}): PnL={pnl['pnl_total_pct']:.4f}%")
            self.db.execute(
                Q.INSERT_NOTIFICATION,
                (self.user_id, f"Позиция закрыта ({action}), PnL: {pnl['pnl_total_pct']:.4f}%"),
            )
            self.sc.refresh_reference_prices()
            self._log_event("info", "Базовые цены пересчитаны после закрытия позиции")
            self._entry_blocked_until = time.time() + 10
            log.info("Entry blocked for 10s after %s", action)
        elif action == "entry":
            self._log_event(
                "trade",
                f"Вход: spread={spread:.4f}%, long={self.pm.state.long_basket}, "
                f"short={self.pm.state.short_basket}",
            )
        elif action == "dca":
            self._log_event(
                "trade",
                f"DCA #{self.pm.state.dca_count}: spread={spread:.4f}%",
            )

    # ------------------------------------------------------------------
    # DB writes
    # ------------------------------------------------------------------

    def _update_db_state(self, actual_state: str) -> None:
        pnl = self.pm.get_pnl_breakdown() if self.pm else {}
        balance = {}
        ping = 0

        now_ts = time.time()
        if now_ts >= self._rest_fail_until:
            try:
                balance = self.okx.get_balance()
                ping = self.okx.ping_ms()
                self._rest_api_ok = True
            except Exception as exc:
                if self._rest_api_ok:
                    log.warning("REST API unavailable: %s — throttling requests", exc)
                    self._log_event("warning", f"REST API недоступен: {exc}")
                self._rest_api_ok = False
                self._rest_fail_until = now_ts + 60

        spread = self.sc.spread() if self.sc.has_reference() else None
        long_b, short_b = (self.pm.state.long_basket, self.pm.state.short_basket) if self.pm.state.is_open else (None, None)
        buy_basket = long_b
        sell_basket = short_b

        import os
        self.db.execute(Q.UPDATE_STATE_FULL, (
            actual_state,
            round(spread, 4) if spread is not None else None,
            buy_basket,
            sell_basket,
            pnl.get("pnl_long_pct"),
            pnl.get("pnl_short_pct"),
            pnl.get("pnl_total_pct"),
            None,  # pnl_total_usdt — TODO: calculate
            balance.get("total_eq"),
            balance.get("avail_eq"),
            ping,
            "connected",
            int(self.pm.state.is_open) if self.pm else 0,
            round(self.pm.state.entry_spread, 4) if self.pm and self.pm.state.is_open else None,
            self.pm.state.dca_count if self.pm else 0,
            long_b,
            short_b,
            json.dumps(self.sc.reference_prices) if self.sc.has_reference() else None,
            json.dumps(self.pm.state.positions) if self.pm else None,
            json.dumps(self.sc.quotes_snapshot()) if self.sc else None,
            os.getpid(),
            self.user_id,
        ))

    def _write_spread_log(self) -> None:
        if not self.sc.has_reference():
            return
        spread = self.sc.spread()
        r1 = self.sc.basket_return("basket1")
        r2 = self.sc.basket_return("basket2")
        self.db.execute(Q.INSERT_SPREAD_LOG, (
            self.user_id, round(spread, 4), round(r1, 4), round(r2, 4),
        ))
        try:
            ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            self.db.execute(Q.INSERT_CHART_SPREAD, (
                self.user_id, ts, round(spread, 4), round(r1, 4), round(r2, 4),
            ))
        except Exception as e:
            log.debug("chart_spread_points insert skipped: %s", e)

    def _write_chart_instrument_points(self) -> None:
        if not self.sc.has_reference():
            return
        try:
            ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            for inst_id, price in self.sc.quotes_snapshot().items():
                if price > 0:
                    self.db.execute(Q.INSERT_CHART_INSTRUMENT, (
                        self.user_id, ts, inst_id, round(float(price), 8),
                    ))
        except Exception as e:
            log.debug("chart_instrument_points insert skipped: %s", e)

    def _record_trade(self, reason: str, exit_spread: float, pnl: dict, snapshot: dict | None = None) -> None:
        snap = snapshot if snapshot is not None else self.pm.state.to_dict()
        self._record_trade_from_snapshot(reason, exit_spread, pnl, snap)

    def _record_trade_from_snapshot(
        self, reason: str, exit_spread: float, pnl: dict, snapshot: dict
    ) -> None:
        entry_time = snapshot.get("entry_time")
        if isinstance(entry_time, str):
            entry_time = datetime.fromisoformat(entry_time)
        opened_at = entry_time or datetime.utcnow()
        closed_at = datetime.utcnow()
        duration = int((closed_at - opened_at).total_seconds())

        entry_spread = float(snapshot.get("entry_spread", 0))
        long_b = snapshot.get("long_basket") or "basket1"
        actual_pnl = (exit_spread - entry_spread) if long_b == "basket1" else (entry_spread - exit_spread)

        # #region agent log
        try:
            import json
            with open("/Users/nickly/Desktop/robot/.cursor/debug-7b318a.log", "a") as f:
                f.write(json.dumps({"sessionId":"7b318a","hypothesisId":"C","location":"engine.py:_record_trade","message":"trade_record","data":{"entry_spread":entry_spread,"exit_spread":exit_spread,"long_basket":long_b,"actual_pnl":actual_pnl,"reason":reason},"timestamp":int(__import__("time").time()*1000)}) + "\n")
        except Exception:
            pass
        # #endregion

        self.db.execute(Q.INSERT_TRADE, (
            self.user_id,
            opened_at,
            closed_at,
            duration,
            round(entry_spread, 4),
            round(exit_spread, 4),
            long_b,
            snapshot.get("short_basket") or "basket2",
            round(actual_pnl, 4),
            0,
            0,
            snapshot.get("dca_count", 0),
            reason,
            json.dumps(snapshot.get("positions", {})),
            0,
        ))

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def save_state_and_stop(self) -> None:
        self._running = False
        self.db.execute(Q.SAVE_STATE_ON_SHUTDOWN, (
            json.dumps(self.sc.reference_prices) if self.sc and self.sc.has_reference() else None,
            json.dumps(self.pm.state.positions) if self.pm else None,
            round(self.pm.state.entry_spread, 4) if self.pm and self.pm.state.is_open else None,
            self.pm.state.dca_count if self.pm else 0,
            int(self.pm.state.is_open) if self.pm else 0,
            self.pm.state.long_basket if self.pm else None,
            self.pm.state.short_basket if self.pm else None,
            self.user_id,
        ))
        self._log_event("info", "Состояние сохранено, бот остановлен")
        log.info("State saved for user_id=%s", self.user_id)

    def request_close_positions(self) -> None:
        self._close_requested = True

    def stop(self) -> None:
        self._running = False

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _log_event(self, level: str, message: str, details: dict | None = None) -> None:
        self.db.execute(
            Q.INSERT_EVENT,
            (self.user_id, level, message, json.dumps(details) if details else None),
        )
