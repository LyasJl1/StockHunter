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
    halt_codes: tuple[int, ...] = ()        # ce code arrête la chaîne (sauf l'étape notifications)


NIGHTLY: tuple[Step, ...] = (
    Step("entretien", "0 base", ("db", "tune"), False),
    # volume ≥ 95 % après entretien : on n'écrit plus rien (seule l'alerte part) plutôt que de saturer le disque
    Step("stockage", "0 base", ("db", "check"), True, warn_codes=(2,), halt_codes=(3,)),
    Step("univers", "1 collecte", ("sync-universe",), False),
    Step("ingestion", "1 collecte", ("ingest", "all"), True),
    Step("marche_large", "1 collecte", ("market", "sync", "--max-calls", "4"), False),
    Step("monde_collecte", "1 collecte", ("world", "sync"), False),         # Euronext, Inde, change BCE (isolés)
    Step("qualite", "2 qualité", ("quality",), False, warn_codes=(1, 2)),   # anomalies critiques : alerte, pas panne
    Step("scan", "3 indicateurs et sélection", ("scan", "--if-new", "--require-fresh"), True),
    # 3.2 : échec technique = rouge (critique) ; données absentes ou séance attendue manquante = alerte (code 2) ;
    # aucune opportunité = succès (code 0, signalé comme tel)
    Step("decouverte", "3 indicateurs et sélection", ("discover", "run"), True),
    Step("monde_scanner", "3 indicateurs et sélection", ("world", "scan"), False),
    Step("paper", "4 décision simulée", ("paper", "run"), True),
    Step("monde_portefeuille", "4 décision simulée", ("world", "portfolio"), False),
    Step("prospectif", "5 suivi des résultats", ("forward", "record"), False),
    Step("retention", "6 maintenance", ("prune", "--days", "30"), False),
    Step("notifications", "7 rapports", ("notify", "evening"), True),
)


def expected_session(now: datetime | None = None):
    """Séance US attendue : clôture de New York + délai de publication des fournisseurs (3 h). Avant 3.2, la
    séance du jour n'était jamais comptée : la collecte du soir était repoussée au lendemain matin."""
    from stockhunter.world import markets as mk
    return mk.last_completed_session(mk.get("US"), now)


def expected_sessions(now: datetime | None = None) -> dict[str, str]:
    """Séance attendue de chaque place dotée de cours (États-Unis, Euronext, Inde)."""
    from stockhunter.world import markets as mk
    return mk.expected_sessions(now)


def already_done(engine: Engine, now: datetime | None = None) -> str | None:
    """Chaîne déjà terminée sans échec ni étape partielle pour la séance attendue de CHAQUE place : renvoie la
    séance US. Un férié américain n'empêche donc plus la collecte d'une place européenne ou indienne ouverte."""
    with session_scope(engine) as s:
        kv = s.get(KeyValue, KV)
        last = json.loads(kv.value) if kv else None
    seance = str(expected_session(now))
    if (last and last.get("final") and last.get("status") == "success" and last.get("seance") == seance
            and last.get("seances") == expected_sessions(now)):
        return seance
    return None


def run(engine: Engine, steps: tuple[Step, ...] = NIGHTLY, runner: Callable[[list[str]], int] | None = None,
        force: bool = False, now: datetime | None = None) -> int:
    """Exécute les étapes dans l'ordre. Renvoie 0 si aucune étape critique n'a échoué, 1 sinon.
    Si la séance attendue a déjà été entièrement traitée, ne refait rien (évite les doubles collectes des
    créneaux de secours) sauf `force`."""
    if runner is None:
        from stockhunter.cli import main as runner
    done = None if force else already_done(engine, now)
    if done:
        print(f"Séance du {done} déjà entièrement traitée : rien à faire (relancer avec --force pour forcer).")
        return 0
    seance = str(expected_session(now))
    seances = expected_sessions(now)
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
            payload = json.dumps({"started": started.isoformat(), "seance": seance, "seances": seances,
                                  "status": status, "final": final,
                                  "steps": results},
                                 ensure_ascii=False)
            kv = s.get(KeyValue, KV)
            if kv is None:
                s.add(KeyValue(key=KV, value=payload))
            else:
                kv.value = payload
        return status

    halted = None
    for st in steps:
        if halted and st.name != "notifications":
            results.append({"name": st.name, "stage": st.stage, "critical": st.critical, "status": "warning",
                            "error": f"non exécutée : {halted}", "seconds": 0.0})
            continue
        if st.name == "notifications":
            record(final=False)                     # les pannes déjà connues partent dans l'alerte de ce soir
        t0 = time.monotonic()
        res = {"name": st.name, "stage": st.stage, "critical": st.critical}
        print(f"\n=== [{st.stage}] {st.name} : stockhunter {' '.join(st.argv)}", flush=True)
        try:
            code = runner(list(st.argv))
            res["code"] = code
            res["status"] = "ok" if code == 0 else ("warning" if code in st.warn_codes else "failed")
            if code in st.halt_codes:
                halted = f"chaîne arrêtée par l'étape {st.name} (code {code})"
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
    _github_report(results, status, seances)
    return 1 if status == "failed" else 0


def _github_report(results: list[dict], status: str, seances: dict) -> None:
    """3.2 : rend les échecs visibles dans GitHub Actions (annotations + résumé), en distinguant une panne
    technique (erreur), une donnée manquante ou partielle (avertissement) et une journée sans opportunité (succès)."""
    import os
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    for r in results:
        if r["status"] == "failed":
            lvl = "error" if r["critical"] else "warning"
            print(f"::{lvl} title=Étape {r['name']} en échec::{r.get('error', '')} — voir le journal de l'étape "
                  f"« Chaîne nocturne » et la page Santé")
        elif r["status"] == "warning":
            print(f"::warning title=Étape {r['name']} partielle::{r.get('error') or 'données partielles ou absentes'}")
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    icon = {"ok": "✅", "warning": "⚠️", "failed": "❌"}
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"## Chaîne nocturne : {status}\n\nSéances attendues : "
                + ", ".join(f"{k} {v}" for k, v in seances.items()) + "\n\n| Étape | Statut | Durée | Détail |\n|---|---|---|---|\n")
        for r in results:
            f.write(f"| {r['name']}{' (critique)' if r['critical'] else ''} | {icon[r['status']]} {r['status']} | "
                    f"{r['seconds']} s | {r.get('error', '')} |\n")
