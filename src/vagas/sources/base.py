from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ..browser import Browser, Page
from ..config import Search
from ..db import Job


@dataclass
class Found:
    source: str
    external_id: str
    url: str
    title: str = ""
    company: str = ""
    location: str = ""
    posted_at: str | None = None
    apply_hint: str = "unknown"      # easy_apply | indeed_apply | external | unknown
    already_applied: bool = False
    ats: str | None = None
    extra: dict = field(default_factory=dict)

    @property
    def job_id(self) -> str:
        return f"{self.source}:{self.external_id}"

    def to_job(self) -> Job:
        return Job(
            id=self.job_id, source=self.source, external_id=self.external_id, url=self.url,
            title=clean(self.title), company=clean(self.company), location=clean(self.location),
            posted_at=self.posted_at, apply_type=self.apply_hint if self.apply_hint != "unknown" else None,
            ats=self.ats, status="applied" if self.already_applied else "new",
            reason="já aplicada manualmente (badge na listagem)" if self.already_applied else None,
        )


def clean(s: str | None) -> str:
    s = (s or "").replace("\xa0", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s


class Source:
    name = "base"

    def search(self, browser: Browser, page: Page, search: Search, max_pages: int) -> list[Found]:
        raise NotImplementedError

    def fetch_details(self, browser: Browser, page: Page, job: Job) -> None:
        """Preenche description, apply_type, external_url (e title/company/location se faltarem)."""
        raise NotImplementedError


def first_text(page_or_loc, selectors: list[str], timeout: int = 1500) -> str:
    for sel in selectors:
        try:
            loc = page_or_loc.locator(sel).first
            if loc.count() and loc.is_visible():
                t = loc.inner_text(timeout=timeout)
                if t and t.strip():
                    return clean(t)
        except Exception:
            continue
    return ""


REL_RE = re.compile(r"(\d+)\s*(minuto|min\b|hora|hour|dia|day|semana|week|m[eê]s|month)", re.I)


def parse_posted(text: str | None, dt_attr: str | None = None) -> str | None:
    """'Há 3 horas' / '2 days ago' / 'Publicada há 1 dia' / datetime="2026-09-09" -> ISO (UTC)."""
    now = datetime.now(timezone.utc)
    t = (text or "").strip().lower()
    if t:
        if re.search(r"agora|just now|rec[eé]m|just posted|hoje|today|new job|nova", t):
            return now.isoformat(timespec="seconds")
        if re.search(r"ontem|yesterday", t):
            return (now - timedelta(hours=24)).isoformat(timespec="seconds")
        m = REL_RE.search(t)
        if m:
            n = int(m.group(1)); u = m.group(2).lower()
            if u.startswith("min"):
                d = timedelta(minutes=n)
            elif u.startswith(("hora", "hour")):
                d = timedelta(hours=n)
            elif u.startswith(("dia", "day")):
                d = timedelta(days=n)
            elif u.startswith(("semana", "week")):
                d = timedelta(weeks=n)
            else:
                d = timedelta(days=30 * n)
            return (now - d).isoformat(timespec="seconds")
    if dt_attr:
        a = dt_attr.strip()
        try:
            if len(a) == 10:
                return a                      # só data: a checagem de idade dá folga de 24h
            return datetime.fromisoformat(a.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat(timespec="seconds")
        except Exception:
            return None
    return None


def age_hours(posted: str | None) -> float | None:
    if not posted:
        return None
    try:
        if len(posted) == 10:  # só data
            d = datetime.fromisoformat(posted).replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - d).total_seconds() / 3600 - 24  # folga: pode ter sido publicada no fim do dia
        d = datetime.fromisoformat(posted)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - d).total_seconds() / 3600
    except Exception:
        return None


def is_too_old(posted: str | None, max_age_hours: int) -> bool:
    h = age_hours(posted)
    return h is not None and h > max_age_hours
