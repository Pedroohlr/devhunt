"""Descoberta via API pública do portal Gupy (sem expor o navegador) + detalhes na página da vaga."""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from rich.console import Console

from .. import humanize as h
from ..browser import Browser, Page
from ..config import Search
from ..db import Job
from .base import Found, Source, clean, first_text

console = Console()

API = "https://employability-portal.gupy.io/api/v1/jobs"


def _parse_date(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _get_json(url: str) -> dict | list | None:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/153.0.0.0 Safari/537.36", "accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        console.print(f"[yellow]gupy api falhou: {e}[/]")
        return None


class GupySource(Source):
    name = "gupy"
    needs_browser = False

    def search(self, browser: Browser, page: Page | None, s: Search, max_pages: int) -> list[Found]:
        out: list[Found] = []
        since = datetime.now(timezone.utc) - timedelta(seconds=s.seconds)
        offset = 0
        limit = 100
        for _ in range(max_pages):
            url = f"{API}?jobName={urllib.parse.quote(s.query)}&offset={offset}&limit={limit}"
            if s.remote_only:
                url += "&workplaceType=remote"
            data = _get_json(url)
            if data is None:
                break
            items = data.get("data") if isinstance(data, dict) else data
            if not isinstance(items, list) or not items:
                break
            new_here = 0
            for it in items:
                pub = _parse_date(it.get("publishedDate") or it.get("publishedAt") or it.get("createdAt"))
                if pub and pub < since:
                    continue
                jid = str(it.get("id") or "")
                job_url = it.get("jobUrl") or it.get("url") or ""
                if not jid or not job_url:
                    continue
                city = it.get("city") or ""
                state = it.get("state") or ""
                wt = it.get("workplaceType") or ("remote" if it.get("isRemoteWork") else "")
                loc = ", ".join(x for x in [city, state] if x) or (wt or "")
                if wt and loc and wt not in loc:
                    loc = f"{loc} ({wt})"
                out.append(Found(
                    source="gupy", external_id=jid, url=job_url,
                    title=it.get("name") or it.get("title") or "", company=it.get("careerPageName") or it.get("companyName") or "",
                    location=loc, posted_at=pub.isoformat() if pub else None, apply_hint="external", ats="gupy",
                    extra={"raw": {k: it.get(k) for k in ("type", "workplaceType", "isRemoteWork", "careerPageUrl", "disabilities")}},
                ))
                new_here += 1
            console.print(f"[dim]gupy '{s.query}' offset {offset}: {new_here} vagas recentes de {len(items)}[/]")
            if len(items) < limit:
                break
            # se a página inteira já é mais antiga que a janela, para
            dates = [_parse_date(i.get("publishedDate")) for i in items]
            dates = [d for d in dates if d]
            if dates and max(dates) < since:
                break
            offset += limit
        return out

    def fetch_details(self, browser: Browser, page: Page | None, job: Job) -> None:
        page = page or browser.page()
        if job.url not in page.url:
            browser.goto(page, job.url)
            h.medium()
        try:
            page.wait_for_selector("h1", timeout=15000)
        except Exception:
            pass
        h.read_page(page)
        job.title = job.title or first_text(page, ["h1"])
        job.description = first_text(page, ['[data-testid="text-section"]', "[class*='description' i]", "main", "body"], timeout=4000)
        body = page.locator("body").inner_text(timeout=3000)[:6000]
        if re.search(r"vaga (encerrada|indisponível|expirada)|não está mais disponível", body, re.I):
            job.apply_type = "closed"
            return
        job.apply_type = "external"
        job.external_url = job.url
        job.ats = "gupy"
