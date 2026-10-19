"""HTML (Jinja2) -> PDF (Chromium headless via Playwright)."""
from __future__ import annotations

import base64
import mimetypes
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..config import resolve

TEMPLATES = Path(__file__).parent / "templates"

MONTHS = {
    "pt-BR": ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"],
    "en": ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
}


def _period(value: str | None, current: str | None = None) -> str:
    """'2021-03' -> 'mar 2021'; vazio -> 'Atual'/'Present'."""
    v = (value or "").strip()
    if not v:
        return "Present" if (current or "").startswith("en") else "Atual"
    parts = v.split("-")
    if len(parts) >= 2 and parts[1].isdigit():
        lang = "en" if (current or "").startswith("en") else "pt-BR"
        m = int(parts[1])
        if 1 <= m <= 12:
            return f"{MONTHS[lang][m - 1]} {parts[0]}"
    return parts[0]


def _env() -> Environment:
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=select_autoescape(["html"]))
    env.filters["period"] = _period
    return env


def _photo_data(profile: dict) -> str:
    p = (profile.get("personal") or {}).get("photo")
    if not p:
        return ""
    path = resolve(p)
    if not path.exists():
        return ""
    mime = mimetypes.guess_type(str(path))[0] or "image/jpeg"
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode()


def _soft(hex_color: str, alpha: float = 0.12) -> str:
    """Versão clara da cor de destaque (mistura com branco)."""
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return "#e9edf8"
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    mix = lambda c: int(round(255 - (255 - c) * alpha))
    return "#{:02x}{:02x}{:02x}".format(mix(r), mix(g), mix(b))


def render_html(resume: dict, profile: dict, template: str = "modern", accent: str = "#1f3a93", photo_mm: int = 38) -> str:
    name = template if template.endswith(".html") else f"{template}.html"
    if name == "classic.html" and not (TEMPLATES / name).exists():
        name = "resume.html"
    tpl = _env().get_template(name)
    return tpl.render(
        resume=resume, personal=profile.get("personal") or {},
        photo_data=_photo_data(profile) if photo_mm else "",
        fonts_dir=(TEMPLATES / "fonts").resolve().as_uri(),
        accent=accent, accent_soft=_soft(accent), photo_mm=photo_mm,
    )


def html_to_pdf(html: str, out_path: Path, playwright=None, html_path: Path | None = None) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    html_path = html_path or out_path.with_suffix(".html")
    html_path.write_text(html, encoding="utf-8")
    own = playwright is None
    if own:
        from playwright.sync_api import sync_playwright
        playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(html_path.resolve().as_uri(), wait_until="load")   # file:// para as fontes locais carregarem
        try:
            page.evaluate("document.fonts && document.fonts.ready")
        except Exception:
            pass
        page.emulate_media(media="print")
        page.pdf(path=str(out_path), format="A4", print_background=True, prefer_css_page_size=True)
        browser.close()
    finally:
        if own:
            playwright.stop()
    return out_path


def generate_pdf(resume: dict, profile: dict, out_pdf: Path, playwright=None, template: str = "modern",
                 accent: str = "#1f3a93", photo_mm: int = 38) -> Path:
    html = render_html(resume, profile, template=template, accent=accent, photo_mm=photo_mm)
    return html_to_pdf(html, out_pdf, playwright=playwright)
