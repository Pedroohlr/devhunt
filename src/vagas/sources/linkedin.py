"""LinkedIn: descoberta e detalhes pelo endpoint público (sem usar sua conta) com fallback para a sessão logada."""
from __future__ import annotations

import html as htmlmod
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from rich.console import Console

from .. import humanize as h
from ..browser import Browser, Page
from ..config import Search
from ..db import Job
from .base import Found, Source, clean, first_text, parse_posted

console = Console()

GUEST_SEARCH = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
GUEST_POSTING = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{id}"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
GEO_ID = "106057199"  # Brasil

CARD_SEL = "li[data-occludable-job-id], div.job-card-container[data-job-id]"

LIST_SCROLL_JS = """
() => {
  const card = document.querySelector('li[data-occludable-job-id], div.job-card-container');
  if (!card) return false;
  let el = card.parentElement;
  while (el && el !== document.body) {
    const st = getComputedStyle(el);
    if ((st.overflowY === 'auto' || st.overflowY === 'scroll') && el.scrollHeight > el.clientHeight + 10) {
      el.scrollTop = Math.min(el.scrollTop + 500, el.scrollHeight);
      return el.scrollTop + el.clientHeight < el.scrollHeight - 5;
    }
    el = el.parentElement;
  }
  window.scrollBy(0, 500);
  return (window.innerHeight + window.scrollY) < document.body.scrollHeight - 5;
}
"""

CARDS_JS = """
() => Array.from(document.querySelectorAll('li[data-occludable-job-id], div.job-card-container[data-job-id]')).map(li => {
  const id = li.getAttribute('data-occludable-job-id') || li.getAttribute('data-job-id');
  const t = (sel) => { const e = li.querySelector(sel); return e ? (e.innerText || e.textContent || '').trim() : ''; };
  const a = li.querySelector('a[href*="/jobs/view/"]');
  let title = t('.job-card-list__title--link strong, .job-card-list__title--link, .job-card-container__link strong, .job-card-list__title, a[href*="/jobs/view/"]');
  title = title.split('\\n')[0].trim();
  const company = t('.artdeco-entity-lockup__subtitle, .job-card-container__primary-description, .job-card-container__company-name');
  const location = t('.artdeco-entity-lockup__caption, .job-card-container__metadata-item, .job-card-container__metadata-wrapper li');
  const footer = t('.job-card-container__footer-job-state, .job-card-list__footer-wrapper');
  const timeEl = li.querySelector('time');
  const posted = timeEl ? (timeEl.getAttribute('datetime') || '') : '';
  const postedText = timeEl ? (timeEl.innerText || '') : '';
  const easy = /simplificada|easy apply/i.test(t('.job-card-container__apply-method, .job-card-list__footer-wrapper, li'));
  return { id, title, company, location, footer, posted, postedText, easy, href: a ? a.getAttribute('href') : '' };
})
"""


def _get(url: str, timeout: int = 30) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.5", "Accept": "text/html,*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", errors="ignore")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception:
        return 0, ""


def _text(fragment: str) -> str:
    return clean(htmlmod.unescape(re.sub(r"<[^>]+>", " ", fragment or "")))


def _html_to_text(fragment: str) -> str:
    d = re.sub(r"<br\s*/?>|</p>|</li>|</div>|</h\d>", "\n", fragment or "")
    d = re.sub(r"<li[^>]*>", "• ", d)
    d = re.sub(r"<[^>]+>", "", d)
    d = htmlmod.unescape(d).replace("\xa0", " ")
    d = re.sub(r"[ \t]+\n", "\n", d)
    return re.sub(r"\n{3,}", "\n\n", d).strip()


def parse_guest_cards(page_html: str) -> list[dict]:
    out = []
    for li in re.findall(r"<li>(.*?)</li>", page_html, re.S):
        m = re.search(r"urn:li:jobPosting:(\d+)", li)
        if not m:
            continue
        title = re.search(r'<h3 class="base-search-card__title">\s*(.*?)\s*</h3>', li, re.S)
        comp = re.search(r'<h4 class="base-search-card__subtitle">\s*(?:<a[^>]*>)?\s*(.*?)\s*(?:</a>)?\s*</h4>', li, re.S)
        loc = re.search(r'<span class="job-search-card__location">\s*(.*?)\s*</span>', li, re.S)
        t = re.search(r'<time[^>]*datetime="([^"]+)"[^>]*>\s*(.*?)\s*</time>', li, re.S)
        out.append({
            "id": m.group(1),
            "title": _text(title.group(1)) if title else "",
            "company": _text(comp.group(1)) if comp else "",
            "location": _text(loc.group(1)) if loc else "",
            "posted": parse_posted(t.group(2) if t else None, t.group(1) if t else None),
        })
    return out


def parse_guest_posting(page_html: str) -> dict:
    title = re.search(r'<h2 class="top-card-layout__title[^>]*>(.*?)</h2>', page_html, re.S)
    comp = re.search(r'<a class="topcard__org-name-link[^>]*>(.*?)</a>', page_html, re.S)
    loc = re.search(r'<span class="topcard__flavor topcard__flavor--bullet">(.*?)</span>', page_html, re.S)
    posted = re.search(r'<span class="posted-time-ago__text[^>]*>(.*?)</span>', page_html, re.S)
    desc = re.search(r'<div class="show-more-less-html__markup[^>]*>(.*?)</div>\s*</section>', page_html, re.S)
    body = re.search(r'<div class="show-more-less-html__markup[^>]*>(.*?)<div class="show-more-less-button', page_html, re.S) if not desc else None
    crit = re.findall(r'<h3 class="description__job-criteria-subheader">(.*?)</h3>\s*<span class="description__job-criteria-text[^>]*>(.*?)</span>', page_html, re.S)
    if "apply-link-onsite" in page_html:
        apply_type = "easy_apply"
    elif "apply-link-offsite" in page_html or "offsite-apply-icon" in page_html:
        apply_type = "external"
    else:
        apply_type = None
    closed = bool(re.search(r"n[aã]o est[aá] mais aceitando candidaturas|no longer accepting applications", page_html, re.I))
    return {
        "title": _text(title.group(1)) if title else "",
        "company": _text(comp.group(1)) if comp else "",
        "location": _text(loc.group(1)) if loc else "",
        "posted": parse_posted(_text(posted.group(1)) if posted else None),
        "description": _html_to_text((desc or body).group(1)) if (desc or body) else "",
        "criteria": {_text(k): _text(v) for k, v in crit},
        "apply_type": "closed" if closed else apply_type,
    }


class LinkedInSource(Source):
    name = "linkedin"
    needs_browser = False

    def __init__(self, geo_id: str = GEO_ID, mode: str = "guest"):
        self.geo_id = geo_id
        self.mode = mode
        self.needs_browser = mode != "guest"

    # ---------- busca: endpoint público ----------
    def guest_url(self, s: Search, start: int) -> str:
        q = {
            "keywords": s.query, "location": s.location, "geoId": self.geo_id,
            "f_TPR": f"r{s.seconds}", "sortBy": "DD", "start": start,
        }
        if s.remote_only:
            q["f_WT"] = "2"
        return GUEST_SEARCH + "?" + urllib.parse.urlencode(q)

    def search_guest(self, s: Search, max_pages: int) -> list[Found] | None:
        out: list[Found] = []
        seen: set[str] = set()
        for p in range(max_pages):
            code, body = _get(self.guest_url(s, p * 10))
            if code == 429:
                console.print("[yellow]linkedin guest: 429, esperando 45s[/]")
                time.sleep(45)
                code, body = _get(self.guest_url(s, p * 10))
            if code != 200:
                if p == 0:
                    return None  # sinaliza fallback
                break
            cards = parse_guest_cards(body)
            new_here = 0
            for c in cards:
                if c["id"] in seen:
                    continue
                seen.add(c["id"])
                new_here += 1
                out.append(Found(
                    source="linkedin", external_id=c["id"], url=f"https://www.linkedin.com/jobs/view/{c['id']}/",
                    title=c["title"], company=c["company"], location=c["location"], posted_at=c["posted"],
                ))
            if len(cards) < 10 or new_here == 0:
                break
            time.sleep(random.uniform(1.5, 4.0))
        return out

    def search(self, browser: Browser, page: Page | None, s: Search, max_pages: int) -> list[Found]:
        if self.mode == "guest":
            res = self.search_guest(s, max_pages)
            if res is not None:
                return res
            console.print("[yellow]linkedin guest indisponível; usando a sessão logada para esta busca[/]")
        return self.search_session(browser, page or browser.page(), s, max_pages)

    # ---------- busca: sessão logada (fallback) ----------
    def search_url(self, s: Search, start: int) -> str:
        url = (
            "https://www.linkedin.com/jobs/search/?keywords=" + urllib.parse.quote_plus(s.query)
            + "&location=" + urllib.parse.quote_plus(s.location) + f"&geoId={self.geo_id}"
            + f"&f_TPR=r{s.seconds}&sortBy=DD"
        )
        if s.remote_only:
            url += "&f_WT=2"
        if start:
            url += f"&start={start}"
        return url

    def search_session(self, browser: Browser, page: Page, s: Search, max_pages: int) -> list[Found]:
        out: list[Found] = []
        seen: set[str] = set()
        for p in range(max_pages):
            url = self.search_url(s, p * 25)
            if not browser.goto(page, url):
                break
            h.medium()
            try:
                page.wait_for_selector(CARD_SEL, timeout=15000)
            except Exception:
                txt = page.locator("body").inner_text(timeout=3000)[:3000]
                if re.search(r"nenhuma vaga|no matching jobs|não encontramos", txt, re.I):
                    console.print(f"[dim]linkedin: sem resultados para '{s.query}' (página {p+1})[/]")
                else:
                    console.print(f"[yellow]linkedin: lista não carregou em {url}[/]")
                break
            for _ in range(14):
                more = page.evaluate(LIST_SCROLL_JS)
                h.pause(0.4, 1.0)
                if not more:
                    break
            cards = page.evaluate(CARDS_JS)
            new_here = 0
            for c in cards:
                jid = str(c.get("id") or "").strip()
                if not jid or jid in seen:
                    continue
                seen.add(jid)
                new_here += 1
                footer = (c.get("footer") or "").lower()
                out.append(Found(
                    source="linkedin", external_id=jid, url=f"https://www.linkedin.com/jobs/view/{jid}/",
                    title=c.get("title") or "", company=c.get("company") or "", location=c.get("location") or "",
                    posted_at=parse_posted(c.get("postedText"), c.get("posted")),
                    apply_hint="easy_apply" if c.get("easy") else "unknown",
                    already_applied=bool(re.search(r"candidatura enviada|applied", footer)),
                ))
            console.print(f"[dim]linkedin '{s.query}' p{p+1}: {new_here} vagas[/]")
            if new_here < 10:
                break
            h.long()
        return out

    # ---------- detalhes ----------
    def fetch_details(self, browser: Browser, page: Page | None, job: Job) -> None:
        if self.mode == "guest":
            code, body = _get(GUEST_POSTING.format(id=job.external_id))
            if code == 429:
                time.sleep(30)
                code, body = _get(GUEST_POSTING.format(id=job.external_id))
            if code == 200 and body:
                d = parse_guest_posting(body)
                if d["description"]:
                    job.title = d["title"] or job.title
                    job.company = d["company"] or job.company
                    job.location = d["location"] or job.location
                    job.posted_at = d["posted"] or job.posted_at
                    job.description = d["description"]
                    if d["criteria"]:
                        job.description += "\n\n" + "\n".join(f"{k}: {v}" for k, v in d["criteria"].items())
                    job.apply_type = d["apply_type"] or job.apply_type
                    time.sleep(random.uniform(1.0, 2.5))
                    return
            console.print(f"[dim]linkedin guest sem detalhes ({code}); usando sessão[/]")
        self.fetch_details_session(browser, page or browser.page(), job)

    def fetch_details_session(self, browser: Browser, page: Page, job: Job) -> None:
        if not page.url.startswith(job.url.rstrip("/")):
            browser.goto(page, job.url)
            h.medium()
        try:
            page.wait_for_selector("h1, .jobs-details", timeout=15000)
        except Exception:
            pass
        h.read_page(page)
        job.title = job.title or first_text(page, ["h1.t-24", ".job-details-jobs-unified-top-card__job-title h1", "h1"])
        job.company = job.company or first_text(page, [".job-details-jobs-unified-top-card__company-name", ".jobs-unified-top-card__company-name", "a[data-test-app-aware-link][href*='/company/']"])
        job.location = job.location or first_text(page, [".job-details-jobs-unified-top-card__primary-description-container span", ".job-details-jobs-unified-top-card__tertiary-description-container span", ".jobs-unified-top-card__bullet"])
        try:
            more = page.locator("button.jobs-description__footer-button, button:has-text('Ver mais'), button:has-text('See more')").first
            if more.count() and more.is_visible():
                h.click(more)
        except Exception:
            pass
        job.description = first_text(page, ["#job-details", ".jobs-description__content", ".jobs-box__html-content", ".jobs-description"], timeout=4000)
        body = page.locator("body").inner_text(timeout=3000)[:6000]
        if not job.posted_at:
            m = re.search(r"(h[aá] \d+ (?:minutos?|horas?|dias?|semanas?)|\d+ (?:minutes?|hours?|days?|weeks?) ago|rec[eé]m-publicada|just posted)", body, re.I)
            if m:
                job.posted_at = parse_posted(m.group(1))
        if re.search(r"não está mais aceitando candidaturas|no longer accepting applications", body, re.I):
            job.apply_type = "closed"
            return
        if re.search(r"candidatura enviada há|candidatura enviada em|applied \d|application submitted|você se candidatou", body, re.I) and not job.apply_type == "easy_apply":
            try:
                if page.locator(".jobs-s-apply .artdeco-inline-feedback, .post-apply-timeline, .jobs-details-top-card__apply-timeline").count():
                    job.apply_type = "already_applied"
                    return
            except Exception:
                pass
        btn = self.apply_button(page)
        if btn is None:
            job.apply_type = job.apply_type or "unknown"
            return
        try:
            label = (btn.inner_text(timeout=2000) + " " + (btn.get_attribute("aria-label") or "")).lower()
        except Exception:
            label = ""
        job.apply_type = "easy_apply" if re.search(r"simplificada|easy apply", label) else "external"

    @staticmethod
    def apply_button(page: Page, timeout_s: float = 12.0):
        """Botão/link de candidatura da VAGA ABERTA (hoje é um <a> com classes embaralhadas).
        Busca por papel+texto e escolhe o candidato mais alto na página: o botão principal fica no topo,
        acima da descrição; os de "Vagas semelhantes" ficam embaixo."""
        patterns = [
            re.compile(r"candidatura simplificada|easy apply", re.I),
            re.compile(r"^\s*candidatar(-se)?\b|^\s*apply\b|candidatar-se a|apply to", re.I),
        ]
        deadline = time.time() + timeout_s
        # o cabeçalho fixo tem um clone DESABILITADO do botão quando a página está rolada: volta ao topo antes de procurar
        try:
            page.evaluate("() => window.scrollTo(0, 0)")
            time.sleep(0.4)
        except Exception:
            pass
        while True:
            cands = []
            for pat in patterns:
                for role in ("button", "link"):
                    try:
                        loc = page.get_by_role(role, name=pat)
                        for i in range(min(loc.count(), 8)):
                            el = loc.nth(i)
                            try:
                                if not el.is_visible():
                                    continue
                                box = el.bounding_box()
                                if not box:
                                    continue
                                txt = (el.inner_text(timeout=500) or "").strip()
                                if len(txt) > 40:      # cards de outras vagas trazem título junto
                                    continue
                                try:
                                    if el.evaluate("e => e.disabled || e.getAttribute('aria-disabled') === 'true' || /disabled/i.test(e.className || '')"):
                                        continue   # clone do cabeçalho fixo (cinza) não abre o modal
                                except Exception:
                                    pass
                                cands.append((box["y"], el))
                            except Exception:
                                continue
                    except Exception:
                        continue
            if cands:
                cands.sort(key=lambda c: c[0])
                return cands[0][1]
            for sel in [".jobs-apply-button--top-card button.jobs-apply-button", "button.jobs-apply-button", ".jobs-s-apply button", ".jobs-s-apply a"]:
                try:
                    loc = page.locator(sel)
                    for i in range(min(loc.count(), 3)):
                        b = loc.nth(i)
                        if b.is_visible():
                            return b
                except Exception:
                    continue
            if time.time() > deadline:
                return None
            time.sleep(0.8)
