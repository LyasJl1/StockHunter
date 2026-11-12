"""Phase 6 : catalyseurs, actualités, synthèse sourcée, alertes. Données SYNTHÉTIQUES (symboles ZZ*)."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from stockhunter.analysis.synthesis import build, to_markdown, to_telegram
from stockhunter.catalysts.classify import classify_8k, classify_headline
from stockhunter.data.ingestion import ensure_sources, ingest_news
from stockhunter.data.providers import parse_finnhub_news
from stockhunter.db.models import Filing, NewsItem, NotificationLog
from stockhunter.db.session import session_scope
from stockhunter.jobs.runner import JobContext
from stockhunter.notifications.engine import answer, enqueue_catalyst_alerts
from tests.test_paper import SIG, setup

UTC = timezone.utc


def test_classify_8k_order_and_watch():
    cats = classify_8k("9.01,2.02,4.02")
    assert [c.item for c in cats] == ["2.02", "4.02", "9.01"]
    assert [c.watch for c in cats] == [False, True, False]
    assert classify_8k("") == [] and classify_8k("7.77")[0].label == "Rubrique 7.77"


def test_classify_headline_rules():
    assert classify_headline("ZZ Corp faces class action lawsuit over disclosures") == "legal"
    assert classify_headline("FDA approval for ZZ drug") == "regulatory"
    assert classify_headline("ZZ board approves quarterly dividend") == "capital"   # pas « réglementaire »
    assert classify_headline("ZZ to acquire rival in $2 billion deal") == "mna"
    assert classify_headline("ZZ raises full-year guidance") == "guidance"
    assert classify_headline("Analyst upgrades ZZ to outperform") == "analyst"
    assert classify_headline("Five things to watch this week") == "other"


def test_parse_finnhub_news_skips_incomplete_rows():
    rows = parse_finnhub_news([
        {"id": 1, "datetime": 1762900000, "headline": " Titre ", "url": "https://ex.test/a", "source": "Ex", "summary": ""},
        {"id": 2, "datetime": None, "headline": "sans date", "url": "https://ex.test/b"},
        {"id": 3, "datetime": 1762900000, "headline": "", "url": "https://ex.test/c"},
        {"id": 4, "datetime": 1762900000, "headline": "lien invalide", "url": "javascript:alert(1)"},
        "pas un objet",
    ])
    assert len(rows) == 1
    r = rows[0]
    assert r["headline"] == "Titre" and r["summary"] is None and r["published_at"].tzinfo is not None


class FakeFinnhub:
    def __init__(self, items):
        self.items, self.calls = items, []

    def fetch_company_news(self, symbol, start, end):
        self.calls.append(symbol)
        return self.items.get(symbol, [])


def _news(i, when, headline):
    return {"external_id": str(i), "published_at": when, "headline": headline, "summary": None,
            "publisher": "ZZ Wire", "url": f"https://ex.test/{i}"}


def test_ingest_news_idempotent_and_classified(engine):
    _src, ids, _ = setup(engine, ("ZZA", "ZZB"))
    fake = FakeFinnhub({"ZZA": [_news(1, datetime(2026, 11, 1, 15, tzinfo=UTC), "ZZA hit by antitrust probe"),
                                _news(2, datetime(2026, 11, 1, 16, tzinfo=UTC), "ZZA launches product")]})
    for _ in range(2):
        ingest_news(engine, JobContext(run_id=0), fake, today=SIG)
    with session_scope(engine) as s:
        assert s.scalar(select(func.count()).select_from(NewsItem)) == 2
        cats = dict(s.execute(select(NewsItem.external_id, NewsItem.category)).all())
    assert cats == {"1": "legal", "2": "business"}
    assert sorted(set(fake.calls)) == ["ZZA", "ZZB"]


def _filing(s, src, iid, acc, d, items, url="https://www.sec.gov/x"):
    s.add(Filing(instrument_id=iid, source_id=src, accession=acc, form="8-K", filed_date=d, items=items, url=url))


def _add_news(s, src, iid, ext, when, headline, cat):
    s.add(NewsItem(instrument_id=iid, source_id=src, external_id=ext, published_at=when, headline=headline,
                   publisher="ZZ Wire", url=f"https://ex.test/{ext}", category=cat))


def test_synthesis_point_in_time_and_family_e(engine):
    _src, ids, _ = setup(engine, ("ZZA",))
    sec = ensure_sources(engine)["sec_edgar"]
    fin = ensure_sources(engine)["finnhub"]
    iid = ids["ZZA"]
    with session_scope(engine) as s:
        _filing(s, sec, iid, "A1", SIG - timedelta(days=3), "2.02,9.01")        # résultats : pas E
        _filing(s, sec, iid, "A2", SIG - timedelta(days=5), "1.01")             # accord important : E
        _filing(s, sec, iid, "A3", SIG + timedelta(days=1), "4.02")             # FUTUR : exclu
        _add_news(s, fin, iid, "n1", datetime(2026, 10, 30, 14, tzinfo=UTC), "ZZA <b>faces</b> lawsuit", "legal")
        # publié le soir de SIG à New York (encore SIG) -> inclus ; le lendemain -> exclu
        _add_news(s, fin, iid, "n2", datetime(2026, 11, 3, 3, tzinfo=UTC), "ZZA late news", "other")
        _add_news(s, fin, iid, "n3", datetime(2026, 11, 3, 15, tzinfo=UTC), "ZZA FUTURE", "mna")
    syn = build(engine, "ZZA", SIG)
    md = to_markdown(syn)
    assert "FUTURE" not in md and "4.02" not in md and "comptes" not in md.lower()
    assert "late news" in md and "Accord important conclu" in md
    assert syn.family_e["active"] and all("résultats" not in h for h in syn.family_e["hits"])
    assert any("lawsuit" in ln.text for _t, lines in syn.sections for ln in lines if _t == "Points de vigilance")
    # chaque catalyseur porte un lien vers sa source
    cat = dict(syn.sections)["Catalyseurs des 30 derniers jours"]
    assert all(ln.sources and ln.sources[0].url for ln in cat)
    tg = to_telegram(syn)
    assert "<b>faces</b>" not in tg and "&lt;b&gt;faces&lt;/b&gt;" in tg     # HTML de la source échappé
    assert "probabilité" in tg                                                 # rappel : score ≠ probabilité


def test_family_e_inactive_with_results_only(engine):
    _src, ids, _ = setup(engine, ("ZZA",))
    sec = ensure_sources(engine)["sec_edgar"]
    with session_scope(engine) as s:
        _filing(s, sec, ids["ZZA"], "B1", SIG - timedelta(days=2), "2.02,9.01")
    assert not build(engine, "ZZA", SIG).family_e["active"]


def test_catalyst_alerts_only_watched_and_deduplicated(engine):
    _src, ids, _ = setup(engine, ("ZZA", "ZZB"))
    sec = ensure_sources(engine)["sec_edgar"]
    with session_scope(engine) as s:
        from stockhunter.db.models import Score
        s.query(Score).filter(Score.instrument_id == ids["ZZB"]).update({"status": "WATCHLIST"})
        _filing(s, sec, ids["ZZA"], "C1", SIG, "2.06")          # dépréciation : alerte
        _filing(s, sec, ids["ZZA"], "C2", SIG, "2.02,9.01")     # résultats seuls : rapport, pas d'alerte
        _filing(s, sec, ids["ZZB"], "C3", SIG, "1.03")          # action non suivie de près : pas d'alerte
    assert enqueue_catalyst_alerts(engine) == 1
    assert enqueue_catalyst_alerts(engine) == 0                 # déduplication
    with session_scope(engine) as s:
        texts = list(s.scalars(select(NotificationLog.text).where(NotificationLog.kind == "alert")))
    assert len(texts) == 1 and "ZZA" in texts[0] and "Dépréciation" in texts[0]


def test_analyse_command(engine):
    setup(engine, ("ZZA",))
    assert "Analyse ZZA" in answer(engine, "/analyse zza")
    assert "hors" in answer(engine, "/analyse NOPE") or "ne fait pas partie" in answer(engine, "/analyse NOPE")
    assert "Usage" in answer(engine, "/analyse")
    assert "/analyse" in answer(engine, "/help")
