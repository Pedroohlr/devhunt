"""Gera o currículo adaptado (JSON) e valida contra o perfil mestre (anti-invenção)."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import unicodedata

from ..config import ResumeCfg
from ..db import Job
from ..llm import LLM
from .profile import all_skills, scale_profile_for_seniority


_STOP = {"para", "como", "pela", "pelo", "pelos", "pelas", "entre", "mais", "menos", "sobre", "onde", "quando", "esse", "essa",
         "este", "esta", "isso", "aquele", "with", "from", "that", "this", "into", "than", "them", "their", "were", "also", "junto"}


def _tokens(s: str) -> set[str]:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    out = set()
    for t in re.findall(r"[a-z0-9][a-z0-9.+#/-]*", s):
        t = t.strip(".-/")
        if len(t) >= 4 and t not in _STOP:
            out.add(t[:5])   # raiz grosseira: desenvolvi/desenvolvimento -> "desen"
        elif len(t) in (2, 3) and t.isalpha() and t not in ("com", "uma", "das", "dos", "the", "and", "for", "por", "que", "nos", "nas", "aos", "sem"):
            out.add(t)       # siglas curtas: sap, api, aws, php, c#, iot
    return out


# nomes de "skill" que são palavras comuns: não contam como tecnologia na checagem por experiência
_GENERIC_SKILLS = {
    "sql", "git", "linux", "tdd", "bdd", "solid", "ux/ui", "iot", "apis rest", "api rest", "apis", "api", "rest", "clean architecture",
    "microservicos", "microsservicos", "ci/cd", "integracoes", "integracao", "integracao de apis", "integracao sap", "automacoes", "automacao",
    "chatbots", "chatbots whatsapp", "web", "mobile", "backend", "back-end", "frontend", "front-end", "full stack", "fullstack",
    "testes e2e", "e2e", "devops", "dados", "cibersegurança", "ciberseguranca", "low-code", "agil", "scrum",
}


def _tech_mentions(text: str, skills: set[str]) -> list[str]:
    """Skills do perfil citadas no texto (por nome, case-insensitive, com fronteira de palavra)."""
    low = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    out = []
    for sk in skills:
        name = unicodedata.normalize("NFKD", sk).encode("ascii", "ignore").decode().lower()
        if len(name) < 2 or name in _GENERIC_SKILLS:
            continue
        if re.search(r"(?<![a-z0-9])" + re.escape(name) + r"(?![a-z0-9])", low):
            out.append(sk)
    return out


def bullet_coverage(bullet: str, corpus: list[str]) -> float:
    """Fração dos termos de conteúdo do bullet que aparecem no perfil daquela experiência."""
    bt = _tokens(bullet)
    if not bt:
        return 1.0
    ct: set[str] = set()
    for c in corpus:
        ct |= _tokens(c)
    return len(bt & ct) / len(bt)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9+#.]", "", (s or "").lower())


def adaptation_context(profile: dict, classification: dict) -> dict:
    """Stack da vaga que existe no perfil + tecnologias permitidas para re-stack."""
    ad = profile.get("adaptation") or {}
    sk = profile.get("skills") or {}
    allowed: list[str] = []
    for k in ad.get("allowed_from") or ["primary", "secondary", "also_experienced"]:
        allowed += [str(x) for x in sk.get(k) or []]
    prof_names = all_skills(profile)
    job_stack = []
    for st in (classification or {}).get("stack") or []:
        st_n = _norm(str(st))
        for name in prof_names:
            if st_n and (st_n == _norm(name) or (len(st_n) >= 3 and (st_n in _norm(name) or _norm(name) in st_n))):
                job_stack.append(name)
                break
    # nomes bonitos (do perfil) em vez do lower()
    pretty = {_norm(x): x for x in allowed + list((profile.get("skill_years") or {}).keys())}
    job_stack = [pretty.get(_norm(x), x) for x in dict.fromkeys(job_stack)]
    return {
        "job_stack": job_stack,
        "allowed": allowed,
        "min_stack_share": float(ad.get("min_stack_share") or 0),
        "restack_all": bool(ad.get("restack_all", True)),
        "classification": classification or {},
    }


def _restack_allowed(src_exp: dict, ctx: dict) -> bool:
    if src_exp.get("restack") is False:
        return False
    return bool(ctx.get("restack_all", True)) or bool(src_exp.get("restack"))


def stack_share(resume: dict, job_stack: list[str]) -> tuple[float, float]:
    """(fração de bullets citando a stack da vaga, fração das competências principais na stack)."""
    if not job_stack:
        return 0.0, 0.0
    js = {_norm(x) for x in job_stack}
    bullets = [b for e in resume.get("experiences") or [] for b in e.get("bullets") or []]
    hit = sum(1 for b in bullets if _tech_mentions(str(b), set(job_stack)))
    main = (resume.get("skills") or {}).get("main") or []
    mhit = sum(1 for m in main if _norm(m) in js)
    return (hit / len(bullets) if bullets else 0.0), (mhit / len(main) if main else 0.0)


def reorder_skills_for_stack(resume: dict, profile: dict, adaptation: dict) -> None:
    """Põe a stack da vaga na frente de skills.main — determinístico, sem IA e sem inventar nada.

    skills.main só pode conter tecnologias que já estão no perfil, então "quantas das principais
    citam a stack da vaga" é problema de SELEÇÃO/ORDEM, não de redação: pedir de novo ao modelo era
    gastar uma chamada de gpt-4.1 para reordenar uma lista. Quando a fração ainda não bate, é porque
    o candidato não tem tantas tecnologias da vaga — nenhuma retentativa resolveria isso.
    """
    job_stack = [str(x) for x in (adaptation.get("job_stack") or [])]
    if not job_stack:
        return
    sk = resume.setdefault("skills", {})
    main = [str(x) for x in (sk.get("main") or [])]
    other = [str(x) for x in (sk.get("other") or [])]
    known = {_norm(x) for x in all_skills(profile)}
    js = {_norm(x) for x in job_stack}
    seen_main = {_norm(m) for m in main}
    # stack da vaga primeiro: o que já está em main (mantendo a grafia do modelo) + o que falta e o perfil tem
    have = [m for m in main if _norm(m) in js]
    have += [x for x in job_stack if _norm(x) not in seen_main and _norm(x) in known]
    have = list(dict.fromkeys(have))
    rest = [m for m in main if _norm(m) not in js]
    share = float(adaptation.get("min_stack_share") or 0)
    limit = max(6, len(have), int(len(have) / share)) if share > 0 and have else max(6, len(main))
    new_main = list(dict.fromkeys(have + rest))[:limit]
    kept = {_norm(m) for m in new_main}
    moved = [m for m in main if _norm(m) not in kept]
    sk["main"] = new_main
    sk["other"] = list(dict.fromkeys(moved + [o for o in other if _norm(o) not in kept]))[:8]


def validate_resume(resume: dict, profile: dict, adaptation: dict | None = None) -> list[str]:
    """Remove/ajusta o que não existe no perfil. Retorna lista de avisos."""
    warnings: list[str] = []
    adaptation = adaptation or {}
    known = {_norm(s) for s in all_skills(profile)}
    sk = resume.get("skills") or {}
    for key in ("main", "other"):
        cleaned = []
        for s in sk.get(key) or []:
            if _norm(s) in known:
                cleaned.append(s)
            else:
                warnings.append(f"skill removida (não está no perfil): {s}")
        sk[key] = cleaned
    resume["skills"] = sk

    companies = {(e.get("company") or "").strip().lower(): e for e in profile.get("experiences") or []}
    exps = []
    for e in resume.get("experiences") or []:
        c = (e.get("company") or "").strip().lower()
        src = companies.get(c)
        if not src:
            warnings.append(f"experiência removida (empresa não está no perfil): {e.get('company')}")
            continue
        # datas sempre do perfil
        e["start"], e["end"] = src.get("start", ""), src.get("end", "") or ""
        allowed_titles = {src.get("title", "").strip().lower(), (src.get("title_alt") or "").strip().lower(), (src.get("title_en") or "").strip().lower()}
        if (e.get("title") or "").strip().lower() not in allowed_titles:
            warnings.append(f"cargo ajustado em {e.get('company')}: '{e.get('title')}' -> '{src.get('title')}'")
            e["title"] = src.get("title")
        e["location"] = e.get("location") or src.get("location", "")
        # procedência: cada bullet precisa se parecer com algum bullet/stack da experiência no perfil
        corpus = [str(b) for b in (src.get("bullets") or []) + (src.get("bullets_ic") or [])] + [", ".join(map(str, src.get("stack") or []))]
        if adaptation.get("allowed") and _restack_allowed(src, adaptation):
            corpus.append(", ".join(map(str, adaptation["allowed"])))   # re-stack permitido: tecnologias da lista contam como base
        kept = []
        exp_tokens = set()
        for c in corpus:
            exp_tokens |= _tokens(c)
        for b in e.get("bullets") or []:
            cov = bullet_coverage(str(b), corpus)
            if cov < 0.5:
                warnings.append(f"bullet removido em {e.get('company')} (sem base no perfil, cobertura {cov:.0%}): {str(b)[:80]}")
                continue
            # tecnologia citada no bullet precisa constar naquela experiência (stack/bullets), não só no perfil geral
            corpus_text = " | ".join(corpus)
            foreign = [t for t in _tech_mentions(str(b), all_skills(profile)) if not _tech_mentions(corpus_text, {t})]
            if foreign:
                warnings.append(f"bullet removido em {e.get('company')} (cita {', '.join(foreign)} que não consta nessa experiência): {str(b)[:80]}")
                continue
            kept.append(b)
        e["bullets"] = kept[:5]
        if e["bullets"]:
            exps.append(e)
    exps.sort(key=lambda e: (e.get("start") or ""), reverse=True)   # ordem cronológica sempre
    resume["experiences"] = exps
    sk["main"] = sk["main"][:10]
    sk["other"] = sk["other"][:8]

    inst = {(x.get("institution") or "").strip().lower() for x in profile.get("education") or []}
    resume["education"] = [x for x in resume.get("education") or [] if (x.get("institution") or "").strip().lower() in inst]
    # certificações: sempre a seleção determinística do pool (por tags), nunca o que o modelo inventar
    titles = resume.get("section_titles") or {}
    if str(resume.get("language", "pt-BR")).startswith("pt"):
        titles["certifications"] = "Cursos e certificações"
    resume["section_titles"] = titles
    resume["certifications"] = select_certifications(profile, adaptation.get("job_stack") or [], adaptation.get("classification"),
                                                     when_none=adaptation.get("certifications_when_none", "generic"),
                                                     auto_enroll=bool(adaptation.get("auto_enroll")), max_in_progress=int(adaptation.get("max_in_progress") or 3))
    return warnings


def ensure_all_experiences(resume: dict, profile: dict, seniority: str) -> list[str]:
    """Pleno/sênior: se o modelo omitiu alguma empresa, acrescenta com os bullets do próprio perfil."""
    warnings: list[str] = []
    present = {(e.get("company") or "").strip().lower() for e in resume.get("experiences") or []}
    for src in profile.get("experiences") or []:
        c = (src.get("company") or "").strip().lower()
        if c in present:
            continue
        bullets = (src.get("bullets_ic") if seniority in ("junior", "mid") and src.get("bullets_ic") else src.get("bullets")) or []
        resume["experiences"].append({
            "company": src.get("company"), "title": src.get("title"), "start": src.get("start", ""), "end": src.get("end", "") or "",
            "location": src.get("location", ""), "bullets": list(bullets[:2]),
        })
        warnings.append(f"experiência acrescentada (modelo omitiu): {src.get('company')}")
    resume["experiences"].sort(key=lambda e: (e.get("start") or ""), reverse=True)
    return warnings


_GENERIC_COURSE_WORDS = {
    "para", "de", "do", "da", "e", "a", "o", "com", "em", "the", "and", "for", "to", "of", "curso", "completo", "completa", "zero",
    "ao", "basico", "avancado", "iniciantes", "iniciante", "guide", "complete", "developer", "developers", "master", "bootcamp", "hero",
    "aprenda", "programacao", "orientada", "objetos", "pratico", "pratica", "formacao", "projetos", "projeto", "essencial", "definitivo",
    "crie", "criando", "desenvolva", "desenvolvendo", "seu", "sua", "um", "uma", "na", "no", "from", "scratch", "ultimate", "full",
}


def _course_key(name: str) -> str:
    """Só as palavras 'de conteúdo' do nome (tecnologias, versões); ignora palavras genéricas de título de curso."""
    n = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    n = re.sub(r"[^a-z0-9#+ ]+", " ", n)
    return " ".join(sorted(w for w in n.split() if w not in _GENERIC_COURSE_WORDS and (len(w) > 1 or w in ("c", "r"))))


def is_same_course(a: str, b: str) -> bool:
    """Mesmo curso só se as palavras de conteúdo coincidem E o nome completo é quase idêntico."""
    ka, kb = _course_key(a), _course_key(b)
    if not ka or not kb or ka != kb:
        return False
    from rapidfuzz import fuzz
    na = unicodedata.normalize("NFKD", a).encode("ascii", "ignore").decode().lower()
    nb = unicodedata.normalize("NFKD", b).encode("ascii", "ignore").decode().lower()
    return fuzz.ratio(na, nb) >= 85


def dedupe_courses(items: list[dict]) -> list[dict]:
    out: list[dict] = []
    for c in items:
        if any(is_same_course(c.get("name", ""), o.get("name", "")) for o in out):
            continue
        out.append(c)
    return out


_GENERIC_TAGS = {"apisrest", "apis", "api", "web", "geral", "http", "poo", "design", "backend", "frontend", "fullstack", "mobile"}


def select_certifications(profile: dict, job_stack: list[str], classification: dict | None = None, max_n: int | None = None,
                          when_none: str = "generic", auto_enroll: bool = False, max_in_progress: int = 3) -> list[dict]:
    """Escolhe do pool os cursos relevantes: concluídos (done) e em andamento (in_progress; todo só com auto_enroll)."""
    # pedido do usuário: status não importa; todo curso do pool (config/courses.yaml + perfil) conta igual
    pool = dedupe_courses([dict(c, status="done") for c in profile.get("certifications") or []])
    max_n = max_n or int(profile.get("max_certifications") or 4)
    if not pool:
        return []
    wanted = {_norm(x) for x in job_stack}
    for x in (classification or {}).get("stack") or []:
        wanted.add(_norm(str(x)))
    fam = (classification or {}).get("role_family") or ""
    scored = []
    for c in pool:
        tags = {_norm(str(t)) for t in (c.get("tags") or [])}
        name_n = _norm(c.get("name") or "")
        score = 0
        specific_tags = {t for t in tags if t not in _GENERIC_TAGS}
        for w in wanted:
            if not w or w in _GENERIC_TAGS:
                continue
            if w in specific_tags or any(len(w) >= 4 and len(t) >= 4 and (w in t or t in w) for t in specific_tags):
                score += 3
            elif len(w) >= 3 and w in name_n:
                score += 2
        if fam and fam in tags:
            score += 1
        if "geral" in tags:
            score += 0.5
        scored.append((score, c))
    scored.sort(key=lambda x: (-x[0], str(x[1].get("year") or "")), reverse=False)
    relevant = [(sc, c) for sc, c in scored if sc >= 2]
    # mais relevantes primeiro; Udemy antes em empate; no máximo 2 por stack para variar
    relevant.sort(key=lambda x: (-x[0], 0 if "udemy" in str(x[1].get("issuer", "")).lower() else 1))
    picked, per_stack = [], {}
    for sc, c in relevant:
        st = c.get("_stack") or ""
        if per_stack.get(st, 0) >= 2:
            continue
        per_stack[st] = per_stack.get(st, 0) + 1
        picked.append(c)
        if len(picked) >= max_n:
            break
    picked = dedupe_courses(picked)
    if picked:
        return [_cert_out(c) for c in picked]
    if when_none == "hide":
        return []
    generic = [c for sc, c in scored if "geral" in {_norm(str(t)) for t in (c.get("tags") or [])} and c.get("status", "done") == "done"][:2]
    return [_cert_out(c) for c in generic]


def _cert_out(c: dict) -> dict:
    return {"name": c.get("name"), "issuer": c.get("issuer") or "", "year": str(c.get("year") or ""), "status": c.get("status") or "done"}


def seniority_bucket(classification: dict) -> str:
    s = (classification or {}).get("seniority") or "unspecified"
    return s if s in ("junior", "mid", "senior", "lead") else "mid"


def resume_language(cfg: ResumeCfg, classification: dict) -> str:
    if cfg.language != "auto":
        return cfg.language
    lang = (classification or {}).get("language") or "pt-BR"
    return "en" if str(lang).lower().startswith("en") else "pt-BR"


def build_resume(llm: LLM, cfg: ResumeCfg, profile: dict, job: Job, courses_cfg=None) -> tuple[dict, list[str]]:
    cls = job.classification or {}
    sen = seniority_bucket(cls)
    max_exp = {
        "junior": cfg.max_experiences_junior,
        "mid": cfg.max_experiences_mid,
        "senior": cfg.max_experiences_senior,
        "lead": cfg.max_experiences_senior,
    }[sen] if cfg.adapt_seniority else cfg.max_experiences_senior
    lang = resume_language(cfg, cls)
    scaled = scale_profile_for_seniority(profile, sen, cfg.seniority_years_factor) if cfg.adapt_seniority else profile
    ad = adaptation_context(profile, cls)
    ad["certifications_when_none"] = cfg.certifications_when_none
    if courses_cfg is not None:
        ad["auto_enroll"] = bool(getattr(courses_cfg, "auto_enroll", False))
        ad["max_in_progress"] = int(getattr(courses_cfg, "max_in_progress_on_resume", 3))
    job_payload = {"title": job.title, "company": job.company, "location": job.location, "description": job.description}
    # o pool de certificações (dezenas de cursos por stack) é 78% do prompt e NÃO é usado pelo modelo:
    # as certificações do currículo vêm de select_certifications(), determinístico. Não enviar para a IA.
    llm_profile = {k: v for k, v in scaled.items() if k != "certifications"}   # 'voice' fica: o prompt cita voice.notes
    extra = ""
    warnings: list[str] = []
    resume = llm.tailor_resume(
        profile=llm_profile, job=job_payload, classification=cls, language=lang,
        max_experiences=max_exp, seniority=sen if cfg.adapt_seniority else "unspecified",
        adaptation=ad, extra_instruction=extra,
    )
    resume["language"] = resume.get("language") or lang

    def _finish(r: dict) -> tuple[list[str], float, float]:
        w = validate_resume(r, profile, ad)
        if max_exp >= len(profile.get("experiences") or []):
            w += ensure_all_experiences(r, profile, sen)
        reorder_skills_for_stack(r, profile, ad)   # competências: ordem/seleção, sem IA
        b, sh = stack_share(r, ad["job_stack"])
        r.setdefault("_meta", {})["stack_share"] = {"bullets": round(b, 2), "skills": round(sh, 2), "job_stack": ad["job_stack"]}
        return w, b, sh

    warnings, b_share, s_share = _finish(resume)
    # Só os BULLETS justificam uma segunda chamada (competências já foram resolvidas acima).
    # E a segunda chamada é enxuta: manda só os bullets gerados, não o perfil inteiro.
    if ad["job_stack"] and ad["min_stack_share"] > 0 and b_share < ad["min_stack_share"]:
        warnings.append(f"adaptação abaixo da meta na 1ª tentativa: bullets {b_share:.0%}, skills {s_share:.0%}")
        try:
            fixed = llm.restack_bullets(
                resume=resume, job=job_payload, adaptation=ad, language=lang,
                blocked=[str(e.get("company") or "") for e in (profile.get("experiences") or []) if e.get("restack") is False],
            )
        except Exception as e:
            fixed, _ = None, warnings.append(f"re-stack dos bullets falhou: {str(e)[:80]}")
        if fixed:
            by_company = {_norm(str(x.get("company") or "")): x.get("bullets") or [] for x in fixed}
            for e in resume.get("experiences") or []:
                nb = by_company.get(_norm(str(e.get("company") or "")))
                if nb:
                    e["bullets"] = [str(b) for b in nb if str(b).strip()]
            w2, b2, s2 = _finish(resume)
            warnings += w2
            if b2 < ad["min_stack_share"]:
                warnings.append(f"adaptação ainda abaixo da meta: bullets {b2:.0%}, skills {s2:.0%}")
    resume["_meta"].update({
        "job_id": job.id,
        "seniority": sen,
        "job_hash": hashlib.sha1((job.description or "").encode()).hexdigest()[:10],
    })
    return resume, warnings


def resume_json_path(out_dir: Path, job: Job) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9]+", "-", f"{job.company or 'empresa'}-{job.title or 'vaga'}").strip("-")[:60]
    return out_dir / f"{safe}-{job.external_id}.json"
