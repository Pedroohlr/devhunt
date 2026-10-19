"""Loop multi-etapas compartilhado por todos os appliers."""
from __future__ import annotations

import re
import time
from typing import Callable

from rich.console import Console

from ..errors import ResumeBuildError
from .. import forms, humanize as h
from ..browser import Page
from ..db import Job
from ..forms import ALREADY_RE, FlowResult, LIMIT_RE, SUCCESS_RE, FormField
from ..qa import NeedsUserInput
from .base import ApplyContext

console = Console()

COOKIE_RE = re.compile(
    r"^(aceit(ar|a|o|e)( todos| tudo| os cookies| todos os cookies| cookies)?( e (fechar|feche|continuar))?|"
    r"accept( all)?( cookies)?( and close)?|entendi|got it|concordo|ok|allow all|permitir todos|"
    r"continuar e aceitar|i agree|agree)$", re.I,
)
COOKIE_CONTAINERS = [
    "#onetrust-accept-btn-handler", "#onetrust-banner-sdk button", "[id*='cookie' i] button", "[class*='cookie' i] button",
    "[class*='consent' i] button", "[id*='consent' i] button", "[class*='lgpd' i] button", "[aria-label*='cookie' i] button",
]
RESUME_FILE_RE = re.compile(r"curr[ií]cul|resume|\bcv\b|anex", re.I)
# Página de BUSCA/lista de vagas do Indeed: o captcha do Cloudflare jogou a candidatura da SIT
# em br.indeed.com/jobs?…searchOnDesktopSerp e o laço passou 6 etapas clicando "Achar vagas" e
# "Ativar", criando ALERTA DE VAGAS na conta do usuário. Só vale com zero campos: o Easy Apply do
# LinkedIn roda legitimamente com a URL da busca (currentJobId), por isso nada de LinkedIn aqui.
JOB_SEARCH_URL_RE = re.compile(r"indeed\.com/jobs\?|from=searchOnDesktopSerp", re.I)
COVER_RE = re.compile(r"carta|cover letter|apresenta[cç][aã]o", re.I)


def dismiss_cookies(page: Page) -> bool:
    for sel in COOKIE_CONTAINERS:
        try:
            loc = page.locator(sel)
            for i in range(min(loc.count(), 8)):
                b = loc.nth(i)
                if not b.is_visible():
                    continue
                t = (b.inner_text(timeout=500) or "").strip()
                if sel.startswith("#onetrust-accept") or COOKIE_RE.match(t):
                    b.click(timeout=2000)
                    h.short()
                    return True
        except Exception:
            continue
    # reserva: banner sem id/classe "cookie" (Factorial: "Aceitar cookies"): qualquer botão visível com texto de aceite de cookies
    try:
        loc = page.locator("button, a[role='button'], [role='button']")
        for k in range(min(loc.count(), 60)):
            el = loc.nth(k)
            try:
                t = (el.inner_text(timeout=300) or "").strip()
                if re.fullmatch(r"(aceitar|accept|permitir|allow)( todos| tudo| all)?( os)?( cookies)?|aceitar todos os cookies|accept all cookies|aceitar e fechar", t, re.I) and el.is_visible():
                    el.click(timeout=2000)
                    time.sleep(0.4)
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False

def _select_file_fields(fields: list[FormField]) -> list[FormField]:
    """Decide quais inputs de arquivo recebem o currículo."""
    files = [f for f in fields if f.q.kind == "file"]
    if not files:
        return []
    resume_like = [f for f in files if RESUME_FILE_RE.search(f.q.label) and not COVER_RE.search(f.q.label)]
    if resume_like:
        return resume_like[:1]
    PHOTO_RE = re.compile(r"foto|photo|imagem|avatar|portf[oó]lio|picture", re.I)
    if len(files) == 1 and not COVER_RE.search(files[0].q.label) and not PHOTO_RE.search(files[0].q.label):
        return files
    # vários arquivos ("Foto" + "Selecionar arquivo*"): o obrigatório que não é foto/carta recebe o currículo
    cands = [f for f in files if f.q.required and not COVER_RE.search(f.q.label) and not PHOTO_RE.search(f.q.label)]
    if len(cands) == 1:
        return cands
    cands = [f for f in files if not COVER_RE.search(f.q.label) and not PHOTO_RE.search(f.q.label)]
    return cands[:1]


ENGLISH_ENV_RE = re.compile(
    r"ambiente (de trabalho )?(onde|em que|no qual) se fala ingl|"
    r"(comunicar|colaborar|trabalhar|atuar)[^.?]{0,40}\bem ingl[eê]s|"
    r"reuni[oõ]es?[^.?]{0,25}\bem ingl[eê]s|dia[- ]a[- ]dia[^.?]{0,25}\bem ingl[eê]s|"
    r"clientes?[^.?]{0,25}\bem ingl[eê]s|times? (globais|internacionais)[^.?]{0,30}ingl|"
    r"ingl[eê]s\s+(fluente|avan[çc]ado|nativo)",
    re.I,
)


def _host_blacklisted(url: str, lista) -> str:
    """Entrada da blacklist que casa com a URL atual, ou "".

    A blacklist é checada na descoberta, quando o link externo ainda não é conhecido: a vaga
    chega pelo LinkedIn/Indeed e só vira jobbol.com.br DENTRO da candidatura. Duas foram enviadas
    para a Jobbol em 11/09 por isso. Aqui a checagem é refeita com a URL real, antes de preencher.
    """
    u = (url or "").lower()
    for h in (lista or []):
        h = (h or "").strip().lower()
        if h and h in u:
            return h
    return ""


def run_form_flow(ctx: ApplyContext, page: Page, job: Job, *, ats: str, root_selector: str | None = None,
                  pre_step: Callable[[Page, int], None] | None = None, max_steps: int | None = None) -> FlowResult:
    max_steps = max_steps or ctx.settings.limits.max_form_steps
    answers_log: list[dict] = []
    prev_sig = None
    same_count = 0
    error_rounds = 0
    last_errors: list[str] = []
    fields: list[FormField] = []

    waited_for_modal = 0
    uploaded_via_chooser = False
    for step in range(max_steps):
        cp = ctx.browser.detect_checkpoint(page)
        if cp and not ctx.browser.wait_for_human(page, f"resolver {cp} para continuar a candidatura"):
            return FlowResult("failed", f"checkpoint não resolvido: {cp}", step, answers_log)
        dismiss_cookies(page)
        if pre_step:
            try:
                pre_step(page, step)
            except Exception:
                pass

        hb = _host_blacklisted(page.url, ctx.settings.filters.host_blacklist)
        if hb:
            return FlowResult("skipped", f"site na blacklist ('{hb}') — descoberto ao abrir o formulário", step, answers_log)
        text = forms.page_text(page, root_selector)
        if LIMIT_RE.search(text):
            return FlowResult("limit", "limite diário de candidaturas atingido", step, answers_log)
        if ALREADY_RE.search(text):
            return FlowResult("already_applied", "já aplicada anteriormente", step, answers_log)

        scope, fields = forms.best_scope(page, root_selector)
        root_for_scope = root_selector if scope is page else None
        if not fields and JOB_SEARCH_URL_RE.search(page.url or ""):
            return FlowResult("failed", "caiu na busca de vagas do Indeed (não é mais o formulário)", step, answers_log)
        # o formulário deixa claro que o trabalho é em inglês (a CI&T pergunta a confiança em
        # "ambiente de trabalho onde se fala inglês"): regra do usuário é não aplicar. Aborta ANTES de enviar.
        amb = next((f for f in fields if ENGLISH_ENV_RE.search(f.q.label or "")), None)
        if amb is not None:
            return FlowResult("skipped", f"formulário indica trabalho em inglês: '{(amb.q.label or '')[:60]}'", step, answers_log)
        forms.prefetch_combobox_options(scope, fields)

        invalid = forms.invalid_field_ids(scope) if last_errors else set()
        falhou_arquivo = False        # input de arquivo que recusou set_input_files (DBC): tenta o seletor do sistema
        to_fill: list[FormField] = []
        for f in fields:
            if f.q.kind == "file":
                continue
            if f.q.id in invalid or any(oid in invalid for oid in f.option_ids.values()):
                f.q.required = True
                f.q.current = ""
                if last_errors:
                    f.q.label = f"{f.q.label} (erro anterior: {last_errors[0][:120]})"
            if not f.is_filled:
                to_fill.append(f)
        file_fields = _select_file_fields(fields)
        to_fill.extend(f for f in file_fields if not f.is_filled)

        if to_fill:
            try:
                answers = ctx.qa.answer_batch([f.q for f in to_fill], job, skip_optional=(same_count == 0))
            except NeedsUserInput as e:
                forms.dump_learn(page, job.id, ats, fields, "needs_user", step)
                return FlowResult("needs_user", f"{len(e.questions)} pergunta(s) pendentes para você", step, answers_log)
            for f in to_fill:
                a = answers.get(f.q.id)
                if not a or a.source == "skip" or a.value in (None, ""):
                    console.print(f"[dim]  etapa {step}: sem resposta para '{f.q.label[:60]}' ({f.q.kind}{', obrigatório' if f.q.required else ''})[/]")
                    continue
                is_file = a.value == "__resume__"
                ok = forms.fill_field(scope, f, a.value, ctx.resume)
                answers_log.append({"q": f.q.label[:200], "a": ("<currículo>" if is_file else ("<senha>" if f.q.kind == "password" else a.value)), "source": a.source, "ok": ok})
                shown = "<currículo>" if is_file else ("<senha>" if f.q.kind == "password" else str(a.value)[:60].replace("\n", " "))
                console.print(f"[dim]  etapa {step}: {'✓' if ok else '✗'} {f.q.kind} '{f.q.label[:50]}' ← {shown} [{a.source}][/]")
                if not ok:
                    console.print(f"[yellow]  não consegui preencher: {f.q.label[:80]} ({f.q.kind})[/]")
                    if f.q.kind == "file":
                        falhou_arquivo = True
                h.pause(0.3, 1.2)

        # campos condicionais que apareceram depois do preenchimento (ex.: LinkedIn revela caixa de mensagem;
        # Quill Forms/InHire avança sozinho para a próxima pergunta ao escolher uma opção -> repete até estabilizar)
        known = {f.q.id for f in fields}
        rounds = 0
        while to_fill and rounds < 8:
            rounds += 1
            h.pause(0.6, 1.2)
            scope2, fields2 = forms.best_scope(page, root_selector)
            extra = [f for f in fields2 if f.q.id not in known and not f.is_filled and f.q.kind != "file"
                     and not re.search(r"\(\w{2,3}\)\s*$", f.q.label or "")]
            if not extra:
                break
            console.print(f"[dim]  etapa {step}: {len(extra)} campo(s) novo(s) após preencher[/]")
            try:
                answers2 = ctx.qa.answer_batch([f.q for f in extra], job, skip_optional=(same_count == 0))
            except NeedsUserInput as e:
                forms.dump_learn(page, job.id, ats, fields2, "needs_user", step)
                return FlowResult("needs_user", f"{len(e.questions)} pergunta(s) pendentes para você", step, answers_log)
            filled_any = False
            for f in extra:
                known.add(f.q.id)
                a = answers2.get(f.q.id)
                if not a or a.source == "skip" or a.value in (None, ""):
                    continue
                ok = forms.fill_field(scope2, f, a.value, ctx.resume)
                filled_any = filled_any or ok
                answers_log.append({"q": f.q.label[:200], "a": a.value, "source": a.source, "ok": ok})
                console.print(f"[dim]  etapa {step}: {'✓' if ok else '✗'} {f.q.kind} '{f.q.label[:50]}' ← {str(a.value)[:60]} [{a.source}][/]")
                h.pause(0.3, 1.0)
            scope, fields = scope2, fields2
            root_for_scope = root_selector if scope is page else None
            if not filled_any:
                break

        # depois de subir o currículo, ATS que "leem" o PDF (Breezy, Gupy) escondem botões/re-renderizam por alguns segundos
        if any(f.q.kind == "file" for f in to_fill):
            deadline = time.time() + 20
            while time.time() < deadline and not forms.find_action_button(scope, root_for_scope, include_disabled=True):
                time.sleep(1.0)
            h.short()
        # currículo sem <input type=file> no DOM (Notion Forms): botão "Upload" + seletor de arquivo do sistema
        if ctx.has_resume and not uploaded_via_chooser and (falhou_arquivo or not any(f.q.kind == "file" for f in fields)):
            try:
                if forms.upload_via_chooser(page, ctx.resume):
                    uploaded_via_chooser = True
                    console.print(f"[dim]  etapa {step}: ✓ currículo enviado pelo seletor de arquivos[/]")
                    answers_log.append({"q": "currículo (upload)", "a": "<currículo>", "source": "rule", "ok": True})
                elif falhou_arquivo:
                    # só é problema quando HAVIA input de arquivo e ele recusou o PDF. Em etapa sem
                    # campo de arquivo a tentativa é só precaução (Notion Forms) e não achar botão de
                    # upload é o normal: avisar ali gerava alarme falso a cada etapa (JEM, NSTECH).
                    console.print(f"[yellow]  etapa {step}: seletor de arquivos não resolveu o currículo[/]")
            except ResumeBuildError:
                raise
            except Exception:
                pass
        btn = forms.find_action_button(scope, root_for_scope, include_disabled=True)
        if btn and btn[1].startswith("disabled:"):
            # botão existe mas está desabilitado: falta algo obrigatório (ex.: cidade não escolhida na lista)
            error_rounds += 1
            missing = [f for f in fields if f.q.required and not f.is_filled and f.q.kind != "file"]
            console.print(f"[yellow]  etapa {step}: botão '{btn[1][9:]}' desabilitado; obrigatórios vazios: {[f.q.label[:30] for f in missing]}[/]")
            if error_rounds > 2 or not missing:
                forms.dump_learn(page, job.id, ats, fields, "botão desabilitado; obrigatórios: " + ", ".join(f.q.label[:40] for f in missing), step)
                return FlowResult("failed", "botão de avançar desabilitado (campos obrigatórios não aceitos)", step, answers_log)
            for f in missing:
                f.q.required = True
            try:
                answers = ctx.qa.answer_batch([f.q for f in missing], job, skip_optional=False)
            except NeedsUserInput as e:
                return FlowResult("needs_user", f"{len(e.questions)} pergunta(s) pendentes para você", step, answers_log)
            for f in missing:
                a = answers.get(f.q.id)
                if a and a.value not in (None, ""):
                    ok = forms.fill_field(scope, f, a.value, ctx.resume)
                    console.print(f"[dim]  etapa {step}: {'✓' if ok else '✗'} (retry) {f.q.kind} '{f.q.label[:40]}' ← {str(a.value)[:40]}[/]")
            h.short()
            continue
        if not btn:
            text = forms.page_text(page, root_selector) + ("\n" + forms.page_text(scope) if scope is not page else "")
            if SUCCESS_RE.search(text) or re.search(r"/(confirmation|confirmacao|thank-?you|obrigado|success|sucesso|candidatura-enviada)\b", page.url or "", re.I):
                return FlowResult("applied", "sucesso (sem botão restante)", step + 1, answers_log)
            if forms.CLOSED_RE.search(page.url or "") or (not fields and forms.CLOSED_RE.search(text[:2000])):
                return FlowResult("skipped", "vaga encerrada ou não encontrada no site da empresa", step, answers_log)
            if forms.REJECTED_RE.search(page.url or "") or forms.REJECTED_RE.search(text[:3000]):
                answers_log.append({"q": "resultado", "a": "reprovado automaticamente pelo ATS (pergunta eliminatória)", "source": "rule", "ok": True})
                return FlowResult("applied", "enviada; reprovada automaticamente (pergunta eliminatória)", step + 1, answers_log)
            if forms.CONFIRM_EMAIL_RE.search(text):
                answers_log.append({"q": "confirmação por e-mail", "a": "pendente (clique no link do e-mail)", "source": "rule", "ok": False})
                return FlowResult("applied", "enviada; confirmar pelo e-mail (registrado em Candidaturas)", step + 1, answers_log)
            if step > 0 and waited_for_modal < 2:
                # a etapa anterior pode estar enviando (botão somem enquanto processa) ou abrindo um questionário
                # embutido (iframe) que ainda não apareceu: espera até 45 s por botão, iframe-modal ou texto de sucesso
                waited_for_modal += 1
                console.print("[dim]  sem botão de avançar: aguardando envio/questionário embutido (até 45 s)[/]")
                deadline = time.time() + 45
                found = False
                while time.time() < deadline:
                    time.sleep(1.0)
                    modal, loading = forms._modal_frames(page)
                    if modal or loading or forms.find_action_button(page, root_for_scope):
                        found = True
                        break
                    if SUCCESS_RE.search(forms.page_text(page, root_selector)):
                        return FlowResult("applied", "sucesso (sem botão restante)", step + 1, answers_log)
                if found:
                    continue
            emb = forms.embedded_form_url(page)
            if emb and ats == "inhire":
                # "Continuar inscrição" já ENVIOU a candidatura; o questionário embutido não carregou/terminou
                answers_log.append({"q": "questionário embutido (responder manualmente)", "a": emb, "source": "rule", "ok": False})
                try:
                    ctx.db.add_pending_question(job.id, f"Questionário da InHire não carregou; responda manualmente em: {emb}", "link", [], "")
                except Exception:
                    pass
                return FlowResult("applied", "candidatura enviada; questionário embutido pendente (link nas respostas)", step + 1, answers_log)
            # última tentativa: a RESPOSTA são os próprios botões (DBC: "Atualmente você está
            # trabalhando?" com só "Sim"/"Não" na tela). Sem botão de avançar, clicar a opção É o avanço.
            try:
                textos = [t for t in forms._visible_buttons_text(scope)
                          if t and len(t) <= 24 and not forms.BAD_RE.search(t)]
            except Exception:
                textos = []
            # o campo pode estar "preenchido" com um valor inútil (a DBC escreveu "Não" num input de
            # texto que não avança nada), então vale também quando há UMA única pergunta na tela.
            perguntas = [f for f in fields if f.q.kind in ("text", "textarea", "radio", "select", "combobox")]
            vazias = [f for f in perguntas if not f.is_filled]
            alvo = vazias[0] if len(vazias) == 1 else (perguntas[0] if len(perguntas) == 1 else None)
            if 2 <= len(textos) <= 4 and alvo is not None:
                q = alvo.q
                q.kind, q.options = "radio", textos
                try:
                    resp = ctx.qa.answer_batch([q], job, skip_optional=False).get(q.id)
                except Exception:
                    resp = None
                escolha = str(resp.value).strip() if resp and resp.value else ""
                alvo = next((t for t in textos if t.strip().lower() == escolha.lower()), "")
                if alvo:
                    try:
                        page.get_by_role("button", name=re.compile(rf"^\s*{re.escape(alvo)}\s*$", re.I)).first.click(timeout=4000)
                        console.print(f"[dim]  etapa {step}: ✓ respondeu '{q.label[:40]}' clicando '{alvo}'[/]")
                        answers_log.append({"q": q.label[:200], "a": alvo, "source": "botão", "ok": True})
                        h.short()
                        continue
                    except Exception:
                        pass
            # lista suspensa esquecida aberta cobre o botão de enviar (Recrutaí: seletor de país)
            if forms.close_open_dropdowns(scope) and forms.find_action_button(scope, root_for_scope, include_disabled=True) is not None:
                console.print(f"[dim]  etapa {step}: fechei uma lista aberta que cobria a página[/]")
                continue
            console.print(f"[dim]  etapa {step}: sem avanço — botões visíveis: {textos[:4]} · perguntas: "
                          f"{[(f.q.label[:28], f.q.kind, bool(f.is_filled)) for f in perguntas][:3]}[/]")
            forms.dump_learn(page, job.id, ats, fields, "sem botão de avançar/enviar", step)
            return FlowResult("failed", "não achei botão de avançar/enviar", step, answers_log)
        loc, kind = btn
        if kind == "submit":
            if ctx.dry_run:
                forms.dump_learn(page, job.id, ats, fields, "dry_run (não enviado)", step)
                return FlowResult("dry_run", "dry-run: parado antes de enviar", step + 1, answers_log)
            if ctx.pause_before_submit and ctx.interactive and ctx.confirm and not ctx.confirm(f"Enviar candidatura para {job.title} @ {job.company}?"):
                return FlowResult("failed", "envio cancelado por você", step, answers_log)

        # texto visível entra na assinatura: formulários "uma pergunta por tela" (Quill Forms/InHire) mantêm os mesmos
        # campos no DOM e só mudam o bloco ativo/progresso; sem isso o loop acharia que está preso
        try:
            txt_sig = hash(((forms.page_text(page, root_selector) or "") + (forms.page_text(scope) if scope is not page else ""))[:6000])
        except Exception:
            txt_sig = 0
        sig = (page.url, tuple(f.q.id for f in fields), kind, txt_sig)
        errs_before = set(forms.errors(scope, root_for_scope))
        try:
            btn_text = loc.inner_text(timeout=1000).strip()[:30]
        except Exception:
            btn_text = "?"
        console.print(f"[dim]  etapa {step}: {len(fields)} campos, clicando '{btn_text}' ({kind})[/]")
        clicked = False
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                h.click(loc)
                clicked = True
                break
            except Exception as e:
                last_err = e
                # SPA re-renderizou o botão entre o scan e o clique (Gupy/React: "not attached", ou timeout no
                # locator antigo): acha de novo e tenta outra vez
                time.sleep(1.0)
                try:
                    scope, _ = forms.best_scope(page, root_selector, wait_modal_s=2, wait_loading_s=4)
                    root_for_scope = root_selector if scope is page else None
                except Exception:
                    pass
                again = forms.find_action_button(scope, root_for_scope)
                if not again:
                    break
                loc, kind = again
        if not clicked:
            forms.dump_learn(page, job.id, ats, fields, f"clique falhou: {last_err}", step)
            return FlowResult("failed", f"clique no botão falhou: {str(last_err)[:100]}", step, answers_log)
        h.pause(1.5, 3.5)
        try:
            page.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:
            pass

        # o frame pode ter sido trocado após o clique: re-resolve o escopo antes de ler texto/erros
        scope_after, _ = forms.best_scope(page, root_selector)
        text = forms.page_text(page, root_selector) or forms.page_text(page)
        if scope_after is not page:
            text += "\n" + forms.page_text(scope_after)   # tela de "obrigado" pode estar dentro do iframe (InHire/Quill Forms)
        if SUCCESS_RE.search(text) or (kind == "submit" and re.search(r"/(confirmation|confirmacao|thank-?you|obrigado|success|sucesso|applied|candidatura-enviada)\b", page.url or "", re.I)):
            return FlowResult("applied", "sucesso", step + 1, answers_log)
        if kind == "submit" and (forms.REJECTED_RE.search(page.url or "") or forms.REJECTED_RE.search(text[:3000])):
            # enviada, mas o ATS reprovou automaticamente (pergunta eliminatória respondida com a verdade)
            answers_log.append({"q": "resultado", "a": "reprovado automaticamente pelo ATS (pergunta eliminatória)", "source": "rule", "ok": True})
            return FlowResult("applied", "enviada; reprovada automaticamente (pergunta eliminatória)", step + 1, answers_log)
        if forms.CONFIRM_EMAIL_RE.search(text):
            # recebida, mas depende de um clique no e-mail: conta como enviada e vira pendência para o usuário
            answers_log.append({"q": "confirmação por e-mail", "a": "pendente (clique no link do e-mail)", "source": "rule", "ok": False})
            return FlowResult("applied", "enviada; confirmar pelo e-mail (registrado em Candidaturas)", step + 1, answers_log)
        if ALREADY_RE.search(text):
            return FlowResult("already_applied", "já aplicada anteriormente", step + 1, answers_log)
        if LIMIT_RE.search(text):
            return FlowResult("limit", "limite diário de candidaturas atingido", step + 1, answers_log)

        errs_all = forms.errors(scope_after, root_selector if scope_after is page else None)
        errs = [e for e in errs_all if e not in errs_before] or ([] if errs_all and sig != prev_sig else errs_all)
        if errs:
            error_rounds += 1
            last_errors = errs
            console.print(f"[yellow]  erros no formulário: {'; '.join(errs[:3])[:200]}[/]")
            mlen = re.search(r"(?:maior|mais|more|max(?:imum)?|exceed)\D{0,25}?(\d{2,5})\s*(?:caracteres|characters|chars)", " ".join(errs), re.I)
            if mlen:
                # "Não deve ser maior que 60 caracteres": trunca os textos longos demais
                limit = int(mlen.group(1))
                _, fields_now = forms.best_scope(page, root_selector)
                bad_ids = forms.invalid_field_ids(scope_after)
                targets = [f for f in fields_now if f.q.id in bad_ids] or [f for f in fields_now if f.q.kind == "text"]   # só o campo marcado; senão só inputs de 1 linha
                for f in targets:
                    cur = f.q.current or ""
                    if f.q.kind in ("text", "textarea") and len(cur) > limit:
                        short = cur[:limit].rsplit(" ", 1)[0].rstrip(",;:") if " " in cur[:limit] else cur[:limit]
                        ok = forms.fill_field(scope_after, f, short, ctx.resume)
                        console.print(f"[dim]  etapa {step}: {'✓' if ok else '✗'} truncado a {limit} '{f.q.label[:40]}'[/]")
            if re.search(r"phone number format|formato (do|de) telefone|telefone inv[aá]lid|invalid phone", " ".join(errs), re.I):
                # site com seletor de DDI separado (Phenom/NTT DATA): só o número nacional, só dígitos
                _, fields_now = forms.best_scope(page, root_selector)
                phone = re.sub(r"\D", "", str((ctx.profile.get("personal") or {}).get("phone") or ""))
                if phone.startswith("55") and len(phone) > 11:
                    phone = phone[2:]
                for f in fields_now:
                    if f.q.kind == "tel" or re.search(r"phone|telefone|celular", f.q.label or "", re.I):
                        if phone:
                            ok = forms.fill_field(scope_after, f, phone, ctx.resume)
                            console.print(f"[dim]  etapa {step}: {'✓' if ok else '✗'} telefone só dígitos '{f.q.label[:40]}' ← {phone}[/]")
            if re.search(r"n[uú]mero|decimal|numeric|number", " ".join(errs), re.I) and not re.search(r"phone|telefone", " ".join(errs), re.I):
                # respostas com letras em campos numéricos ("6 anos", "Sênior", "R$ 13.000/mês") -> número coerente
                from ..qa import numeric_fallback
                _, fields_now = forms.best_scope(page, root_selector)
                compact = ctx.qa._compact_for(job)
                for f in fields_now:
                    cur = f.q.current or ""
                    if f.q.kind in ("text", "number") and re.search(r"[A-Za-zÀ-ú]", cur):
                        new_val = numeric_fallback(compact, job, f.q.label, cur)
                        if new_val:
                            ok = forms.fill_field(scope_after, f, new_val, ctx.resume)
                            console.print(f"[dim]  etapa {step}: {'✓' if ok else '✗'} corrigido para número '{f.q.label[:40]}' ← {new_val}[/]")
                    elif f.q.kind in ("text", "number") and re.search(r"hora|hour", f.q.label or "", re.I) and re.fullmatch(r"\d{4,}", cur.strip()):
                        # valor MENSAL num campo de valor/HORA (13000 -> ~80)
                        new_val = numeric_fallback(compact, job, f.q.label, "")
                        if new_val:
                            ok = forms.fill_field(scope_after, f, new_val, ctx.resume)
                            console.print(f"[dim]  etapa {step}: {'✓' if ok else '✗'} corrigido valor/hora '{f.q.label[:40]}' ← {new_val}[/]")
            if error_rounds > 2:
                forms.dump_learn(page, job.id, ats, fields, "erros persistentes: " + "; ".join(errs[:5]), step)
                return FlowResult("failed", "erros no formulário: " + "; ".join(errs[:3])[:200], step, answers_log)
            continue
        last_errors = []
        if sig == prev_sig:
            same_count += 1
            if same_count >= 2:
                forms.dump_learn(page, job.id, ats, fields, "preso na mesma etapa", step)
                return FlowResult("failed", "preso na mesma etapa do formulário", step, answers_log)
        else:
            same_count = 0
        prev_sig = sig

    emb = forms.embedded_form_url(page)
    if emb and ats == "inhire":
        # o formulário já foi enviado ("Continuar inscrição"); o questionário embutido travou -> enviada + link pendente
        answers_log.append({"q": "questionário embutido (responder manualmente)", "a": emb, "source": "rule", "ok": False})
        try:
            ctx.db.add_pending_question(job.id, f"Questionário da InHire travou no meio; responda manualmente em: {emb}", "link", [], "")
        except Exception:
            pass
        return FlowResult("applied", "candidatura enviada; questionário embutido pendente (link nas respostas)", max_steps, answers_log)
    forms.dump_learn(page, job.id, ats, fields, "excedeu max_form_steps", max_steps)
    return FlowResult("failed", f"excedeu {max_steps} etapas", max_steps, answers_log)
