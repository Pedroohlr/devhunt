"""LinkedIn: Easy Apply (modal) e redirecionamento para site externo."""
from __future__ import annotations

import re
import time
from urllib.parse import parse_qs, unquote, urlparse

from rich.console import Console

from .. import forms, humanize as h
from ..browser import Page
from ..db import Job
from ..forms import FlowResult
from ..sources.linkedin import LinkedInSource
from .base import Applier, ApplyContext
from .flow import run_form_flow
from .registry import detect_ats, get_applier

console = Console()

MODAL_SELS = [".jobs-easy-apply-modal", "div[data-test-modal][role='dialog']", "div[role='dialog']"]
CONTINUE_RE = re.compile(r"continuar candidatura|continue applying|continuar", re.I)


def _button_kind(btn) -> str:
    """'easy_apply' ou 'external' conforme o texto/aria do botão encontrado na página."""
    try:
        label = (btn.inner_text(timeout=2000) or "") + " " + (btn.get_attribute("aria-label") or "")
    except Exception:
        label = ""
    return "easy_apply" if re.search(r"simplificada|easy apply", label, re.I) else "external"



def _no_apply_button(ctx: ApplyContext, page: Page, job: Job) -> FlowResult:
    """Sem botão de candidatura na página da vaga: já aplicada? encerrada? senão recarrega uma vez e registra dump."""
    text = forms.page_text(page)
    if forms.ALREADY_RE.search(text):
        return FlowResult("already_applied", "já aplicada anteriormente (página da vaga)")
    if forms.CLOSED_RE.search(text):
        return FlowResult("skipped", "vaga encerrada (não aceita mais candidaturas)")
    try:
        page.reload(wait_until="domcontentloaded")
        h.medium()
        btn = LinkedInSource.apply_button(page)
        if btn is not None:
            return FlowResult("retry", "botão apareceu após recarregar")
    except Exception:
        pass
    text = forms.page_text(page)
    if forms.CLOSED_RE.search(text):
        return FlowResult("skipped", "vaga encerrada (não aceita mais candidaturas)")
    forms.dump_learn(page, job.id, "linkedin", [], "botão de candidatura não encontrado", 0)
    return FlowResult("failed", "botão de candidatura não encontrado")


class LinkedInEasyApplyApplier(Applier):
    name = "linkedin_easy_apply"

    @staticmethod
    def _modal_selector(page: Page) -> str | None:
        for sel in MODAL_SELS:
            try:
                loc = page.locator(sel)
                for i in range(min(loc.count(), 3)):
                    if loc.nth(i).is_visible():
                        return sel
            except Exception:
                continue
        return None

    @staticmethod
    def _continue_applying(page: Page) -> None:
        """Dialog 'Lembrete de segurança na busca de vagas' -> 'Continuar candidatura'."""
        try:
            for sel in ["a:has-text('Continuar candidatura')", "button:has-text('Continuar candidatura')", "a:has-text('Continue applying')", "button:has-text('Continue applying')"]:
                b = page.locator(sel).first
                if b.count() and b.is_visible():
                    h.click(b)
                    h.short()
                    return
        except Exception:
            pass

    @staticmethod
    def _unfollow(page: Page) -> None:
        try:
            cb = page.locator("#follow-company-checkbox")
            if cb.count() and cb.is_checked():
                page.locator("label[for='follow-company-checkbox']").first.click(timeout=2000)
                h.tiny()
        except Exception:
            pass

    @staticmethod
    def _close_success(page: Page) -> None:
        for sel in ["button[aria-label='Fechar']", "button[aria-label='Dismiss']", "button:has-text('Concluído')", "button:has-text('Done')", "button[aria-label='Fechar caixa de diálogo']"]:
            try:
                b = page.locator(sel).first
                if b.count() and b.is_visible():
                    h.click(b)
                    h.short()
                    return
            except Exception:
                continue

    @staticmethod
    def _discard(page: Page) -> None:
        try:
            for sel in ["button[aria-label='Fechar']", "button[aria-label='Dismiss']", ".artdeco-modal__dismiss"]:
                b = page.locator(sel).first
                if b.count() and b.is_visible():
                    h.click(b)
                    h.short()
                    break
            for sel in ["button[data-control-name='discard_application_confirm_btn']", "button:has-text('Descartar')", "button:has-text('Discard')"]:
                b = page.locator(sel).first
                if b.count() and b.is_visible():
                    h.click(b)
                    h.short()
                    break
        except Exception:
            pass

    def apply(self, ctx: ApplyContext, page: Page, job: Job) -> FlowResult:
        if job.url.rstrip("/") not in page.url:
            ctx.browser.goto(page, job.url)
            h.medium()
        btn = LinkedInSource.apply_button(page)
        if btn is None:
            res = _no_apply_button(ctx, page, job)
            if res.status != "retry":
                return res
            btn = LinkedInSource.apply_button(page)
            if btn is None:
                return FlowResult("failed", "botão de candidatura não encontrado")
        if _button_kind(btn) == "external":
            console.print("[dim]  a vaga virou candidatura externa; redirecionando para o fluxo externo[/]")
            job.apply_type = "external"
            ctx.db.save_job(job)
            return LinkedInExternalApplier().apply(ctx, page, job)
        h.click(btn)
        h.medium()
        self._continue_applying(page)
        root = self._modal_selector(page)
        if not root:
            h.medium()
            root = self._modal_selector(page)
        if not root:
            # o clique pode ter sido engolido (página ainda carregando, botão trocado no re-render): acha de novo e reclica
            btn2 = LinkedInSource.apply_button(page)
            if btn2 is not None and _button_kind(btn2) != "external":
                try:
                    h.click(btn2)
                except Exception:
                    pass
                deadline = time.time() + 10
                while time.time() < deadline and not root:
                    time.sleep(1.0)
                    self._continue_applying(page)
                    root = self._modal_selector(page)
        if not root:
            body = forms.page_text(page)
            if forms.LIMIT_RE.search(body):
                return FlowResult("limit", "limite diário do Easy Apply")
            if forms.ALREADY_RE.search(body):
                return FlowResult("already_applied", "já aplicada anteriormente", 0, [])
            forms.dump_learn(page, job.id, "linkedin", [], "modal do Easy Apply não abriu", 0)
            return FlowResult("failed", "modal do Easy Apply não abriu")

        def pre_step(p: Page, step: int) -> None:
            self._continue_applying(p)
            self._unfollow(p)

        res = run_form_flow(ctx, page, job, ats="linkedin", root_selector=root, pre_step=pre_step)
        if res.status == "applied":
            forms.screenshot_evidence(page, job.id)
            self._close_success(page)
        elif res.status == "dry_run":
            self._discard(page)
        else:
            self._discard(page)
        return res


class LinkedInExternalApplier(Applier):
    name = "linkedin_external"

    @staticmethod
    def _find_external_link(page: Page) -> str | None:
        """Procura na página o link de candidatura externa (redirecionador do LinkedIn)."""
        for sel in ["a[href*='linkedin.com/safety/go']", "a[href*='/redir/']", "a[target='_blank'][href*='url=']"]:
            try:
                loc = page.locator(sel)
                for i in range(min(loc.count(), 5)):
                    a = loc.nth(i)
                    href = a.get_attribute("href") or ""
                    txt = (a.inner_text(timeout=500) or "") + " " + (a.get_attribute("aria-label") or "")
                    if re.search(r"candidat|apply", txt, re.I) or "safety/go" in href:
                        url = LinkedInExternalApplier._external_url(a)
                        if url:
                            return url
            except Exception:
                continue
        return None

    @staticmethod
    def _external_url(btn) -> str | None:
        try:
            href = btn.get_attribute("href") or ""
        except Exception:
            return None
        if not href:
            return None
        if "linkedin.com/safety/go" in href or "/redir/" in href:
            q = parse_qs(urlparse(href).query)
            target = (q.get("url") or [""])[0]
            return unquote(target) if target else None
        if href.startswith("http") and "linkedin.com" not in href:
            return href
        return None

    def apply(self, ctx: ApplyContext, page: Page, job: Job) -> FlowResult:
        if job.url.rstrip("/") not in page.url:
            ctx.browser.goto(page, job.url)
            h.medium()
        btn = LinkedInSource.apply_button(page)
        if btn is None:
            res = _no_apply_button(ctx, page, job)
            if res.status != "retry":
                return res
            btn = LinkedInSource.apply_button(page)
            if btn is None:
                return FlowResult("failed", "botão de candidatura não encontrado")
        if _button_kind(btn) == "easy_apply":
            console.print("[dim]  a vaga está em Easy Apply; redirecionando para o modal[/]")
            job.apply_type = "easy_apply"
            ctx.db.save_job(job)
            return LinkedInEasyApplyApplier().apply(ctx, page, job)
        newp: Page | None = None
        ext = self._external_url(btn) or self._find_external_link(page)
        if not ext:
            try:
                console.print(f"[dim]  botão externo sem href: tag={btn.evaluate('e => e.tagName')} aria={btn.get_attribute('aria-label')!r}[/]")
            except Exception:
                pass
        if ext:
            # abre direto o site da empresa (o link do LinkedIn é um redirecionador "safety/go?url=...")
            newp = page.context.new_page()
            try:
                newp.goto(ext, wait_until="domcontentloaded", timeout=45000)
            except Exception as e:
                console.print(f"[yellow]  abrir {ext[:80]}: {str(e)[:80]}[/]")
        else:
            try:
                with page.context.expect_page(timeout=15000) as np:
                    h.click(btn)
                    h.short()
                    LinkedInEasyApplyApplier._continue_applying(page)
                newp = np.value
                newp.wait_for_load_state("domcontentloaded", timeout=45000)
            except Exception:
                h.medium()
                if "linkedin.com/jobs" not in page.url:
                    newp = page
        if newp is None:
            return FlowResult("failed", "site externo não abriu")
        h.medium()
        job.external_url = newp.url
        host = urlparse(newp.url).hostname or ""
        job.ats = detect_ats(newp.url, ctx.settings.ats.known_hosts)
        console.print(f"[dim]  externo: {job.ats} · {newp.url[:100]}[/]")
        if any(host == s or host.endswith("." + s) for s in ctx.settings.ats.skip_hosts):
            res = FlowResult("failed", f"host ignorado por configuração: {host}")
        else:
            res = get_applier(job.ats).apply(ctx, newp, job)
        if newp is not page:
            try:
                newp.close()
            except Exception:
                pass
        return res


class LinkedInAutoApplier(Applier):
    """Tipo de candidatura desconhecido (descoberta sem conta): abre a vaga logado, descobre e despacha."""
    name = "linkedin_auto"

    def apply(self, ctx: ApplyContext, page: Page, job: Job) -> FlowResult:
        src = LinkedInSource(mode="session")
        src.fetch_details_session(ctx.browser, page, job)
        ctx.db.save_job(job)
        if job.apply_type == "closed":
            return FlowResult("failed", "vaga encerrada")
        if job.apply_type == "already_applied":
            return FlowResult("already_applied", "já aplicada anteriormente")
        if job.apply_type == "easy_apply":
            return LinkedInEasyApplyApplier().apply(ctx, page, job)
        if job.apply_type == "external":
            return LinkedInExternalApplier().apply(ctx, page, job)
        return FlowResult("failed", "não identifiquei o botão de candidatura")
