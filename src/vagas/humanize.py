"""Ações com cara de gente: pausas, digitação, scroll, mouse."""
from __future__ import annotations

import os
import random
import time

from playwright.sync_api import Locator, Page

# VAGAS_FAST=1 (só para calibração/testes): encurta todas as pausas humanizadas para ~1/5.
# Nunca usar nas levas reais: o ritmo humano é a proteção contra bloqueio.
SPEED = 0.2 if os.environ.get("VAGAS_FAST", "").strip() == "1" else 1.0


def _trunc_normal(lo: float, hi: float) -> float:
    mu = (lo + hi) / 2
    sigma = (hi - lo) / 4
    for _ in range(10):
        x = random.gauss(mu, sigma)
        if lo <= x <= hi:
            return x
    return random.uniform(lo, hi)


def pause(lo: float, hi: float) -> None:
    time.sleep(_trunc_normal(lo, hi) * SPEED)


def tiny() -> None:
    pause(0.15, 0.5)


def short() -> None:
    pause(0.5, 1.5)


def medium() -> None:
    pause(1.5, 4.0)


def long() -> None:
    pause(4.0, 9.0)


def wiggle(page: Page) -> None:
    """Move o mouse aleatoriamente um pouco."""
    try:
        vp = page.viewport_size or {"width": 1280, "height": 800}
        for _ in range(random.randint(1, 3)):
            page.mouse.move(random.randint(50, vp["width"] - 50), random.randint(80, vp["height"] - 80), steps=random.randint(5, 15))
            time.sleep(random.uniform(0.05, 0.2))
    except Exception:
        pass


def scroll(page: Page, total_px: int | None = None) -> None:
    total = total_px if total_px is not None else random.randint(300, 1200)
    done = 0
    sign = 1 if total >= 0 else -1
    total = abs(total)
    try:
        while done < total:
            step = random.randint(80, 260)
            page.mouse.wheel(0, sign * step)
            done += step
            time.sleep(random.uniform(0.04, 0.18))
    except Exception:
        pass


def read_page(page: Page) -> None:
    """Simula alguém lendo a vaga: scroll pra baixo, pausa, volta um pouco."""
    scroll(page, random.randint(500, 1500))
    pause(1.0, 3.0)
    if random.random() < 0.5:
        scroll(page, -random.randint(200, 600))
        pause(0.5, 1.5)


def click(locator: Locator, timeout: float = 10000) -> None:
    locator.scroll_into_view_if_needed(timeout=timeout)
    try:
        locator.hover(timeout=timeout)
        time.sleep(random.uniform(0.08, 0.35))
    except Exception:
        pass
    try:
        locator.click(timeout=timeout)
    except Exception as ex:
        msg = str(ex)
        # overlay/animação interceptando o ponteiro (ex.: Quill Forms ".renderer-core-form-flow"): clique forçado
        if "intercepts pointer events" in msg or "Timeout" in type(ex).__name__ or "Timeout" in msg:
            try:
                locator.click(force=True, timeout=min(timeout, 5000))
            except Exception:
                locator.evaluate("el => el.click()")   # botão fora da viewport (rodapé fixo em modal)
        elif "outside of the viewport" in msg:
            locator.evaluate("el => el.click()")
        else:
            raise
    tiny()


def type_text(locator: Locator, text: str, clear: bool = True) -> None:
    locator.scroll_into_view_if_needed()
    locator.click()
    time.sleep(random.uniform(0.1, 0.3))
    if clear:
        locator.press("Control+a")
        locator.press("Backspace")
    # digita em blocos com velocidade variável; pausa ocasional "pensando"
    i = 0
    while i < len(text):
        chunk = text[i : i + random.randint(3, 9)]
        locator.press_sequentially(chunk, delay=random.randint(30, 110))
        i += len(chunk)
        if random.random() < 0.08:
            time.sleep(random.uniform(0.3, 0.9))
    tiny()
