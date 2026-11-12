"""Chaîne nocturne : étapes séparées, chaque résultat consigné, aucun échec critique masqué.

Avant (v2) : chaque étape du workflow se terminait par `|| echo warning`, si bien qu'un plantage complet laissait
le job vert. Ici, toutes les étapes s'exécutent (une panne n'empêche pas les suivantes indépendantes), le bilan est
enregistré en base (page « Santé du système », alerte Telegram), et la commande sort en erreur si une étape
CRITIQUE a échoué — le workflow devient rouge et GitHub prévient.

Codes de sortie des commandes : 0 = OK ; 2 = partiel attendu (quota atteint, source optionnelle absente) ;
autre code ou exception = échec.
"""

from __future__ import annotations

import json
import logging
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy.engine import Engine

from stockhunter.db.models import JobRun, KeyValue
from stockhunter.db.session import session_scope

log = logging.getLogger(__name__)
JOB = "pipeline_nightly"
KV = "pipeline_last"


@dataclass(frozen=True)
class Step:
    name: str
    stage: str
    argv: tuple[str, ...]
    critical: bool
    warn_codes: tuple[int, ...] = (2,)


NIGHTLY: tuple[Step, ...] = (
    Step("univers", "1 collecte", ("sync-universe",), False),
    Step("ingestion", "1 collecte", ("ingest", "all"), True),
    Step("marche_large", "1 collecte", ("market", "sync", "--max-calls", "4"), False),
    Step("qualite", "2 qualité", ("quality",), False, warn_codes=(1, 2)),   # anomalies critiques : alerte, pas panne
    Step("scan", "3 indicateurs et sélection", ("scan", "--if-new", "--require-fresh"), True),
    Step("decouverte", "3 indicateurs et sélection", ("discover", "run"), False),
    Step("paper", "4 décision simulée", ("paper", "run"), True),
    Step("prospectif", "5 suivi des résultats", ("forward", "record"), False),
    Step("retention", "6 maintenance", ("prune", "--days", "30"), False),
    Step("notifications", "7 rapports", ("notify", "evening"), True),
)


def run(engine: Engine, steps: tuple[Step, ...] = NIGHTLY, runner: Callable[[list[str]], int] | None = None) -> int:
    """Exécute les étapes dans l'ordre. Renvoie 0 si aucune étape critique n'a échoué, 1 sinon."""
    if runner is None:
        from stockhunter.cli import main as runner
    started = datetime.now(timezone.utc)
    results: list[dict] = []
    with session_scope(engine) as s:
        jr = JobRun(job_name=JOB, status="running", started_at=started)
        s.add(jr)
        s.flush()
        run_id = jr.id

    def record(final: bool) -> str:
        bad = [r for r in results if r["status"] == "failed" and r["critical"]]
        soft = [r for r in results if r["status"] in ("failed", "warning")]
        status = "failed" if bad else ("partial" if soft else "success")
        detail = "; ".join(f"{r['name']} : {r['status']}" + (f" ({r['error']})" if r.get("error") else "") for r in soft)
        with session_scope(engine) as s:
            jr = s.get(JobRun, run_id)
            jr.status = status if final or bad else "running"
            jr.items_ok = sum(r["status"] == "ok" for r in results)
            jr.items_failed = sum(r["status"] == "failed" for r in results)
            jr.detail = detail or None
            if final:
                jr.finished_at = datetime.now(timezone.utc)
            payload = json.dumps({"started": started.isoformat(), "status": status, "final": final, "steps": results},
                                 ensure_ascii=False)
            kv = s.get(KeyValue, KV)
            if kv is None:
                s.add(KeyValue(key=KV, value=payload))
            else:
                kv.value = payload
        return status

    for st in steps:
        if st.name == "notifications":
            record(final=False)                     # les pannes déjà connues partent dans l'alerte de ce soir
        t0 = time.monotonic()
        res = {"name": st.name, "stage": st.stage, "critical": st.critical}
        print(f"\n=== [{st.stage}] {st.name} : stockhunter {' '.join(st.argv)}", flush=True)
        try:
            code = runner(list(st.argv))
            res["code"] = code
            res["status"] = "ok" if code == 0 else ("warning" if code in st.warn_codes else "failed")
            if res["status"] == "failed":
                res["error"] = f"code de sortie {code}"
        except SystemExit as exc:                    # argparse ou sys.exit dans une commande
            res.update(status="failed", error=f"arrêt (code {exc.code})")
        except Exception as exc:                     # plantage : consigné, jamais masqué
            log.error("étape %s en échec\n%s", st.name, traceback.format_exc())
            res.update(status="failed", error=f"{type(exc).__name__} : {str(exc)[:200]}")
        res["seconds"] = round(time.monotonic() - t0, 1)
        results.append(res)
    status = record(final=True)
    print("\n=== Bilan de la chaîne nocturne")
    for r in results:
        mark = {"ok": "OK      ", "warning": "PARTIEL ", "failed": "ÉCHEC   "}[r["status"]]
        print(f"  {mark} {r['name']:14} {r['seconds']:>7.1f} s  {'(critique) ' if r['critical'] else ''}{r.get('error', '')}")
    print(f"Statut global : {status}")
    return 1 if status == "failed" else 0
