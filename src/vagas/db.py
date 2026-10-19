from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field, asdict
from datetime import datetime, date
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id            TEXT PRIMARY KEY,            -- "<source>:<external_id>"
  source        TEXT NOT NULL,               -- linkedin | indeed
  external_id   TEXT NOT NULL,
  url           TEXT NOT NULL,
  title         TEXT,
  company       TEXT,
  location      TEXT,
  description   TEXT,
  posted_at     TEXT,
  discovered_at TEXT NOT NULL,
  apply_type    TEXT,                        -- easy_apply | indeed_apply | external | unknown
  external_url  TEXT,
  ats           TEXT,                        -- gupy | inhire | generic | ...
  classification TEXT,                       -- JSON do classificador
  status        TEXT NOT NULL DEFAULT 'new', -- new | queued | applied | skipped | failed | pending_question | in_progress
  reason        TEXT,
  attempts      INTEGER NOT NULL DEFAULT 0,
  resume_path   TEXT,
  applied_at    TEXT,
  updated_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_company_title ON jobs(company, title);

CREATE TABLE IF NOT EXISTS applications (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id      TEXT NOT NULL,
  source      TEXT NOT NULL,                 -- linkedin | indeed | external
  ats         TEXT,
  applied_at  TEXT NOT NULL,
  resume_path TEXT,
  answers     TEXT,                          -- JSON [{q,a}]
  notes       TEXT,
  FOREIGN KEY(job_id) REFERENCES jobs(id)
);

CREATE TABLE IF NOT EXISTS pending_questions (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id      TEXT,
  question    TEXT NOT NULL,
  field_type  TEXT,
  options     TEXT,                          -- JSON
  suggested   TEXT,
  created_at  TEXT NOT NULL,
  answered_at TEXT,
  answer      TEXT
);

CREATE TABLE IF NOT EXISTS events (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  at        TEXT NOT NULL,
  level     TEXT NOT NULL,
  job_id    TEXT,
  message   TEXT NOT NULL,
  data      TEXT
);

CREATE TABLE IF NOT EXISTS llm_usage (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  at        TEXT NOT NULL,
  model     TEXT NOT NULL,
  purpose   TEXT,
  prompt_tokens INTEGER,
  completion_tokens INTEGER
);

CREATE TABLE IF NOT EXISTS kv (
  key   TEXT PRIMARY KEY,
  value TEXT
);
"""


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class Job:
    id: str
    source: str
    external_id: str
    url: str
    title: str | None = None
    company: str | None = None
    location: str | None = None
    description: str | None = None
    posted_at: str | None = None
    discovered_at: str = field(default_factory=now)
    apply_type: str | None = None
    external_url: str | None = None
    ats: str | None = None
    classification: dict | None = None
    status: str = "new"
    reason: str | None = None
    attempts: int = 0
    resume_path: str | None = None
    applied_at: str | None = None
    updated_at: str | None = None

    @staticmethod
    def from_row(r: sqlite3.Row) -> "Job":
        d = dict(r)
        if d.get("classification"):
            try:
                d["classification"] = json.loads(d["classification"])
            except Exception:
                d["classification"] = None
        return Job(**d)

    def to_row(self) -> dict:
        d = asdict(self)
        d["classification"] = json.dumps(self.classification, ensure_ascii=False) if self.classification else None
        return d

    @property
    def is_dev_role(self) -> bool | None:
        return (self.classification or {}).get("is_dev_role")


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30)   # espera até 30s por lock
        self.conn.row_factory = sqlite3.Row
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")     # leituras não bloqueiam escritas (threads paralelas)
            self.conn.execute("PRAGMA busy_timeout=30000")
        except Exception:
            pass
        self.conn.executescript(SCHEMA)
        # migração: tokens de entrada em cache custam 1/4 (preço real do gasto)
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(llm_usage)")}
        if "cached_tokens" not in cols:
            self.conn.execute("ALTER TABLE llm_usage ADD COLUMN cached_tokens INTEGER DEFAULT 0")
        self.conn.commit()

    # ---------- jobs ----------
    def upsert_job(self, job: Job) -> bool:
        """Insere se não existe. Retorna True se é novo."""
        cur = self.conn.execute("SELECT id FROM jobs WHERE id=?", (job.id,))
        if cur.fetchone():
            return False
        d = job.to_row()
        d["updated_at"] = now()
        cols = ",".join(d.keys())
        qs = ",".join("?" for _ in d)
        self.conn.execute(f"INSERT INTO jobs ({cols}) VALUES ({qs})", tuple(d.values()))
        self.conn.commit()
        return True

    def save_job(self, job: Job) -> None:
        d = job.to_row()
        d["updated_at"] = now()
        sets = ",".join(f"{k}=?" for k in d if k != "id")
        vals = [v for k, v in d.items() if k != "id"] + [job.id]
        self.conn.execute(f"UPDATE jobs SET {sets} WHERE id=?", vals)
        self.conn.commit()

    def get_job(self, job_id: str) -> Job | None:
        r = self.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return Job.from_row(r) if r else None

    def find_job_by_prefix(self, prefix: str) -> Job | None:
        r = self.conn.execute("SELECT * FROM jobs WHERE id LIKE ? OR external_id=? LIMIT 1", (f"%{prefix}%", prefix)).fetchone()
        return Job.from_row(r) if r else None

    def list_jobs(self, status: str | None = None, limit: int = 200, source: str | None = None) -> list[Job]:
        q = "SELECT * FROM jobs"
        conds, args = [], []
        if status:
            conds.append("status=?"); args.append(status)
        if source:
            conds.append("source=?"); args.append(source)
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY discovered_at DESC LIMIT ?"
        args.append(limit)
        return [Job.from_row(r) for r in self.conn.execute(q, args).fetchall()]

    def queue(self, limit: int = 500) -> list[Job]:
        rows = self.conn.execute(
            "SELECT * FROM jobs WHERE status IN ('new','queued') ORDER BY discovered_at ASC LIMIT ?", (limit,)
        ).fetchall()
        return [Job.from_row(r) for r in rows]

    def set_status(self, job_id: str, status: str, reason: str | None = None) -> None:
        self.conn.execute(
            "UPDATE jobs SET status=?, reason=?, updated_at=? WHERE id=?", (status, reason, now(), job_id)
        )
        self.conn.commit()

    def already_applied_same_posting(self, company: str | None, title: str | None) -> bool:
        """Mesma vaga publicada em outra fonte (ex.: LinkedIn + Indeed) conta como já aplicada."""
        if not company or not title:
            return False
        r = self.conn.execute(
            "SELECT 1 FROM jobs WHERE status='applied' AND lower(company)=lower(?) AND lower(title)=lower(?) "
            "AND replace(applied_at,'T',' ') >= datetime('now','localtime','-30 days') LIMIT 1",
            (company.strip(), title.strip()),
        ).fetchone()
        return bool(r)

    def same_posting_already_failed(self, company: str | None, title: str | None,
                                    exclude_id: str) -> tuple[str, int, str] | None:
        """Outro ANÚNCIO da mesma vaga (empresa + título) que já falhou pelo menos uma vez.

        O LinkedIn republica a mesma vaga com ids diferentes (3x "Desenvolvimento Salesforce
        Sênior" da DBC em 2026-09-12) e cada linha gastava o próprio orçamento de
        `apply.max_attempts` contra a MESMA página quebrada: 6 ciclos de navegador por uma vaga.
        Basta UMA falha na irmã — o anúncio continua com as tentativas completas pela primeira
        linha, então pular as duplicatas não tira nenhuma chance de envio. `reason` não vazio é o
        que separa "já tentou e falhou" de "ainda não foi tentada".
        """
        if not company or not title:
            return None
        r = self.conn.execute(
            "SELECT id, attempts, COALESCE(reason,'') FROM jobs WHERE id != ? "
            "AND lower(company)=lower(?) AND lower(title)=lower(?) "
            "AND attempts >= 1 AND status IN ('queued','failed') AND COALESCE(reason,'') != '' "
            "AND replace(updated_at,'T',' ') >= datetime('now','localtime','-30 days') "
            "ORDER BY attempts DESC LIMIT 1",
            (exclude_id, company.strip(), title.strip()),
        ).fetchone()
        return (r[0], r[1], r[2]) if r else None

    # ---------- applications ----------
    def add_application(self, job: Job, source: str, ats: str | None, resume_path: str | None,
                        answers: list[dict] | None, notes: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO applications (job_id, source, ats, applied_at, resume_path, answers, notes) VALUES (?,?,?,?,?,?,?)",
            (job.id, source, ats, now(), resume_path, json.dumps(answers or [], ensure_ascii=False), notes),
        )
        self.conn.execute(
            "UPDATE jobs SET status='applied', applied_at=?, resume_path=?, reason=NULL, updated_at=? WHERE id=?",
            (now(), resume_path, now(), job.id),
        )
        self.conn.commit()

    def applied_today(self, source: str | None = None) -> int:
        today = date.today().isoformat()
        if source:
            r = self.conn.execute(
                "SELECT COUNT(*) FROM applications WHERE substr(applied_at,1,10)=? AND source=?", (today, source)
            ).fetchone()
        else:
            r = self.conn.execute("SELECT COUNT(*) FROM applications WHERE substr(applied_at,1,10)=?", (today,)).fetchone()
        return int(r[0])

    def applications_count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0])

    # ---------- pending questions ----------
    def add_pending_question(self, job_id: str | None, question: str, field_type: str | None,
                             options: list[str] | None, suggested: str | None) -> int:
        # mesma pergunta em aberto para a mesma vaga (rescans do formulário) -> não duplica
        r = self.conn.execute(
            "SELECT id FROM pending_questions WHERE answered_at IS NULL AND question=? AND (job_id=? OR (job_id IS NULL AND ? IS NULL)) LIMIT 1",
            (question, job_id, job_id),
        ).fetchone()
        if r:
            return int(r[0])
        cur = self.conn.execute(
            "INSERT INTO pending_questions (job_id, question, field_type, options, suggested, created_at) VALUES (?,?,?,?,?,?)",
            (job_id, question, field_type, json.dumps(options or [], ensure_ascii=False), suggested, now()),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def pending_questions(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM pending_questions WHERE answered_at IS NULL ORDER BY created_at ASC"
        ).fetchall()

    def answer_pending(self, qid: int, answer: str) -> None:
        self.conn.execute(
            "UPDATE pending_questions SET answer=?, answered_at=? WHERE id=?", (answer, now(), qid)
        )
        self.conn.commit()

    def recover_stale_in_progress(self, minutes: int = 30) -> int:
        """Vaga presa em 'in_progress' depois de queda/reinício volta para a fila.

        `process_queue` só lê `queued`, então sem isto a vaga fica órfã para sempre — foi o que
        aconteceu quando o run foi reiniciado no meio de uma vaga (2026-09-12). Se já existe
        candidatura registrada, o status certo é 'applied': nunca reenviar.
        """
        idade = f"-{int(minutes)} minutes"
        cur = self.conn.execute(
            "UPDATE jobs SET status='applied', updated_at=? WHERE status='in_progress'"
            " AND replace(updated_at,'T',' ') < datetime('now','localtime',?)"
            " AND EXISTS (SELECT 1 FROM applications a WHERE a.job_id = jobs.id)",
            (now(), idade),
        )
        n = cur.rowcount
        cur = self.conn.execute(
            "UPDATE jobs SET status='queued', reason=NULL, updated_at=? WHERE status='in_progress'"
            " AND replace(updated_at,'T',' ') < datetime('now','localtime',?)",
            (now(), idade),
        )
        n += cur.rowcount
        self.conn.commit()
        return n

    def requeue_pending_jobs(self) -> int:
        cur = self.conn.execute(
            "UPDATE jobs SET status='queued', reason=NULL, updated_at=? WHERE status='pending_question'", (now(),)
        )
        self.conn.commit()
        return cur.rowcount

    # ---------- misc ----------
    def log(self, level: str, message: str, job_id: str | None = None, data: Any = None) -> None:
        self.conn.execute(
            "INSERT INTO events (at, level, job_id, message, data) VALUES (?,?,?,?,?)",
            (now(), level, job_id, message, json.dumps(data, ensure_ascii=False, default=str) if data is not None else None),
        )
        self.conn.commit()

    def add_llm_usage(self, model: str, purpose: str, prompt_tokens: int, completion_tokens: int, cached_tokens: int = 0) -> None:
        self.conn.execute(
            "INSERT INTO llm_usage (at, model, purpose, prompt_tokens, completion_tokens, cached_tokens) VALUES (?,?,?,?,?,?)",
            (now(), model, purpose, prompt_tokens, completion_tokens, int(cached_tokens or 0)),
        )
        self.conn.commit()

    def kv_get(self, key: str, default: str | None = None) -> str | None:
        r = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return r[0] if r else default

    def kv_set(self, key: str, value: str) -> None:
        self.conn.execute("INSERT INTO kv (key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        self.conn.commit()

    def stats(self) -> dict:
        c = self.conn
        out = {
            "jobs_total": c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
            "by_status": {r[0]: r[1] for r in c.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status")},
            "applied_today": self.applied_today(),
            "applied_total": self.applications_count(),
            "pending_questions": len(self.pending_questions()),
            "llm_tokens": dict(c.execute("SELECT COALESCE(SUM(prompt_tokens),0), COALESCE(SUM(completion_tokens),0) FROM llm_usage").fetchone() and
                              zip(("prompt", "completion"), c.execute("SELECT COALESCE(SUM(prompt_tokens),0), COALESCE(SUM(completion_tokens),0) FROM llm_usage").fetchone())),
        }
        return out
