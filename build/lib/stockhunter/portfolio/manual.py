"""Portefeuille RÉEL saisi à la main (« My Investments »). Calculs au prix moyen pondéré :
- achat : le coût de revient inclut les frais ;
- vente partielle : P&L réalisé = quantité × (prix − prix moyen) − frais ; le prix moyen ne change pas ;
- dividende : montant net (après frais/retenue saisis), compté à part (performance avec / sans dividendes).
Valorisation : dernière clôture connue et datée (Tiingo pour l'univers suivi, sinon univers large Massive),
jamais présentée comme un cours en temps réel. Conversion EUR/USD : dernier taux FRED (DEXUSEU), daté.
Ces chiffres ne sont jamais mélangés avec le portefeuille simulé.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Engine

from stockhunter.db.models import (
    Instrument,
    MacroObservation,
    MacroSeries,
    ManualTransaction,
    MarketSplit,
    PriceBar,
)
from stockhunter.db.session import session_scope

KINDS = ("BUY", "SELL", "DIVIDEND")


class TransactionError(ValueError):
    pass


@dataclass
class Position:
    symbol: str
    currency: str
    quantity: float = 0.0
    cost: float = 0.0              # coût de revient restant (frais inclus)
    invested: float = 0.0          # total investi (achats + frais d'achat)
    realized: float = 0.0
    dividends: float = 0.0
    first_buy: date | None = None
    warnings: list[str] = field(default_factory=list)
    price: float | None = None
    price_date: date | None = None
    price_source: str | None = None
    sector: str | None = None

    @property
    def avg_cost(self) -> float | None:
        return self.cost / self.quantity if self.quantity > 1e-12 else None

    @property
    def value(self) -> float | None:
        return self.quantity * self.price if self.price is not None else None

    @property
    def unrealized(self) -> float | None:
        return self.value - self.cost if self.value is not None else None

    def as_dict(self) -> dict[str, Any]:
        d = {k: getattr(self, k) for k in ("symbol", "currency", "quantity", "cost", "invested", "realized", "dividends",
                                         "first_buy", "warnings", "price", "price_date", "price_source", "sector",
                                         "avg_cost", "value", "unrealized")}
        u = self.unrealized
        d["unrealized_pct"] = u / self.cost if u is not None and self.cost > 0 else None
        base = self.invested or None
        gain = (u or 0) + self.realized
        d["return_without_div"] = gain / base if base else None
        d["return_with_div"] = (gain + self.dividends) / base if base else None
        return d


def validate(tx: dict) -> dict:
    kind = str(tx.get("kind", "")).upper()
    if kind not in KINDS:
        raise TransactionError(f"type d'opération inconnu : {kind}")
    sym = str(tx.get("symbol", "")).strip().upper()
    if not sym or len(sym) > 20:
        raise TransactionError("symbole invalide")
    cur = str(tx.get("currency", "USD")).upper()
    if cur not in ("USD", "EUR"):
        raise TransactionError("devise : USD ou EUR")
    qty, price, fees = float(tx.get("quantity") or 0), float(tx.get("price") or 0), float(tx.get("fees") or 0)
    d = tx["trade_date"] if isinstance(tx["trade_date"], date) else date.fromisoformat(str(tx["trade_date"]))
    if d > date.today():
        raise TransactionError("date dans le futur")
    if fees < 0 or price < 0:
        raise TransactionError("prix et frais doivent être positifs")
    if kind in ("BUY", "SELL") and (qty <= 0 or price <= 0):
        raise TransactionError("quantité et prix doivent être strictement positifs")
    if kind == "DIVIDEND" and price <= 0:
        raise TransactionError("montant du dividende (champ prix) strictement positif")
    return {"symbol": sym, "trade_date": d, "kind": kind, "quantity": qty if kind != "DIVIDEND" else 0.0,
            "price": price, "fees": fees, "currency": cur, "note": (tx.get("note") or None)}


def compute_positions(txs: list[dict]) -> dict[str, Position]:
    pos: dict[str, Position] = {}
    for t in sorted(txs, key=lambda x: (x["trade_date"], x.get("id") or 0)):
        p = pos.setdefault(t["symbol"], Position(t["symbol"], t["currency"]))
        if t["currency"] != p.currency:
            p.warnings.append(f"devises mélangées ({p.currency}/{t['currency']}) : vérifier la saisie")
        if t["kind"] == "BUY":
            amount = t["quantity"] * t["price"] + t["fees"]
            p.quantity += t["quantity"]
            p.cost += amount
            p.invested += amount
            p.first_buy = p.first_buy or t["trade_date"]
        elif t["kind"] == "SELL":
            qty = t["quantity"]
            if qty > p.quantity + 1e-9:
                p.warnings.append(f"vente de {qty:g} le {t['trade_date']} supérieure à la position ({p.quantity:g})")
                qty = p.quantity
            if qty <= 0:
                continue
            avg = p.cost / p.quantity
            p.realized += qty * (t["price"] - avg) - t["fees"]
            p.cost -= avg * qty
            p.quantity -= qty
            if p.quantity < 1e-9:
                p.quantity, p.cost = 0.0, 0.0
        else:
            p.dividends += t["price"] - t["fees"]
    return pos


def latest_fx(s, as_of: date | None = None) -> tuple[float, date] | None:
    q = (select(MacroObservation.value, MacroObservation.obs_date).join(MacroSeries)
         .where(MacroSeries.series_code == "DEXUSEU", MacroObservation.value.is_not(None)))
    if as_of:
        q = q.where(MacroObservation.obs_date <= as_of)
    row = s.execute(q.order_by(MacroObservation.obs_date.desc()).limit(1)).first()
    return (row[0], row[1]) if row else None


def _broad_last(market_dir: Path | None, symbols: set[str]) -> dict[str, tuple[float, date]]:
    if not market_dir or not symbols:
        return {}
    from stockhunter.market.store import MarketStore
    st = MarketStore(market_dir)
    sessions = st.sessions()
    out: dict[str, tuple[float, date]] = {}
    for d in reversed(sessions[-5:]):   # dernière séance où le titre a coté
        df = st.load(start=d, end=d, tickers=symbols - set(out))
        for _, r in df.iterrows():
            out[r["ticker"]] = (float(r["close"]), d)
        if len(out) == len(symbols):
            break
    return out


def portfolio(engine: Engine, market_dir: Path | None = None) -> dict[str, Any]:
    with session_scope(engine) as s:
        rows = [dict(id=t.id, symbol=t.symbol, trade_date=t.trade_date, kind=t.kind, quantity=t.quantity,
                     price=t.price, fees=t.fees, currency=t.currency, note=t.note)
                for t in s.scalars(select(ManualTransaction).order_by(ManualTransaction.trade_date, ManualTransaction.id))]
        pos = compute_positions(rows)
        missing = set()
        for sym, p in pos.items():
            inst = s.scalar(select(Instrument).where(Instrument.symbol == sym))
            if inst:
                p.sector = inst.sector
                last = s.execute(select(PriceBar.close, PriceBar.bar_date).where(PriceBar.instrument_id == inst.id)
                                 .order_by(PriceBar.bar_date.desc()).limit(1)).first()
                if last:
                    p.price, p.price_date, p.price_source = last.close, last.bar_date, "Tiingo (clôture)"
            if p.price is None:
                missing.add(sym)
            if p.first_buy:
                sp = s.scalar(select(MarketSplit.execution_date).where(MarketSplit.ticker == sym,
                                                                       MarketSplit.execution_date > p.first_buy).limit(1))
                if sp:
                    p.warnings.append(f"split le {sp} après le premier achat : ajuster la quantité saisie si besoin")
        for sym, (px, d) in _broad_last(market_dir, missing).items():
            pos[sym].price, pos[sym].price_date, pos[sym].price_source = px, d, "Massive (clôture)"
        fx = latest_fx(s)

    def to(cur_amount: float, cur: str, target: str) -> float | None:
        if cur == target:
            return cur_amount
        if fx is None:
            return None
        return cur_amount / fx[0] if (cur, target) == ("USD", "EUR") else cur_amount * fx[0]

    totals: dict[str, dict[str, float | None]] = {}
    for target in ("USD", "EUR"):
        agg: dict[str, float | None] = defaultdict(float)
        for p in pos.values():
            for k, v in (("invested", p.invested), ("cost", p.cost), ("value", p.value), ("realized", p.realized),
                         ("dividends", p.dividends)):
                if v is None or agg[k] is None:
                    agg[k] = None if v is None and p.quantity > 0 and k == "value" else agg[k]
                    continue
                c = to(v, p.currency, target)
                agg[k] = None if c is None else agg[k] + c
        agg["unrealized"] = (agg["value"] - agg["cost"]) if agg["value"] is not None and agg["cost"] is not None else None
        for k in ("invested", "cost", "value", "realized", "dividends"):
            agg.setdefault(k, 0.0 if not pos else None)
        totals[target] = dict(agg)
    sectors: dict[str, float] = defaultdict(float)
    for p in pos.values():
        if p.value:
            v = to(p.value, p.currency, "USD")
            if v is not None:
                sectors[p.sector or "Non classé"] += v
    return {"positions": [p.as_dict() for p in sorted(pos.values(), key=lambda x: x.symbol)],
            "totals": totals, "fx": fx, "sectors": dict(sectors), "transactions": rows}


def add_transaction(engine: Engine, tx: dict) -> int:
    clean = validate(tx)
    with session_scope(engine) as s:
        if clean["kind"] == "SELL":
            held = compute_positions([dict(id=t.id, symbol=t.symbol, trade_date=t.trade_date, kind=t.kind,
                                           quantity=t.quantity, price=t.price, fees=t.fees, currency=t.currency)
                                      for t in s.scalars(select(ManualTransaction).where(
                                          ManualTransaction.symbol == clean["symbol"],
                                          ManualTransaction.trade_date <= clean["trade_date"]))])
            have = held.get(clean["symbol"]).quantity if clean["symbol"] in held else 0.0
            if clean["quantity"] > have + 1e-9:
                raise TransactionError(f"vente de {clean['quantity']:g} {clean['symbol']} alors que la position "
                                       f"au {clean['trade_date']} est de {have:g}")
        t = ManualTransaction(**clean)
        s.add(t)
        s.flush()
        return t.id


def delete_transaction(engine: Engine, tx_id: int) -> bool:
    with session_scope(engine) as s:
        t = s.get(ManualTransaction, tx_id)
        if t is None:
            return False
        s.delete(t)
        return True
