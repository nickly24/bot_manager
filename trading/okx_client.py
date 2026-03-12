"""
Wrapper around the python-okx SDK for REST and WebSocket operations.

Provides a unified interface used by TradingEngine.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable

import okx.Account as OkxAccount
import okx.Trade as OkxTrade
import okx.PublicData as OkxPublic
from okx.websocket.WsPublicAsync import WsPublicAsync

log = logging.getLogger("okx_client")


class OKXClient:
    """Synchronous REST helpers + async WebSocket subscription."""

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        passphrase: str,
        demo: bool = False,
    ) -> None:
        flag = "1" if demo else "0"
        self.account = OkxAccount.AccountAPI(api_key, secret_key, passphrase, False, flag)
        self.trade = OkxTrade.TradeAPI(api_key, secret_key, passphrase, False, flag)
        self.public = OkxPublic.PublicAPI("", "", "", False, flag)

        self._demo = demo
        self._ws: WsPublicAsync | None = None
        self._instruments_cache: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # Instruments
    # ------------------------------------------------------------------

    def load_instruments(self) -> dict[str, dict]:
        result = self.public.get_instruments(instType="SWAP")
        if result.get("code") != "0":
            raise RuntimeError(f"Failed to load instruments: {result}")
        for inst in result["data"]:
            if inst.get("settleCcy") == "USDT" and inst.get("state") == "live":
                self._instruments_cache[inst["instId"]] = inst
        log.info("Loaded %d USDT-SWAP instruments", len(self._instruments_cache))
        return self._instruments_cache

    def get_ct_val(self, inst_id: str) -> float:
        inst = self._instruments_cache.get(inst_id)
        if not inst:
            raise ValueError(f"Instrument {inst_id} not in cache")
        return float(inst["ctVal"])

    def get_lot_sz(self, inst_id: str) -> float:
        inst = self._instruments_cache.get(inst_id)
        if not inst:
            raise ValueError(f"Instrument {inst_id} not in cache")
        return float(inst.get("lotSz", "1"))

    # ------------------------------------------------------------------
    # Account
    # ------------------------------------------------------------------

    def set_position_mode_net(self) -> None:
        r = self.account.set_position_mode(posMode="net_mode")
        log.info("set_position_mode: %s", r)

    def set_leverage(self, inst_id: str, lever: int) -> None:
        r = self.account.set_leverage(
            instId=inst_id, lever=str(lever), mgnMode="cross"
        )
        if r.get("code") != "0":
            log.warning("set_leverage %s: %s", inst_id, r)

    def get_balance(self) -> dict:
        r = self.account.get_account_balance()
        if r.get("code") != "0":
            raise RuntimeError(f"get_balance failed: {r}")
        details = r["data"][0].get("details", [])
        usdt = next((d for d in details if d["ccy"] == "USDT"), {})

        def _f(val) -> float:
            if val is None or val == "":
                return 0.0
            return float(val)

        return {
            "total_eq": _f(r["data"][0].get("totalEq")),
            "avail_eq": _f(usdt.get("availEq")),
            "frozen": _f(usdt.get("frozenBal")),
            "upl": _f(usdt.get("upl")),
        }

    def get_positions(self) -> list[dict]:
        r = self.account.get_positions(instType="SWAP")
        if r.get("code") != "0":
            raise RuntimeError(f"get_positions failed: {r}")
        return r.get("data", [])

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    def usdt_to_contracts(self, inst_id: str, usdt_amount: float, price: float) -> int:
        ct_val = self.get_ct_val(inst_id)
        base = usdt_amount / price
        contracts = int(base / ct_val)
        lot = int(self.get_lot_sz(inst_id))
        if lot > 1:
            contracts = (contracts // lot) * lot
        return max(contracts, 0)

    def place_market_order(
        self,
        inst_id: str,
        side: str,
        sz: int,
        reduce_only: bool = False,
    ) -> dict:
        params = dict(
            instId=inst_id,
            tdMode="cross",
            side=side,
            ordType="market",
            sz=str(sz),
        )
        if reduce_only:
            params["reduceOnly"] = True
        return self._safe_order(self.trade.place_order, **params)

    def place_batch_orders(self, orders: list[dict]) -> list[dict]:
        results = []
        for i in range(0, len(orders), 20):
            batch = orders[i:i + 20]
            r = self._safe_request(self.trade.place_multiple_orders, batch)
            results.extend(r.get("data", []))
        return results

    def _safe_order(self, fn, **kwargs) -> dict:
        for attempt in range(3):
            r = fn(**kwargs)
            code = r.get("code", "-1")
            if code == "0":
                s_code = r["data"][0].get("sCode", "0") if r.get("data") else "0"
                if s_code == "0":
                    return r
                raise RuntimeError(
                    f"Order rejected: {r['data'][0].get('sMsg', '')} (sCode={s_code})"
                )
            if code in ("50011", "50013"):
                time.sleep(2 * (attempt + 1))
                continue
            raise RuntimeError(f"OKX API error: code={code}, msg={r.get('msg', '')}")
        raise RuntimeError("Max retries exceeded for OKX order")

    def _safe_request(self, fn, *args, **kwargs) -> dict:
        for attempt in range(3):
            r = fn(*args, **kwargs)
            code = r.get("code", "-1")
            if code == "0":
                return r
            if code in ("50011", "50013"):
                time.sleep(2 * (attempt + 1))
                continue
            raise RuntimeError(f"OKX API error: code={code}, msg={r.get('msg', '')}")
        raise RuntimeError("Max retries exceeded for OKX request")

    # ------------------------------------------------------------------
    # WebSocket (tickers)
    # ------------------------------------------------------------------

    async def subscribe_tickers(
        self,
        symbols: list[str],
        callback: Callable,
    ) -> WsPublicAsync:
        url = (
            "wss://wspap.okx.com:8443/ws/v5/public"
            if self._demo
            else "wss://ws.okx.com:8443/ws/v5/public"
        )
        self._ws = WsPublicAsync(url=url)
        await self._ws.start()
        args = [{"channel": "tickers", "instId": s} for s in symbols]
        await self._ws.subscribe(args, callback=callback)
        log.info("Subscribed to tickers: %s", symbols)
        return self._ws

    async def close_ws(self) -> None:
        if self._ws:
            try:
                await self._ws.stop()
            except Exception:
                pass
            self._ws = None

    # ------------------------------------------------------------------
    # Ping (measure REST latency)
    # ------------------------------------------------------------------

    def ping_ms(self) -> int:
        start = time.time()
        self.account.get_account_balance()
        return int((time.time() - start) * 1000)
