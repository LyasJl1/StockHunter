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
    assert "13/11/2026 00:20 (Paris)" in page          # tiingo : appel réussi
    assert "23:21" not in page                      # fred : échec non compté comme réussi
    assert "MANQUANT: SH_TIINGO_API_KEY" not in page


def test_csp_nonce_and_no_unsafe_inline_script(engine, client):
    login(client)
    r = client.get("/performance")
    csp = r.headers["content-security-policy"]
    assert "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0]
    nonce = re.search(r"'nonce-([^']+)'", csp).group(1)
    scripts = re.findall(r"<script(?![^>]*\bsrc=)([^>]*)>", r.text)
    assert scripts and all(f'nonce="{nonce}"' in s for s in scripts)
    assert "onsubmit=" not in client.get("/portfolio?tab=real").text      # aucun gestionnaire inline
    assert client.get("/performance").headers["content-security-policy"] != csp   # nonce renouvelé


def test_login_rate_limit_uses_proxy_ip_and_global_cap(engine, settings):
    app = create_app(engine, settings, secret="s" * 48, password_hash=hash_password(PW), secure_cookies=False)
    c = TestClient(app)
    for _ in range(5):
        c.post("/login", data={"password": "faux"}, headers={"X-Forwarded-For": "1.1.1.1, 10.0.0.1"})
    # même client derrière le proxy -> bloqué ; un autre visiteur ne l'est pas
    assert c.post("/login", data={"password": PW}, headers={"X-Forwarded-For": "1.1.1.1, 10.0.0.1"}).status_code == 429
    assert c.post("/login", data={"password": PW}, headers={"X-Forwarded-For": "10.0.0.2"},
                  follow_redirects=False).status_code == 303
    # plafond global : 30 échecs toutes IP confondues bloquent la connexion
    for i in range(30):
        c.post("/login", data={"password": "faux"}, headers={"X-Forwarded-For": f"9.9.{i}.9"})
    assert c.post("/login", data={"password": PW}, headers={"X-Forwarded-For": "8.8.8.8"}).status_code == 429


def test_v3_pages_render_with_discovery_data(engine, client):
    """Scanner, fiche approfondie, laboratoire, santé : rendu complet sur un classement SYNTHÉTIQUE."""
    from stockhunter import pipeline
    from stockhunter.discovery import engine as disc
    from stockhunter.discovery import store as dstore
    from stockhunter.lab.panel import build_panel
    from tests.test_lab import NO_SPLITS, synthetic_long
    long, tickers = synthetic_long(n_tickers=150, n_sessions=430, drift_scale=0.0015)
    p = build_panel(long, NO_SPLITS, tickers)
    past = p.truncate(p.close.index[-60])
    res = disc.run(past, {t: f"{t} Corp <script>" for t in tickers}, validation=dstore.registry_decisions())
    dstore.save(engine, res)
    dstore.deepen(engine, res, past)
    dstore.update_outcomes(engine, p)
    pipeline.run(engine, (pipeline.Step("a", "1 collecte", ("a",), True),), lambda argv: 0)
    login(client)
    top = res["categories"]["momentum"]["candidats"][0]["ticker"]
    page = client.get("/scanner").text
    assert top in page and "pas une recommandation" in page and "NON RETENU" in page
    assert "<script>" not in page.split("</head>")[1].replace('<script nonce', '')   # nom de société échappé
    assert client.get("/scanner?category=shock", headers={"HX-Request": "true"}).status_code == 200
    assert top not in client.get("/scanner?q=INEXISTANT", headers={"HX-Request": "true"}).text
    deep = client.get(f"/discover/{top}")
    assert deep.status_code == 200 and "Plan simulé" in deep.text and "Historique des signaux" in deep.text
    assert client.get("/discover/NOPE").status_code == 404
    lab = client.get("/lab").text
    assert "Aucune stratégie validée" in lab and "v3-V3" in lab and "INTACTE" in lab and "CONSOMMÉE" in lab
    sysp = client.get("/system").text
    assert "Chaîne nocturne" in sysp and "Quotas des sources" in sysp and "tiingo" in sysp
    home = client.get("/").text
    assert "Marché large" in home
    assert "Journal de décision" in client.get("/portfolio").text
    for path in ("/scanner", "/lab", "/system", f"/discover/{top}"):
        assert client.get(path, follow_redirects=False).status_code == 200
    c2 = TestClient(client.app)
    for path in ("/scanner", "/lab", "/system", f"/discover/{top}"):
        assert c2.get(path, follow_redirects=False).status_code == 303          # non connecté : redirection
