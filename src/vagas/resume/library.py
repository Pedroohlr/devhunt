"""Biblioteca de currículos já gerados: reaproveitar em vez de pedir outro para a IA.

A ideia é simples e vai ficando melhor com o uso: todo currículo que a IA escreve entra num
índice por (nível, tecnologias citadas nos bullets). Quando chega uma vaga nova, procuramos um
currículo cujos bullets JÁ citem a stack pedida na proporção exigida; o que falta para ele servir
a esta vaga é determinístico — reordenar as competências e ajustar o headline para o cargo.
Depois de alguns dias as stacks se repetem (Node, React, Java, PHP, .NET, Python) e a maioria das
vagas é atendida sem nenhuma chamada de IA.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .profile import all_skills
from .tailor import _norm, adaptation_context, seniority_bucket, stack_share, reorder_skills_for_stack

# ruído do título da vaga que não entra no headline
TITLE_NOISE_RE = re.compile(
    r"\(.*?\)|\[.*?\]|\b(vaga|exclusiv[ao]|afirmativ[ao]|pcd|banco de talentos|remoto|remote|home ?office|"
    r"h[ií]brido|h[ií]brida|presencial|urgente|efetivo|clt|pj|s[êe]nior|senior|pleno|j[úu]nior|junior|jr|sr|pl|"
    r"mid[\s-]?level|entry[\s-]?level|mid|level|i{1,3}|1|2|3|iv|brazil|brasil)\b|[–—|/]+\s*$",
    re.I,
)


def headline_for_job(job_title: str, profile: dict, seniority: str) -> str | None:
    """Headline que espelha o cargo da vaga, sem IA e sem citar tecnologia fora do perfil.

    Só devolve algo quando o título limpo é reconhecível como papel de desenvolvimento e toda
    tecnologia citada existe no perfil; senão devolve None e fica o headline que já estava.
    """
    t = TITLE_NOISE_RE.sub(" ", job_title or "")
    t = re.sub(r"\s{2,}", " ", t).strip(" -–—|,·")
    if not t or len(t) > 60:
        return None
    known = {_norm(s) for s in all_skills(profile)}
    # tira primeiro as tecnologias compostas do perfil ("React Native", "Node.js") para não
    # sobrar a metade da palavra ("Native") sendo cobrada como desconhecida
    rest = t
    for name in sorted(all_skills(profile), key=len, reverse=True):
        if len(name) < 3:
            continue
        rest = re.sub(rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])", " ", rest, flags=re.I)
    # toda palavra "técnica" que sobrou precisa ser vocabulário de cargo
    for w in re.findall(r"[A-Za-zÀ-ú0-9\.\+#]{2,}", rest):
        nw = _norm(w)
        if nw in known:
            continue
        if re.fullmatch(r"(desenvolvedor|desenvolvedora|engenheiro|engenheira|programador|programadora|analista|"
                        r"arquiteto|arquiteta|especialista|tech|lead|l[ií]der|t[ée]cnico|de|da|do|e|em|para|com|"
                        r"software|sistemas|full|stack|fullstack|back|front|end|backend|frontend|mobile|web|dados|"
                        r"developer|engineer|de[sv]ops|sre|cloud|data)", nw):
            continue
        return None   # palavra desconhecida (produto, sigla da empresa): não arriscar
    suffix = {"senior": " Sênior", "lead": " Sênior", "mid": " Pleno", "junior": " Júnior"}.get(seniority, "")
    if re.search(r"\b(lead|l[ií]der|especialista|arquiteto|arquiteta|principal|staff|head|coordenador)\b", t, re.I):
        suffix = ""      # "Tech Lead Sênior" fica redundante
    return (t[:1].upper() + t[1:] + suffix).strip()


def _load(path: str | Path) -> dict | None:
    jp = Path(path).with_suffix(".json")
    if not jp.exists():
        return None
    try:
        return json.loads(jp.read_text(encoding="utf-8"))
    except Exception:
        return None


def candidates(db, job_id: str, limit: int = 400) -> list[tuple[str, dict]]:
    """Currículos já gerados, do mais novo para o mais antigo (não só os de vagas enviadas)."""
    rows = db.conn.execute(
        "SELECT resume_path, classification FROM jobs WHERE id != ? AND resume_path IS NOT NULL "
        "ORDER BY COALESCE(applied_at, updated_at) DESC LIMIT ?",
        (job_id, limit),
    ).fetchall()
    out = []
    for r in rows:
        try:
            cls = json.loads(r[1] or "{}")
        except Exception:
            cls = {}
        out.append((r[0], cls))
    return out


def best_match(db, profile: dict, job, need_override: float | None = None) -> tuple[dict | None, dict]:
    """Melhor currículo da biblioteca para esta vaga, já adaptado deterministicamente.

    Devolve (resume, info). O corte é só pela fração de BULLETS citando a stack da vaga: as
    competências são reordenadas aqui mesmo, sem IA, então não fazem parte do critério.
    """
    cls = job.classification or {}
    ad = adaptation_context(profile, cls)
    need = float(need_override if need_override is not None else (ad.get("min_stack_share") or 0))
    job_stack = ad.get("job_stack") or []
    sen = seniority_bucket(cls)
    if not job_stack:
        # vaga sem stack reconhecida (descrição genérica): não há o que adaptar, serve o currículo
        # mais recente do mesmo nível — continua sendo só fato do perfil.
        for path, ccls in candidates(db, job.id):
            if seniority_bucket(ccls) != sen:
                continue
            data = _load(path)
            if data is not None:
                adapt_for_job(data, profile, job, ad)
                data.setdefault("_meta", {})["reused_from"] = Path(path).stem
                return data, {"de": Path(path).stem, "motivo": "vaga sem stack: currículo do mesmo nível"}
        return None, {"motivo": "vaga sem stack e biblioteca vazia"}
    best, best_share, best_path = None, -1.0, ""
    for path, ccls in candidates(db, job.id):
        if seniority_bucket(ccls) != sen:
            continue
        data = _load(path)
        if data is None:
            continue
        b, _sk = stack_share(data, job_stack)
        if b > best_share:
            best, best_share, best_path = data, b, path
        if b >= need and need > 0:
            break          # já serve: não precisa varrer o resto
    if best is None:
        return None, {"motivo": "biblioteca vazia para este nível"}
    info = {"de": Path(best_path).stem, "bullets": round(best_share, 2), "meta": need}
    if need > 0 and best_share < need:
        return None, {**info, "motivo": "nenhum currículo cobre a stack desta vaga"}
    adapt_for_job(best, profile, job, ad)
    best.setdefault("_meta", {}).update({"reused_from": Path(best_path).stem, "reuse_share": info})
    return best, info


def adapt_for_job(resume: dict, profile: dict, job, adaptation: dict | None = None) -> None:
    """Ajustes determinísticos que deixam um currículo reaproveitado com cara de feito para a vaga."""
    ad = adaptation or adaptation_context(profile, job.classification or {})
    reorder_skills_for_stack(resume, profile, ad)
    hl = headline_for_job(job.title or "", profile, seniority_bucket(job.classification or {}))
    if hl:
        resume["headline"] = hl
