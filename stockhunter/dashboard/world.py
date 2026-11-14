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
    from pathlib import Path
    from stockhunter.world.imports import CombinedStore, ImportStore, sources_status
    primary = WorldStore(settings.world_dir, settings.world_budget_mb)
    imports = ImportStore(Path(settings.world_dir) / "imports", settings.world_budget_mb)
    store = CombinedStore(primary, imports)
    fx = wr.fx_for_display(engine, store)
    try:
        top = int(f.get("top") or 20)
    except ValueError:
        top = 20
    top = top if top in TOP_CHOICES else 20
    filters = {k: (f.get(k) or None) for k in ("continent", "country", "market", "currency", "category", "tier", "risk")}
    mode = "recherche" if f.get("mode") == "recherche" else "surveillance"
    rank = global_ranking(engine, fx if not fx.wide.empty else None, top=top, mode=mode, **filters)
    table = wr.market_table(engine, store)
    port = {b: wr.kv_get(engine, f"{wr.KV_PORTFOLIO}_{b}") for b in ("EUR", "USD")}
    state = wr.kv_get(engine, wr.KV_STATE) or {}
    covered = [r for r in table if r["donnees"] == mk.PRICES]
    active = [r for r in covered if (r.get("scanner") or {}).get("statut") in ("OK", "AUCUNE_OPPORTUNITE")]
    from stockhunter.lab import registry as R
    reg = R.load()
    intl = [e for e in reg["experiences"] if e.get("programme") == "3.2-international"]
    validation = {"tests_famille": len(R.tests(reg)),
                  "valides": [e["id"] for e in reg["experiences"] if str(e.get("decision", "")).startswith(("VALID", "CONFIRM"))],
                  "international": [(e["id"], e["decision"], e.get("t")) for e in intl]}
    storage = primary.status()
    if not storage["places"] and (state.get("collecte") or {}).get("stockage"):
        storage = state["collecte"]["stockage"]          # tableau de bord hébergé : état consigné par la chaîne
    return {"mode": mode, "sources": sources_status(), "imports": imports.files()[-30:],
            "rank": rank, "markets": table, "port": port, "state": state, "storage": storage,
            "providers": list(pv.PROVIDERS.values()), "top": top, "top_choices": TOP_CHOICES,
            "continents": sorted({m.continent for m in mk.MARKETS.values()}),
            "countries": sorted({(m.country, m.country_name) for m in mk.MARKETS.values() if m.data == mk.PRICES}),
            "market_codes": sorted({r["code"] for r in table if r["seances"] or r["code"] == "US"}), "categories": CATEGORIES,
            "currencies": sorted({m.currency for m in mk.with_prices()}),
            "fx_last": fx.last_date, "n_covered": len(covered), "active": active,
            "n_analysed": sum((r.get("scanner") or {}).get("univers") or 0 for r in active),
            "failed": [r for r in table if r.get("etat") in (wr.CRITICAL, wr.PARTIAL)],
            "archive": state.get("archive"), "validation": validation,
            "n_listings": sum((r["listings"] or {}).get("principal", 0) for r in covered)}


def register(app, engine: Engine, settings: Settings, guard, render) -> None:
    @app.get("/world", response_class=HTMLResponse)
    def world(request: Request, continent: str = "", country: str = "", market: str = "", currency: str = "",
              category: str = "", tier: str = "", top: str = "20", base: str = "EUR", mode: str = "",
              risk: str = ""):
        sess = guard(request)
        f = {"continent": continent[:20], "country": country[:2], "market": market[:5], "currency": currency[:3],
             "category": category[:12], "tier": tier[:10], "top": top[:3], "mode": mode[:12], "risk": risk[:12]}
        v = world_view(engine, settings, f)
        base = base if base in ("EUR", "USD") else "EUR"
        p = v["port"].get(base) or {}
        return render(request, "world.html", sess, v=v, f=f, base=base, p=p,
                      curve=json.dumps(p.get("courbe") or []))
