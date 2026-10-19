# vagas — auto-aplicador pessoal

Ferramenta de uso pessoal que **descobre vagas de desenvolvimento publicadas no dia** (LinkedIn, Indeed, Gupy), **gera um currículo adaptado por vaga** a partir do seu perfil mestre e **aplica automaticamente** (LinkedIn Easy Apply, Indeed Apply, Gupy, InHire, Greenhouse, Lever, Workable e qualquer outro ATS via preenchimento genérico guiado por LLM). Usa o **seu navegador logado** (perfil persistente), ritmo humano e tetos diários para não tomar bloqueio.

Não é produto. É pra rodar na sua máquina, com as suas contas.

## Regras fixas (em código, não só em config)

- **Só vagas das últimas 24h** e **só remoto**, em toda busca e toda vaga (`config.py: HARD_MAX_AGE_HOURS / HARD_REMOTE_ONLY`). Vagas mais antigas ou presenciais/híbridas são descartadas antes de qualquer chamada de IA.
- **Só descrições em pt-BR** (`filters.job_languages`); descrições em inglês são descartadas por heurística sem IA. Vagas que exigem outro idioma além de inglês são puladas.
- **Descoberta no LinkedIn sem usar a conta**: endpoint público `jobs-guest` com `geoId` do Brasil + filtros 24h/remoto; a sessão logada só entra para candidatar. Indeed continua no navegador (Cloudflare). Gupy usa a API pública.
- **Cobertura por linguagem**: o LinkedIn personaliza "desenvolvedor" pelo seu perfil, então a descoberta roda uma matriz `base_queries` × fontes + `"desenvolvedor {stack}"` para cada item de `discovery.stack_queries` (php, rust, c#, java, ...). Hoje: 61 buscas no LinkedIn, 61 na Gupy, 10 no Indeed por ciclo.
- **Pretensão salarial**: se a vaga informa salário/faixa, a resposta segue a vaga; senão usa `facts.salary_expectation_<nível>_<regime>` (nível vem do classificador, regime CLT por padrão).
- **Anos de experiência por nível**: `skill_years` no perfil são os reais (sênior); vaga pleno usa metade, júnior um quarto (`resume.seniority_years_factor`), tanto no currículo quanto nas respostas de formulário.
- **Adaptação à stack da vaga**: `adaptation.min_stack_share` no perfil (0.5) obriga pelo menos 50% dos bullets e das competências a citar a stack da vaga. O gerador reescreve bullets trocando/acrescentando tecnologias permitidas nas experiências sem `restack: false`; empresa, cargo, datas, clientes e números não mudam. Se ficar abaixo da meta, tenta uma segunda vez. A fração alcançada fica em `_meta.stack_share` no JSON do currículo.
- **Design do currículo**: template `modern` (fonte Inter embutida, foto de 38 mm, cor de destaque `resume.accent_color`, chips de competências). `resume.template: classic` volta ao layout simples. Todas as experiências entram para pleno/sênior (2 páginas); júnior fica com 4.
- **Tecnologias declaradas**: `skills.also_experienced` lista linguagens/stacks com que o usuário declara experiência mas que não constam no LinkedIn; entram em Competências e no headline quando a vaga pede, com anos = `skill_years_default`. Os bullets de cada empresa continuam restritos ao que o perfil registra para aquela empresa (checagem de tecnologia por experiência).

## Como funciona

```
vagas search  ──► descobre (LinkedIn/Indeed/Gupy, últimas 24h)
                  ├─ abre cada vaga, lê a descrição
                  ├─ classifica com LLM: é vaga de dev? nível? stack? idioma? requisito eliminatório? fit 0-100
                  └─ fila (SQLite) ordenada por fit

vagas apply   ──► para cada vaga da fila, respeitando limites/ritmo:
                  ├─ gera currículo adaptado (LLM → HTML → PDF), validado contra o perfil (nada inventado)
                  ├─ escolhe o applier (Easy Apply / Indeed Apply / externo por host)
                  ├─ preenche etapa a etapa: regras → banco de respostas → LLM → você
                  └─ registra: applied / failed / pending_question (+ screenshot, dump para aprendizado)

vagas run     ──► loop do dia: descobre a cada N min e aplica dentro da janela ativa
```

Tudo que a ferramenta não souber responder vira **pergunta pendente**. Você responde uma vez (`vagas questions` ou modo `--interactive`), a resposta vai pro `config/answers.yaml` e nunca mais é perguntada.

## Setup

Precisa de Python 3.12+ e Linux/WSL.

```bash
cd ~/vagas
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/pip install patchright            # fork stealth do Playwright (recomendado)
.venv/bin/patchright install chromium       # ou: .venv/bin/playwright install chromium

.venv/bin/vagas setup                       # ⬅️ COMECE AQUI
```

O `vagas setup` pergunta cada dado que a ferramenta usa e escreve `config/profile.yaml` e `.env`.
São 8 blocos (dados pessoais, documentos/endereço, diversidade, carreira, tecnologias, formação,
pretensão/preferências, chaves). Enter aceita o valor entre colchetes ou pula o campo — **nada é
inventado**: o que ficar em branco vira pendência quando um formulário pedir, em vez de sair um dado
errado. Cada bloco é salvo assim que termina, então dá para parar no meio (Ctrl+C) e retomar:

```bash
vagas setup --secao preferencias     # mexe só na pretensão salarial
vagas setup --secao carreira         # acrescenta uma experiência
vagas setup --secao chaves           # troca a chave da OpenAI
```

Blocos: `pessoal`, `documentos`, `diversidade`, `carreira`, `skills`, `formacao`, `preferencias`, `chaves`.

Se preferir preencher o YAML à mão, `vagas init` só copia os arquivos de exemplo. E dá para gerar um
rascunho da carreira a partir do PDF do LinkedIn (Perfil > Mais > Salvar como PDF) — isso usa IA e
**sobrescreve** o `profile.yaml`, então faça antes do resto:

```bash
vagas profile import ~/Downloads/Profile.pdf && vagas setup
```

Depois:

```bash
.venv/bin/vagas doctor        # confere chave, perfil, navegador e display
.venv/bin/vagas login         # loga no LinkedIn, Indeed e Gupy dentro do perfil do navegador (uma vez)
```

Dica: `alias vagas=~/vagas/.venv/bin/vagas` no `.zshrc` (ou `.bashrc`).

### O que revisar antes da primeira leva

- `config/profile.yaml` — principalmente os **bullets** de cada experiência: são a matéria-prima do
  currículo e o gerador só reescreve o que estiver lá.
- `config/settings.yaml` — `discovery.stack_queries` (as buscas por tecnologia), `filters.company_blacklist`,
  `limits` (ritmo e tetos diários) e `llm.monthly_budget_brl` (teto de gasto com IA, em reais).
- `config/courses.yaml` — catálogo de cursos por stack, já preenchido mas **tudo marcado como "a fazer"**.
  Marque os que você fez: `vagas courses done <trecho do nome>`. Só os marcados entram no currículo.

### Navegador: Chrome real (recomendado)

Chromium do Playwright reprova em Turnstile/Cloudflare (Workable, Greenhouse, Indeed). Duas opções:

1. **Chrome extraído sem root** (padrão em `settings.yaml`):
   ```bash
   curl -sSL -o /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
   mkdir -p ~/.local/chrome && dpkg -x /tmp/chrome.deb ~/.local/chrome
   ```
   `browser.executable_path: ~/.local/chrome/opt/google/chrome/chrome`
2. **Chrome do Windows via CDP** (fingerprint do seu Chrome de verdade):
   no Windows: `chrome.exe --remote-debugging-port=9222 --user-data-dir=C:\vagas-profile`
   e em `settings.yaml`: `browser.mode: cdp`, `cdp_url: http://<ip-do-windows>:9222` (no WSL2 com mirrored networking, `localhost`).

Se nada disso existir, cai no Chromium do Patchright (funciona no LinkedIn; pode falhar em sites com Cloudflare).

## Uso diário

```bash
vagas search                       # descobre + classifica (últimas 24h)
vagas jobs --status queued         # o que entrou na fila
vagas apply --interactive --dry-run --limit 3   # calibração: preenche, NÃO envia, pergunta no terminal
vagas apply --interactive          # aplica de verdade; pergunta o que não souber
vagas apply                        # sem perguntar: o que não souber vira pendência
vagas questions                    # responde pendências -> volta pra fila
vagas run                          # modo contínuo (deixe num tmux)
vagas stats / vagas jobs / vagas answers
vagas resume linkedin:4123456789 --show   # ver o currículo gerado de uma vaga
vagas skip <id> / vagas requeue <id>
```

Primeiros dias: rode com `--interactive` para alimentar o banco de respostas (pretensão salarial, PJ/CLT, disponibilidade…). Depois `vagas run` sozinho.

## Configuração (`config/settings.yaml`)

- `discovery`: fontes, geoId, modo do LinkedIn (guest/session), `base_queries`, `stack_queries` e template. `searches` são extras (também forçadas a 24h + remoto).
- `filters`: `apply_roles` / `exclude_roles` guiam o classificador (aplicamos pra **qualquer linguagem**; o que importa é ser papel de dev). `title_blacklist` corta antes do LLM.
- `limits`: teto diário total e por fonte (LinkedIn Easy Apply tem teto não-publicado de ~50/24h), pausas entre candidaturas, pausa longa a cada N, janela ativa, intervalo de descoberta.
- `apply`: `dry_run`, `pause_before_submit`, `min_fit_score` (0 = todas), `apply_once_per_company_days`, `max_attempts`.
- `resume`: idioma (auto = idioma da vaga), adaptação por nível (júnior ⇒ menos experiências e bullets hands-on; **nunca** muda cargo/data; pode usar `title_alt` se você definir no perfil).
- `llm`: modelos (`cheap_model` para classificar/responder, `smart_model` para o currículo), confiança mínima, temas que sempre exigem sua resposta.
- `ats.known_hosts`: host → applier. `skip_hosts`: hosts que nem tentamos.

## Perfil mestre (`config/profile.yaml`)

Única fonte de verdade. O gerador de currículo só pode usar o que está lá:
- `skills.primary` (destaque), `secondary` (pode destacar), `familiar` (só em "Outros conhecimentos").
- `skill_years`: evita exagero em "anos de experiência com X".
- `experiences[].bullets`: escreva muitos, com números. `bullets_ic`: versões sem liderança (usadas em vagas júnior/pleno). `title_alt`: cargo alternativo permitido (ex.: sem "Sênior").
- `facts`: pretensão por nível, PJ/CLT, disponibilidade, modelos de trabalho, etc. Alimenta as respostas de formulário.
- `voice.notes`: como você escreve. Reduz a "cara de IA".

Depois de gerado, cada currículo passa por `validate_resume`: skills fora do perfil são removidas, empresas/datas/cargos são forçados a bater com o perfil.

## Aprendizado por ATS

Toda candidatura que falha ou fica pendente gera um dump em `data/learn/<ats>/<job>/stepN.{png,html,json}` (campos detectados, labels, opções). Use isso pra ajustar seletores/regras em `src/vagas/appliers/*.py` e `src/vagas/forms.py`. Sucesso gera `data/evidence/<job>.png`.

Fluxo típico pra "ensinar" um ATS novo: `vagas apply --job <id> --interactive --dry-run` → olhar `data/learn` → ajustar → repetir.

## Anti-bloqueio (o que está implementado)

- Perfil persistente com sua sessão real; janela visível; flags de automação removidas; Patchright (fecha vazamentos de CDP como `Runtime.enable` e `navigator.webdriver`).
- Pausas com distribuição normal entre ações/candidaturas, digitação com velocidade variável, scroll de "leitura", pausa longa periódica, janela de horário.
- Tetos por dia e por fonte; detecção da mensagem de limite do LinkedIn (para a fonte até o dia seguinte).
- Checkpoints (login/captcha/Cloudflare): a ferramenta **para, apita e espera você** resolver na janela (timeout configurável); nunca tenta burlar captcha.
- Descoberta na Gupy via API pública (sem navegar).

## Estrutura

```
src/vagas/
  cli.py          comandos
  config.py       settings.yaml + .env
  db.py           SQLite (jobs, applications, pending_questions, events, llm_usage)
  browser.py      perfil persistente / CDP, checkpoints, wait_for_human
  humanize.py     pausas, digitação, scroll
  llm.py          OpenAI: classify_job, tailor_resume, answer_questions, extract_profile
  qa.py           banco de respostas (fuzzy) + regras + LLM + pergunta pra você
  forms.py        scanner JS de campos, preenchimento, botões, erros, sucesso
  pipeline.py     descoberta → preparação → currículo → candidatura, ritmo e limites
  sources/        linkedin.py, indeed.py, gupy.py (API)
  appliers/       flow.py (loop de etapas), linkedin.py, indeed.py, generic.py (+Gupy/InHire/Greenhouse/Lever/Workable), registry.py
  resume/         profile.py, tailor.py (LLM + validação), render.py (Jinja2 → PDF), importer.py (PDF do LinkedIn)
config/           settings.yaml, profile.yaml (seu), answers.yaml (aprendido)
data/             vagas.db, resumes/, profile/ (navegador), learn/, evidence/
```

## Dashboard

`vagas dashboard --serve` sobe um painel em http://localhost:8765 (abra no Chrome do Windows; o WSL encaminha localhost). Mostra candidaturas do dia e totais, vagas encontradas, fila, pendências, tokens de IA, funil de descarte, stacks mais pedidas, resultado por ATS, candidaturas enviadas com links e currículo, e falhas recentes com o motivo. Atualiza sozinho a cada 60 s. `vagas dashboard` sem `--serve` só gera `data/dashboard.html`.

## Credenciais opcionais no `.env`

- `GUPY_PASSWORD`: login automático nas páginas de empresa da Gupy (e-mail vem do perfil).
- `ATS_SIGNUP_PASSWORD`: senha usada para criar conta em ATS que exigem cadastro (Recrutei etc.). Vazio = a ferramenta não cria contas.

## Notificações (opcional)

`TELEGRAM_BOT_TOKEN` e `TELEGRAM_CHAT_ID` no `.env`: avisa perguntas pendentes, limites atingidos e resumo de cada rodada.

## Limitações conhecidas / próximos passos

- Seletores de LinkedIn/Indeed mudam; os do código seguem o DOM de 2025-26 com fallbacks por texto/role. Primeiras rodadas pedem calibração com `--dry-run`.
- Gupy ranqueia o **perfil do portal** (portal.gupy.io/curriculum), não o PDF: mantenha aquele perfil completo.
- Indeed costuma pedir captcha; resolva na janela quando apitar.
- Sem detecção de retorno (e-mails de entrevista) ainda; ver "funil" abaixo.

Ideias vindas de outros projetos (AIHawk, forks com Patchright, davamix, LuisMiguel/auto-apply-bot) que ainda não estão aqui: funil de retorno (triagem de e-mail/inbox), carta de apresentação quando pedida, notas de conexão com recrutador, estatísticas de skills mais pedidas, cap de custo de tokens.
