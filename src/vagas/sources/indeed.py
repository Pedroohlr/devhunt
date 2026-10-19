"""Descoberta e detalhes de vagas no Indeed (br.indeed.com)."""
from __future__ import annotations

import re
from urllib.parse import quote_plus, urljoin

from rich.console import Console

from .. import humanize as h
from ..browser import Browser, Page
from ..config import Search
from ..db import Job
from .base import Found, Source, clean, first_text, parse_posted

console = Console()

CARDS_JS = """
() => Array.from(document.querySelectorAll('div.job_seen_beacon, [data-testid="slider_item"], li:has(a[data-jk])')).map(card => {
  const a = card.querySelector('a[data-jk], h2.jobTitle a, a.jcs-JobTitle');
  const jk = a ? (a.getAttribute('data-jk') || (a.href.match(/jk=([a-z0-9]+)/i) || [])[1]) : '';
  const t = (sel) => { const e = card.querySelector(sel); return e ? (e.innerText || e.textContent || '').trim() : ''; };
  const title = (a && (a.querySelector('span[title]') ? a.querySelector('span[title]').getAttribute('title') : a.innerText)) || t('h2.jobTitle');
  return {
    jk, title: (title || '').trim(),
    company: t('[data-testid="company-name"], .companyName, .css-1h7lukg'),
    location: t('[data-testid="text-location"], .companyLocation'),
    posted: t('[data-testid="myJobsStateDate"], .date'),
    easy: /candidatura simplificada|easily apply|easy apply/i.test(card.innerText || ''),
    applied: /candidatou-se|applied|candidatura enviada/i.test(t('[data-testid="myJobsStateDate"], .css-1v1qjtb') || ''),
    href: a ? a.href : ''
  };
}).filter(c => c.jk)
"""


class IndeedSource(Source):
    name = "indeed"

    needs_browser = True

    def search_url(self, s: Search, start: int, remote_style: str = "attr") -> str:
        days = max(1, s.seconds // 86400)
        if s.remote_only and remote_style == "location":
            url = f"https://br.indeed.com/jobs?q={quote_plus(s.query)}&l={quote_plus('Remoto')}&fromage={days}&sort=date"
        else:
            url = f"https://br.indeed.com/jobs?q={quote_plus(s.query)}&l={quote_plus(s.location)}&fromage={days}&sort=date"
            if s.remote_only:
                url += "&sc=0kf%3Aattr%28DSQF7%29%3B"   # atributo "Remoto" do Indeed
        if start:
            url += f"&start={start}"
        return url

    def search(self, browser: Browser, page: Page, s: Search, max_pages: int) -> list[Found]:
        out: list[Found] = []
        seen: set[str] = set()
        remote_style = "attr"
        for p in range(max_pages):
            url = self.search_url(s, p * 10, remote_style)
            if not browser.goto(page, url):
                break
            h.medium()
            try:
                page.wait_for_selector("a[data-jk], div.job_seen_beacon", timeout=15000)
            except Exception:
                txt = page.locator("body").inner_text(timeout=3000)[:3000]
                if re.search(r"não encontrou|did not match|nenhum resultado|0 vagas", txt, re.I):
                    if p == 0 and remote_style == "attr" and s.remote_only:
                        remote_style = "location"     # tenta l=Remoto se o atributo não funcionar
                        console.print(f"[dim]indeed: sem resultados com filtro remoto por atributo; tentando l=Remoto[/]")
                        url = self.search_url(s, 0, remote_style)
                        if not browser.goto(page, url):
                            break
                        h.medium()
                        try:
                            page.wait_for_selector("a[data-jk], div.job_seen_beacon", timeout=15000)
                        except Exception:
                            console.print(f"[dim]indeed: sem resultados para '{s.query}'[/]")
                            break
                    else:
                        console.print(f"[dim]indeed: sem resultados para '{s.query}' (página {p+1})[/]")
                        break
                else:
                    if not browser.ensure_ok(page):
                        break
                    console.print(f"[yellow]indeed: lista não carregou em {url}[/]")
                    break
            h.scroll(page, 900)
            cards = page.evaluate(CARDS_JS)
            new_here = 0
            for c in cards:
                jk = c["jk"]
                if jk in seen:
                    continue
                seen.add(jk)
                new_here += 1
                out.append(Found(
                    source="indeed", external_id=jk, url=f"https://br.indeed.com/viewjob?jk={jk}",
                    title=c.get("title") or "", company=c.get("company") or "", location=c.get("location") or "",
                    posted_at=parse_posted(c.get("posted")),
                    apply_hint="indeed_apply" if c.get("easy") else "unknown",
                    already_applied=bool(c.get("applied")),
                ))
            console.print(f"[dim]indeed '{s.query}' p{p+1}: {new_here} vagas[/]")
            if new_here < 8:
                break
            h.long()
        return out

    def fetch_details(self, browser: Browser, page: Page | None, job: Job) -> None:
        page = page or browser.page()
        if job.external_id not in page.url:
            browser.goto(page, job.url)
            h.medium()
        try:
            page.wait_for_selector("#jobDescriptionText, h1", timeout=15000)
        except Exception:
            if not browser.ensure_ok(page):
                return
        h.read_page(page)
        job.title = job.title or first_text(page, ['[data-testid="jobsearch-JobInfoHeader-title"]', "h1"])
        job.company = job.company or first_text(page, ['[data-testid="inlineHeader-companyName"]', '[data-company-name="true"]', ".jobsearch-InlineCompanyRating div"])
        job.location = job.location or first_text(page, ['[data-testid="inlineHeader-companyLocation"]', '[data-testid="jobsearch-JobInfoHeader-companyLocation"]', ".jobsearch-JobInfoHeader-subtitle div:nth-child(2)"])
        job.description = first_text(page, ["#jobDescriptionText", ".jobsearch-jobDescriptionText", ".jobsearch-JobComponent-description"], timeout=4000)
        body = page.locator("body").inner_text(timeout=3000)[:5000]
        if not job.posted_at:
            m = re.search(r"(publicad[ao] h[aá] \d+ (?:minutos?|horas?|dias?)|h[aá] \d+ (?:minutos?|horas?|dias?)|hoje|ontem|rec[eé]m-publicada|just posted|\d+ days? ago)", body, re.I)
            if m:
                job.posted_at = parse_posted(m.group(1))
        if re.search(r"esta vaga expirou|this job has expired|vaga encerrada|no longer available", body, re.I):
            job.apply_type = "closed"
            return
        if re.search(r"^\s*candidatou-se\b|você se candidatou|applied on", body, re.I | re.M):
            job.apply_type = "already_applied"
            return
        for sel in ["#indeedApplyButton", ".ia-IndeedApplyButton", '[data-testid*="indeedApply"]', 'button[aria-label*="Candidatar-se agora"]', 'button[aria-label*="Apply now"]']:
            try:
                b = page.locator(sel).first
                if b.count() and b.is_visible():
                    job.apply_type = "indeed_apply"
                    return
            except Exception:
                continue
        for sel in ["#applyButtonLinkContainer a", '[data-testid="applyButtonLinkContainer"] a', 'a[href*="/applystart"]', "a:has-text('Candidatar-se no site da empresa')", "a:has-text('Apply on company site')", "button:has-text('Candidatar-se no site da empresa')"]:
            try:
                a = page.locator(sel).first
                if a.count() and a.is_visible():
                    href = a.get_attribute("href") or ""
                    job.apply_type = "external"
                    job.external_url = urljoin(page.url, href) if href else None
                    return
            except Exception:
                continue
        job.apply_type = job.apply_type or "unknown"
