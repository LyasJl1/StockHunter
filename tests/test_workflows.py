"""Vérifie les workflows GitHub Actions RÉELS du dépôt (non-régression de l'audit 3.1 : le workflow nocturne
n'appelait pas la chaîne v3 et masquait les échecs avec `|| echo`)."""

import re
from pathlib import Path

import pytest
import yaml

WF = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def load(name):
    return yaml.safe_load((WF / name).read_text())


def steps(wf):
    return [s for job in wf["jobs"].values() for s in job["steps"]]


def test_nightly_workflow_calls_the_v3_pipeline_and_fails_red():
    wf = load("ingest.yml")
    st = steps(wf)
    pipe = [s for s in st if "stockhunter pipeline nightly" in (s.get("run") or "")]
    assert len(pipe) == 1, "le workflow nocturne doit appeler `stockhunter pipeline nightly` une fois"
    pipe = pipe[0]
    assert pipe.get("id") == "pipeline" and pipe.get("continue-on-error") is True
    assert not re.search(r"\|\|\s*(echo|true|exit 0)", pipe["run"]), "aucun masquage d'erreur sur la chaîne"
    # l'étape finale rend le job rouge et référence le BON identifiant
    final = st[-1]
    assert "exit 1" in final["run"] and "steps.pipeline.outcome == 'failure'" in final["if"]
    ids = {s.get("id") for s in st if s.get("id")}
    for ref in re.findall(r"steps\.([A-Za-z0-9_-]+)\.", str(wf)):
        assert ref in ids, f"référence à une étape inexistante : steps.{ref}"
    # l'ancienne chaîne (commandes séparées) ne doit plus être exécutée en dehors du pipeline
    for s in st:
        run = s.get("run") or ""
        for old in ("stockhunter ingest", "stockhunter scan", "stockhunter paper run", "stockhunter notify evening",
                    "stockhunter forward record", "stockhunter discover run"):
            assert old not in run, f"commande hors chaîne : {old}"
    # rapport et sauvegarde du cache s'exécutent même en cas d'échec, AVANT l'étape qui rend le job rouge
    names = [s.get("name", "") for s in st]
    assert names.index("Sauvegarder la base") < len(st) - 1
    assert wf["jobs"]["ingest"]["env"]["SH_MARKET_DIR"] == "var/market"
    assert wf["concurrency"]["group"] == "ingestion" and wf["concurrency"]["cancel-in-progress"] is False


@pytest.mark.parametrize("name", sorted(p.name for p in WF.glob("*.yml")))
def test_no_workflow_masks_failures_of_production_commands(name):
    """`|| echo` / `|| true` interdits sur une commande qui écrit en production ; tolérés pour l'affichage."""
    allowed = ("stockhunter top", "stockhunter prices", "stockhunter market status", "tail ", "cat ", "ls ")
    for s in steps(load(name)):
        for line in (s.get("run") or "").splitlines():
            if re.search(r"\|\|\s*(echo|true)", line) and "stockhunter" in line:
                assert any(a in line for a in allowed), f"{name} : échec masqué dans « {line.strip()} »"


def test_pipeline_steps_cover_the_whole_chain():
    from stockhunter.pipeline import NIGHTLY
    names = [s.name for s in NIGHTLY]
    for required in ("ingestion", "marche_large", "scan", "decouverte", "paper", "prospectif", "notifications"):
        assert required in names
    crit = {s.name for s in NIGHTLY if s.critical}
    assert {"ingestion", "scan", "paper", "notifications"} <= crit
    assert names.index("decouverte") < names.index("notifications")


@pytest.mark.parametrize("name", sorted(p.name for p in WF.glob("*.yml")))
def test_workflows_use_read_only_token_and_never_upload_data(name):
    wf = load(name)
    assert wf.get("permissions") == {"contents": "read"}, f"{name} : jeton non restreint"
    for s in steps(wf):
        uses = s.get("uses") or ""
        if "upload-artifact" in uses:            # dépôt public : jamais la base, les cours ni une sauvegarde
            path = str((s.get("with") or {}).get("path", ""))
            assert not re.search(r"var/|\.db|market|backup|\.dump|\.parquet", path), f"{name} publie {path}"
        run = s.get("run") or ""
        assert "SH_DASHBOARD_PASSWORD_HASH" not in run or name == "dashboard-credentials.yml"


# ============================================================================ 3.2.1 : chaîne mondiale
def test_world_steps_are_critical_and_temporary_issues_are_warnings():
    from stockhunter.pipeline import NIGHTLY
    world = {s.name: s for s in NIGHTLY if s.name.startswith("monde_")}
    assert set(world) == {"monde_collecte", "monde_scanner", "monde_portefeuille"}
    assert all(s.critical and 2 in s.warn_codes for s in world.values())


def test_world_verify_workflow_never_masks_failures():
    wf = load("world-verify.yml")
    for s in steps(wf):
        assert "continue-on-error" not in s, f"échec masqué : {s.get('name')}"
        run = s.get("run") or ""
        if "stockhunter world sync" in run or "stockhunter world scan" in run:
            assert "exit $c" in run and "-eq 2" in run          # seul le code 2 (dégradation) est toléré


def test_pipeline_turns_red_when_a_world_step_crashes(engine):
    from stockhunter import pipeline as pl
    world = [s for s in pl.NIGHTLY if s.name.startswith("monde_")]
    codes = {"monde_collecte": 2, "monde_scanner": 1, "monde_portefeuille": 0}
    runner = lambda argv: codes[next(s.name for s in world if list(s.argv) == argv)]
    assert pl.run(engine, world, runner=runner, force=True) == 1                 # plantage scanner : rouge
    codes["monde_scanner"] = 2
    assert pl.run(engine, world, runner=runner, force=True) == 0                 # dégradations : alerte, pas rouge


def test_world_exit_codes_and_targeted_alerts(engine):
    from datetime import datetime, timezone
    from sqlalchemy import select
    from stockhunter.db.models import NotificationLog
    from stockhunter.db.session import session_scope
    from stockhunter.world import run as wr
    assert wr.exit_code({"XPAR": (wr.OK, ""), "XNSE": (wr.NO_OPP, "")}) == 0
    assert wr.exit_code({"XPAR": (wr.PARTIAL, ""), "XNSE": (wr.OK, "")}) == 2
    assert wr.exit_code({"XPAR": (wr.CRITICAL, ""), "XNSE": (wr.OK, "")}) == 1
    assert wr.exit_code({"XPAR": (wr.PARTIAL, ""), "XNSE": (wr.PARTIAL, "")}, all_failed_is_critical=True) == 1
    now = datetime(2026, 11, 14, 1, 0, tzinfo=timezone.utc)
    bad = {c: (wr.PARTIAL, "HTTP 503") for c in ("XPAR", "XAMS", "XMIL")} | {"XNSE": (wr.CRITICAL, "format")}
    assert wr.queue_alerts(engine, "collecte", bad, now) == 2                 # Euronext regroupé + NSE
    assert wr.queue_alerts(engine, "collecte", bad, now) == 0                 # aucun doublon
    with session_scope(engine) as s:
        texts = list(s.scalars(select(NotificationLog.text).where(NotificationLog.kind == "alert")))
    assert len(texts) == 2 and any("XAMS, XMIL, XPAR" in t for t in texts)
    assert wr.queue_alerts(engine, "collecte", {"XPAR": (wr.OK, "rien à faire")}, now) == 0   # place fermée


def test_world_scan_crash_is_critical_but_other_markets_continue(engine, tmp_path, monkeypatch):
    from datetime import date
    from stockhunter.world import run as wr
    from stockhunter.world import scan as ws
    from stockhunter.world.store import WorldStore
    from tests.world_fixtures import fx_table, synth_market
    st = WorldStore(tmp_path / "w")
    fx = fx_table(date(2025, 1, 1), date(2026, 11, 13))
    st.save_fx(fx.wide.drop(columns="EUR").stack().rename("rate").reset_index()
               .assign(date=lambda x: x["date"].dt.date))
    synth_market(st, "XPAR", 20)
    real = ws.scan_market

    def boom(engine, store, code, *a, **k):
        if code == "XAMS":
            raise ZeroDivisionError("calcul incohérent")
        return real(engine, store, code, *a, **k)
    monkeypatch.setattr(ws, "scan_market", boom)
    assert wr.scan_all(engine, st, ("XPAR", "XAMS")) == 1
    etats = wr.kv_get(engine, wr.KV_STATE)["scanners"]["etats"]
    assert etats["XAMS"][0] == wr.CRITICAL and etats["XPAR"][0] == wr.OK
