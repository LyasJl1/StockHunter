"""Tableau de bord 3.2 : marchés mondiaux, scanner mondial, portefeuille mondial simulé, santé par place.

Lecture seule. Toute donnée affichée vient de la base (référentiel, sélections, états consignés par la chaîne
nocturne) ou du stockage mondial (séances stockées) : rien n'est calculé à la volée sur des cours en direct.
"""

from __future__ import annotations

import json

from fastapi import Request
from fastapi.responses import HTMLResponse
from sqlalchemy.engine import Engine

from stockhunter.config import Settings
from stockhunter.discovery.engine import CATEGORIES
from stockhunter.world import markets as mk
from stockhunter.world import providers as pv
from stockhunter.world import run as wr
from stockhunter.world.ranking import TOP_CHOICES, global_ranking
from stockhunter.world.store import WorldStore


def world_view(engine: Engine, settings: Settings, f: dict) -> dict:
    store = WorldStore(settings.world_dir, settings.world_budget_mb)
    fx = wr.fx_for_display(engine, store)
    try:
        top = int(f.get("top") or 20)
    except ValueError:
        top = 20
    top = top if top in TOP_CHOICES else 20
    filters = {k: (f.get(k) or None) for k in ("continent", "country", "market", "currency", "category", "tier")}
    rank = global_ranking(engine, fx if not fx.wide.empty else None, top=top, **filters)
    table = wr.market_table(engine, store)
    port = {b: wr.kv_get(engine, f"{wr.KV_PORTFOLIO}_{b}") for b in ("EUR", "USD")}
    state = wr.kv_get(engine, wr.KV_STATE) or {}
    covered = [r for r in table if r["donnees"] == mk.PRICES]
    storage = store.status()
    if not storage["places"] and (state.get("collecte") or {}).get("stockage"):
        storage = state["collecte"]["stockage"]          # tableau de bord hébergé : état consigné par la chaîne
    return {"rank": rank, "markets": table, "port": port, "state": state, "storage": storage,
            "providers": list(pv.PROVIDERS.values()), "top": top, "top_choices": TOP_CHOICES,
            "continents": sorted({m.continent for m in mk.MARKETS.values()}),
            "countries": sorted({(m.country, m.country_name) for m in mk.MARKETS.values() if m.data == mk.PRICES}),
            "market_codes": [m.code for m in mk.with_prices()], "categories": CATEGORIES,
            "currencies": sorted({m.currency for m in mk.with_prices()}),
            "fx_last": fx.last_date, "n_covered": len(covered),
            "n_listings": sum((r["listings"] or {}).get("principal", 0) for r in covered)}


def register(app, engine: Engine, settings: Settings, guard, render) -> None:
    @app.get("/world", response_class=HTMLResponse)
    def world(request: Request, continent: str = "", country: str = "", market: str = "", currency: str = "",
              category: str = "", tier: str = "", top: str = "20", base: str = "EUR"):
        sess = guard(request)
        f = {"continent": continent[:20], "country": country[:2], "market": market[:5], "currency": currency[:3],
             "category": category[:12], "tier": tier[:10], "top": top[:3]}
        v = world_view(engine, settings, f)
        base = base if base in ("EUR", "USD") else "EUR"
        p = v["port"].get(base) or {}
        return render(request, "world.html", sess, v=v, f=f, base=base, p=p,
                      curve=json.dumps(p.get("courbe") or []))
