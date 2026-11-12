"""Dashboard web (base SYNTHÉTIQUE, aucun serveur réel)."""

import re
from datetime import date

import pytest
from fastapi.testclient import TestClient

from stockhunter.dashboard.app import create_app
from stockhunter.dashboard.auth import hash_password, verify_password
from stockhunter.portfolio import paper
from tests.test_paper import bars, setup

PW = "mot-de-passe-de-test"


@pytest.fixture
def client(engine, settings):
    app = create_app(engine, settings, secret="s" * 48, password_hash=hash_password(PW), secure_cookies=False)
    return TestClient(app)


def login(c):
    r = c.post("/login", data={"password": PW}, follow_redirects=False)
    assert r.status_code == 303
    page = c.get("/").text
    return re.search(r'name="csrf" value="([^"]+)"', page).group(1)


def test_password_hashing():
    h = hash_password("abc")
    assert verify_password("abc", h) and not verify_password("abd", h) and not verify_password("abc", None)
    assert "abc" not in h


def test_auth_required_and_rate_limit(client):
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"
    assert client.get("/api/prices/ZZA", follow_redirects=False).status_code == 303
    assert client.get("/health").json()["status"] == "ok"   # seule route publique
    for _ in range(5):
        assert client.post("/login", data={"password": "faux"}).status_code == 401
    assert client.post("/login", data={"password": PW}).status_code == 429   # bloqué même avec le bon mot de passe


def test_pages_render_with_data(engine, client):
    src, ids, run = setup(engine)
    paper.plan_from_scan(engine, run)
    bars(engine, src, ids["ZZA"], [("2026-11-03", 100, 101, 99, 100.5)])
    paper.process(engine)
    from stockhunter.data.ingestion import ensure_sources
    from stockhunter.db.models import Filing
    from stockhunter.db.session import session_scope
    with session_scope(engine) as s:   # cas réel : la colonne « items » d'un 8-K (bug du 12/11/2026)
        s.add(Filing(instrument_id=ids["ZZA"], source_id=ensure_sources(engine)["sec_edgar"], accession="FX-8K",
                     form="8-K", filed_date=date(2026, 10, 29), items="2.02,9.01", url="https://example.invalid/8k"))
    login(client)
    for url, needle in [("/", "Portefeuille simulé"), ("/opportunities", "ZZA"), ("/stock/ZZA", "· résultats"),
                        ("/portfolio", "Positions ouvertes"), ("/portfolio?tab=real", "Saisir une opération"),
                        ("/performance", "Drawdown"), ("/settings", "Paper trading")]:
        r = client.get(url)
        assert r.status_code == 200 and needle in r.text, url
        assert r.headers["x-frame-options"] == "DENY"
    assert client.get("/opportunities?status=REJECTED", headers={"HX-Request": "true"}).text.count("<tr>") == 2
    assert client.get("/api/prices/ZZA").json()["close"][-1] == 100.5
    assert client.get("/stock/INCONNU").status_code == 404


def test_manual_transactions_with_csrf(client):
    csrf = login(client)
    form = {"symbol": "ZZA", "trade_date": "2026-01-05", "kind": "BUY", "quantity": "10", "price": "100,5",
            "fees": "1", "currency": "USD"}
    assert client.post("/portfolio/transactions", data=form, follow_redirects=False).status_code == 403  # sans jeton
    client.post("/portfolio/transactions", data={**form, "csrf": csrf})
    page = client.get("/portfolio?tab=real").text
    assert "ZZA" in page and "100,50" in page
    bad = client.post("/portfolio/transactions", data={**form, "kind": "SELL", "quantity": "50", "csrf": csrf},
                      follow_redirects=False)
    assert "error=" in bad.headers["location"]


def test_settings_bounds_and_effect(engine, client):
    csrf = login(client)
    r = client.post("/settings", data={"csrf": csrf, "capital": "50000", "risk_fraction": "0.5", "max_positions": "5",
                                       "max_per_sector": "2"}, follow_redirects=False)
    assert "error=" in r.headers["location"]                     # 50 % de risque refusé
    client.post("/settings", data={"csrf": csrf, "capital": "50000", "risk_fraction": "0.01", "max_positions": "5",
                                   "max_per_sector": "2"})
    cfg = paper.load_config(engine)
    assert cfg.capital == 50000 and cfg.risk_fraction == 0.01 and cfg.max_positions == 5
    assert date.today()


def test_settings_sources_from_database(engine, client):
    """Les sources affichent le dernier appel réussi en base, pas l'absence de clé sur le serveur web."""
    from datetime import datetime, timezone
    from stockhunter.db.models import ApiCall
    from stockhunter.db.session import session_scope
    with session_scope(engine) as s:
        s.add(ApiCall(provider="tiingo", endpoint="/fixture", status_code=200,
                      called_at=datetime(2026, 11, 12, 23, 20, tzinfo=timezone.utc)))
        s.add(ApiCall(provider="fred", endpoint="/fixture", status_code=500,
                      called_at=datetime(2026, 11, 12, 23, 21, tzinfo=timezone.utc)))
    login(client)
    page = client.get("/settings").text
    assert "12/11/2026 23:20 UTC" in page          # tiingo : appel réussi
    assert "23:21" not in page                      # fred : échec non compté comme réussi
    assert "MANQUANT: SH_TIINGO_API_KEY" not in page
