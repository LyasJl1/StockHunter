"""Chaîne nocturne : un échec critique n'est jamais masqué (non-régression de l'audit v3)."""

import json

from sqlalchemy import select

from stockhunter import pipeline
from stockhunter.db.models import JobRun, KeyValue
from stockhunter.db.session import session_scope
from stockhunter.pipeline import Step

STEPS = (Step("a", "1", ("a",), True), Step("b", "2", ("b",), False), Step("c", "3", ("c",), True),
         Step("notifications", "7", ("n",), True))


def _run(engine, behaviour):
    calls = []

    def runner(argv):
        calls.append(argv[0])
        out = behaviour.get(argv[0], 0)
        if isinstance(out, Exception):
            raise out
        return out
    code = pipeline.run(engine, STEPS, runner)
    with session_scope(engine) as s:
        jr = s.scalars(select(JobRun).where(JobRun.job_name == pipeline.JOB).order_by(JobRun.id.desc())).first()
        last = json.loads(s.get(KeyValue, pipeline.KV).value)
        return code, calls, jr.status, jr.detail, last


def test_all_ok(engine):
    code, calls, status, detail, last = _run(engine, {})
    assert code == 0 and status == "success" and detail is None and calls == ["a", "b", "c", "n"]
    assert last["final"] and all(s["status"] == "ok" for s in last["steps"])


def test_partial_quota_is_warning_not_failure(engine):
    code, _calls, status, detail, _ = _run(engine, {"a": 2})
    assert code == 0 and status == "partial" and "a : warning" in detail


def test_critical_crash_fails_job_but_later_steps_still_run(engine):
    code, calls, status, detail, last = _run(engine, {"a": RuntimeError("base inaccessible")})
    assert code == 1 and status == "failed" and "RuntimeError" in detail
    assert calls == ["a", "b", "c", "n"]                          # les étapes suivantes s'exécutent quand même
    assert last["steps"][0]["status"] == "failed"


def test_non_critical_failure_does_not_fail_job(engine):
    code, _calls, status, detail, _ = _run(engine, {"b": 1})
    assert code == 0 and status == "partial" and "b : failed" in detail


def test_failure_is_recorded_before_notifications_so_alert_goes_out_same_night(engine):
    seen = {}

    def runner(argv):
        if argv[0] == "n":
            with session_scope(engine) as s:
                seen["status"] = s.scalars(select(JobRun).where(JobRun.job_name == pipeline.JOB)).first().status
        return 1 if argv[0] == "c" else 0
    assert pipeline.run(engine, STEPS, runner) == 1
    assert seen["status"] == "failed"
