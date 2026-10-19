"""Painel local: servidor HTTP com API JSON sobre o SQLite + página única (dashboard_static/index.html)."""
from __future__ import annotations

import json
import mimetypes
import os
import re
import sqlite3
import subprocess
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import yaml

from .config import CONFIG_DIR, DATA_DIR, ROOT, Settings, load_settings
from .db import DB

STATIC = Path(__file__).parent / "dashboard_static"
import threading
_COURSES_LOCK = threading.Lock()   # cliques rápidos no painel não podem gravar courses.yaml ao mesmo tempo
PRICES = {  # USD por 1M tokens (entrada, saída) — ajuste se trocar de modelo
    "gpt-4.1-mini": (0.40, 1.60), "gpt-4.1": (2.00, 8.00), "gpt-4o-mini": (0.15, 0.60), "gpt-4o": (2.50, 10.00),
    "gpt-5-mini": (0.25, 2.00), "gpt-5": (1.25, 10.00),
}


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def reason_key(r: str | None) -> str:
    r = r or ""
    if r.startswith("não é vaga de dev"): return "não é vaga de dev"
    if "inglês" in r or "descrição em en" in r: return "descrição em inglês"
    if "não é remoto" in r: return "presencial ou híbrida"
    if re.search(r"mais de \d+h", r): return "mais de 24h"
    if "exige idioma" in r: return "exige outro idioma"
    if "eliminat" in r: return "requisito eliminatório"
    if "blacklist" in r or "título contém" in r: return "lista de bloqueio"
    if "já aplicad" in r: return "duplicada"
    if "ignorado" in r or "whatsapp" in r.lower(): return "site ignorado"
    if "pulada manualmente" in r: return "pulada por você"
    return (r.split(":")[0] or "outro")[:40]


def _job(row) -> dict:
    j = dict(row)
    try:
        j["classification"] = json.loads(j.get("classification") or "{}")
    except Exception:
        j["classification"] = {}
    return j


class Data:
    def __init__(self, settings: Settings):
        self.s = settings
        self.db = DB(settings.db_path)

    # ---------- visão geral ----------
    def _budget_status(self) -> dict:
        """Gasto de IA do mês contra o teto (R$), para o painel mostrar quanto ainda cabe."""
        try:
            from .budget import Budget
            l = self.s.llm
            return Budget(self.db, l.monthly_budget_brl, l.usd_brl, since=l.budget_since, overrides_brl=l.budget_overrides_brl).status()
        except Exception:
            return {}

    def overview(self) -> dict:
        c = self.db.conn
        today = date.today().isoformat()
        jobs = [_job(r) for r in c.execute("SELECT * FROM jobs").fetchall()]
        apps = [dict(r) for r in c.execute("SELECT * FROM applications ORDER BY applied_at DESC").fetchall()]
        by_status = Counter(j["status"] for j in jobs)
        apps_today = [a for a in apps if (a["applied_at"] or "")[:10] == today]
        # ticks do dia: candidaturas + tentativas (eventos)
        ticks = [{"t": a["applied_at"][11:16], "kind": "applied", "job_id": a["job_id"]} for a in apps_today]
        for r in c.execute("SELECT at, message, job_id FROM events WHERE substr(at,1,10)=? AND level='info' AND message LIKE 'failed:%'", (today,)):
            ticks.append({"t": r["at"][11:16], "kind": "failed", "job_id": r["job_id"]})
        for r in c.execute("SELECT at, message, job_id FROM events WHERE substr(at,1,10)=? AND message LIKE 'needs_user:%'", (today,)):
            ticks.append({"t": r["at"][11:16], "kind": "pending", "job_id": r["job_id"]})
        titles = {j["id"]: (j["title"], j["company"]) for j in jobs}
        for t in ticks:
            t["title"], t["company"] = titles.get(t["job_id"], ("", ""))
        ticks.sort(key=lambda x: x["t"])
        days = [(date.today() - timedelta(days=i)).isoformat() for i in range(13, -1, -1)]
        per_day = {d: {"applied": 0, "found": 0, "queued": 0} for d in days}
        for a in apps:
            d = (a["applied_at"] or "")[:10]
            if d in per_day: per_day[d]["applied"] += 1
        for j in jobs:
            d = (j["discovered_at"] or "")[:10]
            if d in per_day:
                per_day[d]["found"] += 1
                if j["status"] in ("queued", "applied", "failed", "pending_question", "in_progress", "dry_run"): per_day[d]["queued"] += 1
        reasons = Counter(reason_key(j["reason"]) for j in jobs if j["status"] == "skipped" and (j["discovered_at"] or "")[:10] == today)
        stacks: Counter = Counter()
        for j in jobs:
            if j["status"] != "skipped" and (j["discovered_at"] or "")[:10] == today:
                for st in j["classification"].get("stack") or []: stacks[str(st).strip().lower()] += 1
        ats: dict[str, Counter] = defaultdict(Counter)
        for j in jobs:
            if not j["attempts"]: continue
            key = j["ats"] or ("linkedin easy apply" if j["source"] == "linkedin" and j.get("apply_type") == "easy_apply" else ("indeed apply" if j.get("apply_type") == "indeed_apply" else j["source"]))
            ats[key][j["status"]] += 1
        tok = c.execute("SELECT model, SUM(prompt_tokens) p, SUM(completion_tokens) c FROM llm_usage WHERE substr(at,1,10)=? GROUP BY model", (today,)).fetchall()
        cost_today = sum(self._cost(r["model"], r["p"], r["c"]) for r in tok)
        running = self._running()
        return {
            "now": datetime.now().strftime("%d/%m %H:%M"), "today": today,
            "kpi": {
                "applied_today": len(apps_today), "cap": self.s.limits.max_applications_per_day, "caps": self.s.limits.max_per_source_per_day,
                "applied_total": len(apps), "found_today": sum(1 for j in jobs if (j["discovered_at"] or "")[:10] == today),
                "queued": by_status.get("queued", 0) + by_status.get("new", 0), "pending": len(self.db.pending_questions()),
                "failed": by_status.get("failed", 0), "skipped_today": sum(reasons.values()), "cost_today_usd": round(cost_today, 3),
                "budget": self._budget_status(),
            },
            "ticks": ticks, "days": days, "per_day": per_day, "reasons": reasons.most_common(12), "stacks": stacks.most_common(14),
            "ats": {k: dict(v) for k, v in sorted(ats.items(), key=lambda x: -sum(x[1].values()))},
            "last_discovery": self.db.kv_get("last_discovery"), "running": running,
            "active_hours": list(self.s.limits.active_hours),
        }

    def _running(self) -> dict:
        return {k: v.get("pid") for k, v in running_tasks().items() if k != "dashboard"}

    def _cost(self, model: str, p: int | None, c: int | None) -> float:
        pi, po = PRICES.get(model or "", (0.4, 1.6))
        return ((p or 0) * pi + (c or 0) * po) / 1_000_000

    # ---------- vagas ----------
    def jobs(self, status: str | None, q: str | None, limit: int = 300, today_only: bool = False) -> list[dict]:
        sql = "SELECT * FROM jobs"; conds, args = [], []
        if status and status != "all":
            if status == "queued": conds.append("status IN ('queued','new')")
            else: conds.append("status=?"); args.append(status)
        if q:
            conds.append("(lower(title) LIKE ? OR lower(company) LIKE ? OR lower(reason) LIKE ?)"); like = f"%{q.lower()}%"; args += [like, like, like]
        if today_only: conds.append("substr(discovered_at,1,10)=?"); args.append(date.today().isoformat())
        if conds: sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY COALESCE(applied_at, updated_at, discovered_at) DESC LIMIT ?"; args.append(limit)
        rows = [_job(r) for r in self.db.conn.execute(sql, args).fetchall()]
        for j in rows:
            j.pop("description", None); j["reason_key"] = reason_key(j.get("reason")) if j["status"] == "skipped" else None
        return rows

    def job(self, job_id: str) -> dict | None:
        r = self.db.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not r: return None
        j = _job(r)
        app = self.db.conn.execute("SELECT * FROM applications WHERE job_id=? ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
        j["application"] = dict(app) if app else None
        if j["application"]:
            try: j["application"]["answers"] = json.loads(j["application"]["answers"] or "[]")
            except Exception: j["application"]["answers"] = []
        j["events"] = [dict(e) for e in self.db.conn.execute("SELECT at, level, message, data FROM events WHERE job_id=? ORDER BY id DESC LIMIT 40", (job_id,)).fetchall()]
        j["pending"] = [dict(p) for p in self.db.conn.execute("SELECT id, question, field_type, options, suggested, answered_at, answer FROM pending_questions WHERE job_id=? ORDER BY id DESC", (job_id,)).fetchall()]
        j["resume_rel"] = self._rel(j.get("resume_path"))
        ev = DATA_DIR / "evidence" / (re.sub(r"[^a-zA-Z0-9_-]", "_", job_id) + ".png")
        j["evidence_rel"] = self._rel(str(ev)) if ev.exists() else None
        learn = DATA_DIR / "learn"
        j["learn"] = []
        if learn.exists():
            for d in learn.glob(f"*/{re.sub(r'[^a-zA-Z0-9_-]', '_', job_id)}"):
                for png in sorted(d.glob("step*.png")):
                    j["learn"].append({"ats": d.parent.name, "file": self._rel(str(png)), "step": png.stem})
        return j

    def _rel(self, p: str | None) -> str | None:
        if not p: return None
        try:
            return str(Path(p).resolve().relative_to(DATA_DIR.resolve()))
        except Exception:
            return None

    # ---------- outros ----------
    def applications(self) -> list[dict]:
        rows = self.db.conn.execute("SELECT a.id, a.job_id, a.source, a.ats, a.applied_at, a.resume_path, a.answers, j.title, j.company, j.url, j.external_url, j.classification FROM applications a JOIN jobs j ON j.id=a.job_id ORDER BY a.applied_at DESC").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try: d["answers"] = json.loads(d["answers"] or "[]")
            except Exception: d["answers"] = []
            try: d["classification"] = json.loads(d["classification"] or "{}")
            except Exception: d["classification"] = {}
            d["resume_rel"] = self._rel(d["resume_path"]); out.append(d)
        return out

    def pending(self) -> list[dict]:
        out = []
        for r in self.db.pending_questions():
            d = dict(r); j = self.db.get_job(d["job_id"]) if d["job_id"] else None
            d["title"], d["company"] = (j.title, j.company) if j else ("", "")
            try: d["options"] = json.loads(d["options"] or "[]")
            except Exception: d["options"] = []
            # aviso (link para criar conta, questionário manual) x pergunta de formulário de verdade
            d["is_note"] = (d.get("field_type") in ("link", "email")) or bool(re.search(r"^(o site|a |confirme a candidatura|question[aá]rio d)", (d.get("question") or ""), re.I))
            d["job_status"] = j.status if j else None
            out.append(d)
        return out

    def dismiss_pending(self, qid: int, skip_job: bool) -> None:
        """Marca a pendência como resolvida; com skip_job=True a vaga é descartada e não volta para a fila."""
        r = self.db.conn.execute("SELECT job_id FROM pending_questions WHERE id=?", (qid,)).fetchone()
        self.db.answer_pending(qid, "descartado no painel")
        if r and r[0] and skip_job:
            self.db.set_status(r[0], "skipped", "descartada por você no painel")

    def answer(self, qid: int, answer: str) -> None:
        from .qa import AnswerBank
        r = self.db.conn.execute("SELECT * FROM pending_questions WHERE id=?", (qid,)).fetchone()
        if not r: return
        self.db.answer_pending(qid, answer)
        bank = AnswerBank(self.s.answers_path); bank.add(r["question"], answer, "user", options=json.loads(r["options"] or "[]"))
        self.db.requeue_pending_jobs()

    def courses(self) -> dict:
        p = CONFIG_DIR / "courses.yaml"
        return (yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else None) or {"stacks": {}}

    def set_course(self, stack: str, name: str, status: str) -> None:
        p = CONFIG_DIR / "courses.yaml"
        with _COURSES_LOCK:
            data = self.courses()
            for c in data["stacks"].get(stack) or []:
                if c.get("name") == name:
                    c["status"] = status
                    if status == "done" and not c.get("year"): c["year"] = str(date.today().year)
            _atomic_write(p, "# Cursos por stack. status: todo | in_progress | done (+ year).\n" + yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=110))

    def llm(self) -> dict:
        rows = self.db.conn.execute("SELECT substr(at,1,10) d, model, purpose, SUM(prompt_tokens) p, SUM(completion_tokens) c, COUNT(*) n FROM llm_usage GROUP BY d, model, purpose ORDER BY d DESC").fetchall()
        out = []
        for r in rows:
            out.append({"day": r["d"], "model": r["model"], "purpose": r["purpose"], "prompt": r["p"], "completion": r["c"], "calls": r["n"], "usd": round(self._cost(r["model"], r["p"], r["c"]), 4)})
        return {"rows": out, "prices": PRICES}

    def events(self, limit: int = 200) -> list[dict]:
        rows = self.db.conn.execute("SELECT e.at, e.level, e.job_id, e.message, j.title, j.company FROM events e LEFT JOIN jobs j ON j.id=e.job_id ORDER BY e.id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def settings(self) -> dict:
        s = self.s
        prof = yaml.safe_load(s.profile_path.read_text(encoding="utf-8")) if s.profile_path.exists() else {}
        bank = yaml.safe_load(s.answers_path.read_text(encoding="utf-8")) if s.answers_path.exists() else {}
        return {
            "discovery": s.discovery.model_dump(), "limits": s.limits.model_dump(), "filters": s.filters.model_dump(), "apply": s.apply.model_dump(),
            "resume": s.resume.model_dump(), "courses": s.courses.model_dump(), "ats": s.ats.model_dump(), "llm": s.llm.model_dump(),
            "searches_total": len(s.all_searches()),
            "profile": {"name": (prof.get("personal") or {}).get("full_name"), "headline": prof.get("headline"), "skills": prof.get("skills"), "facts": {k: v for k, v in (prof.get("facts") or {}).items()}, "experiences": [(e.get("company"), e.get("title"), e.get("start"), e.get("end")) for e in prof.get("experiences") or []]},
            "answers": (bank or {}).get("answers") or [],
            "env": {k: bool(os.environ.get(k)) for k in ("OPENAI_API_KEY", "GUPY_PASSWORD", "ATS_SIGNUP_PASSWORD", "TELEGRAM_BOT_TOKEN")},
        }


# ---------- tarefas (comandos disparados pelo painel) ----------
LOGS = DATA_DIR / "logs"
BROWSER_TASKS = {"search", "prepare", "apply", "run", "resume"}   # usam o perfil do navegador: um por vez


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def running_tasks() -> dict:
    out = {}
    for pf in LOGS.glob("*.pid"):
        try:
            info = json.loads(pf.read_text()) if pf.read_text().strip().startswith("{") else {"pid": int(pf.read_text().strip())}
        except Exception:
            continue
        if _pid_alive(int(info.get("pid", 0))):
            out[pf.stem] = info
    return out


def start_task(kind: str, args: dict) -> dict:
    import shlex, sys
    running = running_tasks()
    if kind in BROWSER_TASKS and any(k in BROWSER_TASKS for k in running):
        busy = ", ".join(k for k in running if k in BROWSER_TASKS)
        raise RuntimeError(f"já existe um processo usando o navegador ({busy}). Pare-o antes ou aguarde terminar.")
    py = sys.executable
    base = [py, "-m", "vagas.cli"]
    if kind == "search":
        cmd = base + ["search"]
    elif kind == "prepare":
        cmd = base + ["prepare"]
    elif kind == "apply":
        cmd = base + ["apply"]
        if args.get("limit"): cmd += ["--limit", str(int(args["limit"]))]
        if args.get("dry_run"): cmd += ["--dry-run"]
        for jid in args.get("jobs") or []: cmd += ["--job", jid]
        if args.get("force"): cmd += ["--force"]
    elif kind == "run":
        cmd = base + ["run"]
    elif kind == "resume":
        cmd = base + ["resume", args["job"], "--force"]
    else:
        raise RuntimeError("comando desconhecido")
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    log = LOGS / f"ui-{kind}-{ts}.log"
    LOGS.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), PYTHONUNBUFFERED="1")
    if args.get("any_hour"):
        env["VAGAS_ANY_HOUR"] = "1"    # rodar fora da janela ativa (pedido explícito)
    proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=open(log, "w"), stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL,   # tarefa do painel roda sem ninguém no teclado:
                            env=env, start_new_session=True)   # sem tty, o checkpoint espera pouco e pula
    info = {"pid": proc.pid, "kind": kind, "cmd": " ".join(shlex.quote(c) for c in cmd[2:]), "log": log.name, "started": datetime.now().isoformat(timespec="seconds"), "args": args}
    (LOGS / f"{kind}.pid").write_text(json.dumps(info))
    return info


def stop_task(kind: str) -> bool:
    import signal, time
    pf = LOGS / f"{kind}.pid"
    if not pf.exists():
        return False
    try:
        txt = pf.read_text().strip(); pid = int(json.loads(txt)["pid"]) if txt.startswith("{") else int(txt)
    except Exception:
        return False
    if not _pid_alive(pid):
        return False
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except Exception:
        try: os.kill(pid, signal.SIGTERM)
        except Exception: return False
    for _ in range(20):
        time.sleep(0.25)
        if not _pid_alive(pid):
            return True
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except Exception:
        pass
    return True


def tail_log(name: str, n: int = 200) -> str:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name or ""):
        return ""
    p = LOGS / name
    if not p.exists():
        return ""
    try:
        data = p.read_bytes()[-120_000:].decode("utf-8", errors="ignore")
    except Exception:
        return ""
    return "\n".join(data.splitlines()[-n:])


def recent_logs(limit: int = 15) -> list[dict]:
    out = []
    for p in sorted(LOGS.glob("ui-*.log"), key=lambda x: x.stat().st_mtime, reverse=True)[:limit]:
        out.append({"name": p.name, "kind": p.name.split("-")[1], "at": datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds"), "size": p.stat().st_size})
    return out


def serve(settings: Settings, port: int = 8765) -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def _json(self, obj, code=200):
            body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(code); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Content-Length", str(len(body))); self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(body)

        def _file(self, path: Path, ctype: str | None = None):
            if not path.exists():
                self.send_response(404); self.end_headers(); return
            body = path.read_bytes(); ctype = ctype or mimetypes.guess_type(str(path))[0] or "application/octet-stream"
            self.send_response(200); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(body))); self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path); qs = {k: v[0] for k, v in parse_qs(u.query).items()}
            data = Data(load_settings())
            try:
                if u.path in ("/", "/index.html"): return self._file(STATIC / "index.html", "text/html; charset=utf-8")
                if u.path == "/api/overview": return self._json(data.overview())
                if u.path == "/api/jobs": return self._json(data.jobs(qs.get("status"), qs.get("q"), int(qs.get("limit", 300)), qs.get("today") == "1"))
                if u.path.startswith("/api/job/"):
                    j = data.job(unquote(u.path[len("/api/job/"):])); return self._json(j) if j else self._json({"error": "não encontrada"}, 404)
                if u.path == "/api/applications": return self._json(data.applications())
                if u.path == "/api/pending": return self._json(data.pending())
                if u.path == "/api/courses": return self._json(data.courses())
                if u.path == "/api/llm": return self._json(data.llm())
                if u.path == "/api/events": return self._json(data.events(int(qs.get("limit", 200))))
                if u.path == "/api/settings": return self._json(data.settings())
                if u.path == "/api/tasks": return self._json({"running": running_tasks(), "recent": recent_logs()})
                if u.path == "/api/log": return self._json({"name": qs.get("name", ""), "text": tail_log(qs.get("name", ""), int(qs.get("tail", 200)))})
                if u.path == "/file":
                    rel = qs.get("p", ""); target = (DATA_DIR / rel).resolve()
                    if not str(target).startswith(str(DATA_DIR.resolve())): self.send_response(403); self.end_headers(); return
                    return self._file(target)
                self.send_response(404); self.end_headers()
            except Exception as e:
                self._json({"error": str(e)}, 500)

        def do_POST(self):
            u = urlparse(self.path); n = int(self.headers.get("Content-Length") or 0)
            try: payload = json.loads(self.rfile.read(n) or b"{}")
            except Exception: payload = {}
            data = Data(load_settings())
            try:
                if u.path == "/api/task":
                    try:
                        return self._json(start_task(payload.get("kind", ""), payload.get("args") or {}))
                    except RuntimeError as e:
                        return self._json({"error": str(e)}, 409)
                if u.path == "/api/stop":
                    return self._json({"ok": stop_task(payload.get("kind", ""))})
                if u.path == "/api/answer":
                    ans = str(payload.get("answer") or "").strip()
                    if not ans:
                        return self._json({"error": "escreva uma resposta antes de enviar"}, 400)
                    data.answer(int(payload["id"]), ans); return self._json({"ok": True})
                if u.path == "/api/pending/dismiss":
                    data.dismiss_pending(int(payload["id"]), bool(payload.get("skip_job"))); return self._json({"ok": True})
                if u.path == "/api/course":
                    data.set_course(payload["stack"], payload["name"], payload["status"]); return self._json({"ok": True})
                m = re.match(r"^/api/job/(.+)/(skip|requeue)$", u.path)
                if m:
                    jid, action = unquote(m.group(1)), m.group(2)
                    if action == "skip": data.db.set_status(jid, "skipped", "pulada manualmente (painel)")
                    else:
                        j = data.db.get_job(jid)
                        if j: j.status, j.reason, j.attempts = "queued", None, 0; data.db.save_job(j)
                    return self._json({"ok": True})
                self._json({"error": "rota desconhecida"}, 404)
            except Exception as e:
                self._json({"error": str(e)}, 500)

        def log_message(self, *a):
            pass

    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()
