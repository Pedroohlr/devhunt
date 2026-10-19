from __future__ import annotations

import copy
import math
from pathlib import Path
from typing import Any

import yaml


def load_profile(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(
            f"Perfil não encontrado em {path}. Copie config/profile.example.yaml para config/profile.yaml "
            f"ou rode `vagas profile import <linkedin.pdf>`."
        )
    profile = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    # cursos marcados como concluídos em config/courses.yaml entram no pool de certificações
    courses_path = path.parent / "courses.yaml"
    if courses_path.exists():
        data = yaml.safe_load(courses_path.read_text(encoding="utf-8")) or {}
        from .tailor import is_same_course
        pool = list(profile.get("certifications") or [])
        for stack, items in (data.get("stacks") or {}).items():
            for c in items or []:
                status = c.get("status") or ("done" if c.get("done") else "todo")
                dup = next((p for p in pool if is_same_course(p.get("name", ""), c.get("name", ""))), None)
                if dup is not None:
                    # mesmo curso em outra stack: só acrescenta a tag e promove o status se estiver mais avançado
                    dup.setdefault("tags", []); dup["tags"] = list(dict.fromkeys(list(dup["tags"]) + [stack]))
                    order = {"todo": 0, "in_progress": 1, "done": 2}
                    if order.get(status, 0) > order.get(dup.get("status", "done"), 2):
                        dup["status"], dup["year"] = status, str(c.get("year") or dup.get("year") or "")
                    continue
                pool.append({"name": c["name"], "issuer": c.get("issuer") or "", "year": str(c.get("year") or ""),
                             "tags": list(dict.fromkeys([stack] + list(c.get("tags") or []))), "status": status, "_stack": stack})
        profile["certifications"] = pool
    return profile


def all_skills(profile: dict) -> set[str]:
    s: set[str] = set()
    sk = profile.get("skills") or {}
    for k in ("primary", "secondary", "also_experienced", "familiar"):
        for x in sk.get(k) or []:
            s.add(str(x).strip().lower())
    for e in profile.get("experiences") or []:
        for x in e.get("stack") or []:
            s.add(str(x).strip().lower())
    return s


def candidate_summary(profile: dict) -> str:
    """Resumo curto do candidato para o classificador (barato em tokens)."""
    p = profile.get("personal") or {}
    sk = profile.get("skills") or {}
    langs = ", ".join(f"{l.get('name')} ({l.get('level')})" for l in profile.get("languages") or [])
    facts = profile.get("facts") or {}
    return (
        f"{profile.get('headline','')}; {profile.get('years_experience_total','?')} anos de experiência; "
        f"mora em {p.get('city','')}/{p.get('state','')} ({p.get('country','')}); "
        f"aceita: {facts.get('work_models_accepted','')}; mudança de cidade: {facts.get('willing_to_relocate','?')}; "
        f"idiomas: {langs}; stack principal: {', '.join(sk.get('primary') or [])}; "
        f"secundária: {', '.join(sk.get('secondary') or [])}; também tem experiência com: {', '.join(sk.get('also_experienced') or [])}; "
        f"conhece: {', '.join(sk.get('familiar') or [])}."
    )


def profile_for_forms(profile: dict) -> dict:
    """Versão compacta do perfil para responder formulários."""
    keep = {k: profile.get(k) for k in ("personal", "headline", "summary", "years_experience_total", "skills",
                                         "skill_years", "skill_years_default", "education", "languages", "facts")}
    # `certifications` vem de load_profile com o POOL inteiro de cursos sugeridos (dezenas por stack):
    # ~45 mil caracteres repetidos em toda chamada de resposta. Formulário só precisa dos nomes.
    certs = profile.get("certifications") or []
    keep["certifications"] = [str(c.get("name") if isinstance(c, dict) else c)[:80] for c in certs][:12]
    keep["experiences"] = [
        {k: e.get(k) for k in ("company", "title", "start", "end", "employment_type", "stack")}
        for e in profile.get("experiences") or []
    ]
    return keep


def scale_profile_for_seniority(profile: dict, seniority: str | None, factors: dict[str, float]) -> dict:
    """Cópia do perfil com skill_years completo (default para also_experienced) e anos reduzidos para o nível da vaga."""
    f = float(factors.get(seniority or "unspecified", factors.get("unspecified", 1.0)))
    p = copy.deepcopy(profile)
    sy = dict(p.get("skill_years") or {})
    default = p.get("skill_years_default")
    if default:
        sk = p.get("skills") or {}
        for k in ("primary", "secondary", "also_experienced"):
            for name in sk.get(k) or []:
                sy.setdefault(str(name), default)
    p["skill_years"] = sy
    if f >= 1.0:
        return p
    p["skill_years"] = {k: max(1, int(math.ceil(float(v) * f))) if isinstance(v, (int, float)) else v for k, v in sy.items()}
    if default:
        p["skill_years_default"] = max(1, int(math.ceil(float(default) * f)))
    try:
        p["years_experience_total"] = max(1, int(math.ceil(float(p.get("years_experience_total") or 0) * f)))
    except Exception:
        pass
    p["_seniority_note"] = f"anos de experiência ajustados para vaga {seniority} (fator {f})"
    return p
