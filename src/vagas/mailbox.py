"""Caixa de entrada da conta de cadastros (Gmail do SIGNUP_EMAIL, aberto no Chrome da ferramenta).

Usado para: pegar código de verificação e abrir link de confirmação de cadastro em ATS.
Nunca guarda o conteúdo dos e-mails em banco/log: só devolve o código ou o link pedido.
"""
from __future__ import annotations

import os
import re
import time

from .browser import Browser, Page

CODE_RE = re.compile(r"\b(\d{4,8})\b")
CONFIRM_LINK_RE = re.compile(r"confirm|verify|verifica|ativa|activate|valida|cadastro|candidatura", re.I)


def signup_email() -> str:
    return (os.environ.get("SIGNUP_EMAIL") or "").strip()


def _mailbox_index(page: Page, email: str) -> int:
    """Descobre em qual perfil do Gmail (u/0, u/1...) está a conta de cadastros."""
    for i in range(4):
        try:
            page.goto(f"https://mail.google.com/mail/u/{i}/#inbox", wait_until="domcontentloaded", timeout=45000)
            time.sleep(3)
            if email.lower() in (page.title() or "").lower():
                return i
        except Exception:
            continue
    return 0


def open_search(page: Page, query: str, email: str | None = None) -> bool:
    """Abre a busca do Gmail da conta de cadastros. Devolve False se a caixa não abriu."""
    email = email or signup_email()
    if not email:
        return False
    idx = _mailbox_index(page, email)
    from urllib.parse import quote
    page.goto(f"https://mail.google.com/mail/u/{idx}/#search/{quote(query)}", wait_until="domcontentloaded", timeout=45000)
    time.sleep(3)
    return email.lower() in (page.title() or "").lower()


def latest_message_text(page: Page, query: str, *, wait_s: float = 90, email: str | None = None) -> str:
    """Espera chegar um e-mail que casa com a busca e devolve o texto do mais recente (abre a conversa)."""
    email = email or signup_email()
    deadline = time.time() + wait_s
    while time.time() < deadline:
        if not open_search(page, query, email):
            return ""
        try:
            rows = page.locator("tr.zA")
            n = min(rows.count(), 5)
            for i in range(n):
                row = rows.nth(i)
                try:
                    if not row.is_visible():
                        continue
                    row.scroll_into_view_if_needed(timeout=3000)
                    # clicar no assunto (a linha inteira nem sempre é clicável)
                    subj = row.locator("span.bog, .y6 span").first
                    (subj if subj.count() else row).click(timeout=6000)
                    time.sleep(3)
                    body = page.evaluate("() => (document.querySelector('div.a3s') || document.body).innerText")
                    if body and len(body) > 40:
                        return body[:8000]
                except Exception:
                    continue
        except Exception:
            pass
        time.sleep(8)
    return ""


def find_code(page: Page, query: str, *, wait_s: float = 120, email: str | None = None) -> str | None:
    """Código de verificação (4 a 8 dígitos) do e-mail mais recente que casa com a busca."""
    text = latest_message_text(page, query, wait_s=wait_s, email=email)
    if not text:
        return None
    # prioriza número perto de palavras como "código"
    m = re.search(r"(?:c[oó]digo|code|pin|token)[^\d]{0,40}(\d{4,8})", text, re.I)
    if m:
        return m.group(1)
    m = CODE_RE.search(text)
    return m.group(1) if m else None


def confirmation_link(page: Page, query: str, *, wait_s: float = 120, email: str | None = None) -> str | None:
    """URL do link de confirmação do e-mail mais recente que casa com a busca."""
    if not latest_message_text(page, query, wait_s=wait_s, email=email):
        return None
    try:
        hrefs = page.evaluate(
            "() => Array.from((document.querySelector('div.a3s') || document.body).querySelectorAll('a[href]')).map(a => a.href)"
        )
    except Exception:
        return None
    for h in hrefs:
        if h.startswith("http") and "google.com" not in h and CONFIRM_LINK_RE.search(h):
            return h
    for h in hrefs:
        if h.startswith("http") and "google.com" not in h:
            return h
    return None


def open_confirmation(browser: Browser, query: str, *, wait_s: float = 120) -> str | None:
    """Abre o link de confirmação numa aba nova e devolve a URL final (ou None)."""
    page = browser.new_page()
    try:
        link = confirmation_link(page, query, wait_s=wait_s)
        if not link:
            return None
        page.goto(link, wait_until="domcontentloaded", timeout=45000)
        time.sleep(3)
        return page.url
    finally:
        try:
            page.close()
        except Exception:
            pass
