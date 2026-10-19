"""CLI: vagas <comando>."""
from __future__ import annotations

import json
import shutil
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.prompt import Confirm, Prompt
from rich.table import Table

from . import __version__
from .config import CONFIG_DIR, DATA_DIR, ROOT, Settings, load_settings, resolve

app = typer.Typer(help="Auto-aplicador pessoal de vagas.", no_args_is_help=True, add_completion=False)
profile_app = typer.Typer(help="Perfil mestre (config/profile.yaml).")
app.add_typer(profile_app, name="profile")
console = Console()


# ---------- helpers ----------
def _settings() -> Settings:
    return load_settings()


def _db(s: Settings):
    from .db import DB
    return DB(s.db_path)


def _llm(s: Settings, db=None):
    import os
    if not os.environ.get("OPENAI_API_KEY"):
        console.print("[red]OPENAI_API_KEY não definida. Crie o arquivo .env (veja .env.example).[/]")
        raise typer.Exit(2)
    from .llm import LLM
    return LLM(s.llm, db)


def _profile(s: Settings) -> dict:
    from .resume.profile import load_profile
    try:
        return load_profile(s.profile_path)
    except FileNotFoundError as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(2)


def _ask_user(field, suggested):
    from .qa import FieldQ
    f: FieldQ = field
    console.print(f"\n[bold yellow]❓ Pergunta do formulário[/] [dim]({f.kind}{', obrigatória' if f.required else ''})[/]")
    console.print(f"   {f.label}")
    if f.options:
        for i, o in enumerate(f.options[:40], 1):
            console.print(f"   [dim]{i}.[/] {o}")
        ans = Prompt.ask("   Resposta (número ou texto; vazio = pular)", default=str(suggested or ""), show_default=bool(suggested))
        if ans.strip().isdigit() and 1 <= int(ans) <= len(f.options):
            return f.options[int(ans) - 1]
        return ans.strip() or None
    ans = Prompt.ask("   Resposta (vazio = pular)", default=str(suggested or ""), show_default=bool(suggested))
    return ans.strip() or None


def _confirm(msg: str) -> bool:
    return Confirm.ask(f"[bold]{msg}[/]", default=True)


class Runtime:
    def __init__(self, interactive: bool = False, dry_run: bool = False, need_browser: bool = True):
        from .browser import Browser
        from .pipeline import Pipeline
        from .qa import AnswerBank, QAEngine
        self.s = _settings()
        self.db = _db(self.s)
        self.llm = _llm(self.s, self.db)
        self.profile = _profile(self.s)
        self.bank = AnswerBank(self.s.answers_path)
        self.qa = QAEngine(self.s.llm, self.llm, self.db, self.profile, self.bank, interactive=interactive, ask_user=_ask_user,
                           seniority_factors=self.s.resume.seniority_years_factor)
        self.browser = Browser(self.s).start() if need_browser else None
        if self.browser:
            import signal
            def _bye(signum, frame):   # SIGTERM (painel "Parar", kill): fecha o Chrome junto para não deixar janela órfã segurando o perfil
                try:
                    self.close()
                finally:
                    raise SystemExit(130)
            signal.signal(signal.SIGTERM, _bye)
        self.pipeline = Pipeline(self.s, self.db, self.browser, self.llm, self.profile, self.qa,
                                 dry_run=dry_run, interactive=interactive, confirm=_confirm) if need_browser else None

    def close(self):
        if self.browser:
            self.browser.stop()


# ---------- comandos ----------
@app.command()
def setup(secao: str = typer.Option("tudo", "--secao", "-s",
                                    help="pessoal | documentos | diversidade | carreira | skills | formacao | preferencias | chaves")):
    """Assistente: pergunta seus dados e escreve config/profile.yaml e .env. Comece por aqui."""
    from .setup import SECOES, run_setup
    if secao != "tudo" and secao not in SECOES:
        console.print(f"[red]Seção desconhecida: {secao}[/]\nUse uma de: tudo, {', '.join(SECOES)}")
        raise typer.Exit(2)
    run_setup(secao)


@app.command()
def init():
    """Só copia os arquivos de exemplo (config/profile.yaml, answers.yaml, .env). Prefira `vagas setup`."""
    pairs = [
        (CONFIG_DIR / "profile.example.yaml", CONFIG_DIR / "profile.yaml"),
        (CONFIG_DIR / "answers.example.yaml", CONFIG_DIR / "answers.yaml"),
        (ROOT / ".env.example", ROOT / ".env"),
    ]
    for src, dst in pairs:
        if dst.exists():
            console.print(f"[dim]já existe: {dst.relative_to(ROOT)}[/]")
        else:
            shutil.copy(src, dst)
            console.print(f"[green]criado:[/] {dst.relative_to(ROOT)}")
    for d in ("resumes", "profile", "learn", "evidence", "logs"):
        (DATA_DIR / d).mkdir(parents=True, exist_ok=True)
    console.print("\nPróximos passos:\n  1. Edite [bold].env[/] com OPENAI_API_KEY\n  2. Preencha [bold]config/profile.yaml[/] (ou rode `vagas setup`, que pergunta campo a campo)\n  3. `vagas login` para logar no LinkedIn/Indeed/Gupy no perfil do navegador\n  4. `vagas search` e depois `vagas apply --interactive --dry-run`")


@app.command()
def doctor():
    """Verifica ambiente: chave, perfil, navegador, display."""
    import os
    s = _settings()
    ok = lambda b: "[green]ok[/]" if b else "[red]falta[/]"
    key = os.environ.get('OPENAI_API_KEY', '')
    console.print(f"OPENAI_API_KEY: {ok(bool(key) and key != 'sk-...')}")
    console.print(f"config/profile.yaml: {ok(s.profile_path.exists())}")
    console.print(f"config/answers.yaml: {ok(s.answers_path.exists())} [dim](opcional)[/]")
    console.print(f"DISPLAY/WAYLAND: {ok(bool(os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')))} [dim](necessário para janela do navegador no WSL)[/]")
    try:
        from .browser import USING_PATCHRIGHT
        console.print(f"patchright: {ok(USING_PATCHRIGHT)} [dim](fork stealth do Playwright)[/]")
    except Exception as e:
        console.print(f"navegador: [red]{e}[/]")
    console.print(f"browser.mode={s.browser.mode} channel={s.browser.channel or 'chromium'} user_data_dir={resolve(s.browser.user_data_dir)}")
    if s.browser.mode == "persistent":
        exe = Path(s.browser.executable_path).expanduser() if s.browser.executable_path else None
        found_exe = bool(exe and exe.exists())
        found_sys = any(Path(p).exists() for p in ("/usr/bin/google-chrome", "/usr/bin/google-chrome-stable", "/opt/google/chrome/chrome"))
        console.print(f"Google Chrome real: {ok(found_exe or found_sys)} [dim]({exe if found_exe else 'ver README > Navegador'}; sem ele cai no Chromium, que reprova em Cloudflare)[/]")
    console.print(f"Telegram: {ok(bool(os.environ.get('TELEGRAM_BOT_TOKEN')))} [dim](opcional)[/]")


LOGIN_SITES = {
    "linkedin": "https://www.linkedin.com/login",
    "indeed": "https://br.indeed.com/",
    "gupy": "https://login.gupy.io/candidates/signin",
    "recrutei": "https://talent.recrutei.com.br/login",
}


@app.command()
def login(site: list[str] = typer.Option([], "--site", help="linkedin | indeed | gupy | recrutei (padrão: linkedin, indeed, gupy)")):
    """Abre o navegador (perfil persistente) para você logar no LinkedIn, Indeed e Gupy uma vez."""
    from .browser import Browser
    s = _settings()
    sites = [x.lower() for x in site] or ["linkedin", "indeed", "gupy"]   # recrutei só quando pedido
    b = Browser(s).start()
    page = b.page()
    for name in sites:
        url = LOGIN_SITES.get(name)
        if not url:
            console.print(f"[yellow]site desconhecido: {name}[/]"); continue
        page.goto(url)
        console.print(f"Aberto: {url}")
        if name == "gupy":
            console.print("[dim]Gupy: preencha e-mail e senha, clique em 'Acessar conta', marque 'Confirme que é humano' e clique em 'Acessar conta' DE NOVO.\n"
                          "Alternativas na mesma tela: 'LinkedIn' (usa a sessão já logada) ou 'Entrar sem senha' (cole o link do e-mail nesta janela).[/]")
        Prompt.ask("Faça login nessa aba e pressione Enter quando terminar", default="")
        try:
            cp = b.detect_checkpoint(page)
            console.print(f"[green]ok[/] {page.url[:80]}" if not cp else f"[yellow]ainda parece página de login/captcha: {page.url[:80]}[/]")
        except Exception:
            pass
    console.print("[green]Sessões salvas no perfil do navegador.[/]")
    b.stop()


@app.command()
def search(source: Optional[str] = typer.Option(None, help="linkedin | indeed | gupy"),
           no_prepare: bool = typer.Option(False, help="só descobrir, sem abrir cada vaga e classificar")):
    """Descobre vagas das últimas 24h e (por padrão) classifica cada uma."""
    rt = Runtime()
    try:
        counts = rt.pipeline.discover(only_source=source)
        console.print(f"\nNovas por busca: {json.dumps(counts, ensure_ascii=False)}")
        if not no_prepare:
            n, q = rt.pipeline.prepare_all()
            console.print(f"\n[bold]{n} analisadas, {q} na fila.[/]")
            _print_funnel(rt.db)
    finally:
        rt.close()


@app.command()
def prepare(limit: Optional[int] = typer.Option(None), workers: int = typer.Option(2, help="threads para o LinkedIn público")):
    """Classifica as vagas ainda 'new' (sem refazer a busca)."""
    rt = Runtime()
    try:
        n, q = rt.pipeline.prepare_all(limit=limit, http_workers=workers)
        console.print(f"\n[bold]{n} analisadas, {q} na fila.[/]")
        _print_funnel(rt.db)
    finally:
        rt.close()


def _print_funnel(db) -> None:
    from collections import Counter
    rows = db.conn.execute("SELECT status, reason FROM jobs WHERE replace(discovered_at,'T',' ') >= datetime('now','localtime','-1 day')").fetchall()
    c = Counter(r[0] for r in rows)
    console.print(f"[bold]Funil (24h):[/] {dict(c)}")
    reasons = Counter((r[1] or "").split(":")[0][:50] for r in rows if r[0] == "skipped")
    for k, v in reasons.most_common(10):
        console.print(f"   [dim]{v:4}[/] {k}")


@app.command()
def apply(limit: Optional[int] = typer.Option(None, help="máximo de vagas nesta execução"),
          job: list[str] = typer.Option([], help="id(s) de vaga específicos (aceita prefixo)"),
          interactive: bool = typer.Option(False, "--interactive", "-i", help="pergunta no terminal quando não souber responder"),
          dry_run: bool = typer.Option(False, help="preenche tudo mas não envia"),
          force: bool = typer.Option(False, "--force", help="com --job: ignora as regras de 24h/remoto para essas vagas"),
          search_first: bool = typer.Option(False, "--search", help="descobrir + classificar antes de aplicar")):
    """Aplica nas vagas da fila (status new/queued), respeitando ritmo e limites diários."""
    rt = Runtime(interactive=interactive, dry_run=dry_run)
    try:
        if search_first:
            rt.pipeline.discover()
            rt.pipeline.prepare_all()
        if not job:
            n, q = rt.pipeline.prepare_all()
            if n:
                console.print(f"[dim]{n} vagas novas analisadas, {q} entraram na fila[/]")
        summary = rt.pipeline.process_queue(limit=limit, job_ids=job or None, force=force)
        _print_summary(summary)
    finally:
        rt.close()


@app.command()
def run(interactive: bool = typer.Option(False, "--interactive", "-i")):
    """Modo contínuo: descobre a cada N minutos e aplica ao longo do dia dentro da janela ativa."""
    from .notify import notify
    rt = Runtime(interactive=interactive)
    p = rt.pipeline
    interval = timedelta(minutes=rt.s.limits.discovery_interval_minutes)
    console.print(f"[bold]run[/] janela {rt.s.limits.active_hours[0]}-{rt.s.limits.active_hours[1]} · descoberta a cada {rt.s.limits.discovery_interval_minutes} min · teto {rt.s.limits.max_applications_per_day}/dia. Ctrl+C para parar.")
    try:
        while True:
            if not p.pacer.in_active_hours():
                secs = p.pacer.seconds_until_active()
                console.print(f"[dim]fora da janela ativa; dormindo {secs//60} min[/]")
                time.sleep(min(secs, 1800))
                continue
            last = rt.db.kv_get("last_discovery")
            due = not last or datetime.fromisoformat(last) + interval <= datetime.now()
            if due:
                counts = p.discover()
                n, q = p.prepare_all()
                console.print(f"[bold]descoberta:[/] {sum(counts.values())} novas · {q} na fila")
                if q:
                    notify(f"🔎 {sum(counts.values())} vagas novas, {q} na fila.")
            summary = p.process_queue()
            if summary.processed:
                _print_summary(summary)
                notify(f"📨 Rodada: {summary.applied} aplicadas, {summary.failed} falhas, {summary.pending} pendentes. Hoje: {rt.db.applied_today()}.")
            if summary.stopped_reason and "teto diário total" in summary.stopped_reason:
                secs = p.pacer.seconds_until_active()
                console.print(f"[dim]teto diário atingido; até amanhã ({secs//60} min)[/]")
                time.sleep(min(secs, 3600))
                continue
            nxt = 600
            if last:
                nxt = max(60, min(nxt, int((datetime.fromisoformat(rt.db.kv_get('last_discovery')) + interval - datetime.now()).total_seconds())))
            console.print(f"[dim]fila vazia; próximo ciclo em {nxt//60} min[/]")
            time.sleep(nxt)
    except (KeyboardInterrupt, SystemExit):   # Ctrl+C ou SIGTERM do painel/reinício: encerra limpo, sem traceback no log
        console.print("\n[dim]parando…[/]")
    finally:
        rt.close()


@app.command()
def questions():
    """Responde no terminal as perguntas pendentes e recoloca as vagas na fila."""
    from .qa import AnswerBank, normalize
    s = _settings()
    db = _db(s)
    bank = AnswerBank(s.answers_path)
    rows = db.pending_questions()
    if not rows:
        console.print("Nenhuma pergunta pendente.")
        return
    console.print(f"[bold]{len(rows)} pergunta(s) pendente(s).[/] Vazio = pular (fica pendente).")
    seen: dict[str, str] = {}
    for r in rows:
        job = db.get_job(r["job_id"]) if r["job_id"] else None
        opts = json.loads(r["options"] or "[]")
        key = normalize(r["question"])
        if key in seen:
            db.answer_pending(r["id"], seen[key])
            continue
        console.print(f"\n[cyan]{(job.title + ' @ ' + (job.company or '')) if job else ''}[/]")
        console.print(f"[bold]{r['question']}[/] [dim]({r['field_type']})[/]")
        for i, o in enumerate(opts[:40], 1):
            console.print(f"   [dim]{i}.[/] {o}")
        ans = Prompt.ask("Resposta", default=r["suggested"] or "", show_default=bool(r["suggested"]))
        if ans.strip().isdigit() and opts and 1 <= int(ans) <= len(opts):
            ans = opts[int(ans) - 1]
        ans = ans.strip()
        if not ans:
            continue
        db.answer_pending(r["id"], ans)
        bank.add(r["question"], ans, "user", options=opts)
        seen[key] = ans
    n = db.requeue_pending_jobs()
    console.print(f"\n[green]Respostas salvas em {s.answers_path.relative_to(ROOT)}. {n} vaga(s) de volta à fila.[/] Rode `vagas apply`.")


@app.command()
def resume(job: str = typer.Argument(..., help="id da vaga (aceita prefixo)"),
           force: bool = typer.Option(False, help="regenerar mesmo se já existir"),
           show: bool = typer.Option(False, help="imprime o JSON gerado")):
    """Gera (ou mostra) o currículo adaptado de uma vaga."""
    rt = Runtime(need_browser=True)
    try:
        j = rt.db.get_job(job) or rt.db.find_job_by_prefix(job)
        if not j:
            console.print("[red]vaga não encontrada[/]"); raise typer.Exit(1)
        if not j.classification or not j.description:
            rt.pipeline.prepare(rt.browser.page(), j)
        if force:
            j.resume_path = None
        pdf = rt.pipeline.build_resume(j)
        console.print(f"[green]PDF:[/] {pdf}\n[dim]HTML: {pdf.with_suffix('.html')} · JSON: {pdf.with_suffix('.json')}[/]")
        if show:
            console.print_json(pdf.with_suffix(".json").read_text(encoding="utf-8"))
    finally:
        rt.close()


@profile_app.command("import")
def profile_import(pdf: Path = typer.Argument(..., help="PDF exportado do LinkedIn (Perfil > Mais > Salvar como PDF) ou .txt"),
                   overwrite: bool = typer.Option(False)):
    """Gera config/profile.yaml a partir do PDF do LinkedIn (rascunho para você revisar)."""
    from .resume.importer import import_linkedin
    s = _settings()
    llm = _llm(s, _db(s))
    out = import_linkedin(llm, pdf, overwrite=overwrite)
    console.print(f"[green]Perfil gerado em {out}. Revise com calma antes de aplicar.[/]")


@profile_app.command("show")
def profile_show():
    s = _settings()
    console.print(s.profile_path.read_text(encoding="utf-8"))


@app.command()
def jobs(status: Optional[str] = typer.Option(None), limit: int = typer.Option(50), source: Optional[str] = typer.Option(None)):
    """Lista vagas no banco."""
    s = _settings()
    db = _db(s)
    rows = db.list_jobs(status=status, limit=limit, source=source)
    t = Table(title=f"{len(rows)} vagas" + (f" ({status})" if status else ""))
    for c in ("id", "título", "empresa", "tipo", "fit", "status", "motivo"):
        t.add_column(c, overflow="fold")
    for j in rows:
        t.add_row(j.id, (j.title or "")[:45], (j.company or "")[:25], j.apply_type or "", str((j.classification or {}).get("fit_score", "")), j.status, (j.reason or "")[:60])
    console.print(t)


@app.command()
def dashboard(port: int = typer.Option(8765), open_browser: bool = typer.Option(False, "--open", help="abre no navegador do Windows (WSL)")):
    """Painel local (http://localhost:8765): visão do dia, candidaturas, fila, descartadas, pendências, cursos, custos, eventos, configuração."""
    from . import dashboard as dash
    s = _settings()
    if open_browser:
        import subprocess
        try:
            subprocess.Popen(["cmd.exe", "/c", "start", "", f"http://localhost:{port}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
    console.print(f"[bold]painel em http://localhost:{port}[/] (Ctrl+C para parar)")
    try:
        dash.serve(s, port)
    except KeyboardInterrupt:
        pass


@app.command()
def stats():
    """Contadores gerais."""
    s = _settings()
    db = _db(s)
    st = db.stats()
    console.print_json(json.dumps(st, ensure_ascii=False))


@app.command()
def custos(meses: int = typer.Option(2, help="quantos meses mostrar")):
    """Gasto com IA por mês e por uso, contra o teto do settings.yaml."""
    from .budget import Budget, call_cost_usd
    s = _settings(); db = _db(s)
    b = Budget(db, s.llm.monthly_budget_brl, s.llm.usd_brl, since=s.llm.budget_since, overrides_brl=s.llm.budget_overrides_brl)
    st = b.status()
    cor = "green" if st["used_pct"] < 70 else ("yellow" if st["used_pct"] < 100 else "red")
    console.print(f"[bold]Orçamento {st['month']}:[/] [{cor}]R$ {st['spent_brl']:.2f} de R$ {st['budget_brl']:.2f}[/] "
                  f"({st['used_pct']:.0f}%) · restam R$ {st['remaining_brl']:.2f}"
                  + (f" · contando desde {st['since']}" if st.get("since") else ""))
    rows = db.conn.execute(
        "SELECT strftime('%Y-%m', at) m, purpose, model, COUNT(*) n, SUM(prompt_tokens) pin, SUM(completion_tokens) pout, "
        "SUM(COALESCE(cached_tokens, 0)) pc "
        "FROM llm_usage GROUP BY m, purpose, model ORDER BY m DESC, n DESC").fetchall()
    meses_vistos: list[str] = []
    for r in rows:
        if r[0] not in meses_vistos:
            if len(meses_vistos) >= meses:
                break
            meses_vistos.append(r[0])
            console.print(f"\n[bold]{r[0]}[/]")
        usd = call_cost_usd(r[2], r[4], r[5], r[6])
        console.print(f"  {r[1]:18} {r[2]:14} {r[3]:5} chamadas   R$ {usd * s.llm.usd_brl:7.2f}")
    hoje = db.conn.execute(
        "SELECT model, prompt_tokens, completion_tokens, COALESCE(cached_tokens, 0) FROM llm_usage WHERE date(at)=date('now','localtime')").fetchall()
    usd_hoje = sum(call_cost_usd(r[0], r[1], r[2], r[3]) for r in hoje)
    console.print(f"\n[bold]hoje:[/] R$ {usd_hoje * s.llm.usd_brl:.2f} em {len(hoje)} chamadas")


@app.command()
def skip(job: str, reason: str = typer.Option("pulada manualmente")):
    """Marca uma vaga como pulada."""
    s = _settings(); db = _db(s)
    j = db.get_job(job) or db.find_job_by_prefix(job)
    if not j:
        console.print("[red]não encontrada[/]"); raise typer.Exit(1)
    db.set_status(j.id, "skipped", reason)
    console.print(f"ok: {j.id} -> skipped")


@app.command()
def requeue(job: str):
    """Recoloca uma vaga na fila (zera tentativas)."""
    s = _settings(); db = _db(s)
    j = db.get_job(job) or db.find_job_by_prefix(job)
    if not j:
        console.print("[red]não encontrada[/]"); raise typer.Exit(1)
    j.status, j.reason, j.attempts = "queued", None, 0
    db.save_job(j)
    console.print(f"ok: {j.id} -> queued")


@app.command()
def answers():
    """Mostra o banco de respostas."""
    from .qa import AnswerBank
    s = _settings()
    bank = AnswerBank(s.answers_path)
    t = Table(title=f"{len(bank.items)} respostas")
    t.add_column("pergunta", overflow="fold"); t.add_column("resposta", overflow="fold"); t.add_column("fonte")
    for it in bank.items:
        t.add_row(str(it.get("q"))[:80], str(it.get("a"))[:60], it.get("source", ""))
    console.print(t)


@app.command()
def stacks(days: int = typer.Option(7, help="janela em dias"), top: int = typer.Option(25)):
    """Stacks mais pedidas nas vagas classificadas (guia para cursos reais e para o pool de certificações)."""
    from collections import Counter
    s = _settings(); db = _db(s)
    rows = db.conn.execute(
        "SELECT classification FROM jobs WHERE classification IS NOT NULL AND replace(discovered_at,'T',' ') >= datetime('now','localtime', ?)", (f"-{days} days",)
    ).fetchall()
    cnt: Counter = Counter()
    for r in rows:
        try:
            for st in (json.loads(r[0]).get("stack") or []):
                cnt[str(st).strip().lower()] += 1
        except Exception:
            continue
    if not cnt:
        console.print("Nenhuma vaga classificada ainda. Rode `vagas search`."); return
    t = Table(title=f"stacks em {len(rows)} vagas dos últimos {days} dias")
    t.add_column("stack"); t.add_column("vagas", justify="right"); t.add_column("%", justify="right")
    for k, v in cnt.most_common(top):
        t.add_row(k, str(v), f"{100*v/len(rows):.0f}%")
    console.print(t)


courses_app = typer.Typer(help="Cursos a fazer por stack (config/courses.yaml). Só os marcados done: true entram no currículo.")
app.add_typer(courses_app, name="courses")


def _courses_path() -> Path:
    return CONFIG_DIR / "courses.yaml"


def _load_courses() -> dict:
    import yaml
    p = _courses_path()
    return (yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else None) or {"stacks": {}}


def _save_courses(data: dict) -> None:
    import os, yaml
    p = _courses_path(); tmp = p.with_suffix(".yaml.tmp")
    tmp.write_text("# Cursos por stack. status: todo | in_progress | done (+ year). done entra como concluído; in_progress como 'em andamento'.\n"
                   + yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=110), encoding="utf-8")
    os.replace(tmp, p)


@courses_app.command("suggest")
def courses_suggest(stack: str = typer.Argument("", help="ex.: php, laravel, java, go"), n: int = typer.Option(3),
                    all_stacks: bool = typer.Option(False, "--all", help="todas as stacks de discovery.stack_queries"),
                    refill: bool = typer.Option(False, "--refill", help="completa stacks que têm menos de min_udemy cursos da Udemy")):
    """Sugere cursos para uma stack e salva em config/courses.yaml (cache; não pesquisa de novo se já existir)."""
    from .resume.tailor import is_same_course
    data = _load_courses()
    if all_stacks or refill:
        s = _settings(); llm = _llm(s, _db(s))
        skip = {"dados", "devops", "sql", "low-code", "mobile"}
        def n_udemy(key): return sum(1 for c in data["stacks"].get(key) or [] if "udemy" in str(c.get("issuer", "")).lower())
        keys = [q.strip().lower() for q in s.discovery.stack_queries if q.strip().lower() not in skip]
        todo = [k for k in keys if (k not in data["stacks"]) or (refill and n_udemy(k) < s.courses.min_udemy)]
        console.print(f"sugerindo cursos para {len(todo)} stacks…")
        all_names = [c.get("name", "") for items in data["stacks"].values() for c in items or []]
        for key in todo:
            try:
                items = llm.suggest_courses(key, max(n, s.courses.min_udemy + 2), min_udemy=s.courses.min_udemy + 2)
            except Exception as e:
                console.print(f"[yellow]{key}: {e}[/]"); continue
            cur = data["stacks"].setdefault(key, [])
            added = 0
            for c in items:
                if not c.get("name") or any(is_same_course(c["name"], x) for x in all_names):
                    continue
                if "udemy" in str(c.get("issuer", "")).lower() and n_udemy(key) >= s.courses.min_udemy:
                    continue
                cur.append({"name": c["name"], "issuer": c.get("issuer") or "", "hours": c.get("hours") or 0, "url": c.get("url") or "", "status": "todo", "year": ""})
                all_names.append(c["name"]); added += 1
            console.print(f"  {key}: +{added} (udemy: {n_udemy(key)})")
        _save_courses(data)
        return
    if not stack:
        console.print("[red]informe a stack ou --all[/]"); raise typer.Exit(1)
    key = stack.strip().lower()
    if data["stacks"].get(key):
        console.print(f"[dim]já existem sugestões para '{key}' em {_courses_path().relative_to(ROOT)}; use --n maior ou edite o arquivo[/]")
    else:
        s = _settings(); llm = _llm(s, _db(s))
        items = llm.suggest_courses(key, n)
        data["stacks"][key] = [{"name": c.get("name"), "issuer": c.get("issuer") or "", "hours": c.get("hours") or 0,
                               "url": c.get("url") or "", "status": "todo", "year": ""} for c in items]
        _save_courses(data)
    for c in data["stacks"][key]:
        mark = {"done": "[green]✔[/]", "in_progress": "[yellow]▶[/]"}.get(c.get("status"), "[dim]☐[/]")
        console.print(f" {mark} {c['name']}  [dim]{c.get('issuer','')} · {c.get('hours') or '?'}h[/]")


@courses_app.command("list")
def courses_list():
    """Lista cursos sugeridos e concluídos por stack."""
    data = _load_courses()
    if not data["stacks"]:
        console.print("Nada ainda. Use `vagas courses suggest <stack>`."); return
    for stack, items in data["stacks"].items():
        console.print(f"[bold]{stack}[/]")
        for c in items or []:
            mark = {"done": "[green]✔[/]", "in_progress": "[yellow]▶[/]"}.get(c.get("status"), "[dim]☐[/]")
            console.print(f"   {mark} {c['name']} [dim]{c.get('issuer','')}{' · ' + str(c['year']) if c.get('year') else ''}[/]")


def _set_course_status(prefix: str, status: str, year: str = "") -> None:
    data = _load_courses()
    hits = [(st, c) for st, items in data["stacks"].items() for c in items or [] if prefix.lower() in (c.get("name") or "").lower()]
    if not hits:
        console.print("[red]curso não encontrado[/]"); raise typer.Exit(1)
    for st, c in hits:
        c["status"] = status
        if year:
            c["year"] = year
        console.print(f"{status}: {c['name']} ({st})")
    _save_courses(data)


@courses_app.command("start")
def courses_start(name: str = typer.Argument(..., help="trecho do nome do curso")):
    """Marca como em andamento (aparece no currículo como 'em andamento')."""
    _set_course_status(name, "in_progress")


@courses_app.command("done")
def courses_done(name: str = typer.Argument("", help="trecho do nome do curso (ou use --stack)"),
                 stack: str = typer.Option("", help="marca TODOS os cursos dessa stack como concluídos"),
                 all_stacks: bool = typer.Option(False, "--all", help="todas as stacks do cache"),
                 issuer: str = typer.Option("", help="com --stack/--all: só os desta plataforma (ex.: udemy)"),
                 year: str = typer.Option("", help="ano de conclusão")):
    """Marca como concluído (aparece no currículo com o ano). Use depois que o certificado for emitido."""
    from datetime import date
    year = year or str(date.today().year)
    if stack or all_stacks:
        data = _load_courses()
        keys = list(data["stacks"].keys()) if all_stacks else [stack.strip().lower()]
        n = 0
        for key in keys:
            for c in data["stacks"].get(key) or []:
                if issuer and issuer.lower() not in str(c.get("issuer", "")).lower():
                    continue
                if c.get("status") != "done":
                    c["status"], c["year"] = "done", year
                    n += 1
                    if not all_stacks:
                        console.print(f"done: {c['name']} [dim]({c.get('issuer','')})[/]")
        if not n and not all_stacks:
            console.print(f"[red]nenhum curso para marcar em '{stack}'[/]"); raise typer.Exit(1)
        _save_courses(data)
        console.print(f"[green]{n} curso(s) marcados como concluídos ({year}).[/]")
        return
    if not name:
        console.print("[red]informe o nome do curso ou --stack[/]"); raise typer.Exit(1)
    _set_course_status(name, "done", year)


@app.command()
def accounts(host: Optional[str] = typer.Option(None, "--host", help="só esse host (ex.: nouslatam.com)"),
             all_: bool = typer.Option(False, "--all", help="tenta todos os sites que exigem conta"),
             list_: bool = typer.Option(False, "--list", help="só lista os sites pendentes"),
             captcha_wait: float = typer.Option(4.0, "--captcha-wait", help="minutos de espera quando o cadastro tem captcha (você resolve na janela)")):
    """Cria conta nos sites/ATS que exigem cadastro, com o e-mail de cadastros (SIGNUP_EMAIL)."""
    from . import signup as sg
    rt = Runtime(need_browser=not list_, interactive=True)
    try:
        rows = [(r[0].split(":", 1)[1], r[1]) for r in rt.db.conn.execute(
            "SELECT key, value FROM kv WHERE key LIKE 'needs_account:%' AND value != ''").fetchall()]
        rows = [(hst, url) for hst, url in rows if not hst.startswith("*.") and not sg.has_account(rt.db, hst)]
        if host:
            rows = [(hst, url) for hst, url in rows if host.lower() in hst.lower()]
        if list_ or not rows:
            for hst, url in rows:
                console.print(f"  {hst:34} {url[:70]}")
            console.print(f"[dim]{len(rows)} site(s) sem conta.[/]")
            return
        if not all_ and len(rows) > 1:
            rows = rows[:1]
        for hst, url in rows:
            console.print(f"\n[bold cyan]▶ criando conta em {hst}[/] [dim]{url[:70]}[/]")
            try:
                ok, msg = sg.create_account(rt, url, captcha_wait_min=captcha_wait)
            except Exception as e:
                ok, msg = False, f"erro: {str(e)[:120]}"
            console.print(("   [green]" if ok else "   [red]") + msg + "[/]")
            if ok:
                for jid, in rt.db.conn.execute("SELECT id FROM jobs WHERE external_url LIKE ? AND status IN ('failed','pending_question')", (f"%{hst}%",)).fetchall():
                    rt.db.set_status(jid, "queued", f"conta criada em {hst}; refazer")
    finally:
        rt.close()


@app.command()
def version():
    console.print(f"vagas {__version__}")


def _print_summary(summary):
    console.print(f"\n[bold]Resumo:[/] {summary.processed} processadas · [green]{summary.applied} aplicadas[/] · {summary.skipped} puladas · [red]{summary.failed} falhas[/] · [yellow]{summary.pending} pendentes[/] · {summary.dry_run} dry-run"
                  + (f" · parou: {summary.stopped_reason}" if summary.stopped_reason else ""))


if __name__ == "__main__":
    app()
