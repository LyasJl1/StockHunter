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
