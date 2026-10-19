"""Navegador com perfil persistente (sua sessão real) e detecção de checkpoints (login/captcha)."""
from __future__ import annotations

import re
import os
import sys
import time
from pathlib import Path

from rich.console import Console

try:  # Patchright = fork do Playwright que fecha vazamentos de CDP (Runtime.enable, flags, navigator.webdriver)
    from patchright.sync_api import BrowserContext, Page, sync_playwright  # type: ignore
    USING_PATCHRIGHT = True
except ImportError:  # pragma: no cover
    from playwright.sync_api import BrowserContext, Page, sync_playwright  # type: ignore
    USING_PATCHRIGHT = False

from .config import BrowserCfg, Settings, resolve

console = Console()

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
if (!window.chrome) { window.chrome = { runtime: {} }; }
Object.defineProperty(navigator, 'languages', {get: () => ['pt-BR', 'pt', 'en-US', 'en']});
const _q = window.navigator.permissions && window.navigator.permissions.query;
if (_q) {
  window.navigator.permissions.query = (p) => (p && p.name === 'notifications')
    ? Promise.resolve({ state: Notification.permission }) : _q.call(window.navigator.permissions, p);
}
"""

CHECKPOINT_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("linkedin_login", re.compile(r"linkedin\.com/(login|uas/login|checkpoint|authwall)", re.I)),
    ("indeed_login", re.compile(r"secure\.indeed\.com/(auth|account/login)", re.I)),
    ("gupy_login", re.compile(r"login\.gupy\.io|gupy\.io/candidates/(signin|login)", re.I)),
]
CHECKPOINT_TEXTS = [
    re.compile(r"verify you are human|confirme que você é humano|verifique que você é humano", re.I),
    re.compile(r"additional verification required|verificação adicional", re.I),
    re.compile(r"security check|verificação de segurança", re.I),
    re.compile(r"cf-challenge|challenge-platform", re.I),
]


class Browser:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.cfg: BrowserCfg = settings.browser
        self._pw = None
        self.context: BrowserContext | None = None
        self._browser = None

    # ---------- ciclo de vida ----------
    @property
    def playwright(self):
        return self._pw

    def start(self) -> "Browser":
        self._pw = sync_playwright().start()
        console.print(f"[dim]navegador: {'patchright' if USING_PATCHRIGHT else 'playwright'} · modo={self.cfg.mode} · channel={self.cfg.channel or 'chromium'}[/]")
        if self.cfg.mode == "cdp":
            self._browser = self._pw.chromium.connect_over_cdp(self.cfg.cdp_url)
            self.context = self._browser.contexts[0] if self._browser.contexts else self._browser.new_context()
        else:
            udd = resolve(self.cfg.user_data_dir)
            udd.mkdir(parents=True, exist_ok=True)
            kwargs = dict(
                user_data_dir=str(udd),
                headless=self.cfg.headless,
                locale=self.cfg.locale,
                timezone_id=self.cfg.timezone,
                slow_mo=self.cfg.slow_mo_ms or 0,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-infobars",
                    "--start-maximized",
                    # WebGL por software (WSL sem GPU): evita fingerprint "sem WebGL"
                    "--use-gl=angle",
                    "--use-angle=swiftshader",
                    "--enable-unsafe-swiftshader",
                    "--ignore-gpu-blocklist",
                ],
                ignore_default_args=["--enable-automation"],
                viewport=None if not self.cfg.headless else {"width": 1366, "height": 850},
            )
            exe = Path(self.cfg.executable_path).expanduser() if self.cfg.executable_path else None
            if exe and exe.exists():
                kwargs["executable_path"] = str(exe)
            elif self.cfg.channel:
                kwargs["channel"] = self.cfg.channel
            try:
                self.context = self._pw.chromium.launch_persistent_context(**kwargs)
            except Exception as e:
                if self.cfg.channel and "channel" in kwargs:
                    console.print(f"[yellow]Chrome '{self.cfg.channel}' não encontrado ({str(e).splitlines()[0][:120]}). Usando Chromium do Patchright/Playwright.[/]")
                    kwargs.pop("channel")
                    self.context = self._pw.chromium.launch_persistent_context(**kwargs)
                else:
                    raise
        if not USING_PATCHRIGHT:
            # com Patchright o init script é dispensável (e o próprio Patchright recomenda não injetar)
            self.context.add_init_script(STEALTH_JS)
        self.context.set_default_timeout(15000)
        self.context.set_default_navigation_timeout(45000)
        return self

    def stop(self) -> None:
        try:
            if self.context and self.cfg.mode != "cdp":
                self.context.close()
        finally:
            if self._pw:
                self._pw.stop()

    def __enter__(self) -> "Browser":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # ---------- páginas ----------
    def page(self) -> Page:
        assert self.context
        pages = [p for p in self.context.pages if not p.is_closed()]
        if pages:
            return pages[0]
        return self.context.new_page()

    def new_page(self) -> Page:
        assert self.context
        return self.context.new_page()

    def close_extra_pages(self, keep: Page) -> None:
        assert self.context
        for p in list(self.context.pages):
            if p is not keep and not p.is_closed():
                try:
                    p.close()
                except Exception:
                    pass

    # ---------- checkpoints ----------
    def detect_checkpoint(self, page: Page) -> str | None:
        url = page.url or ""
        for name, pat in CHECKPOINT_PATTERNS:
            if pat.search(url):
                return name
        try:
            title = page.title() or ""
            body = page.locator("body").inner_text(timeout=2000)[:4000]
        except Exception:
            return None
        for pat in CHECKPOINT_TEXTS:
            if pat.search(title) or pat.search(body):
                return "captcha"
        return None

    def attended(self) -> bool:
        """Tem alguém no teclado? A leva do painel roda sem terminal, então não adianta esperar 15 min.

        `VAGAS_WAIT_HUMAN=1/0` força o modo; sem isso, vale ter um terminal (tty).
        """
        v = (os.environ.get("VAGAS_WAIT_HUMAN") or "").strip().lower()
        if v in ("1", "true", "sim", "yes", "y"):
            return True
        if v in ("0", "false", "nao", "não", "no", "n"):
            return False
        try:
            return bool(sys.stdin and sys.stdin.isatty())
        except Exception:
            return False

    def wait_for_human(self, page: Page, reason: str, timeout_min: float | None = None,
                       auto_seconds: float | None = None) -> bool:
        """Espera o checkpoint sair. Muito checkpoint (captcha do Indeed) se resolve sozinho em segundos.

        Sem ninguém no teclado só vale a espera curta: depois dela desiste e o chamador pula a vaga.
        """
        # `auto_seconds` por chamada: login de site externo nunca se resolve sozinho, não vale 45 s
        auto_s = max(2.0, float(auto_seconds if auto_seconds is not None else self.settings.limits.checkpoint_auto_seconds))
        attended = self.attended()
        timeout = (timeout_min or self.settings.limits.human_checkpoint_timeout_minutes) * 60 if attended else auto_s
        if attended:
            console.print(f"[bold yellow]⏸  Preciso de você no navegador:[/] {reason}  (esperando até {int(timeout/60)} min)")
            sys.stdout.write("\a"); sys.stdout.flush()
        else:
            console.print(f"[yellow]⏸  checkpoint: {reason}  (sem ninguém no teclado: {int(auto_s)}s e pula)[/]")
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(2)
            try:
                if not self.detect_checkpoint(page):
                    console.print("[green]✔ Checkpoint resolvido, seguindo.[/]")
                    return True
            except Exception:
                pass
        console.print("[red]✖ Checkpoint não resolvido, pulando a vaga.[/]" if not attended
                      else "[red]✖ Tempo esgotado esperando o checkpoint.[/]")
        return False

    def ensure_ok(self, page: Page) -> bool:
        cp = self.detect_checkpoint(page)
        if not cp:
            return True
        return self.wait_for_human(page, f"resolver {cp} em {page.url}")

    def goto(self, page: Page, url: str, wait: str = "domcontentloaded") -> bool:
        page.goto(url, wait_until=wait)
        # LinkedIn às vezes manda a página da vaga para o "authwall" mesmo com sessão válida (li_at):
        # passar pelo feed e voltar resolve; só chama o humano se persistir
        for attempt in range(2):
            if not re.search(r"linkedin\.com/authwall", page.url or "", re.I):
                break
            time.sleep(2.5)
            try:
                page.goto("https://www.linkedin.com/feed/", wait_until=wait)
                time.sleep(2.0)
                page.goto(url, wait_until=wait)
            except Exception:
                break
        return self.ensure_ok(page)
