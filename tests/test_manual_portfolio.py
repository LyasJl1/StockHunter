"""Portefeuille réel saisi à la main (opérations FICTIVES de test)."""

from datetime import date

import pytest

from stockhunter.data.ingestion import ensure_sources
from stockhunter.db.models import Instrument, MacroObservation, MacroSeries, PriceBar
from stockhunter.db.session import session_scope
from stockhunter.portfolio import manual
from stockhunter.portfolio.manual import TransactionError


def tx(sym, d, kind, q=0.0, p=0.0, fees=0.0, cur="USD"):
    return {"symbol": sym, "trade_date": date.fromisoformat(d), "kind": kind, "quantity": q, "price": p,
            "fees": fees, "currency": cur}


def test_average_cost_partial_sale_and_dividends():
    pos = manual.compute_positions([
        tx("ZZA", "2026-01-05", "BUY", 10, 100, fees=10),     # coût 1010
        tx("ZZA", "2026-02-05", "BUY", 10, 120, fees=10),     # coût total 2220, prix moyen 111
        tx("ZZA", "2026-03-05", "SELL", 5, 130, fees=5),      # réalisé = 5×(130−111) − 5 = 90
        tx("ZZA", "2026-04-05", "DIVIDEND", p=12, fees=2),    # dividende net 10
    ])["ZZA"]
    assert pos.quantity == 15 and pos.avg_cost == pytest.approx(111)
    assert pos.realized == pytest.approx(90) and pos.dividends == pytest.approx(10)
    assert pos.invested == pytest.approx(2220) and pos.cost == pytest.approx(1665)
    pos.price = 140.0
    d = pos.as_dict()
    assert d["unrealized"] == pytest.approx(15 * 140 - 1665)
    gain = (15 * 140 - 1665) + 90
    assert d["return_without_div"] == pytest.approx(gain / 2220)
    assert d["return_with_div"] == pytest.approx((gain + 10) / 2220)


def test_validation_rules():
    with pytest.raises(TransactionError):
        manual.validate(tx("ZZA", "2026-01-05", "BUY", 0, 100))
    with pytest.raises(TransactionError, match="futur"):
        manual.validate(tx("ZZA", "2099-01-05", "BUY", 1, 100))
    with pytest.raises(TransactionError, match="devise"):
        manual.validate(tx("ZZA", "2026-01-05", "BUY", 1, 100, cur="GBP"))
    assert manual.validate(tx("zza ", "2026-01-05", "BUY", 1, 100))["symbol"] == "ZZA"


def test_portfolio_valuation_fx_and_oversell_refused(engine):
    src = ensure_sources(engine)
    with session_scope(engine) as s:
        inst = Instrument(symbol="ZZA", asset_type="stock", sector="Technology")
        s.add(inst)
        s.flush()
        s.add(PriceBar(instrument_id=inst.id, source_id=src["tiingo"], bar_date=date(2026, 11, 11), open=150,
                       high=150, low=150, close=150.0, volume=1))
        ser = MacroSeries(series_code="DEXUSEU", name="fx", source_id=src["fred"])
        s.add(ser)
        s.flush()
        s.add(MacroObservation(series_id=ser.id, obs_date=date(2026, 11, 6), value=1.25))
    manual.add_transaction(engine, tx("ZZA", "2026-01-05", "BUY", 10, 100))
    with pytest.raises(TransactionError, match="position"):
        manual.add_transaction(engine, tx("ZZA", "2026-02-05", "SELL", 11, 120))
    pf = manual.portfolio(engine)
    p = pf["positions"][0]
    assert p["price"] == 150 and p["price_date"] == date(2026, 11, 11) and p["price_source"].startswith("Tiingo")
    assert pf["totals"]["USD"]["value"] == pytest.approx(1500)
    assert pf["totals"]["EUR"]["value"] == pytest.approx(1500 / 1.25) and pf["fx"][1] == date(2026, 11, 6)
    assert pf["sectors"] == {"Technology": 1500.0}


def test_unknown_price_is_not_invented(engine):
    manual.add_transaction(engine, tx("ZZUNKNOWN", "2026-01-05", "BUY", 1, 10))
    pf = manual.portfolio(engine)
    assert pf["positions"][0]["price"] is None and pf["positions"][0]["value"] is None
    assert pf["totals"]["USD"]["value"] is None
