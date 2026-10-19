"""Camada Claude Code SDK: classificação de vaga, currículo adaptado, respostas de formulário, importação de perfil.

Usa o claude-code-sdk (pip install claude-code-sdk), que chama o CLI local do Claude Code.
Dessa forma as chamadas de IA vão pela sessão/plano do Claude Code, sem API key separada.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from .budget import Budget, BudgetExceeded
from .config import LLMCfg
from .db import DB


async def _query_claude(prompt: str, system: str) -> tuple[str, int, int]:
    """Chama o Claude Code SDK e retorna (texto, input_tokens, output_tokens).

    Usa max_turns=1 para que o modelo responda direto em texto, sem executar ferramentas.
    """
    from claude_code_sdk import query, ClaudeCodeOptions  # type: ignore[import]

    text_parts: list[str] = []
    input_tokens = 0
    output_tokens = 0

    async for message in query(
        prompt=prompt,
        options=ClaudeCodeOptions(
            max_turns=1,
            system_prompt=system,
        ),
    ):
        # AssistantMessage: contém os blocos de texto da resposta
        if getattr(message, "role", None) == "assistant":
            for block in getattr(message, "content", []):
                if getattr(block, "type", None) == "text" and hasattr(block, "text"):
                    text_parts.append(block.text)

        # ResultEvent: metadados de uso/custo ao final da sessão
        elif hasattr(message, "usage") and message.usage is not None:
            usage = message.usage
            input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
            output_tokens = int(getattr(usage, "output_tokens", 0) or 0)

    return "".join(text_parts), input_tokens, output_tokens


def _run_async(coro: Any) -> Any:
    """Executa uma coroutine em contexto síncrono."""
    try:
        # asyncio.run() cria um loop novo — funciona enquanto não há loop ativo na thread
        return asyncio.run(coro)
    except RuntimeError:
        # Fallback: já existe um loop na thread (ex.: Jupyter ou testes assíncronos)
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()


def _log_call(purpose: str, model: str, system: str, user: str, pin: int, pout: int, cached: int) -> None:
    """Uma linha por chamada em data/logs/llm_calls.jsonl: tamanho de cada seção do prompt e, nas
    respostas de formulário, os rótulos das perguntas. Serve para achar de onde vêm os tokens.
    Não grava o conteúdo do perfil nem as respostas."""
    try:
        import time as _t
        from .config import DATA_DIR
        rec = {"at": _t.strftime("%Y-%m-%dT%H:%M:%S"), "purpose": purpose, "model": model,
               "prompt_tokens": pin, "completion_tokens": pout, "cached": cached,
               "system_chars": len(system or ""), "user_chars": len(user or ""),
               "sections": [[sec.split("\n", 1)[0][:45], len(sec)] for sec in (user or "").split("\n\n")][:14]}
        if purpose == "answer_questions" and "PERGUNTAS DO FORMULÁRIO:\n" in (user or ""):
            try:
                qs = json.loads(user.split("PERGUNTAS DO FORMULÁRIO:\n", 1)[1])
                rec["questions"] = [[str(q.get("question") or "")[:70], len(q.get("options") or [])] for q in qs]
            except Exception:
                pass
        d = DATA_DIR / "logs"
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "llm_calls.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


class LLM:
    def __init__(self, cfg: LLMCfg, db: DB | None = None):
        self.cfg = cfg
        self.db = db
        self.budget = (Budget(db, cfg.monthly_budget_brl, cfg.usd_brl, since=cfg.budget_since,
                              overrides_brl=cfg.budget_overrides_brl) if db is not None else None)

    # ---------- base ----------
    def json(self, *, model: str, system: str, user: str, purpose: str,
             temperature: float | None = None, max_tokens: int | None = None) -> dict:
        # O parâmetro `model` é ignorado: o SDK usa o modelo configurado no Claude Code.
        # `temperature` e `max_tokens` não são expostos pelo SDK na versão atual.
        if self.budget is not None and not self.budget.allows(purpose):
            raise BudgetExceeded(self.budget.why_blocked(purpose))

        system_final = system + "\n\nIMPORTANTE: Responda SOMENTE com JSON válido, sem texto antes ou depois."
        last_err: Exception | None = None
        last_content: str = "{}"
        retry_prompt = user

        for attempt in range(3):
            try:
                last_content, pin, pout = _run_async(_query_claude(retry_prompt, system_final))
                if self.db and (pin or pout):
                    self.db.add_llm_usage(model, purpose, pin, pout, 0)
                    if self.budget is not None:
                        self.budget._cache.clear()
                    _log_call(purpose, model, system, user, pin, pout, 0)
                content = re.sub(r"^```(?:json)?\s*|\s*```$", "", last_content.strip())
                return json.loads(content)
            except json.JSONDecodeError as e:
                last_err = e
                # Na próxima tentativa reforça a instrução de JSON no prompt do usuário
                retry_prompt = user + "\n\nResposta anterior inválida. Responda SOMENTE com JSON válido, sem texto adicional."
            except Exception as e:
                msg = str(e)
                if "rate_limit" in msg.lower() or "overloaded" in msg.lower():
                    last_err = e
                    import time
                    time.sleep(2 * (attempt + 1))
                else:
                    raise
        raise RuntimeError(f"LLM falhou ({purpose}): {last_err}")

    # ---------- classificação ----------
    def classify_job(self, *, title: str, company: str, location: str, description: str,
                     apply_roles: list[str], exclude_roles: list[str], candidate_summary: str) -> dict:
        system = (
            "Você é um recrutador técnico que triage vagas para um candidato. Responda em JSON estrito.\n"
            "Objetivo: decidir se a vaga é um papel de DESENVOLVIMENTO DE SOFTWARE (qualquer linguagem, qualquer nível, "
            "inclusive júnior, pleno, sênior, tech lead, arquiteto hands-on). A linguagem/stack NÃO importa para a decisão; "
            "importa se a pessoa vai escrever código no dia a dia.\n"
            f"Papéis que SÃO alvo: {'; '.join(apply_roles)}\n"
            f"Papéis que NÃO são alvo: {'; '.join(exclude_roles)}\n"
            "Se a descrição estiver ausente ou muito curta, decida is_dev_role PELO TÍTULO (ex.: 'Desenvolvedor', 'Programador', 'Engenheiro de Software', "
            "'Tech Lead', 'Developer' => true) e use fit_score 50, seniority pelo título, stack pelas tecnologias do título.\n"
            "Também extraia requisitos rígidos que possam eliminar o candidato (mudança de cidade, presencial em cidade "
            "específica, idioma obrigatório, certificação obrigatória, formação obrigatória, nacionalidade/visto).\n"
            "Campos do JSON de saída:\n"
            "{\n"
            '  "is_dev_role": bool,\n'
            '  "role_family": "backend|frontend|fullstack|mobile|data|devops|lead|architect|other",\n'
            '  "seniority": "junior|mid|senior|lead|unspecified",\n'
            '  "stack": [tecnologias principais citadas],\n'
            '  "language": "pt-BR|en|es" (idioma em que a descrição está escrita),\n'
            '  "work_model": "remote|hybrid|onsite|unspecified",\n'
            '  "job_city": "cidade/UF se presencial ou híbrido, senão \\"\\"",\n'
            '  "requires_relocation": bool,\n'
            '  "required_languages": ["ex: inglês fluente"],\n'
            '  "hard_requirements": [outros requisitos eliminatórios explícitos],\n'
            '  "min_years_required": int ou null,\n'
            '  "salary_offered": "valor ou faixa salarial informado na descrição, ex. \"R$ 8.000 - R$ 10.000\"; \"\" se não informa",\n'
            '  "contract_type": "CLT|PJ|unspecified",\n'
            '  "is_staffing_agency": bool,\n'
            '  "fit_score": 0-100 (aderência do candidato à vaga considerando stack, nível e requisitos),\n'
            '  "hard_reject": bool (true se um requisito rígido claramente exclui o candidato),\n'
            '  "reason": "uma frase curta"\n'
            "}"
        )
        user = (
            f"CANDIDATO (resumo): {candidate_summary}\n\n"
            f"VAGA\nTítulo: {title}\nEmpresa: {company}\nLocal: {location}\n\nDescrição:\n{description[:9000]}"
        )
        return self.json(model=self.cfg.cheap_model, system=system, user=user, purpose="classify", temperature=0)

    # ---------- currículo ----------
    def tailor_resume(self, *, profile: dict, job: dict, classification: dict, language: str,
                      max_experiences: int, seniority: str, adaptation: dict | None = None, extra_instruction: str = "") -> dict:
        adaptation = adaptation or {}
        job_stack = adaptation.get("job_stack") or []
        allowed = adaptation.get("allowed") or []
        share = adaptation.get("min_stack_share") or 0
        restack_rule = (
            f"10. ADAPTAÇÃO À STACK DA VAGA (obrigatório): a vaga pede {', '.join(job_stack) or 'a stack descrita'}. "
            f"Pelo menos {int(share * 100)}% dos bullets e das competências principais devem citar essa stack. "
            "Para isso, nas experiências SEM 'restack: false', reescreva bullets trocando ou acrescentando tecnologias da lista permitida "
            f"({', '.join(allowed)}), por exemplo 'microserviços em Node.js' -> 'microserviços em PHP (Laravel)'. "
            "Mantenha intocados: empresa, cargo, datas, clientes citados, números e o que foi construído. "
            "Experiências com 'restack: false' ficam com a stack original. Use cada tecnologia de forma plausível para o papel "
            "(não coloque PHP numa experiência de Salesforce, por exemplo). Nunca cite tecnologia fora de skills.* do perfil.\n"
            if job_stack and allowed and share else ""
        )
        system = (
            "Você é um redator de currículos experiente. Monta o currículo de UMA pessoa real para UMA vaga específica.\n"
            "REGRAS ABSOLUTAS:\n"
            "1. Use SOMENTE fatos presentes no PERFIL. Nunca invente empresas, cargos, datas, números, tecnologias, cursos ou certificações.\n"
            "2. Tecnologias citadas no currículo precisam existir em skills.primary, skills.secondary, skills.also_experienced ou skills.familiar do perfil (ou no stack de alguma experiência).\n"
            "   - primary/secondary/also_experienced podem ser destaque em Competências e no headline (ex.: vaga PHP -> 'Desenvolvedor PHP' se PHP estiver em also_experienced). "
            "familiar só pode aparecer em 'Outros conhecimentos', nunca como principal.\n"
            "   - Nos BULLETS de uma experiência, cite tecnologias do 'stack'/bullets daquela experiência ou, se a regra 10 permitir, da lista de adaptação.\n"
            "3. Você PODE: escolher quais experiências/bullets entram, reordenar, reescrever bullets para enfatizar o que a vaga pede "
            "(cada bullet do currículo deve derivar de um bullet existente ou do 'stack' daquela experiência; NÃO crie bullets novos, genéricos ou vagos), "
            "usar o 'title_alt' de uma experiência (só em vagas senior/lead; em júnior/pleno use o 'title' original) ou 'title_en' em currículos em inglês, ajustar o headline e o resumo.\n"
            "4. Você NÃO PODE: mudar cargos (exceto usar title_alt/title_en), datas, empresas; citar anos de experiência acima de skill_years/years_experience_total "
            "(esses números já vêm ajustados para o nível da vaga; use-os como estão).\n"
            f"5. Adaptação de nível: a vaga é '{seniority}'. "
            + ("Inclua TODAS as experiências do perfil (o currículo pode ter 2 páginas), em ordem cronológica decrescente; nas menos relevantes use 1-2 bullets. "
               if max_experiences >= len(profile.get("experiences") or []) else
               f"Inclua no máximo {max_experiences} experiências (as mais relevantes/recentes). ")
            + 
            "Para júnior/pleno: bullets mais hands-on, sem ênfase em liderança (prefira bullets_ic quando existirem), resumo mais curto e SEM citar anos totais de experiência (as datas já aparecem). "
            "Para sênior/lead: destaque impacto, arquitetura, liderança técnica.\n"
            f"6. Idioma do currículo: {language}. Traduza bullets se necessário mantendo o sentido.\n"
            "7. ESTILO (não pode parecer gerado por IA): frases curtas, verbo de ação no início, números reais quando existirem no perfil, "
            "zero adjetivos vazios (apaixonado, dinâmico, proativo, comprometido), zero clichês ('trabalho em equipe', 'fácil adaptação'), "
            "sem emojis, sem repetição de palavras-chave forçada. Siga 'voice.notes' do perfil. Cada experiência: 2 a 5 bullets.\n"
            "8. O headline deve espelhar o cargo da vaga quando compatível (ex.: vaga 'Desenvolvedor PHP Pleno' -> 'Desenvolvedor PHP' ou 'Desenvolvedor Back-end PHP').\n"
            "9. skills.main: 6 a 10 itens, primeiro as que a vaga pede e o candidato tem, depois as primary mais relevantes. "
            "skills.other: no máximo 8 itens relevantes para a vaga (não liste tudo). Certificações: devolva lista vazia (são escolhidas pelo sistema).\n"
            "Saída JSON:\n"
            "{\n"
            '  "language": "pt-BR|en",\n'
            '  "headline": "...",\n'
            '  "summary": "2-4 frases",\n'
            '  "skills": {"main": ["..."], "other": ["..."]},\n'
            '  "experiences": [{"company": "...", "title": "...", "start": "AAAA-MM", "end": "AAAA-MM ou vazio", "location": "...", "bullets": ["..."]}],\n'
            '  "education": [{"institution": "...", "degree": "...", "period": "..."}],\n'
            '  "certifications": [{"name": "...", "issuer": "...", "year": "..."}],\n'
            '  "languages": [{"name": "...", "level": "..."}],\n'
            '  "section_titles": {"summary": "...", "skills": "...", "other_skills": "...", "experience": "...", "education": "...", "certifications": "...", "languages": "..."}\n'
            "}\n"
            "section_titles no idioma do currículo (ex.: pt-BR: Resumo, Competências, Outros conhecimentos, Experiência, Formação, Certificações, Idiomas).\n"
            + restack_rule + (extra_instruction + "\n" if extra_instruction else "")
        )
        user = (
            f"PERFIL (fonte única de verdade):\n{json.dumps(profile, ensure_ascii=False)}\n\n"
            f"VAGA:\nTítulo: {job.get('title')}\nEmpresa: {job.get('company')}\nLocal: {job.get('location')}\n"
            f"Classificação: {json.dumps(classification, ensure_ascii=False)}\n\n"
            f"Descrição:\n{(job.get('description') or '')[:7000]}"
        )
        return self.json(model=self.cfg.smart_model, system=system, user=user, purpose="tailor_resume", temperature=0.5)

    def restack_bullets(self, *, resume: dict, job: dict, adaptation: dict, language: str,
                        blocked: list[str] | None = None) -> list[dict]:
        """Segunda passada BARATA: reescreve só os bullets para citar a stack da vaga.

        A primeira chamada manda o perfil inteiro (~15k tokens). Quando só a fração de bullets
        ficou abaixo da meta, não há por que repetir isso: aqui vão apenas os bullets já gerados
        e a lista de tecnologias permitidas. Custa cerca de 1/5 de uma chamada completa.
        """
        blocked_n = {re.sub(r"[^a-z0-9]+", "", (b or "").lower()) for b in (blocked or [])}
        stack = adaptation.get("job_stack") or []
        allowed = adaptation.get("allowed") or []
        share = int((adaptation.get("min_stack_share") or 0) * 100)
        exps = []
        for e in resume.get("experiences") or []:
            comp = str(e.get("company") or "")
            if re.sub(r"[^a-z0-9]+", "", comp.lower()) in blocked_n:
                continue          # experiência com restack: false mantém a stack original
            exps.append({"company": comp, "title": e.get("title"), "bullets": e.get("bullets") or []})
        if not exps or not stack or not allowed:
            return []
        system = (
            "Você reescreve bullets de currículo de uma pessoa real. Responda em JSON estrito.\n"
            f"Objetivo: pelo menos {share}% dos bullets devem citar a stack da vaga ({', '.join(stack)}).\n"
            "REGRAS ABSOLUTAS:\n"
            "1. NÃO invente: mantenha empresa, cargo, datas, clientes, números e o que foi construído. Só troque/acrescente a TECNOLOGIA.\n"
            f"2. Só pode citar tecnologias desta lista: {', '.join(allowed)}. Nunca cite nada fora dela.\n"
            "3. Use cada tecnologia de forma plausível para o papel daquela experiência (não ponha PHP numa experiência de Salesforce).\n"
            "4. Mantenha o MESMO número de bullets por experiência e o estilo: frase curta, verbo de ação no início, sem adjetivo vazio.\n"
            f"5. Idioma: {language}.\n"
            'Saída: {"experiences": [{"company": "...", "bullets": ["..."]}]}'
        )
        user = (
            f"VAGA: {job.get('title')} @ {job.get('company')}\n"
            f"STACK DA VAGA: {', '.join(stack)}\n\n"
            f"BULLETS ATUAIS:\n{json.dumps(exps, ensure_ascii=False)}"
        )
        out = self.json(model=self.cfg.smart_model, system=system, user=user, purpose="restack_bullets", temperature=0.4)
        return list(out.get("experiences") or [])

    # ---------- respostas de formulário ----------
    def answer_questions(self, *, questions: list[dict], profile: dict, bank_hits: list[dict],
                         job: dict, always_ask_topics: list[str]) -> list[dict]:
        system = (
            "Você preenche formulários de candidatura em nome de um candidato real. Responda em JSON estrito.\n"
            "Fontes de verdade, nesta ordem: (1) BANCO DE RESPOSTAS (respostas que o próprio candidato já deu), (2) PERFIL.\n"
            "Regras:\n"
            "- Para campos select/radio/combobox: 'answer' DEVE ser exatamente uma das 'options' (copie o texto). Escolha a mais compatível com o perfil; "
            "se nenhuma se aplica de fato e a pergunta é de diversidade/EEO, escolha a opção de 'prefiro não responder' quando existir.\n"
            "- Para checkbox de termos/LGPD/consentimento/privacidade: answer 'true'.\n"
            "- Para múltipla escolha (multiselect): 'answer' é uma lista com as opções aplicáveis.\n"
            "- Anos de experiência: use skill_years do perfil; se a tecnologia não está lá, use um número coerente com skills (familiar = 1, secondary = 2, primary = years_experience_total mínimo 3). Nunca 0 se a tecnologia consta no perfil.\n"
            "- Perguntas abertas ('por que quer trabalhar aqui', 'fale sobre você', 'descreva um projeto'): escreva 2-5 frases em primeira pessoa, específicas, "
            "citando fatos do perfil e algo da vaga/empresa. Sem clichês, sem tom de IA, no idioma do formulário.\n"
            "- Campos que pedem número (salário, anos) devem conter só o número quando o tipo é number.\n"
            "- PRETENSÃO SALARIAL: se a vaga informa salário ou faixa ('salary_offered'), responda exatamente esse valor (ou o topo da faixa). "
            "Senão use facts.salary_expectation_<nível>_<regime> do perfil, escolhendo nível pela 'seniority' da vaga (junior|mid|senior) "
            "e regime pelo 'contract_type' (CLT por padrão). Em campos de faixa (select), escolha a faixa que contém esse valor.\n"
            f"- Temas que exigem decisão do candidato quando NÃO houver resposta no banco/perfil: {', '.join(always_ask_topics)}. "
            "Nesses casos, retorne needs_user=true e uma 'suggested' com seu melhor palpite.\n"
            "- DADOS PESSOAIS (CEP, endereço, bairro, número, RG, data de nascimento, documento, nome da mãe): SOMENTE se estiverem no PERFIL. "
            "Se não estiverem, NÃO invente (nem 'Centro', nem um CEP plausível): retorne answer vazio, needs_user=true e suggested vazio.\n"
            "- 'confidence' de 0 a 1: quão certo você está de que o candidato responderia assim.\n"
            "- 'topic': tema curto normalizado da pergunta (ex.: 'pretensão salarial', 'anos de experiência PHP', 'consentimento LGPD').\n"
            "- 'company_specific': true se a resposta só vale para esta empresa/vaga (ex.: 'por que quer trabalhar na X'). Essas não vão para o banco.\n"
            'Saída: {"answers": [{"id": "...", "answer": "...", "confidence": 0.0, "needs_user": false, "suggested": "", "topic": "...", "company_specific": false}]}'
        )
        cls = job.get("classification") or {}
        # perfil primeiro (parte estável) para o cache de prefixo da OpenAI reaproveitar entre chamadas
        user = (
            f"PERFIL:\n{json.dumps(profile, ensure_ascii=False)}\n\n"
            f"BANCO DE RESPOSTAS (mais parecidas):\n{json.dumps(bank_hits, ensure_ascii=False)}\n\n"
            f"VAGA: {job.get('title')} @ {job.get('company')} ({job.get('location')})\n"
            f"seniority: {cls.get('seniority', 'unspecified')} · contract_type: {cls.get('contract_type', 'unspecified')} · "
            f"salary_offered: {cls.get('salary_offered') or '(não informado)'} · work_model: {cls.get('work_model', '')}\n"
            f"Resumo da vaga: {(job.get('description') or '')[:2500]}\n\n"
            f"PERGUNTAS DO FORMULÁRIO:\n{json.dumps(questions, ensure_ascii=False)}"
        )
        out = self.json(model=self.cfg.cheap_model, system=system, user=user, purpose="answer_questions", temperature=0.2)
        return out.get("answers", [])

    # ---------- importação de perfil ----------
    def extract_profile(self, text: str, template_yaml: str) -> dict:
        system = (
            "Extraia um perfil profissional estruturado a partir do texto de um currículo/perfil do LinkedIn. "
            "Responda em JSON com EXATAMENTE a estrutura do template YAML fornecido (mesmas chaves). "
            "Não invente: campos sem informação ficam vazios. Bullets devem ser fiéis ao texto, um por linha de realização. "
            "Preencha skills.primary com as tecnologias claramente dominantes, secondary com as citadas em projetos, familiar com as só listadas. "
            "Estime skill_years pelos períodos das experiências em que a tecnologia aparece."
        )
        user = f"TEMPLATE (YAML):\n{template_yaml}\n\nTEXTO DO LINKEDIN:\n{text[:30000]}"
        return self.json(model=self.cfg.smart_model, system=system, user=user, purpose="import_profile", temperature=0)

    # ---------- cursos a fazer ----------
    def suggest_courses(self, stack: str, n: int = 3, min_udemy: int = 3) -> list[dict]:
        system = (
            f"Sugira cursos populares e bem avaliados para a tecnologia pedida: PELO MENOS {min_udemy} da Udemy (em português quando existirem, "
            "com nome exato como aparece na plataforma) e opcionalmente 1-2 de Alura/Rocketseat/Coursera. Responda em JSON: "
            '{"courses": [{"name": "nome exato do curso", "issuer": "plataforma", "hours": 0, "url": "", "free": false}]}. '
            "Se não tiver certeza do nome exato, use o nome mais provável e marque url vazia."
        )
        out = self.json(model=self.cfg.cheap_model, system=system, user=f"Tecnologia: {stack}. Quantidade total: {max(n, min_udemy + 1)}.", purpose="suggest_courses", temperature=0.3)
        courses = out.get("courses", [])
        udemy = [c for c in courses if "udemy" in str(c.get("issuer", "")).lower()]
        others = [c for c in courses if c not in udemy]
        return (udemy[:max(min_udemy, n)] + others)[: max(n, min_udemy + 2)]
