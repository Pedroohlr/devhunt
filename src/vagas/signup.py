"""Criação de conta em ATS/portais que exigem cadastro (com o e-mail de cadastros do usuário).

Fluxo: abre a URL de cadastro -> preenche o formulário com o motor genérico (e-mail de cadastros,
senha do .env, dados do perfil) -> envia -> se pedir confirmação/código por e-mail, resolve pela caixa
de entrada -> marca o host como "conta criada" no kv.
"""
from __future__ import annotations

import os
import re
import time
from urllib.parse import urlparse

from rich.console import Console

from . import forms, humanize as h, mailbox
from .browser import Browser, Page
from .db import DB, Job

console = Console()

SIGNUP_LINK_RE = re.compile(r"criar (uma )?conta|cadastre-se|cadastrar|registre-se|registrar|sign ?up|create (an )?account|nova conta|novo cadastro", re.I)
LOGIN_LINK_RE = re.compile(r"^(entrar|fazer login|log ?in|sign ?in|acessar( conta)?)$", re.I)
ACCOUNT_OK_RE = re.compile(
    r"conta criada|cadastro (realizado|conclu[ií]do|efetuado)|bem-?vindo|welcome|verifique (seu|o) e-?mail|"
    r"enviamos (um )?(e-?mail|link|c[oó]digo)|confirme (seu|o) e-?mail|account created|check your e-?mail", re.I,
)
CODE_FIELD_RE = re.compile(r"c[oó]digo|code|token|pin|verifica", re.I)
# a conta já existe (cadastro anterior nosso): tratar como sucesso e seguir para o login
ALREADY_ACCOUNT_RE = re.compile(
    r"e-?mail j[aá] (est[aá] (sendo )?(utilizado|em uso|cadastrado)|cadastrado|existe)|j[aá] (existe|possui) (uma )?conta|"
    r"already (registered|in use|exists|have an account)|usu[aá]rio j[aá] cadastrado", re.I,
)


def account_password() -> str:
    return (os.environ.get("ATS_SIGNUP_PASSWORD") or "").strip()


def host_of(url: str) -> str:
    return urlparse(url or "").netloc


def mark_account_created(db: DB, host: str, email: str) -> None:
    db.kv_set(f"account_created:{host}", email)
    db.kv_set(f"needs_account:{host}", "")   # não pular mais por falta de conta


def has_account(db: DB, host: str) -> bool:
    return bool(db.kv_get(f"account_created:{host}"))


EMAIL_ENTRY_RE = re.compile(r"(fazer login|entrar|continuar|continue|sign ?in) com (e-?mail|email)|com e-?mail|with email", re.I)


def _click_email_entry(page: Page) -> bool:
    """Telas que oferecem Google/LinkedIn/e-mail (Workday, TOTVS): escolhe o caminho por e-mail."""
    try:
        if page.locator("input[type='password']").count():
            return False
        el = page.locator("button, a").filter(has_text=EMAIL_ENTRY_RE).first
        if el.count() and el.is_visible():
            h.click(el)
            time.sleep(3)
            return True
    except Exception:
        pass
    return False


def _click_signup_entry(page: Page) -> bool:
    """Na tela de login, vai para o cadastro ('Criar conta', 'Cadastre-se')."""
    _click_email_entry(page)
    for role in ("link", "button"):
        try:
            loc = page.get_by_role(role, name=SIGNUP_LINK_RE)
            for i in range(min(loc.count(), 4)):
                el = loc.nth(i)
                if el.is_visible():
                    h.click(el)
                    time.sleep(2.5)
                    return True
        except Exception:
            continue
    for sel in ("a, button", "span, div, p, li"):
        try:
            el = page.locator(sel).filter(has_text=SIGNUP_LINK_RE).last
            if el.count() and el.is_visible():
                txt = (el.inner_text(timeout=800) or "").strip()
                if len(txt) <= 60:
                    h.click(el)
                    time.sleep(3)
                    return True
        except Exception:
            continue
    return False



# no cadastro o botão de envio é "Criar conta"/"Cadastrar" (que o motor de vagas evita de propósito)
SIGNUP_SUBMIT_RE = re.compile(
    r"^(criar( minha)? conta( gr[aá]tis| gratuita)?|cadastrar(-me)?|cadastre-me|finalizar cadastro|registrar(-me)?|"
    r"criar cadastro|sign ?up|create( my)? account|register|continuar|avan[cç]ar|pr[oó]xim[oa]|enviar|salvar|confirmar)$", re.I,
)


ICON_TEXT_RE = re.compile(r"\s*(visibility(_off)?|arrow_\w+|expand_\w+|close|menu|search|edit|done|check|help|info|\W*)\s*", re.I)


def _submit_button(page: Page):
    """Botão de enviar do cadastro (não usa forms.find_action_button: lá 'cadastrar' é propositalmente ignorado)."""
    best = None
    try:
        els = page.locator("button, input[type='submit'], a[role='button'], [role='button']")
        n = min(els.count(), 40)
    except Exception:
        return None
    for i in range(n):
        el = els.nth(i)
        try:
            if not el.is_visible() or el.is_disabled():
                continue
            t = ((el.inner_text(timeout=400) or "") or (el.get_attribute("value") or "")).strip()
            if not t or not SIGNUP_SUBMIT_RE.match(t):
                continue
            if re.search(r"c[oó]digo por e-?mail|com senha|google|linkedin|facebook|apple", t, re.I):
                continue
            box = el.bounding_box() or {"y": 0}
            if best is None or box["y"] > best[0]:
                best = (box["y"], el)
        except Exception:
            continue
    if best:
        return best[1]
    # sem texto reconhecível: botão de submit do formulário que tem a senha
    # (ignora ícones do próprio campo: visibility_off, olho de mostrar senha, etc.)
    try:
        form = page.locator("form:has(input[type='password'])").last
        if form.count():
            btns = form.locator("button[type='submit'], input[type='submit'], button")
            for i in range(min(btns.count(), 20) - 1, -1, -1):
                b = btns.nth(i)
                if not b.is_visible() or b.is_disabled():
                    continue
                t = ((b.inner_text(timeout=400) or "") or (b.get_attribute("value") or "")).strip()
                if ICON_TEXT_RE.fullmatch(t) or re.search(r"voltar|cancelar|back|cancel", t, re.I):
                    continue
                return b
    except Exception:
        pass
    return None



TERMS_RE = re.compile(r"pol[ií]tica de privacidade|termos? (de uso|e condi)|aceit|concordo|li e (aceito|concordo)|privacy policy|terms", re.I)


def accept_terms(page: Page) -> int:
    """Marca caixas de aceite (termos/privacidade) em qualquer lugar da página, inclusive fora do <form>."""
    n = 0
    try:
        n = page.evaluate("""() => { let n = 0;
          const labelOf = (cb) => { let t = '';
            if (cb.id) { const l = document.querySelector(`label[for="${CSS.escape(cb.id)}"]`); if (l) t = l.innerText || ''; }
            if (!t && cb.closest('label')) t = cb.closest('label').innerText || '';
            if (!t && cb.parentElement) t = (cb.parentElement.innerText || '').slice(0, 200);
            return t; };
          const RE = /pol[ií]tica de privacidade|termos? (de uso|e condi)|aceit|concordo|li e (aceito|concordo)|privacy policy|terms/i;
          const NO = /newsletter|promo|marketing|novidades|ofertas|receber e-?mails? (sobre|com)/i;
          for (const cb of document.querySelectorAll('input[type=checkbox], button[role=checkbox]')) {
            const t = labelOf(cb);
            if (!RE.test(t) || NO.test(t)) continue;
            const checked = cb.checked || cb.getAttribute('aria-checked') === 'true';
            if (checked) continue;
            (cb.id ? (document.querySelector(`label[for="${CSS.escape(cb.id)}"]`) || cb) : cb).click();
            if (!(cb.checked || cb.getAttribute('aria-checked') === 'true')) cb.click();
            n++;
          }
          return n; }""")
    except Exception:
        return 0
    return int(n or 0)


def _synthetic_job(db: DB) -> Job:
    """Job 'de mentira' só para as regras de QA responderem dados pessoais no cadastro."""
    j = Job.__new__(Job)
    j.id = "signup"; j.source = "signup"; j.external_id = "signup"; j.url = ""; j.title = "Cadastro de candidato"
    j.company = ""; j.location = "Brasil"; j.description = ""; j.posted_at = None; j.discovered_at = None
    j.apply_type = "signup"; j.external_url = None; j.ats = None; j.classification = {"seniority": "senior"}
    j.status = "signup"; j.reason = None; j.attempts = 0; j.resume_path = None; j.applied_at = None; j.updated_at = None
    return j


PWD_MAX_RE = re.compile(r"(?:n[aã]o (?:mais que|exceder)|no more than|m[aá]ximo de|at most|at[eé])\s*(\d{1,2})\s*caracteres?|maximum of\s*(\d{1,2})\s*characters?", re.I)


def site_password(page: Page, password: str) -> str:
    """Senha do .env cortada no limite que o site aceita (SuccessFactors/EY: 18).

    Truncar é determinístico: o mesmo .env sempre gera a mesma senha para o mesmo site,
    então o login futuro funciona sem guardar nada em banco/log.
    """
    limit = None
    try:
        ml = page.evaluate(
            "() => { const i=[...document.querySelectorAll(\"input[type=password]\")].find(e=>e.maxLength>0); return i ? i.maxLength : 0; }"
        )
        if ml and 6 <= int(ml) < len(password):
            limit = int(ml)
    except Exception:
        pass
    if limit is None:
        try:
            m = PWD_MAX_RE.search(forms.page_text(page)[:6000])
        except Exception:
            m = None
        if m:
            n = int(m.group(1) or m.group(2))
            if 6 <= n < len(password):
                limit = n
    return password[:limit] if limit else password


def fill_signup_form(rt, page: Page, *, email: str, password: str, max_steps: int = 4) -> bool:
    """Preenche e envia o formulário de cadastro. True se a página indicou sucesso."""
    job = _synthetic_job(rt.db)
    password = site_password(page, password)
    for step in range(max_steps):
        scope, fields = forms.best_scope(page)
        fields = [f for f in fields if not f.is_filled or f.q.kind in ("password", "email")]
        if not fields and step:
            break
        forms.prefetch_combobox_options(scope, [f for f in fields if f.q.kind == "combobox"])
        ask = [f for f in fields if f.q.kind not in ("file",)]
        answers = {}
        if ask:
            try:
                answers = rt.qa.answer_batch([f.q for f in ask], job)
            except Exception:
                answers = {}
        for f in fields:
            lab = (f.q.label or "").lower()
            val = None
            if f.q.kind == "password":
                val = password
            elif f.q.kind == "email" or (f.q.kind in ("text",) and re.search(r"e-?mail", lab)):
                val = email
            elif f.q.kind == "checkbox":
                # SÓ aceite de termos/privacidade. Nunca marcar caixa que declara um fato
                # sobre a pessoa (PCD, reabilitado pelo INSS, veterano, etc.) — isso é papel do qa.
                if TERMS_RE.search(f.q.label or ""):
                    val = True
                else:
                    a = answers.get(f.q.id)
                    val = a.value if a and a.value not in (None, "") else None
            elif f.q.kind == "radio" and re.search(r"professional|candidat|profissional|pessoa f[ií]sica|talento", " ".join(f.q.options).lower() if f.q.options else ""):
                val = next((o for o in f.q.options if re.search(r"professional|candidat|profissional|pessoa f[ií]sica|talento", o, re.I)), None)
            else:
                a = answers.get(f.q.id)
                val = a.value if a and a.value not in (None, "") else None
            if val in (None, ""):
                continue
            ok = forms.fill_field(scope, f, val, None)
            shown = "<senha>" if f.q.kind == "password" else str(val)[:40]
            console.print(f"[dim]  cadastro {step}: {'✓' if ok else '✗'} {f.q.kind} '{f.q.label[:40]}' ← {shown}[/]")
            h.pause(0.2, 0.6)
        acc = accept_terms(page)
        if acc:
            console.print(f"[dim]  cadastro {step}: ✓ {acc} aceite(s) de termos marcados[/]")
        btn = _submit_button(page)
        if btn is None:
            break
        try:
            label = (btn.inner_text(timeout=1000) or "").strip()[:28]
        except Exception:
            label = "?"
        console.print(f"[dim]  cadastro {step}: clicando '{label}'[/]")
        try:
            h.click(btn)
        except Exception:
            break
        h.pause(2, 4)
        text = forms.page_text(page)[:4000]
        if ACCOUNT_OK_RE.search(text):
            return True
        if ALREADY_ACCOUNT_RE.search(text) or any(ALREADY_ACCOUNT_RE.search(e) for e in forms.errors(page)):
            console.print("[dim]  cadastro: e-mail já cadastrado nesse site (conta existe)[/]")
            return True
        errs = forms.errors(page)
        if errs:
            console.print(f"[yellow]  cadastro: {'; '.join(errs[:3])[:160]}[/]")
    return bool(ACCOUNT_OK_RE.search(forms.page_text(page)[:4000]))


def confirm_by_email(rt, page: Page, host: str) -> str | None:
    """Resolve confirmação por e-mail: abre o link ou preenche o código pedido na página."""
    query = host.split(".")[-2] if host.count(".") >= 1 else host
    # 1) a página pede um código?
    scope, fields = forms.best_scope(page)
    code_field = next((f for f in fields if CODE_FIELD_RE.search(f.q.label or "") and f.q.kind in ("text", "number")), None)
    if code_field is not None:
        mail = rt.browser.new_page()
        try:
            code = mailbox.find_code(mail, query, wait_s=150)
        finally:
            try: mail.close()
            except Exception: pass
        if code:
            forms.fill_field(scope, code_field, code, None)
            btn = forms.find_action_button(page)
            if btn:
                h.click(btn[0]); h.pause(2, 4)
            return f"código {code[:2]}…"
        return None
    # 2) link de confirmação
    url = mailbox.open_confirmation(rt.browser, query, wait_s=150)
    return f"link aberto: {url[:70]}" if url else None



def _has_captcha(page: Page) -> bool:
    try:
        return bool(page.evaluate("() => /turnstile|cf-challenge|recaptcha|hcaptcha/i.test(document.documentElement.innerHTML)"))
    except Exception:
        return False


def _logged_in(page: Page) -> bool:
    """Heurística de sessão iniciada: sumiu o formulário de senha e apareceu menu/saudação."""
    try:
        txt = forms.page_text(page)[:4000]
        has_pwd = bool(page.locator("input[type='password']").count())
        return (not has_pwd) and bool(re.search(r"\bsair\b|logout|logoff|meu perfil|minha conta|ol[aá],|bem-?vindo|minhas candidaturas|preencha o formul[aá]rio", txt, re.I))
    except Exception:
        return False



LOGIN_SUBMIT_RE = re.compile(r"^\s*(fazer login|login|entrar|acessar|sign ?in|log ?in)\s*$", re.I)


def login(rt, url: str, *, email: str | None = None, password: str | None = None) -> bool:
    """Entra na conta de cadastros no site (sessão fica salva no perfil do Chrome)."""
    email = email or mailbox.signup_email(); password = password or account_password()
    page = rt.browser.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        h.pause(2, 4)
        from .appliers.flow import dismiss_cookies
        dismiss_cookies(page)
        if _click_email_entry(page):   # Workday/TOTVS: escolher o caminho por e-mail antes do formulário
            h.pause(1, 2)
        scope, fields = forms.best_scope(page)
        pwd = next((f for f in fields if f.q.kind == "password"), None)
        mail = next((f for f in fields if f.q.kind == "email" or re.search(r"e-?mail|usu[aá]rio", f.q.label or "", re.I)), None)
        if pwd is None or mail is None:
            return _logged_in(page)
        forms.fill_field(scope, mail, email, None)
        forms.fill_field(scope, pwd, site_password(page, password), None)
        btn = None
        for loc in (
            page.locator("[data-automation-id='signInSubmitButton']"),
            page.get_by_role("button", name=LOGIN_SUBMIT_RE),
            page.locator("button, input[type=submit]").filter(has_text=LOGIN_SUBMIT_RE),
        ):
            try:
                b = loc.first
                if b.count() and b.is_visible():
                    btn = b
                    break
            except Exception:
                continue
        if btn is None:
            btn = _submit_button(page)
        if btn is not None:
            h.click(btn)
        else:
            page.keyboard.press("Enter")
        h.pause(3, 6)
        return _logged_in(page) or not page.locator("input[type='password']").count()
    finally:
        try:
            page.close()
        except Exception:
            pass



def wait_captcha_solved(page: Page, minutes: float = 4.0) -> bool:
    """Espera VOCÊ resolver o captcha na janela: token do Turnstile/reCAPTCHA preenchido ou botão de envio liberado.
    (browser.wait_for_human só reconhece checkpoints de LinkedIn/Indeed/Gupy e voltaria na hora.)"""
    console.print(f"[bold yellow]⏸  Resolva o captcha na janela do Chrome (aguardando até {int(minutes)} min)…[/]")
    deadline = time.time() + minutes * 60
    while time.time() < deadline:
        time.sleep(3)
        try:
            solved = page.evaluate("""() => {
              const t = document.querySelector('input[name="cf-turnstile-response"], textarea[name="g-recaptcha-response"], input[name="h-captcha-response"]');
              if (t && (t.value || '').length > 20) return true;
              const b = Array.from(document.querySelectorAll('button')).find(e => /criar conta|cadastrar|sign ?up|registrar|continuar|avan[cç]ar/i.test(e.innerText||''));
              return !!(b && !b.disabled);
            }""")
        except Exception:
            solved = False
        if solved:
            console.print("[green]✔ captcha resolvido, seguindo.[/]")
            return True
    console.print("[yellow]tempo de espera do captcha esgotado.[/]")
    return False


def create_account(rt, url: str, captcha_wait_min: float = 4.0) -> tuple[bool, str]:
    """Cria a conta no site da URL. Devolve (ok, mensagem)."""
    email, password = mailbox.signup_email(), account_password()
    if not email or not password:
        return False, "faltam SIGNUP_EMAIL/ATS_SIGNUP_PASSWORD no .env"
    host = host_of(url)
    if has_account(rt.db, host):
        return True, f"conta já existia em {host}"
    page = rt.browser.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        h.pause(2, 4)
        from .appliers.flow import dismiss_cookies
        dismiss_cookies(page)
        scope, fields = forms.best_scope(page)
        if not any(f.q.kind == "password" for f in fields) or len(fields) <= 2:
            _click_signup_entry(page)
        ok = fill_signup_form(rt, page, email=email, password=password)
        if not ok and (ALREADY_ACCOUNT_RE.search(forms.page_text(page)[:4000]) or _logged_in(page)):
            ok = True
        if not ok and _has_captcha(page):
            # captcha (Cloudflare Turnstile/reCAPTCHA): nunca burlar; se você estiver por perto, resolve na janela
            console.print(f"[yellow]  {host}: o cadastro tem captcha. Resolva na janela do Chrome e clique em criar conta.[/]")
            try:
                from .notify import notify
                notify(f"🔐 Captcha no cadastro em {host}. Resolva na janela do Chrome (aguardando {int(captcha_wait_min)} min).")
            except Exception:
                pass
            if wait_captcha_solved(page, captcha_wait_min):
                h.pause(2, 4)
                btn = _submit_button(page)
                if btn is not None:
                    try:
                        h.click(btn); h.pause(2, 4)
                    except Exception:
                        pass
                ok = bool(ACCOUNT_OK_RE.search(forms.page_text(page)[:4000])) or _logged_in(page)
        extra = ""
        if ok or ACCOUNT_OK_RE.search(forms.page_text(page)[:3000]):
            done = confirm_by_email(rt, page, host)
            extra = f" ({done})" if done else " (sem confirmação por e-mail)"
            mark_account_created(rt.db, host, email)
            try:
                if login(rt, url):
                    extra += " · sessão iniciada"
            except Exception:
                pass
            return True, f"conta criada em {host}{extra}"
        return False, f"não consegui concluir o cadastro em {host}"
    finally:
        try:
            page.close()
        except Exception:
            pass
