"""Fixtures SYNTHÉTIQUES des marchés mondiaux (3.2) : ISIN fictifs « ZZ… », symboles ZZ*, cours générés.
Jamais utilisées en production. Les formats reproduisent ceux des sources réelles (sondages du 14/11/2026)."""

from __future__ import annotations

import io
import zipfile
from datetime import date

import numpy as np
import pandas as pd

from stockhunter.world import markets as mk
from stockhunter.world.fx import FxTable
from stockhunter.world.providers import BAR_COLUMNS

EURONEXT_HEADER = ('Name;ISIN;Symbol;Market;Currency;"Open Price";"High Price";"low Price";"last Price";'
                   '"last Trade MIC Time";"Time Zone";Volume;Turnover;"Closing Price";"Closing Price DateTime"')


def euronext_csv(stamp: str, rows: list[tuple]) -> bytes:
    """rows : (nom, isin, symbole, marché, devise, open, high, low, close, volume, turnover)."""
    out = ["﻿" + EURONEXT_HEADER, '"European Equities"', f'"{stamp}"',
           '"All datapoints provided as of end of last active trading day."']
    for n, isin, sym, mkt, ccy, o, h, lo, c, v, t in rows:
        out.append(f'"{n}";{isin};{sym};"{mkt}";{ccy};{o};{h};{lo};{c};" 17:35";CET;{v};{t};{c};')
    return "\n".join(out).encode("utf-8")


BHAV_HEADER = ("TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,FininstrmActlXpryDt,"
               "StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,PrvsClsgPric,UndrlygPric,"
               "SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,Rmks,"
               "Rsvd1,Rsvd2,Rsvd3,Rsvd4")


def bhavcopy(d: date, rows: list[tuple], src: str = "NSE", zipped: bool = True) -> bytes:
    """rows : (isin, symbole, série, nom, open, high, low, close, prev_close_adj, volume, turnover, lot)."""
    out = [BHAV_HEADER]
    for isin, sym, ser, name, o, h, lo, c, prev, v, t, lot in rows:
        out.append(f"{d},{d},CM,{src},STK,1,{isin},{sym},{ser},,,,,{name},{o},{h},{lo},{c},{c},{prev},,{c},,,"
                   f"{v},{t},10,F1,{lot},,,,,")
    body = "\n".join(out).encode()
    if not zipped:
        return body
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(f"BhavCopy_{src}_CM_0_0_0_{d:%Y%m%d}_F_0000.csv", body)
    return buf.getvalue()


def fx_table(start: date, end: date, usd: float = 1.10, inr: float = 95.0, nok: float = 11.5,
             drift_inr: float = 0.0) -> FxTable:
    days = pd.bdate_range(start, end)
    rows = []
    for i, d in enumerate(days):
        rows += [{"date": d.date(), "currency": "USD", "rate": usd},
                 {"date": d.date(), "currency": "INR", "rate": inr * (1 + drift_inr) ** i},
                 {"date": d.date(), "currency": "NOK", "rate": nok}]
    return FxTable.from_long(pd.DataFrame(rows))


def synth_market(store, code: str, n_sessions: int, n_stocks: int = 40, end: date = date(2026, 11, 13),
                 seed: int = 7, currency: str | None = None, trend_first: int = 6, turnover_scale: float = 5e7,
                 isin_prefix: str = "ZZ") -> list[date]:
    """Écrit `n_sessions` séances d'une place dans le stockage ; les `trend_first` premiers titres montent."""
    m = mk.get(code)
    ss = [s.session for s in mk.sessions(m, date(2020, 1, 1), end)][-n_sessions:]
    rng = np.random.default_rng(seed)
    price = np.full(n_stocks, 100.0)
    for k, d in enumerate(ss):
        drift = np.where(np.arange(n_stocks) < trend_first, 0.004, 0.0)
        r = rng.normal(drift, 0.012)
        o = price * (1 + rng.normal(0, 0.002, n_stocks))
        price = price * (1 + r)
        hi, lo = np.maximum(o, price) * 1.006, np.minimum(o, price) * 0.994
        rows = []
        for j in range(n_stocks):
            vol = 1_000_000.0
            rows.append({"date": d, "market": code, "ticker": f"ZZ{j:02d}", "isin": f"{isin_prefix}{code[:2]}{j:08d}",
                         "name": f"ZZ Synthétique {j}", "currency": currency or m.currency, "segment": "Réglementé",
                         "open": o[j], "high": hi[j], "low": lo[j], "close": price[j], "volume": vol,
                         "turnover": turnover_scale, "prev_close_adj": None, "lot_size": 1, "traded": True})
        store.write_session(code, d, pd.DataFrame(rows, columns=BAR_COLUMNS), "synthetique")
    return ss
