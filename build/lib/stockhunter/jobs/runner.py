"""Exécution des jobs : verrou en base (anti-concurrence), journal JobRun, comptage des échecs par élément."""

from __future__ import annotations

import logging
import os
import socket
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterator

from sqlalchemy import delete
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from stockhunter.db.models import JobLock, JobRun
from stockhunter.db.session import session_scope

log = logging.getLogger(__name__)


class JobLocked(RuntimeError):
    pass


@dataclass
class JobContext:
    run_id: int
    ok: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)

    def success(self, n: int = 1):
        self.ok += n

    def failure(self, item: str, exc: Exception):
        self.failed += 1
        msg = f"{item}: {exc}"
        self.errors.append(msg)
        log.error(msg)


@contextmanager
def job_lock(engine: Engine, name: str, ttl: timedelta = timedelta(hours=2)) -> Iterator[None]:
    owner = f"{socket.gethostname()}:{os.getpid()}"
    now = datetime.now(timezone.utc)
    with session_scope(engine) as s:
        # libère un verrou expiré (crash précédent)
        s.execute(delete(JobLock).where(JobLock.job_name == name, JobLock.expires_at < now))
    try:
        with session_scope(engine) as s:
            s.add(JobLock(job_name=name, owner=owner, acquired_at=now, expires_at=now + ttl))
    except IntegrityError as exc:
        raise JobLocked(f"job '{name}' déjà en cours") from exc
    try:
        yield
    finally:
        with session_scope(engine) as s:
            s.execute(delete(JobLock).where(JobLock.job_name == name, JobLock.owner == owner))


def run_job(engine: Engine, name: str, fn: Callable[[JobContext], None]) -> JobRun:
    try:
        with job_lock(engine, name):
            with session_scope(engine) as s:
                run = JobRun(job_name=name)
                s.add(run)
                s.flush()
                run_id = run.id
            ctx = JobContext(run_id=run_id)
            status, detail = "success", None
            try:
                fn(ctx)
                if ctx.failed:
                    status = "partial" if ctx.ok else "failed"
                    detail = "\n".join(ctx.errors[:50])
            except Exception as exc:  # erreur globale du job
                status, detail = "failed", f"{type(exc).__name__}: {exc}"
                log.exception("job %s en échec", name)
            with session_scope(engine) as s:
                run = s.get(JobRun, run_id)
                run.status, run.detail = status, detail
                run.items_ok, run.items_failed = ctx.ok, ctx.failed
                run.finished_at = datetime.now(timezone.utc)
                s.flush()
                s.expunge(run)
                return run
    except JobLocked as exc:
        with session_scope(engine) as s:
            run = JobRun(job_name=name, status="skipped", detail=str(exc),
                         finished_at=datetime.now(timezone.utc))
            s.add(run)
            s.flush()
            s.expunge(run)
            return run
