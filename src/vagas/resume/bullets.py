"""Banco de bullets aprendidos: o texto que a IA já escreveu (e que já passou pela validação)
vira matéria-prima para montar currículos novos sem gastar IA de novo.

Cada bullet de currículo gerado é guardado com a empresa a que pertence e as tecnologias que
cita. Quando chega uma vaga, dá para montar as experiências escolhendo, entre as variantes já
aprovadas daquela mesma empresa, as que citam a stack pedida. Nada é inventado: todo texto aqui
saiu de um currículo que passou por `validate_resume`, que só deixa passar fato do perfil.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from rapidfuzz import fuzz

from .profile import all_skills
from .tailor import _norm, _tech_mentions

BANK_NAME = "bullets.json"


def _key(company: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (company or "").lower())


class BulletBank:
    """{empresa: [{"text": ..., "tech": [...], "n": vezes_vista}]}"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.data: dict[str, list[dict]] = {}
        self.load()

    def load(self) -> None:
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                self.data = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")

    # ---------- aprender ----------
    def harvest(self, resume: dict, profile: dict) -> int:
        """Guarda os bullets de um currículo recém-gerado. Devolve quantos são novos."""
        skills = set(all_skills(profile))
        novos = 0
        for e in resume.get("experiences") or []:
            k = _key(str(e.get("company") or ""))
            if not k:
                continue
            slot = self.data.setdefault(k, [])
            for b in e.get("bullets") or []:
                text = str(b).strip()
                if len(text) < 20:
                    continue
                dup = next((it for it in slot if fuzz.ratio(it["text"], text) >= 92), None)
                if dup:
                    dup["n"] = int(dup.get("n", 1)) + 1
                    continue
                slot.append({"text": text, "tech": sorted(set(_tech_mentions(text, skills))), "n": 1})
                novos += 1
        return novos

    # ---------- usar ----------
    def variants_for(self, company: str, job_stack: list[str]) -> tuple[list[dict], list[dict]]:
        """(variantes que citam a stack da vaga, demais variantes) daquela empresa."""
        js = {_norm(x) for x in job_stack}
        slot = list(self.data.get(_key(company)) or [])
        hit = [it for it in slot if any(_norm(t) in js for t in it.get("tech") or [])]
        rest = [it for it in slot if it not in hit]
        hit.sort(key=lambda it: (-int(it.get("n", 1)), len(it["text"])))
        rest.sort(key=lambda it: (-int(it.get("n", 1)), len(it["text"])))
        return hit, rest

    def coverage(self, profile: dict, job_stack: list[str]) -> float:
        """Fração das experiências do perfil que já têm ao menos um bullet citando a stack da vaga."""
        exps = profile.get("experiences") or []
        if not exps or not job_stack:
            return 0.0
        ok = sum(1 for e in exps if self.variants_for(str(e.get("company") or ""), job_stack)[0])
        return ok / len(exps)


def compose(bank: BulletBank, profile: dict, job_stack: list[str], base: dict,
            min_share: float, max_bullets: int = 4) -> tuple[dict | None, float]:
    """Monta um currículo trocando os bullets de `base` por variantes já aprendidas.

    `base` é um currículo pronto (o mais parecido da biblioteca): dele vêm resumo, formação e
    idiomas. Só os bullets são recompostos. Devolve (currículo, fração de bullets na stack) ou
    (None, share) se nem assim a meta for atingida.
    """
    import copy

    out = copy.deepcopy(base)
    total = hit = 0
    for e in out.get("experiences") or []:
        comp = str(e.get("company") or "")
        with_stack, others = bank.variants_for(comp, job_stack)
        atual = [str(b) for b in (e.get("bullets") or [])]
        n = min(max(len(atual), 2), max_bullets)
        escolhidos: list[str] = []
        for it in with_stack:
            if len(escolhidos) >= n:
                break
            if not any(fuzz.ratio(it["text"], x) >= 92 for x in escolhidos):
                escolhidos.append(it["text"])
        faltam = n - len(escolhidos)
        if faltam > 0:
            pool = atual + [it["text"] for it in others]
            for t in pool:
                if faltam <= 0:
                    break
                if not any(fuzz.ratio(t, x) >= 92 for x in escolhidos):
                    escolhidos.append(t)
                    faltam -= 1
        if escolhidos:
            e["bullets"] = escolhidos
        total += len(e.get("bullets") or [])
        hit += len(with_stack[:n])
    share = (hit / total) if total else 0.0
    return (out if share >= min_share else None), share
