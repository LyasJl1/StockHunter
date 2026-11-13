"""Historique long : stockage Parquet séparé, plafond de symboles mensuels, reprise. Données SYNTHÉTIQUES."""

from datetime import date, timedelta

from stockhunter.data.http import QuotaExceeded
from stockhunter.history.store import HistoryStore, fetch


class FakeTiingo:
    NAME = "tiingo"

    def __init__(self, fail_on=None, quota_on=None):
        self.calls, self.fail_on, self.quota_on = [], fail_on, quota_on

    def fetch_prices(self, symbol, start, end=None):
        self.calls.append(symbol)
        if symbol == self.quota_on:
            raise QuotaExceeded("tiingo", "50 requêtes/heure")
        if symbol == self.fail_on:
            raise RuntimeError("404")
        d0 = date(2015, 1, 2)
        return [{"bar_date": d0 + timedelta(days=i), "open": 10.0 + i, "high": 11.0 + i, "low": 9.0 + i, "close": 10.5 + i,
                 "volume": 1e6, "adj_open": 5.0, "adj_high": 5.5, "adj_low": 4.5, "adj_close": 5.25, "adj_volume": 2e6,
                 "div_cash": 0.0, "split_factor": 1.0} for i in range(30)]


def test_monthly_symbol_cap_resume_and_already_tracked_symbols(tmp_path):
    st = HistoryStore(tmp_path)
    tg = FakeTiingo()
    today = date(2026, 11, 13)
    out = fetch(st, tg, ["ZZAA", "ZZBB", "ZZCC", "ZZDD"], date(2015, 1, 1), max_new_symbols=2,
                already_used={"ZZAA"}, today=today, progress=lambda m: None)
    # ZZAA déjà interrogé ce mois (suivi chaque soir) : ne consomme rien ; plafond de 2 NOUVEAUX titres
    assert [t for t, _ in out["collectes"]] == ["ZZAA", "ZZBB", "ZZCC"] and out["plafond_atteint"]
    assert out["nouveaux_symboles_ce_mois"] == 2
    again = fetch(st, tg, ["ZZAA", "ZZBB", "ZZCC", "ZZDD"], date(2015, 1, 1), max_new_symbols=2,
                  already_used={"ZZAA"}, today=today, progress=lambda m: None)
    assert again["collectes"] == [] and again["ignores_deja_presents"] == 3 and again["plafond_atteint"]
    nxt = fetch(st, tg, ["ZZDD"], date(2015, 1, 1), max_new_symbols=2, today=date(2026, 12, 1), progress=lambda m: None)
    assert [t for t, _ in nxt["collectes"]] == ["ZZDD"]                    # nouveau mois : nouveau budget
    df = st.read("ZZBB")
    assert len(df) == 30 and {"adj_close", "div_cash", "split_factor"} <= set(df.columns)
    assert st.manifest()["titres"]["ZZBB"]["premiere"] == "2015-01-02" and st.size_mb() > 0


def test_errors_and_quota_stop_cleanly_and_still_count_the_symbol(tmp_path):
    st = HistoryStore(tmp_path)
    out = fetch(st, FakeTiingo(fail_on="ZZBB", quota_on="ZZCC"), ["ZZAA", "ZZBB", "ZZCC", "ZZDD"], date(2015, 1, 1),
                max_new_symbols=10, today=date(2026, 11, 13), progress=lambda m: None)
    assert [t for t, _ in out["collectes"]] == ["ZZAA"] and set(out["erreurs"]) == {"ZZBB", "ZZCC"}
    assert out["nouveaux_symboles_ce_mois"] == 3                           # un appel en échec compte quand même
    assert st.tickers() == ["ZZAA"]
