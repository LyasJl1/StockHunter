"""Catalyseurs récents de l'univers suivi (8-K classés + articles importants), pour rapport et alertes."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from stockhunter.catalysts.classify import NEWS_LABELS, classify_8k
from stockhunter.db.models import Filing, Instrument, NewsItem

NY = ZoneInfo("America/New_York")
KEY_NEWS = {"legal", "regulatory", "mna", "guidance"}   # articles retenus pour le rapport et les alertes


def recent_catalysts(s, start: date, end: date, symbols: set[str] | None = None,
                     min_importance: int = 2) -> list[dict]:
    """Événements datés de ]start, end] (dépôt SEC : date de dépôt ; article : date de publication à New York)."""
    out: list[dict] = []
    q = (select(Filing, Instrument.symbol).join(Instrument)
         .where(Filing.form.in_(("8-K", "8-K/A")), Filing.filed_date > start, Filing.filed_date <= end))
    for f, sym in s.execute(q):
        if symbols is not None and sym not in symbols:
            continue
        cats = [c for c in classify_8k(f.items) if c.importance >= min_importance]
        if not cats:
            continue
        out.append({"key": f"8k:{f.accession}", "symbol": sym, "date": f.filed_date, "kind": f.form,
                    "label": " ; ".join(c.label for c in cats), "importance": max(c.importance for c in cats),
                    "watch": any(c.watch for c in cats), "earnings_only": all(c.item == "2.02" for c in cats),
                    "url": f.url, "source": "SEC EDGAR"})
    lo = datetime.combine(start + timedelta(days=1), time(0), NY).astimezone(timezone.utc)
    hi = datetime.combine(end + timedelta(days=1), time(0), NY).astimezone(timezone.utc)
    q = (select(NewsItem, Instrument.symbol).join(Instrument)
         .where(NewsItem.published_at >= lo, NewsItem.published_at < hi, NewsItem.category.in_(KEY_NEWS)))
    for n, sym in s.execute(q):
        if symbols is not None and sym not in symbols:
            continue
        lab, imp = NEWS_LABELS[n.category]
        pub = n.published_at if n.published_at.tzinfo else n.published_at.replace(tzinfo=timezone.utc)
        out.append({"key": f"news:{n.id}", "symbol": sym, "date": pub.astimezone(NY).date(), "kind": "article",
                    "label": f"{lab} : {n.headline}", "importance": imp, "watch": n.category in ("legal", "regulatory"),
                    "earnings_only": False, "url": n.url, "source": n.publisher or "Finnhub"})
    return sorted(out, key=lambda c: (-c["importance"], -c["date"].toordinal(), c["symbol"]))
