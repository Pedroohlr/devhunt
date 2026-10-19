"""Indeed: 'Candidatura simplificada' (smartapply) e redirecionamento externo."""
from __future__ import annotations

import re
from urllib.parse import urlparse

from rich.console import Console

from .. import forms, humanize as h
from ..browser import Page
from ..db import Job
from ..forms import FlowResult
from .base import Applier, ApplyContext
from .flow import run_form_flow
from .registry import detect_ats, get_applier

console = Console()

APPLY_SELS = ["#indeedApplyButton", ".ia-IndeedApplyButton", '[data-testid*="indeedApply"]', "button[aria-label*='Candidatar-se agora']", "button[aria-label*='Apply now']"]
EXTERNAL_SELS = ["#applyButtonLinkContainer a", '[data-testid="applyButtonLinkContainer"] a', 'a[href*="/applystart"]', "a:has-text('Candidatar-se no site da empresa')", "a:has-text('Apply on company site')"]
UPLOAD_OPTION_RE = re.compile(r"carregar( um| seu| novo)? curr|enviar( um| seu)? curr|upload( a| your)?( new)? resume|fazer upload", re.I)


def _first_visible(page: Page, sels: list[str]):
    for sel in sels:
        try:
            loc = page.locator(sel)
            for i in range(min(loc.count(), 3)):
                b = loc.nth(i)
                if b.is_visible():
                    return b
        except Exception:
            continue
    return None


class IndeedApplyApplier(Applier):
    name = "indeed_apply"

    def apply(self, ctx: ApplyContext, page: Page, job: Job) -> FlowResult:
        if job.external_id not in page.url:
            ctx.browser.goto(page, job.url)
            h.medium()
        btn = _first_visible(page, APPLY_SELS)
        if btn is None:
            return FlowResult("failed", "botão Indeed Apply não encontrado")
        target = page
        try:
            with page.context.expect_page(timeout=8000) as np:
                h.click(btn)
            target = np.value
            target.wait_for_load_state("domcontentloaded", timeout=45000)
        except Exception:
            h.medium()
        h.medium()
        if not ctx.browser.ensure_ok(target):
            return FlowResult("failed", "checkpoint no Indeed não resolvido")

        def pre_step(p: Page, step: int) -> None:
            # prefere enviar o currículo adaptado em vez do currículo salvo no Indeed
            b = forms.find_button(p, UPLOAD_OPTION_RE)
            if b is not None:
                try:
                    if b.is_visible():
                        h.click(b)
                        h.short()
                except Exception:
                    pass

        res = run_form_flow(ctx, target, job, ats="indeed", pre_step=pre_step)
        if res.status == "applied":
            forms.screenshot_evidence(target, job.id)
        if target is not page:
            try:
                target.close()
            except Exception:
                pass
        return res


class IndeedExternalApplier(Applier):
    name = "indeed_external"

    def apply(self, ctx: ApplyContext, page: Page, job: Job) -> FlowResult:
        if job.external_id not in page.url:
            ctx.browser.goto(page, job.url)
            h.medium()
        # anúncio removido ("Não encontrada | Indeed"): não é falha do robô
        try:
            if forms.CLOSED_RE.search(page.title() or "") or forms.CLOSED_RE.search(forms.page_text(page)[:1500]) or re.search(r"n[aã]o encontrada|n[aã]o foi poss[ií]vel encontrar|this job has expired|job not found", (page.title() or "") + " " + forms.page_text(page)[:800], re.I):
                return FlowResult("skipped", "anúncio não encontrado/expirado no Indeed")
        except Exception:
            pass
        a = _first_visible(page, EXTERNAL_SELS)
        newp: Page | None = None
        if a is None:
            # botão "Candidatar-se no site da empresa" não achado (layout novo/scroll): a descoberta guardou o link
            # "applystart" do Indeed, que redireciona para o site da empresa
            ext = job.external_url or ""
            if "indeed.com/applystart" in ext or "indeed.com/rc/clk" in ext:
                try:
                    page.goto(ext, wait_until="domcontentloaded", timeout=45000)
                    h.medium()
                    if "indeed.com" not in (page.url or ""):
                        newp = page
                except Exception:
                    pass
            if newp is None:
                forms.dump_learn(page, job.id, "indeed", [], "link para site da empresa não encontrado", 0)
                return FlowResult("failed", "link para site da empresa não encontrado")
        else:
            try:
                with page.context.expect_page(timeout=15000) as np:
                    h.click(a)
                newp = np.value
                newp.wait_for_load_state("domcontentloaded", timeout=45000)
            except Exception:
                h.medium()
                if "indeed.com" not in page.url:
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
