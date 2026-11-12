"""Dashboard web StockHunter (FastAPI + Jinja2 + HTMX + Chart.js).

Lancement : `stockhunter dashboard` (voir CLAUDE.md). Exige SH_DASHBOARD_PASSWORD_HASH et SH_DASHBOARD_SECRET.
"""

from __future__ import annotations

import json
import secrets
from datetime import date
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.engine import Engine

from stockhunter.config import Settings
from stockhunter.dashboard import queries
from stockhunter.dashboard.auth import COOKIE, MAX_AGE, RateLimiter, Sessions, verify_password
from stockhunter.db.models import KeyValue
from stockhunter.db.session import session_scope
from stockhunter.portfolio import manual, paper

TEMPLATES = Path(__file__).with_name("templates")
RUNTIME_KEYS = {  # réglages modifiables depuis la page Réglages : (type, min, max)
    "capital": (float, 1_000, 10_000_000), "risk_fraction": (float, 0.001, 0.02),
    "max_positions": (int, 1, 30), "max_per_sector": (int, 1, 10),
}


# ----------------------------------------------------------------------------- filtres d'affichage
def fr_num(x, d=2):
    if not isinstance(x, (int, float)) or isinstance(x, bool) or x != x:
        return "N/D"
    return f"{x:,.{d}f}".replace(",", " ").replace(".", ",")


def fr_pct(x, d=1, signed=True):
    if not isinstance(x, (int, float)) or isinstance(x, bool) or x != x:
        return "N/D"
    return (f"{x * 100:+.{d}f}" if signed else f"{x * 100:.{d}f}").replace(".", ",") + " %"


def fr_date(d):
    if d is None:
        return "N/D"
    return d.strftime("%d/%m/%Y") if hasattr(d, "strftime") else str(d)


def create_app(engine: Engine, settings: Settings, secret: str | None = None, password_hash: str | None = None,
               secure_cookies: bool = True) -> FastAPI:
    secret = secret or (settings.dashboard_secret.get_secret_value() if settings.dashboard_secret else None)
    password_hash = password_hash or (settings.dashboard_password_hash.get_secret_value()
                                      if settings.dashboard_password_hash else None)
    if not secret or not password_hash:
        raise RuntimeError("SH_DASHBOARD_SECRET et SH_DASHBOARD_PASSWORD_HASH sont requis "
                           "(`stockhunter dashboard hash-password` génère le hachage)")
    sessions, limiter = Sessions(secret), RateLimiter()
    app = FastAPI(title="StockHunter AI", docs_url=None, redoc_url=None, openapi_url=None)
    tpl = Jinja2Templates(directory=str(TEMPLATES))
    # scripts embarqués (versions figées, aucune dépendance à un CDN externe)
    app.mount("/static", StaticFiles(directory=str(TEMPLATES.with_name("static"))), name="static")
    tpl.env.filters.update(num=fr_num, pct=fr_pct, d=fr_date)

    def session(request: Request) -> dict | None:
        return sessions.read(request.cookies.get(COOKIE))

    def render(request: Request, name: str, sess: dict, **ctx):
        return tpl.TemplateResponse(request, name, {"csrf": sess["csrf"], "page": name.split(".")[0], **ctx})

    def guard(request: Request) -> dict:
        sess = session(request)
        if sess is None:
            raise HTTPException(status_code=303, headers={"Location": "/login"})
        return sess

    def check_csrf(sess: dict, token: str):
        if not secrets.compare_digest(sess.get("csrf", ""), token or ""):
            raise HTTPException(status_code=403, detail="jeton CSRF invalide")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["Content-Security-Policy"] = ("default-src 'self'; script-src 'self' 'unsafe-inline'; "
                                                   "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                                                   "frame-ancestors 'none'")
        return resp

    # ------------------------------------------------------------------ santé et connexion
    @app.get("/health")
    def health():
        f = queries.freshness(engine)
        return {"status": "ok", "derniere_cloture": str(f["last_price"])}

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request, error: str | None = None):
        return tpl.TemplateResponse(request, "login.html", {"error": error})

    @app.post("/login")
    def login(request: Request, password: str = Form(...)):
        ip = request.client.host if request.client else "?"
        if limiter.blocked(ip):
            return tpl.TemplateResponse(request, "login.html", {"error": "Trop de tentatives : réessayer dans 15 minutes."},
                                        status_code=429)
        if not verify_password(password, password_hash):
            limiter.fail(ip)
            return tpl.TemplateResponse(request, "login.html", {"error": "Mot de passe incorrect."}, status_code=401)
        limiter.reset(ip)
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(COOKIE, sessions.issue(), max_age=MAX_AGE, httponly=True, samesite="lax", secure=secure_cookies)
        return resp

    @app.post("/logout")
    def logout(request: Request, csrf: str = Form("")):
        check_csrf(guard(request), csrf)
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(COOKIE)
        return resp

    # ------------------------------------------------------------------ pages
    @app.get("/", response_class=HTMLResponse)
    def overview(request: Request):
        sess = guard(request)
        meta, rows = queries.score_rows(engine)
        active = [r for r in rows if r["status"] in ("READY", "DEVELOPING")]
        return render(request, "overview.html", sess, meta=meta, top=active[:5], n_active=len(active),
                      n_total=len(rows), pf=paper.summary(engine, paper.load_config(engine)),
                      real=manual.portfolio(engine, settings.market_dir), fresh=queries.freshness(engine))

    @app.get("/opportunities", response_class=HTMLResponse)
    def opportunities(request: Request, status: str = "", strategy: str = "", min_score: str = "", q: str = ""):
        sess = guard(request)
        sts = tuple(x for x in status.split(",") if x) or None
        ms = float(min_score) if min_score.replace(".", "", 1).isdigit() else None
        meta, rows = queries.score_rows(engine, sts, strategy or None, ms, q or None)
        name = "_opportunities_table.html" if request.headers.get("HX-Request") else "opportunities.html"
        return render(request, name, sess, meta=meta, rows=rows,
                      f={"status": status, "strategy": strategy, "min_score": min_score, "q": q})

    @app.get("/stock/{symbol}", response_class=HTMLResponse)
    def stock(request: Request, symbol: str):
        sess = guard(request)
        det = queries.stock_detail(engine, symbol)
        if det is None:
            raise HTTPException(404, "symbole hors univers")
        return render(request, "stock.html", sess, d=det)

    @app.get("/api/prices/{symbol}")
    def prices(request: Request, symbol: str):
        guard(request)
        return JSONResponse(queries.price_series(engine, symbol))

    @app.get("/portfolio", response_class=HTMLResponse)
    def portfolio_page(request: Request, tab: str = "paper", error: str | None = None):
        sess = guard(request)
        cfg = paper.load_config(engine)
        return render(request, "portfolio.html", sess, tab=tab, error=error, pf=paper.summary(engine, cfg), cfg=cfg,
                      real=manual.portfolio(engine, settings.market_dir), today=date.today())

    @app.post("/portfolio/transactions")
    def add_tx(request: Request, csrf: str = Form(""), symbol: str = Form(...), trade_date: str = Form(...),
               kind: str = Form(...), quantity: str = Form("0"), price: str = Form(...), fees: str = Form("0"),
               currency: str = Form("USD"), note: str = Form("")):
        check_csrf(guard(request), csrf)
        try:
            manual.add_transaction(engine, {"symbol": symbol, "trade_date": trade_date, "kind": kind,
                                            "quantity": quantity.replace(",", ".") or 0, "price": price.replace(",", "."),
                                            "fees": fees.replace(",", ".") or 0, "currency": currency, "note": note})
        except (manual.TransactionError, ValueError) as exc:
            return RedirectResponse(f"/portfolio?tab=real&error={quote(str(exc)[:200])}", status_code=303)
        return RedirectResponse("/portfolio?tab=real", status_code=303)

    @app.post("/portfolio/transactions/{tx_id}/delete")
    def del_tx(request: Request, tx_id: int, csrf: str = Form("")):
        check_csrf(guard(request), csrf)
        manual.delete_transaction(engine, tx_id)
        return RedirectResponse("/portfolio?tab=real", status_code=303)

    @app.get("/performance", response_class=HTMLResponse)
    def performance(request: Request):
        sess = guard(request)
        cfg = paper.load_config(engine)
        curve = paper.equity_curve(engine, cfg)
        pf = paper.summary(engine, cfg)
        return render(request, "performance.html", sess, curve=json.dumps(curve), pf=pf, stats=_stats(curve),
                      by_strategy=_by_strategy(engine))

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request, saved: str | None = None, error: str | None = None):
        sess = guard(request)
        from stockhunter.scoring.config import DEFAULT_CONFIG
        from stockhunter.universe import load_universe
        return render(request, "settings.html", sess, cfg=paper.load_config(engine), scoring=DEFAULT_CONFIG,
                      sources=settings.provider_status(), universe=load_universe(settings.universe_file),
                      saved=saved, error=error, morning=settings.report_morning_hour_paris)

    @app.post("/settings")
    async def save_settings(request: Request):
        sess = guard(request)
        form = await request.form()
        check_csrf(sess, form.get("csrf", ""))
        new = {}
        try:
            for k, (typ, lo, hi) in RUNTIME_KEYS.items():
                v = typ(str(form.get(k, "")).replace(",", "."))
                if not lo <= v <= hi:
                    raise ValueError(f"{k} hors bornes [{lo} ; {hi}]")
                new[k] = v
        except ValueError as exc:
            return RedirectResponse(f"/settings?error={quote(str(exc))}", status_code=303)
        with session_scope(engine) as s:
            kv = s.get(KeyValue, "paper_config")
            if kv is None:
                s.add(KeyValue(key="paper_config", value=json.dumps(new)))
            else:
                kv.value = json.dumps(new)
        return RedirectResponse("/settings?saved=1", status_code=303)

    @app.exception_handler(HTTPException)
    async def http_exc(request: Request, exc: HTTPException):
        if exc.status_code == 303:
            return RedirectResponse(exc.headers["Location"], status_code=303)
        return HTMLResponse(f"<h1>{exc.status_code}</h1><p>{exc.detail}</p>", status_code=exc.status_code)

    return app


def _stats(curve: list[dict]) -> dict:
    if len(curve) < 2:
        return {}
    import pandas as pd
    eq = pd.Series([c["equity"] for c in curve])
    spy = pd.Series([c["spy"] for c in curve]).ffill()
    dd = (eq / eq.cummax() - 1).min()
    return {"jours": len(curve), "rendement": eq.iloc[-1] / eq.iloc[0] - 1, "drawdown_max": dd,
            "spy": (spy.iloc[-1] / spy.iloc[0] - 1) if spy.notna().all() else None}


def _by_strategy(engine: Engine) -> list[dict]:
    from sqlalchemy import select

    from stockhunter.db.models import PaperTrade
    out: dict[str, dict] = {}
    with session_scope(engine) as s:
        for t in s.scalars(select(PaperTrade).where(PaperTrade.status == "CLOSED")):
            for k in (t.strategies or "-").split(","):
                d = out.setdefault(k, {"strategie": k, "trades": 0, "pnl": 0.0, "gagnants": 0, "jours": 0})
                net = t.realized_pnl - t.costs
                d["trades"] += 1
                d["pnl"] += net
                d["gagnants"] += net > 0
                d["jours"] += (t.exit_date - t.entry_date).days if t.exit_date and t.entry_date else 0
    return sorted(out.values(), key=lambda x: x["strategie"])
