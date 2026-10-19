from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..browser import Browser, Page
from ..config import Settings
from ..db import DB, Job
from ..forms import FlowResult
from ..qa import QAEngine


@dataclass
class ApplyContext:
    settings: Settings
    browser: Browser
    db: DB
    qa: QAEngine
    profile: dict
    # Currículo PREGUIÇOSO: função que gera o PDF. Só é chamada quando o formulário pede o
    # arquivo, então vaga que falha antes disso (login, botão sumido, anúncio expirado) não
    # gasta uma chamada de gpt-4.1. Use `ctx.resume` ao passar adiante (forms resolve sozinho).
    resume: Callable[[], Path | None] | Path | None = None
    dry_run: bool = False
    interactive: bool = False
    pause_before_submit: bool = False
    confirm: Callable[[str], bool] | None = None   # usado com pause_before_submit
    _resume_cache: Path | None = field(default=None, init=False, repr=False)
    _resume_done: bool = field(default=False, init=False, repr=False)

    @property
    def resume_path(self) -> Path | None:
        """Gera (uma vez) e devolve o PDF. Quem só quer saber se HÁ currículo usa `has_resume`."""
        if not self._resume_done:
            self._resume_done = True
            self._resume_cache = self.resume() if callable(self.resume) else self.resume
        return self._resume_cache

    @property
    def has_resume(self) -> bool:
        """True se existe um currículo a enviar — sem disparar a geração."""
        return self.resume is not None


class Applier:
    name = "generic"

    def apply(self, ctx: ApplyContext, page: Page, job: Job) -> FlowResult:
        raise NotImplementedError
