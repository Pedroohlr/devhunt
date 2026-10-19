"""Orquestração: descobrir -> preparar (detalhes + classificar) -> currículo -> aplicar, com ritmo humano e limites."""
from __future__ import annotations

import os

import json
import random
import re
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

from rich.console import Console

from . import humanize as h
from .appliers import ApplyContext, get_applier
from .appliers.indeed import IndeedApplyApplier, IndeedExternalApplier
from .appliers.linkedin import LinkedInAutoApplier, LinkedInEasyApplyApplier, LinkedInExternalApplier
from .browser import Browser, Page
from .config import HARD_REMOTE_ONLY, Settings, resolve
from .db import DB, Job
from .forms import FlowResult
from .llm import LLM
from .notify import notify
from .qa import QAEngine
from .resume.profile import candidate_summary
from .budget import BudgetExceeded
from .errors import ResumeBuildError
from .resume.render import generate_pdf
from .resume.tailor import build_resume, resume_json_path
from .sources import SOURCES
from .sources.base import age_hours, is_too_old

console = Console()


def _fold(s: str) -> str:
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()


_GENERIC_STACKS = {"apis", "api", "rest", "apis rest", "sql", "git", "web", "mobile", "backend", "frontend", "linux", "agile", "scrum", "html", "css"}

_PT = ("de", "e", "para", "com", "que", "uma", "nao", "voce", "em", "dos", "das", "como", "mais", "ser", "nossa", "nosso", "vaga")
_EN = ("the", "and", "with", "for", "you", "our", "will", "are", "we", "of", "to", "in", "is", "as", "your", "team", "job")


ONSITE_RE = re.compile(r"\b(presencial|h[íi]brid[oa]s?|hibrido)\b", re.I)
REMOTE_RE = re.compile(r"\b(remot[oa]s?|home\s?office|100%\s*remot|trabalho remoto|remote|anywhere|de qualquer lugar)\b", re.I)


def looks_onsite(title: str, description: str) -> bool:
    """Heurística sem IA: fala em presencial/híbrido e em nenhum momento em remoto."""
    text = f"{title or ''}\n{description or ''}"
    return bool(ONSITE_RE.search(text)) and not REMOTE_RE.search(text)


def desc_hash(description: str | None) -> str:
    import hashlib
    return hashlib.sha1((description or "").strip().lower().encode()).hexdigest()[:16]


def guess_language(text: str) -> str:
    """Heurística sem IA: 'en' se a descrição é claramente em inglês; senão 'pt-BR'."""
    words = re.findall(r"[a-z]+", _fold(text)[:6000])
    if len(words) < 40:
        return "pt-BR"
    pt = sum(1 for w in words if w in _PT)
    en = sum(1 for w in words if w in _EN)
    return "en" if en > 12 and en > pt * 1.5 else "pt-BR"


# ---------- ritmo ----------
class Pacer:
    def __init__(self, settings: Settings, db: DB):
        self.s = settings
        self.db = db
        self.session_count = 0

    def _hm(self, s: str) -> tuple[int, int]:
        a, b = s.split(":")
        return int(a), int(b)

    def in_active_hours(self, now: datetime | None = None) -> bool:
        if os.environ.get("VAGAS_ANY_HOUR", "").strip() == "1":   # leva pedida fora da janela (decisão explícita do usuário)
            return True
        now = now or datetime.now()
        start, end = self.s.limits.active_hours
        sh, sm = self._hm(start)
        eh, em = self._hm(end)
        t = now.hour * 60 + now.minute
        return sh * 60 + sm <= t <= eh * 60 + em

    def seconds_until_active(self) -> int:
        now = datetime.now()
        sh, sm = self._hm(self.s.limits.active_hours[0])
        nxt = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        return int((nxt - now).total_seconds())

    def bucket(self, job: Job) -> str:
        if job.source == "linkedin" and job.apply_type == "easy_apply":
            return "linkedin"
        if job.source == "indeed" and job.apply_type == "indeed_apply":
            return "indeed"
        return "external"

    def can_apply(self, job: Job) -> tuple[bool, str]:
        total = self.db.applied_today()
        if total >= self.s.limits.max_applications_per_day:
            return False, f"teto diário total atingido ({total})"
        b = self.bucket(job)
        cap = self.s.limits.max_per_source_per_day.get(b)
        if cap is not None and self.db.applied_today(b) >= cap:
            return False, f"teto diário de '{b}' atingido ({cap})"
        if self.db.kv_get(f"limit_hit:{b}:{date.today().isoformat()}"):
            return False, f"'{b}' bloqueou candidaturas hoje (limite do site)"
        return True, ""

    def mark_limit_hit(self, job: Job) -> None:
        self.db.kv_set(f"limit_hit:{self.bucket(job)}:{date.today().isoformat()}", "1")

    def after_application(self, status: str = "applied", bucket: str = "linkedin") -> float:
        """Espera depois de uma tentativa. Devolve os segundos esperados (para teste).

        O ritmo humano existe para o LinkedIn/Indeed não verem padrão de robô, e isso só importa
        depois de um ENVIO. Tentativa que não enviou nada (login, conta, anúncio expirado) espera
        poucos segundos; envio em site externo espera menos que Easy Apply/Indeed Apply; e a pausa
        longa conta só os envios dentro da própria plataforma.
        """
        L = self.s.limits
        if status not in ("applied", "dry_run"):
            lo, hi = L.delay_after_failure
            secs = random.uniform(lo, hi)
            console.print(f"[dim]⏳ próxima em {secs:.0f}s (nada foi enviado)[/]")
            time.sleep(secs)
            return secs
        nativo = bucket in ("linkedin", "indeed")
        if nativo:
            self.session_count += 1
            every = L.long_break_every
            if every and self.session_count % every == 0:
                lo, hi = L.long_break_minutes
                mins = random.uniform(lo, hi)
                console.print(f"[dim]☕ pausa longa de {mins:.0f} min ({every} envios pela plataforma)[/]")
                time.sleep(mins * 60)
                return mins * 60
        lo, hi = L.delay_between_applications if nativo else L.delay_between_applications_external
        secs = h._trunc_normal(lo, hi)
        console.print(f"[dim]⏳ próxima candidatura em {secs:.0f}s[/]")
        time.sleep(secs)
        return secs


@dataclass
class RunSummary:
    processed: int = 0
    applied: int = 0
    skipped: int = 0
    failed: int = 0
    pending: int = 0
    dry_run: int = 0
    stopped_reason: str = ""
    items: list[tuple[str, str, str]] = field(default_factory=list)


class Pipeline:
    def __init__(self, settings: Settings, db: DB, browser: Browser, llm: LLM, profile: dict, qa: QAEngine,
                 dry_run: bool = False, interactive: bool = False, confirm: Callable[[str], bool] | None = None):
        self.s = settings
        self.db = db
        self.browser = browser
        self.llm = llm
        self.profile = profile
        self.qa = qa
        self.dry_run = dry_run or settings.apply.dry_run
        self.interactive = interactive
        self.confirm = confirm
        self.pacer = Pacer(settings, db)
        self._summary = candidate_summary(profile)
        self._courses_lock = threading.Lock()

    # ---------- descoberta ----------
    def _source(self, name: str):
        if name == "linkedin":
            return SOURCES["linkedin"](geo_id=self.s.discovery.linkedin_geo_id, mode=self.s.discovery.linkedin_mode)
        return SOURCES[name]()

    def discover(self, only_source: str | None = None) -> dict[str, int]:
        page = None
        counts: dict[str, int] = {}
        max_age = self.s.discovery.max_age_hours
        searches = [x for x in self.s.all_searches() if not only_source or x.source == only_source]
        console.print(f"[dim]{len(searches)} buscas (24h, remoto)[/]")
        for s in searches:
            if s.source not in SOURCES:
                console.print(f"[yellow]fonte desconhecida: {s.source}[/]")
                continue
            src = self._source(s.source)
            if getattr(src, "needs_browser", True) and page is None:
                page = self.browser.page()
            try:
                found = src.search(self.browser, page, s, self.s.discovery.max_pages_per_search or self.s.max_pages_per_search)
            except Exception as e:
                console.print(f"[red]{s.source} '{s.query}': erro na busca: {e}[/]")
                self.db.log("error", f"busca falhou: {e}", data={"source": s.source, "query": s.query})
                continue
            new = old = 0
            for f in found:
                job = f.to_job()
                if job.status == "new" and is_too_old(job.posted_at, max_age):
                    job.status, job.reason = "skipped", f"publicada há mais de {max_age}h"
                    old += 1
                if self.db.upsert_job(job):
                    new += 1
            counts[f"{s.source}:{s.query}"] = new
            console.print(f"[green]{s.source}[/] '{s.query}': {len(found)} encontradas, {new} novas" + (f" ({old} antigas ignoradas)" if old else ""))
            if getattr(src, "needs_browser", True):
                h.medium()
        self.db.kv_set("last_discovery", datetime.now().isoformat(timespec="seconds"))
        return counts

    # ---------- preparação ----------
    # vagas afirmativas/exclusivas para grupos (PCD, mulheres, pessoas negras, trans, 50+...): só se o usuário
    # pertencer ao grupo (facts.diversity_groups_member); a classificação por IA não pega todos os casos
    AFFIRMATIVE_RE = re.compile(
        r"(vaga |oportunidade |posicao |programa )?(afirmativ|exclusiv|inclusiv|prioritari|voltad[ao]|direcionad[ao]|reservad[ao]|destinad[ao]|dedicad[ao])[a-z]*\s*"
        r"(para |a |as |aos |ao |de |p/ )?\s*(pessoas? (com deficiencia|pretas?|pardas?|negras?|trans|lgbt|indigenas?|refugiadas?|50\+|60\+|maduras?|surdas?|cegas?|neurodivergentes?)|"
        r"pcd|pcds|mulheres?|talentos? diversos?|diversidade|profissionais? negr[ao]s?|lgbtqia?\+?|pretos? e pardos?|indigenas|migrantes|refugiados|idosos|veteranos|neurodivergentes)|"
        r"\b(pcd|pcds)\b|\[(exclusiv[ao]|afirmativ[ao]) [^\]]*\]|\((vaga )?(exclusiv[ao]|afirmativ[ao])[^)]*\)",
        re.I,
    )
    GROUP_HINTS = {
        "pcd": r"deficiencia|pcd", "mulher": r"mulher", "negr": r"negr|pret|pard", "trans": r"\btrans\b|lgbt", "lgbt": r"lgbt",
        "indigena": r"indigena", "50": r"50\+|60\+|madur|idos", "refugiad": r"refugiad|migrant", "neurodiverg": r"neurodiverg",
    }

    def _affirmative_for_other_group(self, job: Job) -> str | None:
        text = _fold((job.title or "") + " " + (job.description or "")[:1500])
        m = self.AFFIRMATIVE_RE.search(text)
        if not m:
            return None
        frag = m.group(0)
        member = [_fold(str(x)) for x in ((self.profile.get("facts") or {}).get("diversity_groups_member") or [])]
        for key, pat in self.GROUP_HINTS.items():
            if re.search(pat, frag) and any(re.search(pat, mem) for mem in member):
                return None   # pertence ao grupo: pode aplicar
        return f"vaga afirmativa/exclusiva para grupo ao qual não pertence ('{frag.strip()[:60]}')"

    def _blacklisted(self, job: Job) -> str | None:
        t = _fold(job.title or "")
        for w in self.s.filters.title_blacklist:
            if _fold(w) in t:
                return f"título contém '{w}'"
        c = _fold(job.company or "")
        for w in self.s.filters.company_blacklist:
            if _fold(w) and _fold(w) in c:
                return f"empresa na blacklist ('{w}')"
        for hst in (self.s.filters.host_blacklist or []):
            hst = (hst or "").strip().lower()
            if hst and hst in ((job.external_url or "") + " " + (job.url or "")).lower():
                return f"site na blacklist ('{hst}')"
        why = self._affirmative_for_other_group(job)
        if why:
            return why
        return None

    def _company_recently_applied(self, job: Job) -> bool:
        days = self.s.apply.apply_once_per_company_days
        if not days or not job.company:
            return False
        r = self.db.conn.execute(
            "SELECT 1 FROM jobs WHERE status='applied' AND lower(company)=lower(?) AND replace(applied_at,'T',' ') >= datetime('now','localtime', ?) LIMIT 1",
            (job.company.strip(), f"-{days} days"),
        ).fetchone()
        return bool(r)

    def prepare(self, page: Page | None, job: Job, allow_browser: bool = True, force: bool = False) -> bool:
        """Busca detalhes e classifica. Retorna True se a vaga fica na fila. force=True ignora as regras de 24h/remoto
        (só para vagas pedidas explicitamente com --job --force)."""
        max_age = self.s.discovery.max_age_hours
        if not force and is_too_old(job.posted_at, max_age):
            self.db.set_status(job.id, "skipped", f"publicada há mais de {max_age}h")
            return False
        why = self._blacklisted(job)
        if why:
            self.db.set_status(job.id, "skipped", why)
            return False
        if self.db.already_applied_same_posting(job.company, job.title):
            self.db.set_status(job.id, "skipped", "mesma vaga já aplicada por outra fonte")
            return False
        if self._company_recently_applied(job):
            self.db.set_status(job.id, "skipped", "já aplicado nessa empresa recentemente")
            return False
        if not job.description:
            src = self._source(job.source)
            try:
                src.fetch_details(self.browser, page, job)
            except Exception as e:
                job.attempts += 1
                self.db.save_job(job)
                self.db.set_status(job.id, "queued" if job.attempts < self.s.apply.max_attempts else "failed", f"detalhes falharam: {str(e)[:120]}")
                return False
            if job.apply_type == "closed":
                job.status, job.reason = "skipped", "vaga encerrada"
                self.db.save_job(job)
                return False
            if not force and is_too_old(job.posted_at, max_age):   # data mais precisa vinda da página
                job.status, job.reason = "skipped", f"publicada há mais de {max_age}h"
                self.db.save_job(job)
                return False
            if job.apply_type == "already_applied":
                job.status, job.reason, job.applied_at = "applied", "já aplicada anteriormente (detectado na página)", datetime.now().isoformat(timespec="seconds")
                self.db.save_job(job)
                return False
            if allow_browser and len(job.description or "") < 200 and job.source == "linkedin" and self.s.discovery.linkedin_mode == "guest":
                try:   # endpoint público às vezes vem sem descrição; a página logada costuma ter
                    page = page or self.browser.page()
                    SOURCES["linkedin"](geo_id=self.s.discovery.linkedin_geo_id, mode="session").fetch_details_session(self.browser, page, job)
                except Exception:
                    pass
            why = self._blacklisted(job)  # título pode ter vindo só agora
            if why:
                job.status, job.reason = "skipped", why
                self.db.save_job(job)
                return False
        if not job.classification:
            if self.s.filters.job_languages and "en" not in self.s.filters.job_languages and guess_language(job.description or "") == "en":
                job.status, job.reason = "skipped", "descrição em inglês (heurística, sem IA)"
                self.db.save_job(job)
                return False
            if not force and HARD_REMOTE_ONLY and looks_onsite(job.title or "", job.description or ""):
                job.status, job.reason = "skipped", "não é remoto (presencial/híbrido na descrição, heurística sem IA)"
                self.db.save_job(job)
                return False
            reused = self._reuse_classification(job)
            if reused:
                job.classification = reused
                console.print("   [dim]classificação reaproveitada de vaga igual (sem IA)[/]")
        if not job.classification:
            # 1º classificador de regras (sem IA). Validado nas 742 vagas já classificadas pelo modelo:
            # 85% saem com confiança >= 0,75 e, nessas, 0,8% de falso "é vaga de dev".
            from .classify_rules import classify_job_rules
            try:
                rcls, conf = classify_job_rules(job.title or "", job.company or "", job.location or "",
                                                job.description or "", self.profile, self.s)
            except Exception:
                rcls, conf = None, 0.0
            if rcls is not None and conf >= self.s.llm.rules_min_confidence:
                job.classification = rcls
                console.print(f"   [dim]classificada por regras (sem IA, confiança {conf:.2f})[/]")
        if not job.classification:
            try:
                cls = self.llm.classify_job(
                    title=job.title or "", company=job.company or "", location=job.location or "",
                    description=job.description or "(sem descrição disponível)",
                    apply_roles=self.s.filters.apply_roles, exclude_roles=self.s.filters.exclude_roles,
                    candidate_summary=self._summary,
                )
            except BudgetExceeded as e:
                # sem orçamento e regra incerta: vaga ambígua (analista de dados, consultor...). Candidatar
                # no escuro arrisca vaga errada — e amanhã ela já passou das 24h. Descarta com o motivo.
                self.db.save_job(job)
                self.db.set_status(job.id, "skipped", f"classificação incerta sem IA ({e})"[:120])
                return False
            except Exception as e:
                self.db.save_job(job)
                self.db.set_status(job.id, "queued", f"classificação falhou: {str(e)[:100]}")
                return False
            job.classification = cls
        cls = job.classification
        reject = self.s.filters.reject_if
        lang = (cls.get("language") or "pt-BR")
        req_langs = [str(x) for x in (cls.get("required_languages") or [])]
        # idioma no TÍTULO ("AI Engineer [REMOTO - INGLÊS]", "Senior Angular Developer (inglês avançado)"):
        # a descrição às vezes não repete a exigência e o classificador não tinha como ver.
        if re.search(r"\bingl[eê]s\b|\benglish\b", job.title or "", re.I) and not re.search(
                r"ingl[eê]s\s+(desej|diferencial|b[aá]sico|opcional)|open english", job.title or "", re.I):
            req_langs.append("inglês (no título da vaga)")
        # Regra do usuário (2026-09-10): NÃO aplicar para vaga que exige inglês, mesmo com
        # inglês avançado no perfil. Só passa se o idioma vier marcado como não obrigatório.
        foreign_required = [
            r for r in req_langs
            if not re.search(r"portugu", r, re.I)
            and not re.search(r"desej[aá]vel|diferencial|plus|nice|opcional|n[aã]o (é|e) (obrigat|requisito)", r, re.I)
        ]
        if not cls.get("is_dev_role"):
            job.status, job.reason = "skipped", f"não é vaga de dev: {cls.get('reason','')}"
        elif self.s.filters.job_languages and lang not in self.s.filters.job_languages:
            job.status, job.reason = "skipped", f"descrição em {lang} (só aceito {', '.join(self.s.filters.job_languages)})"
        elif HARD_REMOTE_ONLY and not force and (cls.get("work_model") in ("onsite", "hybrid")):
            job.status, job.reason = "skipped", f"não é remoto ({cls.get('work_model')})"
        elif foreign_required and reject.get("requires_language_i_dont_have", True):
            job.status, job.reason = "skipped", f"exige idioma: {', '.join(foreign_required)}"
        elif cls.get("hard_reject"):
            job.status, job.reason = "skipped", f"requisito eliminatório: {cls.get('reason','')}"
        elif int(cls.get("fit_score") or 0) < self.s.apply.min_fit_score:
            job.status, job.reason = "skipped", f"fit {cls.get('fit_score')} < {self.s.apply.min_fit_score}"
        else:
            job.status, job.reason = "queued", None
        self.db.save_job(job)
        if job.status == "queued":
            self._auto_suggest_courses(cls)
        return job.status == "queued"

    def _auto_suggest_courses(self, cls: dict) -> None:
        """Garante >= min_udemy cursos sugeridos (cache) para cada stack da vaga que exista no perfil."""
        if not self.s.courses.auto_suggest:
            return
        with self._courses_lock:
            self._auto_suggest_courses_locked(cls)

    def _auto_suggest_courses_locked(self, cls: dict) -> None:
        import yaml
        from .resume.profile import all_skills
        from .resume.tailor import _norm
        path = self.s.profile_path.parent / "courses.yaml"
        data = (yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else None) or {"stacks": {}}
        known = {_norm(x): x for x in all_skills(self.profile)}
        changed = False
        for st in (cls.get("stack") or [])[:6]:
            key = str(st).strip().lower()
            if _norm(key) not in known or key in data["stacks"] or key in _GENERIC_STACKS:
                continue
            try:
                items = self.llm.suggest_courses(key, n=self.s.courses.min_udemy, min_udemy=self.s.courses.min_udemy)
            except Exception as e:
                self.db.log("warn", f"sugestão de cursos falhou para {key}: {e}")
                continue
            data["stacks"][key] = [{"name": c.get("name"), "issuer": c.get("issuer") or "", "hours": c.get("hours") or 0,
                                   "url": c.get("url") or "", "status": "todo", "year": ""} for c in items if c.get("name")]
            changed = True
            console.print(f"   [dim]cursos sugeridos para '{key}': {len(data['stacks'][key])} (config/courses.yaml)[/]")
        if changed:
            import os
            tmp = path.with_suffix(".yaml.tmp")
            tmp.write_text("# Cursos por stack. status: todo | in_progress | done (+ year). done entra como concluído; in_progress como 'em andamento'.\n"
                           + yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=110), encoding="utf-8")
            os.replace(tmp, path)
            from .resume.profile import load_profile
            self.profile = load_profile(self.s.profile_path)

    def _reuse_classification(self, job: Job) -> dict | None:
        """Mesma vaga em outra fonte (empresa+título iguais) ou descrição idêntica: copia a classificação."""
        h = desc_hash(job.description)
        row = self.db.conn.execute(
            "SELECT classification FROM jobs WHERE id != ? AND classification IS NOT NULL AND json_extract(classification, '$._desc_hash') = ? LIMIT 1",
            (job.id, h),
        ).fetchone()
        if not row and job.company and job.title:
            row = self.db.conn.execute(
                "SELECT classification FROM jobs WHERE id != ? AND classification IS NOT NULL AND lower(company)=lower(?) AND lower(title)=lower(?) LIMIT 1",
                (job.id, job.company.strip(), job.title.strip()),
            ).fetchone()
        if not row:
            return None
        try:
            cls = json.loads(row[0])
        except Exception:
            return None
        cls["_desc_hash"] = h
        cls["_reused"] = True
        return cls

    def prepare_all(self, limit: int | None = None, http_workers: int = 2) -> tuple[int, int]:
        """Classifica as vagas 'new'. Vagas que não precisam do navegador (LinkedIn público) rodam em threads
        paralelas ao navegador, que cuida do Indeed."""
        jobs = self.db.list_jobs(status="new", limit=limit or 2000)
        needs_browser = lambda j: getattr(self._source(j.source), "needs_browser", True)
        browser_jobs = [j for j in jobs if needs_browser(j)]
        http_jobs = [j for j in jobs if not needs_browser(j)]
        total = len(jobs)
        console.print(f"[dim]classificando {total} vagas: {len(browser_jobs)} pelo navegador (Indeed) + {len(http_jobs)} por HTTP (LinkedIn público, {http_workers} threads)[/]")
        counter = {"done": 0, "queued": 0}
        lock = threading.Lock()

        def report(job: Job, ok: bool) -> None:
            with lock:
                counter["done"] += 1
                if ok:
                    counter["queued"] += 1
                i = counter["done"]
            cls = job.classification or {}
            if ok:
                console.print(f"[dim]({i}/{total})[/] [green]fila[/] fit={cls.get('fit_score')} {cls.get('seniority')} · {job.title or '?'} · {job.company or '?'} [{job.source}]")
            else:
                console.print(f"[dim]({i}/{total}) {job.status}: {(job.reason or '')[:90]} · {job.title or '?'} [{job.source}][/]")

        def http_worker(chunk: list[Job]) -> None:
            db = DB(self.s.db_path)   # conexão própria por thread
            worker = Pipeline(self.s, db, self.browser, self.llm, self.profile, self.qa, dry_run=self.dry_run, interactive=False)
            worker._courses_lock = self._courses_lock
            for job in chunk:
                try:
                    ok = worker.prepare(None, job, allow_browser=False)
                except Exception as e:
                    ok = False
                    try:
                        db.log("error", f"prepare falhou: {e}", job.id)
                    except Exception:
                        console.print(f"[red]prepare falhou ({job.id}): {str(e)[:120]}[/]")
                try:
                    report(job, ok)
                except Exception:
                    pass
                time.sleep(random.uniform(0.5, 1.5))

        threads: list[threading.Thread] = []
        if http_jobs:
            n = max(1, min(http_workers, len(http_jobs)))
            chunks = [http_jobs[i::n] for i in range(n)]
            for ch in chunks:
                t = threading.Thread(target=http_worker, args=(ch,), daemon=True)
                t.start()
                threads.append(t)

        page = None
        for job in browser_jobs:
            try:
                if page is None:
                    page = self.browser.page()
                ok = self.prepare(page, job)
            except Exception as e:
                self.db.log("error", f"prepare falhou: {e}", job.id)
                ok = False
            report(job, ok)
            h.pause(1, 3)
        for t in threads:
            t.join()
        return total, counter["queued"]

    # ---------- currículo ----------
    @property
    def bullet_bank(self):
        """Banco de bullets já aprovados (aprende a cada currículo novo)."""
        from .resume.bullets import BulletBank, BANK_NAME
        if getattr(self, "_bullet_bank", None) is None:
            self._bullet_bank = BulletBank(resolve(self.s.resume.output_dir) / BANK_NAME)
        return self._bullet_bank

    def _compose_resume(self, job: Job) -> tuple[dict | None, float]:
        """Monta o currículo com bullets já aprendidos, sem IA. (currículo, fração na stack)."""
        from .resume.bullets import compose
        from .resume.library import best_match, adapt_for_job
        from .resume.tailor import adaptation_context
        ad = adaptation_context(self.profile, job.classification or {})
        need = float(ad.get("min_stack_share") or 0)
        job_stack = ad.get("job_stack") or []
        if not job_stack:
            return None, 0.0
        base, _ = best_match(self.db, self.profile, job, need_override=0.0)   # o mais parecido, sem exigir meta
        if base is None:
            return None, 0.0
        out, share = compose(self.bullet_bank, self.profile, job_stack, base, need)
        if out is not None:
            adapt_for_job(out, self.profile, job, ad)
            out.setdefault("_meta", {})["composed"] = {"bullets": round(share, 2), "meta": need}
        return out, share

    def build_resume(self, job: Job) -> Path:
        out_dir = resolve(self.s.resume.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        if job.resume_path and Path(job.resume_path).exists():
            return Path(job.resume_path)
        warnings: list[str] = []
        resume = self._reuse_resume(job)
        if resume is not None:
            console.print("   [dim]currículo reaproveitado da biblioteca (sem IA)[/]")
        else:
            resume, share = self._compose_resume(job)
            if resume is not None:
                console.print(f"   [dim]currículo montado com bullets já aprendidos (sem IA): {share:.0%} na stack[/]")
        if resume is None:
            try:
                resume, warnings = build_resume(self.llm, self.s.resume, self.profile, job, courses_cfg=self.s.courses)
                try:
                    novos = self.bullet_bank.harvest(resume, self.profile)
                    self.bullet_bank.save()
                    if novos:
                        console.print(f"   [dim]aprendi {novos} bullet(s) novo(s) para reaproveitar[/]")
                except Exception:
                    pass
            except BudgetExceeded as e:
                # sem orçamento a leva NÃO para: usa o melhor currículo disponível, mesmo abaixo da meta
                resume, share = self._compose_resume_fallback(job)
                if resume is None:
                    raise
                warnings = [f"{e}; currículo montado sem IA ({share:.0%} na stack)"]
                console.print(f"   [yellow]sem orçamento de IA: currículo montado do banco ({share:.0%} na stack)[/]")
        jp = resume_json_path(out_dir, job)
        jp.write_text(__import__("json").dumps(resume, ensure_ascii=False, indent=2), encoding="utf-8")
        pdf = generate_pdf(resume, self.profile, jp.with_suffix(".pdf"), playwright=self.browser.playwright,
                           template=self.s.resume.template, accent=self.s.resume.accent_color, photo_mm=self.s.resume.photo_mm)
        for w in warnings:
            console.print(f"   [yellow]currículo: {w}[/]")
        job.resume_path = str(pdf)
        self.db.save_job(job)
        return pdf

    def resume_for_upload(self, job: Job) -> Path | None:
        """Gera (se preciso) e devolve a cópia com nome neutro. Chamada só quando o formulário pede o arquivo."""
        try:
            self.build_resume(job)
        except Exception as e:
            raise ResumeBuildError(str(e)) from e
        return self.upload_copy(job)

    def _compose_resume_fallback(self, job: Job) -> tuple[dict | None, float]:
        """Último recurso (sem orçamento): melhor currículo possível, mesmo abaixo da meta de adaptação."""
        from .resume.bullets import compose
        from .resume.library import best_match, adapt_for_job
        from .resume.tailor import adaptation_context, stack_share
        ad = adaptation_context(self.profile, job.classification or {})
        job_stack = ad.get("job_stack") or []
        base, _ = best_match(self.db, self.profile, job, need_override=0.0)
        if base is None:
            return None, 0.0
        out, share = compose(self.bullet_bank, self.profile, job_stack, base, 0.0)
        out = out if out is not None else base
        adapt_for_job(out, self.profile, job, ad)
        out.setdefault("_meta", {})["composed"] = {"bullets": round(share, 2), "meta": ad.get("min_stack_share")}
        return out, share

    def upload_copy(self, job: Job) -> Path | None:
        """Cópia do PDF da vaga com nome neutro (não revela empresa nem geração automática)."""
        if not job.resume_path or not Path(job.resume_path).exists():
            return None
        personal = self.profile.get("personal") or {}
        name = self.s.resume.upload_filename.format(
            first_name=personal.get("first_name", ""), last_name=personal.get("last_name", ""), full_name=personal.get("full_name", "")
        ).strip() or "Curriculo.pdf"
        if not name.lower().endswith(".pdf"):
            name += ".pdf"
        dst = resolve(self.s.resume.output_dir) / "upload" / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        import shutil
        shutil.copy(job.resume_path, dst)
        return dst

    def _reuse_resume(self, job: Job) -> dict | None:
        """Reaproveita um currículo já gerado quando ele serve para esta vaga (sem IA).
        1) descrição idêntica ou mesma empresa+título: serve direto.
        2) qualquer currículo recente do mesmo nível: só se JÁ atingir a meta de adaptação à stack desta vaga
           (mesma métrica exigida de um currículo novo, então não há perda de qualidade)."""
        from .resume.tailor import adaptation_context, stack_share, seniority_bucket

        def load(path: str) -> dict | None:
            jp = Path(path).with_suffix(".json")
            if not jp.exists():
                return None
            try:
                return json.loads(jp.read_text(encoding="utf-8"))
            except Exception:
                return None

        h = desc_hash(job.description)
        rows = self.db.conn.execute(
            "SELECT resume_path FROM jobs WHERE id != ? AND resume_path IS NOT NULL AND (json_extract(classification, '$._desc_hash') = ? OR (lower(company)=lower(?) AND lower(title)=lower(?)))",
            (job.id, h, (job.company or "").strip(), (job.title or "").strip()),
        ).fetchall()
        for r in rows:
            data = load(r[0])
            if data is not None:
                data.setdefault("_meta", {})["reused_from"] = Path(r[0]).stem
                return data

        # 2) biblioteca: currículo cujos bullets já cobrem a stack desta vaga. Competências e
        #    headline são ajustados deterministicamente, então não entram no critério de corte.
        from .resume.library import best_match
        data, _info = best_match(self.db, self.profile, job)
        return data

    # ---------- candidatura ----------
    def _applier(self, job: Job):
        if job.source == "linkedin":
            if job.apply_type == "easy_apply":
                return LinkedInEasyApplyApplier()
            if job.apply_type == "external":
                return LinkedInExternalApplier()
            return LinkedInAutoApplier()   # descobre o tipo na página logada e despacha
        if job.source == "indeed":
            return IndeedApplyApplier() if job.apply_type == "indeed_apply" else IndeedExternalApplier()
        return get_applier(job.ats or "generic")

    def apply(self, page: Page, job: Job) -> FlowResult:
        ctx = ApplyContext(
            settings=self.s, browser=self.browser, db=self.db, qa=self.qa, profile=self.profile,
            resume=lambda: self.resume_for_upload(job),   # só gera o PDF se o formulário pedir
            dry_run=self.dry_run, interactive=self.interactive,
            pause_before_submit=self.s.apply.pause_before_submit, confirm=self.confirm,
        )
        applier = self._applier(job)
        try:
            return applier.apply(ctx, page, job)
        except ResumeBuildError as e:
            self.db.log("error", f"currículo falhou em {job.id}: {e}", job.id)
            return FlowResult("failed", f"currículo falhou: {str(e)[:150]}")
        except Exception as e:
            self.db.log("error", f"applier {applier.name} explodiu: {e}", job.id)
            return FlowResult("failed", f"exceção em {applier.name}: {str(e)[:150]}")

    def process(self, page: Page, job: Job, force: bool = False) -> FlowResult:
        job.attempts += 1
        job.status = "in_progress"
        self.db.save_job(job)
        if job.status == "in_progress" and (not job.classification or not job.description):
            if not self.prepare(page, job, force=force):
                return FlowResult("skipped", job.reason or "")
        elif not force:
            if is_too_old(job.posted_at, self.s.discovery.max_age_hours):
                self.db.set_status(job.id, "skipped", f"publicada há mais de {self.s.discovery.max_age_hours}h")
                return FlowResult("skipped", "publicada há mais de 24h")
        # o currículo é gerado dentro de apply(), só quando o formulário pede o arquivo
        res = self.apply(page, job)
        if res.status == "failed" and "currículo falhou" in (res.reason or ""):
            self.db.set_status(job.id, "queued" if job.attempts < self.s.apply.max_attempts else "failed", (res.reason or "")[:120])
            return res
        bucket = self.pacer.bucket(job)
        if res.status == "applied":
            # o motivo do fluxo tem de FICAR na candidatura: a GeekHunter devolve "confirmar pelo
            # e-mail" e a própria mensagem diz "registrado em Candidaturas", mas `notes` era
            # sempre None (o argumento nunca era passado) — a informação só existia no log.
            nota = res.reason if (res.reason or '').strip() not in ('', 'sucesso') else None
            self.db.add_application(job, bucket, job.ats, job.resume_path, res.answers, nota)
        elif res.status == "already_applied":
            job.status, job.reason, job.applied_at = "applied", "já aplicada anteriormente", datetime.now().isoformat(timespec="seconds")
            self.db.save_job(job)
        elif res.status == "dry_run":
            job.status, job.reason = "dry_run", res.reason
            self.db.save_job(job)
        elif res.status == "needs_user":
            job.status, job.reason = "pending_question", res.reason
            self.db.save_job(job)
        elif res.status == "skipped":
            # vaga encerrada/anúncio removido/afirmativa etc.: não volta para a fila (antes voltava e repetia a cada rodada)
            job.status, job.reason = "skipped", res.reason
            self.db.save_job(job)
        elif res.status == "limit":
            self.pacer.mark_limit_hit(job)
            job.status, job.reason = "queued", res.reason
            self.db.save_job(job)
        else:
            job.status = "queued" if job.attempts < self.s.apply.max_attempts else "failed"
            job.reason = res.reason
            self.db.save_job(job)
        self.db.log("info", f"{res.status}: {res.reason}", job.id, {"steps": res.steps, "bucket": bucket})
        return res

    def process_queue(self, limit: int | None = None, job_ids: list[str] | None = None, force: bool = False) -> RunSummary:
        summary = RunSummary()
        presas = self.db.recover_stale_in_progress()
        if presas:
            console.print(f"[dim]  {presas} vaga(s) presa(s) em in_progress voltaram para a fila[/]")
        page = self.browser.page()
        if job_ids:
            jobs = [j for j in (self.db.get_job(i) or self.db.find_job_by_prefix(i) for i in job_ids) if j]
        else:
            jobs = self.db.queue()
            jobs.sort(key=lambda j: (-(int((j.classification or {}).get("fit_score") or 0)), j.discovered_at))
        for job in jobs:
            if limit and summary.processed >= limit:
                summary.stopped_reason = "limite da execução"
                break
            if not job_ids and not self.pacer.in_active_hours():   # vaga pedida explicitamente (--job) roda a qualquer hora
                summary.stopped_reason = "fora da janela ativa"
                break
            ok, why = self.pacer.can_apply(job)
            if not ok:
                if "total" in why:
                    summary.stopped_reason = why
                    break
                continue
            # mesma vaga (empresa + título) já enviada por outra fonte hoje/recentemente (LinkedIn e Indeed anunciam a mesma):
            # a checagem do prepare não pega quando as duas estavam na fila ao mesmo tempo
            if self.db.already_applied_same_posting(job.company, job.title):
                self.db.set_status(job.id, "skipped", "mesma vaga já aplicada por outra fonte")
                continue
            # o MESMO anúncio republicado com outro id: se uma irmã já falhou, repetir só gasta
            # ciclo de navegador contra a mesma página — as tentativas do anúncio ficam com ela
            irma = self.db.same_posting_already_failed(job.company, job.title, job.id)
            if irma:
                _, tent, motivo = irma
                self.db.set_status(job.id, "skipped",
                                   f"mesma vaga já falhou em outro anúncio ({tent}x: {motivo[:52]})")
                continue
            # Indeed Apply exige endereço + CEP em quase todo formulário: sem esses dados no perfil, nem abre a vaga
            # (fica na fila com o motivo; volta sozinha quando o perfil tiver personal.address/zip)
            if (job.apply_type or "") == "indeed_apply":
                personal = self.profile.get("personal") or {}
                if not (personal.get("address") and personal.get("zip")):
                    self.db.set_status(job.id, "queued", "aguardando endereço/CEP no perfil (Indeed Apply)")
                    if not getattr(self, "_warned_address", False):
                        console.print("[yellow]  Indeed Apply: pulando vagas até o perfil ter endereço e CEP (personal.address / personal.zip)[/]")
                        self._warned_address = True
                    continue
            console.print(f"\n[bold cyan]▶ {job.title or '?'}[/] · {job.company or '?'} · {job.source}/{job.apply_type or '?'}  [dim]{job.id}[/]")
            summary.processed += 1
            try:
                res = self.process(page, job, force=force and bool(job_ids))
            except KeyboardInterrupt:
                raise
            except Exception as e:
                res = FlowResult("failed", f"erro inesperado: {e}")
                self.db.set_status(job.id, "queued" if job.attempts < self.s.apply.max_attempts else "failed", res.reason)
            color = {"applied": "green", "dry_run": "blue", "needs_user": "yellow", "skipped": "dim"}.get(res.status, "red")
            console.print(f"   [{color}]{res.status}[/] {res.reason}")
            summary.items.append((job.id, res.status, res.reason))
            if res.status == "applied":
                summary.applied += 1
            elif res.status == "skipped":
                summary.skipped += 1
            elif res.status == "needs_user":
                summary.pending += 1
                notify(f"❓ Pergunta pendente em {job.title} @ {job.company}. Rode `vagas questions`.")
            elif res.status == "dry_run":
                summary.dry_run += 1
            elif res.status == "limit":
                notify(f"⛔ Limite diário atingido em {self.pacer.bucket(job)}.")
            else:
                summary.failed += 1
            self.browser.close_extra_pages(page)
            if res.status in ("applied", "failed", "dry_run", "limit", "already_applied"):
                self.pacer.after_application(res.status, self.pacer.bucket(job))
            else:
                h.pause(3, 8)
        return summary
