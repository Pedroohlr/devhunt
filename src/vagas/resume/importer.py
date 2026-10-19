"""Importa o PDF do LinkedIn (Perfil -> Mais -> Salvar como PDF) e gera config/profile.yaml."""
from __future__ import annotations

from pathlib import Path

import yaml
from pypdf import PdfReader

from ..config import CONFIG_DIR
from ..llm import LLM


def pdf_text(path: Path) -> str:
    reader = PdfReader(str(path))
    return "\n".join((p.extract_text() or "") for p in reader.pages)


def import_linkedin(llm: LLM, pdf: Path, out: Path | None = None, overwrite: bool = False) -> Path:
    out = out or CONFIG_DIR / "profile.yaml"
    if out.exists() and not overwrite:
        raise FileExistsError(f"{out} já existe. Use --overwrite para substituir.")
    template = (CONFIG_DIR / "profile.example.yaml").read_text(encoding="utf-8")
    text = pdf_text(pdf) if pdf.suffix.lower() == ".pdf" else pdf.read_text(encoding="utf-8")
    data = llm.extract_profile(text, template)
    header = (
        "# Gerado a partir do LinkedIn. REVISE: confira datas, bullets, skills e preencha 'facts' e 'voice'.\n"
        "# Dica: bullets ricos (com números) e 'skill_years' precisos = currículos melhores.\n"
    )
    out.write_text(header + yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=110), encoding="utf-8")
    return out
