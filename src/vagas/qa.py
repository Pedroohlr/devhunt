"""Motor de respostas: regras -> banco de respostas (fuzzy) -> LLM -> você."""
from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import yaml

from .budget import BudgetExceeded
from rapidfuzz import fuzz, process

from .config import LLMCfg
from .db import DB, Job
from .llm import LLM
from .resume.profile import profile_for_forms, scale_profile_for_seniority



def _norm_opt(t: str) -> str:
    t = unicodedata.normalize("NFKD", str(t or "")).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", t).strip().lower()


def best_option(value: str, options: list[str], min_score: int = 60) -> str | None:
    """Melhor opção para um valor. Ordem: igual (sem acento/caixa) > opção começa pelo valor > valor aparece como
    palavra inteira (a MAIS CURTA vence: "LinkedIn" bate "LinkedIn" e não "Headhunting (LinkedIn Recruiter...)")
    > fuzzy (token_set_ratio, desempate pela menor opção)."""
    if not options:
        return None
    v = _norm_opt(value)
    if not v:
        return None
    normed = [(_norm_opt(o), o) for o in options]
    for n, o in normed:
        if n == v:
            return o
    starts = [o for n, o in normed if n.startswith(v + " ") or n.startswith(v + ":") or n.startswith(v + " -") or n.startswith(v + " (") or n.startswith(v + ",") or n.startswith(v + "/")]
    if starts:
        return min(starts, key=len)
    whole = [o for n, o in normed if re.search(r"(^|[^a-z0-9])" + re.escape(v) + r"($|[^a-z0-9])", n)]
    if whole:
        return min(whole, key=len)
    if re.fullmatch(r"[\d.,\s]+", v):
        return None   # número (salário, anos): fuzzy escolhia "R$ 1.000 - R$ 2.000" para 9000; faixas são tratadas à parte
    scored = [(fuzz.token_set_ratio(v, n), -len(n), o) for n, o in normed]
    scored.sort(reverse=True)
    sc, _, o = scored[0]
    need = max(min_score, 85) if len(options) > 50 else min_score   # lista grande (universidades): fuzzy frouxo escolhe errado
    return o if sc >= need else None


EXPERIENCE_MULTI_RE = re.compile(
    r"(possui|tem|tenha|voce tem|ja teve|tiver) (experiencia|conhecimento|vivencia|dominio|familiaridade)|"
    r"(experiencia|conhecimento|vivencia|dominio|familiaridade|habilidade|competencia|skills?)( profissional| pratic[ao])? (com|em|nas?|nos?|de)\b|"
    r"quais (das |dos |desses |destas |destes |deles |delas )?(tecnologias|ferramentas|linguagens|frameworks|areas|itens|opcoes|habilidades|competencias|skills|stacks?|bancos|plataformas|servicos|clouds?|metodologias)|"
    r"(ja )?(trabalhou|atuou|utilizou|usou|programou) (com|em)\b|domina|conhece|which of the following|experience with|skills? (you|do you) have",
    re.I,
)
EXPERIENCE_YESNO_RE = re.compile(
    r"(possui|tem|tenha|voce tem|ja teve|tiver|tem alguma) (experiencia|conhecimento|vivencia|dominio|familiaridade)|"
    r"(ja )?(trabalhou|atuou|utilizou|usou|programou|desenvolveu) (com|em|usando)\b|domina|conhece|(sabe|consegue) (desenvolver|criar|trabalhar|programar|usar|utilizar)|"
    r"experiencia (com|em|usando)|do you have experience|have you worked with|are you (familiar|proficient)",
    re.I,
)
YESNO_OPT_RE = re.compile(r"^(sim|yes|nao|não|no)\b", re.I)
LEVEL_LADDER = ["Básico", "Intermediário", "Avançado", "Especialista"]
LANG_LEVEL_FRAC = {"nativo": 1.0, "fluente": 1.0, "proficiente": 1.0, "avancado": 0.75, "intermediario": 0.5, "basico": 0.25}
NEG_OPT_RE = re.compile(r"^\s*(n[aã]o|no|nenhum|nunca|sem )|n[aã]o tive|n[aã]o tenho|n[aã]o possuo|n[aã]o conhe", re.I)
SKIP = "__skip__"   # regra decidiu deixar o campo em branco (ex.: widget de alerta de vagas)
TECH_SYNONYMS = {"sql": "postgresql", ".net": "asp.net", "net": "asp.net", "dotnet": "asp.net", "net core": "asp.net", "node": "node.js", "nodejs": "node.js",
                 "reactjs": "react", "vue": "vue.js", "js": "javascript", "ts": "typescript", "postgres": "postgresql"}


def pick_graded(options: list[str], frac: float) -> str | None:
    """Opção de uma escala crescente (Básico..Especialista, "Não tive contato".."Uso avançado") pela fração 0..1."""
    opts = [o for o in options if o and not re.search(r"prefiro n[aã]o", o, re.I)]
    if not opts:
        return None
    for i, word in enumerate(LEVEL_LADDER):     # escala com os nomes usuais: casa pelo nome
        if abs(i / (len(LEVEL_LADDER) - 1) - frac) < 0.17:
            m = best_option(word, opts)
            if m:
                return m
    pos = [o for o in opts if not NEG_OPT_RE.search(o)] or opts
    if frac <= 0 and NEG_OPT_RE.search(opts[0]):
        return opts[0]
    return pos[min(len(pos) - 1, max(0, math.ceil(frac * (len(pos) - 1) - 1e-9)))]


NEUTRAL_OPT_RE = re.compile(r"prefiro n[aã]o|n[aã]o (se aplica|possuo|tenho|sei|desejo)|nenhum|^outr[oa]s?\b|^other|none|n/?a\b", re.I)


def options_for_llm(label: str, options: list[str], hints: list[str], limit: int = 25) -> list[str]:
    """Corta listas enormes (país, cidade, código de telefone) antes de mandar para a IA.

    Numa leva real, 8 perguntas com 60 opções davam ~6.700 tokens só de opções. Ficam as opções
    mais parecidas com a pergunta e com os dados do perfil, mais as neutras ("prefiro não",
    "não se aplica"), na ordem original. A resposta da IA é casada de volta contra a lista inteira.
    """
    if len(options) <= limit:
        return options
    refs = [normalize(h) for h in ([label] + hints) if h]
    def score(o: str) -> float:
        no = normalize(o)
        return max((fuzz.partial_ratio(no, r) for r in refs), default=0)
    keep = {o for o in options if NEUTRAL_OPT_RE.search(o)}
    for o in sorted(options, key=score, reverse=True):
        if len(keep) >= limit:
            break
        keep.add(o)
    return [o for o in options if o in keep]


OPEN_QUESTION_RE = re.compile(r"por que|porque|fale (sobre|um pouco)|descreva|conte(-nos)?|motiv|o que te|apresente-se|carta|cover letter|why (do|are) you|tell us|describe", re.I)
PARENT_NAME_RE = re.compile(r"\bnome\b.{0,25}\b(mae|pai|genitora?|genitor)\b|^\W*(mae|pai|genitora?)\W*$|(mother|father)( ?'?s)? (full )?(name|family name|maiden name)|filia[cç][aã]o", re.I)
# rótulo fala de OUTRA pessoa: regra genérica de "nome completo" nunca pode responder com o nome do candidato
OTHER_PERSON_RE = re.compile(r"\b(mae|pai|genitora?|conjuge|esposa|esposo|marido|companheir[oa]|filh[oa]s?|responsavel|contato de emergencia|emergencia|referencia|quem (te )?indicou|indicad[oa] por|recrutador|gestor|lider|parente|familiar|mother|father|spouse|emergency contact|reference|referral)\b", re.I)
NEVER_CHECK_OPT_RE = re.compile(r"nenhum|nenhuma|n[aã]o (possuo|tenho|conhe[cç]o|se aplica)|none|n/a|^outr[oa]s?\b|^other", re.I)


def experience_multi_answer(options: list[str]) -> list[str]:
    """Todas as opções menos uma (a última), pulando 'nenhuma/não possuo/outros'."""
    cands = [o for o in options if not NEVER_CHECK_OPT_RE.search(o.strip())]
    if len(cands) <= 1:
        return cands
    return cands[:-1]


AREA_LABEL_RE = re.compile(r"area(s)? de (interesse|atuacao|formacao|especializacao)|area profissional|categoria (profissional|da vaga)?|segmento de atuacao|setor de atuacao|^areas?$", re.I)
TECH_AREA_PRIORITY = [
    re.compile(r"desenvolvimento de (software|sistemas)|engenharia de software|programa[cç][aã]o", re.I),
    re.compile(r"tecnologia da informa|^ti\b|\bti\b|t\.i\.", re.I),
    re.compile(r"inform[aá]tica|sistemas|software|tecnologia|desenvolv|computa", re.I),
]


def tech_area_answer(options: list[str]) -> str | None:
    """Primeira opção de tecnologia/desenvolvimento na ordem de prioridade (regra do usuário: sempre dev)."""
    for pat in TECH_AREA_PRIORITY:
        hits = [o for o in options if pat.search(o)]
        if hits:
            return min(hits, key=len)
    return None


def hourly_answer(profile: dict, job: Job) -> str | None:
    """Valor/hora (PJ): faixa por hora informada na vaga (topo) ou pretensão PJ mensal do nível / 160 h, múltiplo de 5."""
    cls = job.classification or {}
    offered = str(cls.get("salary_offered") or "")
    if re.search(r"hora|/h\b|hour", offered, re.I):
        nums = re.findall(r"\d+(?:[.,]\d+)?", offered)
        if nums:
            return str(int(round(float(nums[-1].replace(",", ".")))))
    facts = profile.get("facts") or {}
    sen = job_seniority(job)
    level = {"junior": "junior", "mid": "mid"}.get(sen, "senior")
    val = str(facts.get(f"salary_expectation_{level}_pj") or facts.get(f"salary_expectation_{level}_clt") or "")
    nums = re.findall(r"\d[\d.]*", val.replace(",", "."))
    if not nums:
        return None
    monthly = float(nums[-1].replace(".", ""))
    hourly = max(5, int(round(monthly / 160 / 5.0)) * 5)
    return str(hourly)


LEVEL_SCALE = {"junior": "3", "mid": "6", "senior": "9"}


def numeric_fallback(compact: dict, job: Job, label: str, current: str) -> str | None:
    """Campo que exige NÚMERO mas recebeu texto ("Sênior"): anos -> anos; valor/hora -> hourly; nível -> escala 1-10;
    senão os dígitos do valor atual."""
    nl = normalize(label)
    if re.search(r"anos|years|quanto tempo|how long", nl):
        v = years_answer(compact, label)
        if v:
            return v
    if re.search(r"hora|hour", nl):
        return hourly_answer({"facts": compact.get("facts") or {}}, job)
    if SALARY_RE.search(nl):
        return salary_answer({"facts": compact.get("facts") or {}}, job, numeric=True, label=label)
    if re.search(r"nivel|level|avalia|escala|de 0 a|de 1 a|nota", nl):
        sen = job_seniority(job)
        return LEVEL_SCALE.get({"junior": "junior", "mid": "mid"}.get(sen, "senior"), "9")
    digits = re.sub(r"\D", "", current or "")
    return digits or None


def salary_range_option(value: int, options: list[str]) -> str | None:
    """Opção de faixa salarial que contém o valor (ou a mais próxima)."""
    best, best_d = None, None
    for o in options:
        nums = [int(n.replace(".", "").replace(",", "")) for n in re.findall(r"\d{1,3}(?:[.,]\d{3})+|\d{3,}", o)]
        nums = [n for n in nums if n >= 100]
        if not nums:
            continue
        lo, hi = min(nums), max(nums)
        if re.search(r"acima|mais de|\+|superior|above|more than", o, re.I) and len(nums) == 1:
            hi = 10**9
        if re.search(r"at[eé]|abaixo|menos de|below|up to", o, re.I) and len(nums) == 1:
            lo = 0
        if lo <= value <= hi:
            return o
    # sem faixa que contenha o valor: NÃO chutar a "mais próxima" (lista pode estar parcial: virou "Até R$ 2.000" na Stone)
    return None


UF_NAMES = {"AC": "Acre", "AL": "Alagoas", "AP": "Amapá", "AM": "Amazonas", "BA": "Bahia", "CE": "Ceará", "DF": "Distrito Federal", "ES": "Espírito Santo",
            "GO": "Goiás", "MA": "Maranhão", "MT": "Mato Grosso", "MS": "Mato Grosso do Sul", "MG": "Minas Gerais", "PA": "Pará", "PB": "Paraíba", "PR": "Paraná",
            "PE": "Pernambuco", "PI": "Piauí", "RJ": "Rio de Janeiro", "RN": "Rio Grande do Norte", "RS": "Rio Grande do Sul", "RO": "Rondônia", "RR": "Roraima",
            "SC": "Santa Catarina", "SP": "São Paulo", "SE": "Sergipe", "TO": "Tocantins"}


def alt_values(label: str, v: str) -> list[str]:
    """Formas alternativas do mesmo valor para casar com as opções do formulário."""
    nl = normalize(label); out = []
    if re.search(r"\b(estado|state|uf)\b", nl):
        if v.upper() in UF_NAMES: out.append(UF_NAMES[v.upper()])
        for uf, name in UF_NAMES.items():
            if normalize(name) == normalize(v): out.append(uf)
    if re.search(r"\b(pais|country)\b", nl) and normalize(v) in ("brasil", "brazil", "br"):
        out += ["Brasil", "Brazil", "BR", "Brasil (BR)"]
    return [a for a in out if normalize(a) != normalize(v)]

class NeedsUserInput(Exception):
    """Levantada quando uma pergunta obrigatória precisa de resposta sua e não estamos em modo interativo."""

    def __init__(self, questions: list[dict]):
        super().__init__(f"{len(questions)} pergunta(s) precisam de você")
        self.questions = questions


@dataclass
class FieldQ:
    id: str
    label: str
    kind: str                 # text|textarea|number|email|tel|url|date|select|radio|checkbox|multiselect|combobox|file
    options: list[str] = field(default_factory=list)
    required: bool = False
    current: str = ""


@dataclass
class Answer:
    value: str | list[str] | bool | None
    source: str               # rule | bank | llm | user | skip
    confidence: float = 1.0
    topic: str = ""


def normalize(q: str) -> str:
    q = unicodedata.normalize("NFKD", q or "").encode("ascii", "ignore").decode()
    q = q.lower()
    q = re.sub(r"\(.*?\)", " ", q)                 # remove parênteses explicativos
    q = re.sub(r"[^a-z0-9%$ ]+", " ", q)
    q = re.sub(r"\b(obrigatorio|obrigatoria|required|opcional|optional)\b", " ", q)
    return re.sub(r"\s+", " ", q).strip()


class AnswerBank:
    def __init__(self, path: Path):
        self.path = path
        self.items: list[dict] = []
        self.load()

    def load(self) -> None:
        if self.path.exists():
            data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
            self.items = list(data.get("answers") or [])
        else:
            self.items = []

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            "# Banco de respostas. Editável. `source: user` tem prioridade sobre `source: llm`.\n"
            + yaml.safe_dump({"answers": self.items}, allow_unicode=True, sort_keys=False, width=120),
            encoding="utf-8",
        )

    def add(self, q: str, a, source: str, topic: str = "", options: list[str] | None = None) -> None:
        nq = normalize(q)
        if not nq:
            return
        for it in self.items:
            if normalize(it.get("q", "")) == nq:
                if source == "user" or it.get("source") != "user":
                    it.update({"a": a, "source": source, "topic": topic or it.get("topic", "")})
                self.save()
                return
        item = {"q": q.strip()[:300], "a": a, "source": source}
        if topic:
            item["topic"] = topic
        if options:
            item["options"] = options[:30]
        self.items.append(item)
        self.save()

    def lookup(self, q: str, options: list[str] | None = None, min_score: int = 88) -> tuple[dict | None, int]:
        nq = normalize(q)
        if not nq or not self.items:
            return None, 0
        choices = {i: normalize(it.get("q", "")) for i, it in enumerate(self.items)}
        best = process.extractOne(nq, choices, scorer=fuzz.token_set_ratio)
        if not best:
            return None, 0
        _, score, idx = best
        it = self.items[idx]
        if score < min_score:
            return None, int(score)
        if options and isinstance(it.get("a"), str):
            # resposta precisa casar com alguma opção do campo
            m = process.extractOne(str(it["a"]), options, scorer=fuzz.token_set_ratio)
            if not m or m[1] < 75:
                return None, int(score)
        return it, int(score)

    def similar(self, q: str, n: int = 6) -> list[dict]:
        nq = normalize(q)
        if not nq or not self.items:
            return []
        choices = {i: normalize(it.get("q", "")) for i, it in enumerate(self.items)}
        hits = process.extract(nq, choices, scorer=fuzz.token_set_ratio, limit=n)
        return [{"q": self.items[i]["q"], "a": self.items[i]["a"]} for _, s, i in hits if s >= 45]


# ---------- regras diretas (sem LLM) ----------
RULES: list[tuple[re.Pattern, Callable[[dict], str | None]]] = [
    # código de discagem do país ("Prefixo do telefone", "Country Phone Code", "Código do país"):
    # antes da regra de telefone, que devolveria o número inteiro. Casa com "Brasil (+55)" pelas opções.
    (re.compile(r"\b(codigo|prefixo|code|ddi|discagem)\b.{0,30}\b(telefone|phone|pais|paises|country)\b|"
                r"\b(telefone|phone)\b.{0,20}\b(codigo|prefixo|code)\b|country (phone|dial(ing)?) code|codigos? de telefone"),
     lambda p: p["personal"].get("country") or "Brasil"),
    # seletor de idioma do formulário/perfil ("Selecionar idioma" com 36 opções no LinkedIn)
    (re.compile(r"^(selecionar |selecione (o |um )?|select (a )?)?(idioma|language)$"), lambda p: "Português"),
    # antes de "e-mail": "Caso tenha sido indicado por colaborador, informe o e-mail dele" não é o e-mail do candidato
    (re.compile(r"^(se|caso) (voce )?(respondeu|marcou|assinalou|tenha respondido|disse) .{0,30}(sim|positiv)|^(se|caso) (sim|positivo)[,:]|"
                r"caso (a )?(resposta|questao|pergunta) (anterior|acima) seja (sim|positiv)|se (a )?resposta (anterior|acima) (for|seja) sim|"
                r"em caso (positivo|afirmativo)|se aplicavel|se marcou sim|caso tenha respondido sim"),
     lambda p: "Não se aplica"),
    (re.compile(r"\b(ja (trabalha|trabalhou|e colaborador|foi colaborador) (para|na|no|em|da|de) (a |o |esta |essa |nossa )?\w|(e|eh|foi|ja foi) (colaborador|funcionario|estagiario) (da|de|na|no|desta|dessa|nesta) (empresa|companhia|\w+)|trabalha atualmente (na|em|para) (nossa|esta|essa|a) empresa|colaborador(a)? (interno|atual|da empresa))\b"),
     lambda p: "Não"),
    (re.compile(r"\b(recomendou voce|foi recomendad[oa]|indicad[oa] por alguem|alguem (que trabalha|da empresa|de dentro).{0,40}(indicou|recomendou)|foi indicad[oa] por (alguem|algum colaborador)|tem alguma indicacao)\b"),
     lambda p: "Não"),
    (re.compile(r"(contat[aá][- ]?l[oa]|contato|contatar|falar|mensagens?|chamar|comunica[cç][aã]o).{0,20}\bwhatsapp\b|\bwhatsapp\b.{0,30}(contato|atualiza|mensag|comunica)"),
     lambda p: "Sim"),
    (re.compile(r"\b(caso (voce )?seja (colaborador|funcionario|empregado)|se (voce )?(ja )?(for|e|eh|foi) (colaborador|funcionario)|(numero d[ae] |sua |informe a )?matricula\b|colaborador(a)? interno)"),
     lambda p: "Não se aplica"),
    (re.compile(r"\b(caso (voce )?tenha sido indicad|foi indicad[oa] por|indicad[oa] por (algum|alguma|um|uma) (colaborador|funcionario)|nome d[oa] (colaborador|funcionario)(a)? que (te|lhe|o|a) indicou|codigo de indicacao|quem (te|lhe|o|a) indicou)\b"),
     lambda p: "Não se aplica"),
    (re.compile(r"\b(nome completo|full name|seu nome completo)\b"), lambda p: p["personal"].get("full_name")),
    (re.compile(r"\b(primeiro nome|first name|nome de preferencia|preferred name)\b|^nome$|^name$"), lambda p: p["personal"].get("first_name") or (p["personal"].get("full_name") or "").split(" ")[0]),
    (re.compile(r"\b(seu nome)\b"), lambda p: p["personal"].get("full_name")),
    (re.compile(r"\b(sobrenome|last name|surname|family name)\b"), lambda p: p["personal"].get("last_name") or " ".join((p["personal"].get("full_name") or "").split(" ")[1:])),
    (re.compile(r"\b(e ?mail|email address)\b"), lambda p: p["personal"].get("email")),
    (re.compile(r"\b(telefone|celular|phone|whatsapp|mobile|contato telefonico)\b"), lambda p: p["personal"].get("phone")),
    (re.compile(r"\blinkedin\b"), lambda p: p["personal"].get("linkedin")),
    (re.compile(r"\bgithub\b"), lambda p: p["personal"].get("github")),
    (re.compile(r"\b(portfolio|portfolio|website|site pessoal|personal site)\b"), lambda p: p["personal"].get("portfolio") or p["personal"].get("github")),
    (re.compile(r"\b(cidade|city)\b"), lambda p: p["personal"].get("city")),
    (re.compile(r"\b(bairro|neighbou?rhood)\b"), lambda p: p["personal"].get("neighborhood")),
    (re.compile(r"\b(cep|zip ?code|postal ?code|codigo postal)\b"), lambda p: p["personal"].get("zip")),
    (re.compile(r"\b(endereco completo|endereco|address|street address)\b"), lambda p: p["personal"].get("address")),
    (re.compile(r"\b(logradouro|rua|street( name)?|avenida)\b"), lambda p: p["personal"].get("street") or p["personal"].get("address")),
    (re.compile(r"^(n[uú]mero|numero|number|n[º°o]\.?)$|numero (da residencia|do endereco|da casa)|house number|street number"), lambda p: p["personal"].get("address_number")),
    (re.compile(r"\b(complemento|complement)\b"), lambda p: "Não se aplica"),
    (re.compile(r"\b(estado|state|uf)\b"), lambda p: p["personal"].get("state")),
    (re.compile(r"\b(pais|country)\b"), lambda p: p["personal"].get("country")),
    (re.compile(r"\b(data de nascimento|birth ?date|date of birth|nascimento)\b"), lambda p: p["personal"].get("birth_date")),
    (re.compile(r"\b(cargo atual|current title|current job title|headline|titulo profissional)\b"), lambda p: p.get("headline")),
    (re.compile(r"\b(empresa atual|current company|current employer|empresa (em que|onde|na qual) (voce )?trabalha|company where you (currently )?work|where do you (currently )?work)\b"),
     lambda p: ("Não estou trabalhando no momento" if str((p.get("facts") or {}).get("currently_employed", "")).strip().lower().startswith(("n", "false"))
                else (p.get("experiences") or [{}])[0].get("company"))),
    (re.compile(r"\b(como (voce )?(ficou sabendo|conheceu|soube|chegou|encontrou|descobriu)|how did you hear|where did you (hear|find)|fonte da vaga|onde (voce )?(viu|encontrou|achou|conheceu|descobriu) (a |essa |esta |nossa |essa nossa |esta nossa )?vaga|por onde (conheceu|chegou)|canal de (origem|divulgacao)|origem da candidatura)\b"),
     lambda p: p["facts"].get("how_did_you_hear")),
    (re.compile(r"\b(escola|school|instituicao( de ensino)?|universidade|faculdade|university|college)\b"),
     lambda p: ((p.get("education") or [{}])[0].get("institution") or (p.get("education") or [{}])[0].get("school"))),
    (re.compile(r"\b(curso|graduacao|formacao|degree|major|field of study|area de formacao|disciplina)\b"),
     lambda p: ((p.get("education") or [{}])[0].get("degree") or (p.get("education") or [{}])[0].get("course"))),
    (re.compile(r"\b(nacionalidade|nationality)\b"), lambda p: p["personal"].get("nationality")),
    (re.compile(r"\b(cpf)\b"), lambda p: p["personal"].get("cpf") or p["facts"].get("cpf")),
    (re.compile(r"^(documento|numero d[oe] documento|documento de identificacao)$"), lambda p: p["personal"].get("cpf") or p["facts"].get("cpf")),   # "Documento" sozinho = CPF no Brasil
    (re.compile(r"\b(rg|identidade|documento de identidade|numero do rg)\b"), lambda p: p["personal"].get("rg")),
    (re.compile(r"\b(ano|year)\b.{0,20}\b(inicio|start)\b|\b(inicio|start).{0,20}\b(ano|year)\b|start year|ano de ingresso"),
     lambda p: str((p.get("education") or [{}])[0].get("start") or "")),
    (re.compile(r"\b(ano|year)\b.{0,20}\b(termino|conclusao|fim|end|graduation)\b|\b(termino|conclusao|end).{0,20}\b(ano|year)\b|end year|ano de conclusao"),
     lambda p: str((p.get("education") or [{}])[0].get("end") or "")),
    (re.compile(r"\b(disponibilidade|availability|data de inicio|start date|quando pode (comecar|iniciar))\b"), lambda p: p["facts"].get("availability")),
    (re.compile(r"\b(aviso previo|notice period)\b"), lambda p: p["facts"].get("notice_period")),
]
HEAR_RE = re.compile(r"como (voce )?(ficou sabendo|conheceu|soube|chegou|encontrou|descobriu)|how did you hear|where did you (hear|find)|fonte da vaga|onde (voce )?(viu|encontrou|achou|conheceu|descobriu) (a |essa |esta |nossa |essa nossa |esta nossa )?vaga|por onde (conheceu|chegou)|canal de (origem|divulgacao)|origem da candidatura", re.I)
SALARY_RE = re.compile(r"pretens|preten\b|preten\.|sal[aá]ri|salary|remunera|expectativa (salarial|de remunera|de valor|financeira)|compensation|valor (mensal|pretendido|esperado|desejado)|quanto (voce )?(espera|pretende|gostaria de) (receber|ganhar)", re.I)
YEARS_RE = re.compile(r"quantos anos|h[aá] quanto tempo|anos de experi|tempo de experi|years of experience|how many years|how long have you", re.I)



def job_seniority(job) -> str:
    """Senioridade efetiva: o título manda quando é explícito (o classificador marcou 'Tech Lead - Data Platform' como junior)."""
    cls = (getattr(job, "classification", None) or {})
    title = normalize(getattr(job, "title", "") or "")
    if re.search(r"\b(tech ?lead|lead|lider|senior|sr\b|especialista|specialist|staff|principal|arquitet|architect|head|coordena|gerente|manager)\b", title):
        return "senior"
    if re.search(r"\b(pleno|mid|mid-level|\biii?\b)\b", title):
        return "mid"
    if re.search(r"\b(junior|jr\b|estagi|trainee|aprendiz|entry)\b", title):
        return "junior"
    return cls.get("seniority") or "senior"

def years_answer(compact: dict, label: str) -> str | None:
    """Anos de experiência (só dígitos) a partir de skill_years (já escalado por nível) ou do total."""
    sy = compact.get("skill_years") or {}
    nl = normalize(label)
    best = None
    for tech, years in sy.items():
        t = normalize(str(tech))
        if t and (re.search(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])", nl) or (len(t) >= 4 and t in nl)):
            if best is None or len(t) > len(best[0]):
                best = (t, years)
    if best:
        return str(int(best[1]))
    # tecnologia específica que não está em skill_years ("Há quantos anos você usa Amazon Connect?"):
    # usa o padrão declarado (skill_years_default, já escalado), nunca o total de carreira
    default = compact.get("skill_years_default")
    if default and re.search(r"\b(usa|utiliza|trabalha com|experiencia (com|em|usando)|conhece|domina|atua com)\b", nl):
        return str(int(default))
    total = compact.get("years_experience_total")
    return str(int(total)) if total else None


def salary_answer(profile: dict, job: Job, numeric: bool = False, label: str = "") -> str | None:
    """REGRA: salário informado na vaga vence; senão pretensão por nível e regime.
    O regime vem do rótulo quando ele diz ("pretensão como PJ" / "como CLT"); senão da classificação."""
    cls = job.classification or {}
    offered = (cls.get("salary_offered") or "").strip()
    facts = profile.get("facts") or {}
    nl = normalize(label or "")
    if offered and not re.search(r"\b(pj|cnpj|pessoa juridica|clt)\b", nl):
        val = offered
    else:
        sen = job_seniority(job)
        level = {"junior": "junior", "mid": "mid"}.get(sen, "senior")
        if re.search(r"\b(pj|cnpj|pessoa juridica)\b", nl):
            regime = "pj"
        elif re.search(r"\bclt\b", nl):
            regime = "clt"
        else:
            regime = "pj" if str(cls.get("contract_type") or "").upper() == "PJ" else "clt"
        val = facts.get(f"salary_expectation_{level}_{regime}") or facts.get(f"salary_expectation_{regime}") or ""
    if not val:
        return None
    if numeric:
        nums = re.findall(r"\d[\d.]*", val.replace(",", "."))
        if nums:
            return nums[-1].replace(".", "")   # topo da faixa, só dígitos
    return val


class QAEngine:
    def __init__(self, cfg: LLMCfg, llm: LLM, db: DB, profile: dict, bank: AnswerBank,
                 interactive: bool = False, ask_user: Callable[[FieldQ, str | None], str | None] | None = None,
                 seniority_factors: dict[str, float] | None = None):
        self.cfg = cfg
        self.seniority_factors = seniority_factors or {"junior": 0.25, "mid": 0.5, "senior": 1.0, "lead": 1.0, "unspecified": 1.0}
        self.llm = llm
        self.db = db
        self.profile = profile
        self.compact = profile_for_forms(profile)
        self.bank = bank
        self.interactive = interactive
        self.ask_user = ask_user

    # ----- camadas -----
    # diversidade: grupos como checkboxes
    DIVERSITY_GROUP_RE = re.compile(r"pessoa (preta|parda|negra)|ind[ií]gena|^mulher\b|pessoa com defici|lgbt|pessoa trans|refugiad|imigrante|60\+|idos", re.I)
    NONE_GROUP_RE = re.compile(r"n[aã]o perten[cç]o|nenhum dos grupos|nenhuma das op", re.I)
    DIVERSITY_SELECT_RULES = [
        (re.compile(r"orienta[cç][aã]o sexual|sexual ?orientation", re.I), "sexual_orientation"),
        (re.compile(r"identidade de g[eê]nero|g[eê]nero|gender ?identity|gender", re.I), "gender_identity"),
        (re.compile(r"cor ou ra[cç]a|ra[cç]a|cor/ra|etnia|ethnicity|race|colou?r ?and ?ethnicity", re.I), "race_color"),
        (re.compile(r"defici[eê]ncia|pcd|disability|people ?with ?disability", re.I), "disability"),
    ]
    GENERIC_LABEL_RE = re.compile(r"^(selecione|select|escolha|choose|informe|digite|op[cç][aã]o)\b", re.I)

    NO_CHECK_RE = re.compile(r"preferencial|top choice|^\s*seguir\b|^\s*follow\b|seguir (a )?empresa|follow (the )?company|newsletter|receber (novidades|e-?mails|ofertas)|marketing|compartilhar (meu )?perfil", re.I)
    CONSENT_RE = re.compile(r"termos|terms|privacidade|privacy|lgpd|consent|consinto|autoriz|aceit|concord|agree|pol[ií]tica|dados pessoais|estou ciente|declaro", re.I)

    def _is_pcd(self) -> bool:
        """O usuário é PCD? 'Não'/vazio no perfil = não (o valor é texto, então `if disability` dava True)."""
        v = str(((self.profile.get("personal") or {}).get("disability") or "")).strip().lower()
        return bool(v) and not re.match(r"n[aã]o|nao|no\b|nenhum|false|0", v)

    def _diversity_value(self, key: str) -> str | None:
        facts = self.profile.get("facts") or {}
        personal = self.profile.get("personal") or {}
        if key == "disability":
            return personal.get("disability") or "Não"
        return facts.get(key)

    def _rule(self, f: FieldQ, job: Job | None = None, siblings: list[str] | None = None) -> str | None:
        if f.kind == "password":
            import os
            pwd = os.environ.get("ATS_SIGNUP_PASSWORD", "").strip()
            if pwd and not re.search(r"atual|current|antiga|old", f.label, re.I):
                return pwd          # cadastro em ATS (Recrutei etc.); confirmação de senha recebe o mesmo valor
            return None
        if f.kind == "checkbox":
            if f.required and not re.search(r"preferencial|top choice", f.label or "", re.I):
                return True   # checkbox OBRIGATÓRIO só pode ser aceito (consentimento LGPD do CESAR cita "receber comunicações")
            member = [normalize(str(x)) for x in ((self.profile.get("facts") or {}).get("diversity_groups_member") or [])]
            if self.NONE_GROUP_RE.search(f.label):
                # só afirma "nenhum" se TODOS os grupos listados nesse formulário são conhecidos e ele não pertence a nenhum
                others = [l for l in (siblings or []) if l != f.label and not self.NONE_GROUP_RE.search(l)
                          and not re.search(r"prefiro n[aã]o", l, re.I) and not self.CONSENT_RE.search(l) and not self.NO_CHECK_RE.search(l)]
                unknown = [l for l in others if not self.DIVERSITY_GROUP_RE.search(l)]
                belongs = [l for l in others if any(m and m in normalize(l) for m in member)]
                if unknown or belongs:
                    return None         # grupo fora da lista conhecida: deixa o modelo (com o perfil) ou você decidir
                return "true"
            if self.DIVERSITY_GROUP_RE.search(f.label):
                if any(m and m in normalize(f.label) for m in member):
                    return "true"
                return "false"          # grupos conhecidos aos quais ele não pertence
            if re.search(r"prefiro n[aã]o (responder|informar)", f.label, re.I):
                return "false"
            if self.NO_CHECK_RE.search(f.label):
                return "false"          # nunca marca "vaga preferencial", "seguir empresa", newsletter
            if self.CONSENT_RE.search(f.label):
                return "true"           # termos/LGPD
            return None
        if f.kind in ("select", "radio", "combobox"):
            # pergunta condicional de PCD ("caso seja, qual o tipo de deficiência?"): ele não é PCD -> não responder
            if re.search(r"(caso seja|se sim|se for|caso possua|caso tenha|qual).{0,40}(tipo de )?defici|tipo de defici", normalize(f.label)) and not self._is_pcd():
                m = next((o for o in f.options if re.search(r"n[aã]o (sou|possuo|tenho|se aplica)|nenhum|sem defici|prefiro n[aã]o|n/?a\b", o, re.I)), None)
                if m:
                    return m
                return "Não se aplica" if f.required else None
            # diversidade: valores fixos do perfil, casados com as opções do campo
            ctx = f.label
            for pat, key in self.DIVERSITY_SELECT_RULES:
                if pat.search(ctx):
                    cands = [self._diversity_value(key)]
                    if key == "gender_identity":
                        cands.append((self.profile.get("personal") or {}).get("gender"))   # "Masculino" para campos simples
                    if key == "race_color":
                        cands.append((self.profile.get("facts") or {}).get("ethnicity"))
                    for v in cands:
                        if not v:
                            continue
                        if not f.options:
                            return v
                        m = best_option(str(v), f.options)
                        if m:
                            return m
                    return None
            # "Qual seu grau de relação com a pessoa que te indicou?" -> opção "não fui indicado/nenhuma"
            if f.options and re.search(r"(pessoa|quem) (que )?(te|lhe|o|a) indicou|grau de relacao|relacao com (a pessoa|quem)|indicad[oa] por", normalize(f.label)):
                m = next((o for o in f.options if re.search(r"n[aã]o (fui|tenho|houve|possuo|se aplica|foi)|nenhum|ningu[eé]m|sem indica|not referred|n/a", o, re.I)), None)
                if m:
                    return m
            # período da pretensão (Breezy: Yearly/Monthly/Hourly) -> mensal; moeda -> BRL
            if f.options and re.search(r"yearly|monthly|mensal|anual", " ".join(f.options), re.I) and len(f.options) <= 6:
                m = best_option("Monthly", f.options) or best_option("Mensal", f.options)
                if m:
                    return m
            # moeda (Breezy: "Brazilian Real (R$)", "US Dollar"): só quando as opções são NOMES de moeda, sem valores/faixas
            if f.options and all(len(o) <= 40 and not re.search(r"\d", o) for o in f.options) and re.search(r"real|brl|dollar|euro|currency|moeda", " ".join(f.options), re.I) and re.search(r"moeda|currency|salar|pretens", normalize(f.label)):
                m = next((o for o in f.options if re.search(r"brazilian real|\bbrl\b|r\$|real brasileiro|\breal\b", o, re.I)), None)
                if m:
                    return m
            # "Aceita a remuneração de R$ 8.000 + benefícios (CLT)?" (Sim/Não): regra do usuário = seguir o salário da vaga -> Sim
            if f.options and len(f.options) <= 4 and YESNO_OPT_RE.search(" | ".join(f.options)) and re.search(r"aceita(ria)? (a |o |essa |esta |uma )?(remuneracao|salario|valor|proposta|faixa|pretensao|contrata|modelo)|(esta|estaria) de acordo com (a |o )?(remuneracao|salario|valor|faixa)|(remuneracao|salario|valor).{0,60}(esta|estaria) de acordo|(concorda|de acordo) com (o|a) (valor|salario|remuneracao|faixa)", normalize(f.label)):
                m = best_option("Sim", f.options) or best_option("Yes", f.options)
                if m:
                    return m
            # "Possui experiência com Kafka?" (Sim/Não): o usuário declarou experiência com as tecnologias atuais -> Sim
            if f.options and len(f.options) <= 4 and YESNO_OPT_RE.search(" | ".join(f.options)) and EXPERIENCE_YESNO_RE.search(normalize(f.label)):
                m = best_option("Sim", f.options) or best_option("Yes", f.options)
                if m:
                    return m
            # "Área de interesse / área de atuação / categoria" (abler, Vagas.com): a opção de tecnologia/desenvolvimento
            if f.options and AREA_LABEL_RE.search(normalize(f.label)):
                v = tech_area_answer(f.options)
                if v:
                    return v
                if f.kind == "combobox":
                    return "Tecnologia da Informação"   # lista virtualizada/parcial: digitar filtra e o clique casa a opção
            # demais selects/combos (cidade, país, estado, escolaridade...) seguem para as regras genéricas abaixo;
            # o valor é casado com as opções em answer_batch (best_option) e, se não existir, vai para banco/LLM
        if f.kind == "multiselect" and f.options:
            # grupos minorizados (Breezy/CESAR: "Você se identifica com algum desses grupos?" com opção "Não"):
            # se todos os grupos são conhecidos e ele não pertence a nenhum -> só a opção "Não/Nenhum"
            none_opts = [o for o in f.options if self.NONE_GROUP_RE.search(o) or re.fullmatch(r"\s*(n[aã]o|nenhum[ao]?|none|no)\s*\.?", o, re.I)]
            groups = [o for o in f.options if o not in none_opts and not re.search(r"prefiro n[aã]o", o, re.I)]
            if none_opts and groups and all(self.DIVERSITY_GROUP_RE.search(o) or re.search(r"comunidade|minori|50 anos|60 anos|lgbt|mulher|negr|pcd|defici|indigen|trans", o, re.I) for o in groups):
                member = [normalize(str(x)) for x in ((self.profile.get("facts") or {}).get("diversity_groups_member") or [])]
                if not any(any(normalize(g) in m or m in normalize(g) for m in member) for g in groups):
                    return [none_opts[0]]
            # Regra do usuário: "com quais você tem experiência/conhecimento" -> marca TODAS as opções menos uma
            # (qualquer uma), nunca as do tipo "nenhuma das anteriores"/"não possuo"/"outros".
            if EXPERIENCE_MULTI_RE.search(normalize(f.label)):
                return experience_multi_answer(f.options)
            return None
        if f.kind == "file":
            return None
        nl = normalize(f.label)
        if job is not None and (re.search(r"valor.{0,12}hora|por hora|\bhora\b|hourly|\bhour\b", nl) and (SALARY_RE.search(nl) or re.search(r"valor|taxa|rate", nl))):
            v = hourly_answer(self.profile, job)
            if v:
                return v
        if job is not None and SALARY_RE.search(nl) and not re.search(r"\bhora\b|hour", nl):
            # dígitos por padrão: campos com máscara/validação numérica (LinkedIn, InHire, Gupy) aceitam; texto livre também
            v = salary_answer(self.profile, job, numeric=(f.kind != "textarea"), label=f.label)
            if v:
                return v
        if job is not None and YEARS_RE.search(nl) and f.kind in ("text", "number"):
            v = years_answer(self._compact_for(job), f.label)
            if v:
                return v
        if job is not None and re.search(r"cargo (pretendido|desejado|de interesse)|vaga (pretendida|desejada|de interesse)|posicao (desejada|pretendida)|position applied|desired (position|role|job title)|para qual vaga", nl) and f.kind in ("text", "textarea"):
            return (job.title or "")[:120]
        if job is not None and HEAR_RE.search(nl):
            src = {"linkedin": "LinkedIn", "indeed": "Indeed"}.get((job.source or "").lower())
            if src:
                return src
        _raw_lbl = re.sub(r"[^a-z0-9]+", " ", unicodedata.normalize("NFKD", f.label or "").encode("ascii", "ignore").decode().lower())
        _outra_pessoa = bool(OTHER_PERSON_RE.search(nl + " " + _raw_lbl))
        for pat, fn in RULES:
            if _outra_pessoa and re.search(r"nome|name", pat.pattern):
                continue   # "nome completo da sua mãe", "nome do contato de emergência": nunca o nome do candidato
            if pat.search(nl):
                try:
                    v = fn(self.compact)
                except Exception:
                    v = None
                if v:
                    return str(v)
        return None

    def _label_techs(self, label: str) -> set[str]:
        """Tecnologias citadas no rótulo, com sinônimos (".Net" -> ASP.NET, "SQL" -> PostgreSQL)."""
        nl = normalize(label)
        techs = set(self._tech_set(label))
        for k, v in TECH_SYNONYMS.items():
            if re.search(rf"(?<![a-z0-9]){re.escape(k)}(?![a-z0-9])", nl):
                techs.add(v)
        return techs

    def _cover_message(self, job) -> str:
        """Mensagem de apresentação obrigatória, montada só com fatos do perfil (sem IA).

        Não cita senioridade nem anos totais (regra do currículo para vagas júnior/pleno) e só
        menciona tecnologias da vaga que estão no perfil.
        """
        from .resume.tailor import adaptation_context
        title = (getattr(job, "title", "") or "").strip() or "a vaga"
        company = (getattr(job, "company", "") or "").strip()
        ad = adaptation_context(self.profile, getattr(job, "classification", None) or {})
        stack = ", ".join((ad.get("job_stack") or [])[:4])
        head = re.sub(r"\b(s[êe]nior|pleno|j[úu]nior|sr|pl|jr)\b", "", str(self.profile.get("headline") or "desenvolvedor")).strip()
        txt = f"Olá! Tenho interesse na vaga de {title}" + (f" na {company}" if company else "") + f". Sou {head[:1].lower() + head[1:]}"
        if stack:
            txt += f" e trabalho com {stack}"
        return txt + ". No currículo estão os projetos e resultados. Fico à disposição para conversar."

    def _skill_frac(self, label: str, job) -> float | None:
        """Nível (0..1) numa tecnologia citada no rótulo, pela faixa do perfil e limitado pelo nível da vaga.

        primary = especialista; secondary/also_experienced = avançado; tecnologia fora do perfil =
        intermediário (regra do usuário: marcar experiência de forma ampla). Vaga pleno limita a avançado
        e júnior a intermediário, igual aos anos pela metade nas respostas de formulário.
        """
        nl = normalize(label)
        techs = self._label_techs(label)
        sk = self.profile.get("skills") or {}
        tiers = {}
        for tier, frac in (("familiar", 0.0), ("also_experienced", 2 / 3), ("secondary", 2 / 3), ("primary", 1.0)):
            for name in sk.get(tier) or []:
                tiers[re.sub(r"[^a-z0-9+#.]", "", str(name).lower())] = frac
        fracs = [tiers[t] for t in techs if t in tiers]
        if techs and fracs:
            frac = max(fracs)
        elif re.search(r"\btest", nl):
            frac = 2 / 3               # automação de testes: Jest/TDD/E2E/Cucumber no perfil
        else:
            frac = 1 / 3
        sen = job_seniority(job) if job is not None else "senior"
        cap = {"junior": 1 / 3, "mid": 2 / 3}.get(sen, 1.0)
        return min(frac, cap)

    def _closed_answer(self, f: FieldQ, job) -> str | None:
        """Perguntas fechadas que o perfil já responde — antes das demais regras, sem IA.

        Saíram da análise de 839 campos de formulários reais (data/learn): eram as que mais iam
        para a IA. Cada regra só dispara com rótulo E opções compatíveis.
        """
        nl = normalize(f.label)
        opts = f.options or []
        facts = self.profile.get("facts") or {}
        yesno = bool(opts) and len(opts) <= 4 and YESNO_OPT_RE.search(" | ".join(opts))
        # caixa que declara deficiência ("♿ Portadores de condições especiais"): nunca marcar se não é PCD
        # (olha também o rótulo cru: "♿️ (Portadores de condições especiais)" vira "" depois de normalizar)
        raw_ascii = unicodedata.normalize("NFKD", f.label or "").encode("ascii", "ignore").decode().lower()
        if f.kind == "checkbox" and re.search(r"defici|pcd|portador|condicoes especiais|necessidades especiais|reabilitad", nl + " " + raw_ascii) \
                and not self._is_pcd():
            return "false"
        # "Como ficou sabendo da vaga?" com LISTA: escolhe entre as opções OFERECIDAS.
        # A lista da CI&T no Indeed não tem "Indeed" (só Linkedin, Vagas.com, GlassDoor, Outro...),
        # e responder "Indeed" deixava o campo obrigatório vazio: 149 falhas num dia só.
        if opts and HEAR_RE.search(nl):
            origem = {"linkedin": "LinkedIn", "indeed": "Indeed"}.get((getattr(job, "source", "") or "").lower(), "")
            for alvo in ([origem] if origem else []) + ["Outro", "Outros", "Other", "Site de Carreiras",
                                                        "Sites de emprego", "Site de empregos", "Vagas.com", "Internet"]:
                m = best_option(alvo, opts)
                if m:
                    return m
        # dia/mês/ano de nascimento em listas separadas (Indeed). A lista só carrega ao abrir o popup,
        # então o scan vê 0 opções: nesse caso devolve o valor puro e o motor digita no filtro.
        if re.search(r"(dia|mes|ano) de nascimento|birth (day|month|year)", nl) and f.kind in ("combobox", "select", "text", "number"):
            nasc = str(((self.profile.get("personal") or {}).get("birth_date")) or "")
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", nasc):
                ano, mes, dia = nasc.split("-")
                if re.search(r"\bdia\b|birth day", nl):
                    return (best_option(str(int(dia)), opts) or best_option(dia, opts)) if opts else str(int(dia))
                if re.search(r"\bano\b|birth year", nl):
                    return (best_option(ano, opts) if opts else ano)
                meses = ["Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
                         "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro"]
                nome = meses[int(mes) - 1]
                if not opts:
                    return nome
                return best_option(nome, opts) or best_option(str(int(mes)), opts) or best_option(mes, opts)
        # seletor de currículos salvos (LinkedIn Easy Apply): as opções são arquivos. Escolhe o que tem o
        # nome do candidato (o PDF recém-enviado ganha nome neutro "Curriculo - <nome>.pdf"), senão o 1º.
        if f.kind in ("radio", "select") and opts and all(re.search(r"\.(pdf|docx?)\b|[uú]ltima utiliza|selecionar (resume|curr)", o, re.I) for o in opts):
            pv = self.profile.get("personal") or {}
            nomes = [normalize(str(pv.get(k) or "")) for k in ("first_name", "last_name")]
            nomes = [n for n in nomes if n]
            m = next((o for o in opts if nomes and all(n in normalize(o) for n in nomes)), None)
            return m or opts[0]
        # parente/amigo trabalhando na empresa
        if re.search(r"(possui|tem|tenha) (algum |alguma )?(familiar|parente|amig[oa])|parentes? (na|no|que trabalh)|"
                     r"conhece (alguem|alguma pessoa|algum (colaborador|funcionario))", nl) and yesno:
            return best_option("Não", opts) or best_option("No", opts)
        if f.kind in ("text", "textarea") and re.search(r"(nome e )?grau de parentesco|nome do (familiar|parente)", nl):
            return "Não se aplica"
        # "É ex-colaborador do banco BV?" / "Trabalha como terceiro no BV?": só Sim se a empresa está nas experiências
        if re.search(r"\bex-? ?(colaborador|funcionari|empregad)|trabalha (como )?terceiro|presta(ou)? servicos? (como terceiro|para)", nl) and opts:
            empresas = [normalize(str(e.get("company") or "")) for e in (self.profile.get("experiences") or [])]
            alvo = normalize(getattr(job, "company", "") or "")
            trabalhou = any(e and (e in nl or (alvo and (e in alvo or alvo in e))) for e in empresas)
            # empresa está nas experiências: é fato do perfil, responde Sim (regras antigas diziam "Não" sempre)
            pat = r"\s*(sim|yes)\b.*" if trabalhou else r"\s*(n[aã]o|no|nunca)\b.*"
            m = next((o for o in opts if re.fullmatch(pat, o, re.I)), None)
            if m:
                return m
        # já trabalhou nesta empresa?
        if re.search(r"\bja (trabalhou|atuou|foi (colaborador|funcionario)|prestou servico)\b", nl) and opts:
            empresas = [normalize(str(e.get("company") or "")) for e in (self.profile.get("experiences") or [])]
            alvo = normalize(getattr(job, "company", "") or "")
            trabalhou = any(e and (e in nl or (alvo and e in alvo)) for e in empresas)
            if trabalhou:
                # "Sim, como efetivo" é o vínculo do perfil (CLT/PJ); não escolhe "temporário"/"estagiário"
                m = next((o for o in opts if re.search(r"^\s*(sim|yes)\b", o, re.I) and not re.search(r"tempor|estagi|aprendiz", o, re.I)), None)
            else:
                m = next((o for o in opts if re.fullmatch(r"\s*(n[aã]o|no|nunca)\b.*", o, re.I)), None)
            if m:
                return m
        # disponibilidade para presencial/híbrido: o usuário só aceita remoto
        if re.search(r"disponibilidade (para|de) (trabalh\w* )?(presencial|hibrid|no escritorio)|trabalh\w* presencialmente|comparecer (ao|no) escritorio", nl) and opts:
            if "remoto" in normalize(str(facts.get("work_models_accepted") or "")):
                m = next((o for o in opts if NEG_OPT_RE.search(o)), None)
                if m:
                    return m
        # comunidade LGBTQIAP+
        if re.search(r"lgbt", nl) and yesno:
            member = " ".join(normalize(str(x)) for x in (facts.get("diversity_groups_member") or []))
            return best_option("Sim" if "lgbt" in member else "Não", opts)
        # consentimento em forma de rádio/lista ("declaro que li... ○ Li e aceito")
        if f.kind in ("radio", "select", "combobox") and self.CONSENT_RE.search(f.label) and opts:
            m = next((o for o in opts if re.search(r"li e (aceito|concordo)|\baceito\b|\bconcordo\b|consinto|autorizo|^sim\b|^yes\b|i agree", o, re.I)), None)
            if m:
                return m
        # nível de confiança/proficiência num idioma
        lang = None
        if re.search(r"^\[en\]|ingles|english", nl):
            lang = "ingles"
        elif re.search(r"^\[es\]|espanhol|spanish", nl):
            lang = "espanhol"
        if lang and re.search(r"nivel|confianca|proficiencia|fluencia|dominio|level|confortavel", nl):
            nivel = next((normalize(str(l.get("level") or "")) for l in (self.profile.get("languages") or [])
                          if isinstance(l, dict) and normalize(str(l.get("name") or "")).startswith(lang[:5])), "")
            frac = next((v for k, v in LANG_LEVEL_FRAC.items() if k in nivel), 0.0)
            if f.kind in ("text", "textarea") and not opts:
                return ("Sim, tenho inglês avançado e trabalho bem em ambiente onde se fala inglês."
                        if lang == "ingles" and frac >= 0.75 else None)
            if opts:
                m = best_option(str(next((l.get("level") for l in (self.profile.get("languages") or [])
                                          if isinstance(l, dict) and normalize(str(l.get("name") or "")).startswith(lang[:5])), "")), opts) if frac else None
                return m or pick_graded(opts, frac)
        # nível profissional (Analista | Júnior | Pleno | Sênior | Especialista...). Vale também para
        # "Qual é seu cargo atual ou a última função que exerceu?" da Locaweb, cuja LISTA é de níveis:
        # responder o headline do perfil não casava nada e travava o formulário obrigatório.
        nivel_label = re.search(r"nivel (de experiencia|profissional|hierarquico|de cargo)|niveis de interesse|senioridade|nivel de interesse", nl)
        cargo_label = re.search(r"cargo atual|ultima funcao|funcao que exerceu|cargo/funcao|nivel do cargo|current (job )?(title|level)", nl)
        lista_de_niveis = bool(opts) and sum(1 for o in opts if re.search(r"j[úu]nior|pleno|s[êe]nior|estagi|aprendiz|coordenador|gerente|especialista", o, re.I)) >= 2
        if (nivel_label and lista_de_niveis) or (cargo_label and lista_de_niveis):
            sen = job_seniority(job) if job is not None else "senior"
            alvos = {"junior": ["Analista Júnior", "Júnior"], "mid": ["Analista Pleno", "Pleno"],
                     "lead": ["Especialista", "Especialista/Líder Técnico", "Analista Sênior", "Sênior"]}.get(
                         sen, ["Analista Sênior", "Sênior"])
            for a in alvos:
                m = best_option(a, opts)
                if m:
                    return m
        # "Como você avalia seu conhecimento com X?" / "já teve contato com X? qual nível?"
        # (só com ASSUNTO: "Nível de experiência" sozinho é o nível profissional, tratado acima)
        if re.search(r"(avali\w*|nivel|grau) (seu |sua |de )?(conhecimento|experiencia|dominio|familiaridade|proficiencia)|(contato|experiencia) com .{1,40}qual (o )?nivel|qual (seu|o) nivel (de|em|com)", nl) \
                and re.search(r"\b(com|em|no|na|with|in)\s+\S", nl):
            frac = self._skill_frac(f.label, job)
            if opts and len(opts) >= 3:
                return pick_graded(opts, frac)
            raw = (f.label or "").lower()
            m = re.search(r"de (0|1) a (5|10)|1\s*=\s*\w+.{0,30}?(5|10)\s*=|escala de (1|0) a (5|10)", raw)
            if f.kind in ("text", "number") and m:
                top = 10 if "10" in m.group(0) else 5
                return str(max(1, round(1 + frac * (top - 1))))
        # escolaridade
        if re.search(r"escolaridade|grau de instrucao|nivel de (escolaridade|formacao|ensino)|formacao academica", nl) and opts:
            ed = (self.profile.get("education") or [{}])[0]
            cursando = normalize(str(ed.get("status") or "")).startswith(("cursando", "em andamento", "incomplet"))
            pat = (r"gradua\w* incomplet|superior incomplet|superior (em andamento|cursando)|cursando|bacharelado (incomplet|em andamento)"
                   if cursando else r"gradua\w* complet|superior complet|bacharelado complet")
            m = next((o for o in opts if re.search(pat, o, re.I) and not re.search(r"tecnol|t[ée]cnico", o, re.I)), None)
            if m:
                return m

        # tipo/regime de contrato
        if re.search(r"(tipo|regime|modelo|forma) (de )?contrat(o|acao|a)", nl) and opts and any(re.search(r"\b(clt|pj)\b", o, re.I) for o in opts):
            ct = str(((getattr(job, "classification", None) or {}).get("contract_type")) or "").upper()
            alvo = "PJ" if ct == "PJ" else "CLT"
            return best_option(alvo, opts)
        # "você se sente confortável trabalhando remoto/online?"
        if re.search(r"confortavel (trabalhando|em trabalhar|com) .{0,30}(remot|online|distancia|home office)", nl) and yesno:
            return best_option("Sim", opts) or best_option("Yes", opts)
        # a mesma pergunta às vezes vem como caixa de texto: "já trabalhou no Grupo X?" / "conhece alguém lá?"
        if f.kind in ("text", "textarea") and not opts:
            if re.search(r"\bja (trabalhou|atuou|foi (colaborador|funcionario))\b", nl):
                empresas = [normalize(str(e.get("company") or "")) for e in (self.profile.get("experiences") or [])]
                if not any(e and e in nl for e in empresas):
                    return "Não"
            # regra do usuário (11/09): "você conhece alguém que trabalha aqui?" é SEMPRE Não
            if re.search(r"conhece (alguem|alguma pessoa|algum (colaborador|funcionario|empregado))|"
                         r"(possui|tem) (algum |alguma )?(familiar|parente|amig)|indicacao de (alguem|colaborador)", nl):
                return "Não"
            if re.search(r"se sim,? (informe|indique|diga|cite) o nome|nome (da|do) (pessoa )?(colaborador|funcionario|indicad)", nl):
                return "Não se aplica"
        # recurso de acessibilidade no processo seletivo
        if re.search(r"recursos? de acessibilidade|adaptac\w* (para|no|durante) (o )?processo|necessit\w* de (algum |alguma )?(recurso|adaptac|apoio)", nl) \
                and not self._is_pcd():
            if opts:
                m = next((o for o in opts if NEG_OPT_RE.search(o)), None)
                if m:
                    return m
            elif f.kind in ("text", "textarea"):
                return "Não necessito."
        # data prevista de conclusão do curso
        if re.search(r"(caso|se) (ainda )?esteja cursando|data prevista (de|para) (a )?(conclusao|formatura)|previsao de (conclusao|formatura)|expected graduation", nl):
            end = str(((self.profile.get("education") or [{}])[0]).get("end") or "")
            if end:
                return f"{end}-12-01" if f.kind == "date" else end
        # tempo de experiência pedido em texto ("Possui conhecimento com Python? Quanto tempo?")
        if f.kind in ("text", "textarea") and not opts and self._label_techs(f.label):
            # só o "Quanto tempo?" solto: "quantos anos"/"há quanto tempo" já têm regra própria que
            # devolve SÓ dígitos (o LinkedIn recusa "4 anos" em campo numérico)
            if re.search(r"quanto tempo", nl) and not YEARS_RE.search(nl):
                v = years_answer(self._compact_for(job) if job is not None else self.compact, f.label)
                if v:
                    return f"{v} anos"
            if re.search(r"^(voce )?(possui|tem) (conhe\w*|experiencia|vivencia)", nl) and not re.search(r"descreva|conte|explique|exemplo|\bcomo\b", nl):
                v = years_answer(self._compact_for(job) if job is not None else self.compact, f.label)
                return f"Sim, {v} anos de experiência." if v else "Sim."
        # frequência de alertas de vaga (widget de alerta que escapou do scan): nunca assinar
        if re.search(r"alertas? (de vagas?|por e-?mail)|recebimento de alertas|frequencia .{0,25}alerta", nl):
            return SKIP
        # mensagem/carta de apresentação OBRIGATÓRIA: modelo com fatos do perfil
        if f.kind in ("text", "textarea") and (f.required or "erro anterior" in nl) and \
                re.search(r"(mensagem|carta) (de apresentacao|de acompanhamento|na sua candidatura|para o recrutador)|cover letter|inclua uma mensagem", nl):
            return self._cover_message(job)
        # "Áreas de interesse" num combobox sem lista lida (retentativa): digitar TI
        if f.kind == "combobox" and not opts and AREA_LABEL_RE.search(nl):
            return "Tecnologia da Informação"
        return None

    def _option_hints(self) -> list[str]:
        """Valores do perfil que ajudam a escolher quais opções de uma lista longa mandar à IA."""
        if getattr(self, "_hints", None) is None:
            pv = self.profile.get("personal") or {}
            ed = (self.profile.get("education") or [{}])[0]
            langs = [str(l.get("name") or "") for l in (self.profile.get("languages") or []) if isinstance(l, dict)]
            sk = self.profile.get("skills") or {}
            self._hints = [str(x) for x in (
                pv.get("city"), pv.get("state"), pv.get("country"), pv.get("nationality"),
                ed.get("institution"), ed.get("degree"), self.profile.get("headline"),
                *langs, *(sk.get("primary") or [])[:12],
            ) if x]
        return self._hints

    def _tech_set(self, text: str) -> frozenset[str]:
        """Tecnologias citadas no rótulo, pelo vocabulário do perfil."""
        if getattr(self, "_skills_vocab", None) is None:
            from .resume.profile import all_skills
            self._skills_vocab = set(all_skills(self.profile))
        from .resume.tailor import _tech_mentions, _norm
        found = {_norm(t) for t in _tech_mentions(text or "", self._skills_vocab)}
        # "node js" (sem o ponto) também é Node.js: compara sem pontuação
        flat = re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower())
        for sk in self._skills_vocab:
            n = _norm(sk).replace(".", "")
            if len(n) >= 4 and n in flat:
                found.add(_norm(sk))
        return frozenset(found)

    def _bank(self, f: FieldQ) -> Answer | None:
        if SALARY_RE.search(normalize(f.label)) or re.search(r"cargo (pretendido|desejado)|vaga (pretendida|desejada)|position applied|desired (position|role)", normalize(f.label)):
            return None   # pretensão e "cargo pretendido" dependem da vaga: nunca vêm do banco
        if self.GENERIC_LABEL_RE.search(f.label):
            return None                       # nunca reaproveita resposta de rótulo genérico
        it, _ = self.bank.lookup(f.label, f.options if f.options else None, min_score=82)
        if not it:
            return None
        # "anos de experiência com Java" e "...com JavaScript" dão 92 de similaridade: só aceita a
        # resposta guardada se as TECNOLOGIAS citadas forem exatamente as mesmas.
        if self._tech_set(f.label) != self._tech_set(str(it.get("q") or "")):
            return None
        return Answer(it["a"], "bank", 1.0, it.get("topic", ""))

    def _match_option(self, value, options: list[str]):
        if not options:
            return value
        if isinstance(value, list):
            return [self._match_option(v, options) for v in value]
        s = str(value)
        m = best_option(s, options)
        return m if m else s

    def _compact_for(self, job: Job) -> dict:
        sen = job_seniority(job) if (job.title or "") else ((job.classification or {}).get("seniority") or "unspecified")
        return profile_for_forms(scale_profile_for_seniority(self.profile, sen, self.seniority_factors))

    def answer_batch(self, fields: list[FieldQ], job: Job, skip_optional: bool = True) -> dict[str, Answer]:
        """`skip_optional=False`: não deixa campo opcional em branco (usado quando a etapa travou)."""
        out: dict[str, Answer] = {}
        compact = self._compact_for(job)
        todo: list[FieldQ] = []
        siblings = [f.label for f in fields if f.kind == "checkbox"]
        for f in fields:
            if f.kind == "file":
                out[f.id] = Answer("__resume__", "rule")
                continue
            _raw = unicodedata.normalize("NFKD", f.label or "").encode("ascii", "ignore").decode().lower()
            _lbl = normalize(f.label) + " " + re.sub(r"[^a-z0-9]+", " ", _raw)
            if PARENT_NAME_RE.search(_lbl) and (f.kind in ("text", "textarea") or (f.kind == "combobox" and not f.options)):
                pv = (self.profile.get("personal") or {})
                known = pv.get("mother_name") if re.search(r"\b(mae|mother|genitora)\b", _lbl) else pv.get("father_name")
                if known:
                    out[f.id] = Answer(known, "rule"); continue
                if not f.required:
                    out[f.id] = Answer(None, "skip", 0); continue
                pending_early = getattr(self, "_pending_early", None)
                if pending_early is None:
                    self._pending_early = pending_early = []
                pending_early.append(f); continue
            v = self._closed_answer(f, job) or self._rule(f, job, siblings)
            if v == SKIP:
                out[f.id] = Answer(None, "skip", 0)
                continue
            if v:
                if f.kind in ("select", "radio", "combobox") and f.options:
                    m = best_option(str(v), f.options)   # None = opção não existe nesse formulário: LLM/banco decidem
                    if m is None:
                        for alt in alt_values(f.label, str(v)):   # "SP" <-> "São Paulo", "Brasil" <-> "Brazil"/"BR"
                            m = best_option(alt, f.options)
                            if m:
                                break
                    if m is None and SALARY_RE.search(normalize(f.label)) and re.fullmatch(r"\d{3,}", str(v).strip()):
                        m = salary_range_option(int(v), f.options)   # "R$ 10.001 a R$ 12.000"
                    if m is None and f.kind == "combobox":
                        m = str(v)   # lista virtualizada/truncada (cidades...): o preenchimento filtra digitando e confere
                    v = m
                if v:
                    out[f.id] = Answer(v, "rule")
                    continue
            b = self._bank(f)
            if b:
                b.value = self._match_option(b.value, f.options) if f.kind in ("select", "radio", "combobox", "multiselect") else b.value
                out[f.id] = b
                continue
            if f.kind in ("select", "combobox") and SALARY_RE.search(normalize(f.label)) and len(f.options) < 3:
                # faixa salarial num select cuja lista não foi lida por completo: não deixar o LLM escolher a única opção
                # visível ("R$ 1.000 - R$ 2.000" na Arco/Greenhouse) -> pendência
                pending_early = getattr(self, "_pending_early", None)
                if pending_early is None:
                    self._pending_early = pending_early = []
                pending_early.append(f)
                continue
            if (skip_optional and not f.required and getattr(self.cfg, "skip_optional_without_rule", True)
                    and "erro anterior" not in normalize(f.label)):
                # opcional e sem resposta por regra/banco: em branco, sem gastar IA. Se o formulário
                # reclamar, a nova tentativa vem com "(erro anterior: ...)" no rótulo e aí pergunta.
                out[f.id] = Answer(None, "skip", 0)
                continue
            todo.append(f)

        early = getattr(self, "_pending_early", None) or []
        if not todo and not early:
            return out

        # perguntas repetidas no mesmo formulário (o LinkedIn repete "Marcar vaga como preferencial"
        # em várias etapas) viram UMA pergunta; a resposta é copiada para as gêmeas.
        twins: dict[str, list[FieldQ]] = {}
        ask: list[FieldQ] = []
        for f in todo:
            key = f"{f.kind}|{normalize(f.label)}|{'|'.join(f.options[:10])}"
            if key in twins:
                twins[key].append(f)
                continue
            twins[key] = [f]
            ask.append(f)
        questions = [
            {"id": f.id, "question": f.label, "type": f.kind, "options": options_for_llm(f.label, f.options, self._option_hints()), "required": f.required}
            for f in ask
        ]
        bank_hits: list[dict] = []
        for f in ask:
            bank_hits.extend(self.bank.similar(f.label, n=3))
        seen = set()
        bank_hits = [h for h in bank_hits if not (h["q"] in seen or seen.add(h["q"]))][:12]
        # a descrição da vaga só importa para pergunta aberta ("por que aqui?", "fale sobre você")
        open_q = any(f.kind == "textarea" or OPEN_QUESTION_RE.search(normalize(f.label)) for f in ask)

        try:
            llm_answers = self.llm.answer_questions(
                questions=questions,
                profile=compact,
                bank_hits=bank_hits,
                job={"title": job.title, "company": job.company, "location": job.location,
                     "description": (job.description or "") if open_q else "",
                     "classification": job.classification or {}},
                always_ask_topics=self.cfg.always_ask_topics,
            ) if ask else []
        except BudgetExceeded as e:
            # sem orçamento: o que não tem regra nem banco vira pendência, em vez de travar a leva
            from rich.console import Console
            Console().print(f"[yellow]  {e}[/]")
            llm_answers = []
        by_id = {a.get("id"): a for a in llm_answers}
        pending: list[dict] = [{"field": f, "suggested": None, "topic": "pretensão salarial (faixa não lida)"} for f in (getattr(self, "_pending_early", None) or [])]
        self._pending_early = []
        for f in todo:
            a = by_id.get(f.id)
            if a is None:   # pergunta gêmea: usa a resposta da que foi perguntada
                key = f"{f.kind}|{normalize(f.label)}|{'|'.join(f.options[:10])}"
                first = (twins.get(key) or [None])[0]
                if first is not None and first.id != f.id:
                    a = by_id.get(first.id)
            if not a:
                if f.required:
                    pending.append({"field": f, "suggested": None})
                else:
                    out[f.id] = Answer(None, "skip", 0)
                continue
            value = a.get("answer")
            conf = float(a.get("confidence") or 0)
            needs = bool(a.get("needs_user"))
            topic = a.get("topic") or ""
            company_specific = bool(a.get("company_specific"))
            if f.kind in ("select", "radio", "combobox", "multiselect") and value not in (None, ""):
                value = self._match_option(value, f.options)
            if needs or (conf < self.cfg.min_answer_confidence and f.required):
                if not f.required and f.kind in ("text", "textarea", "number", "date", "url", "tel", "email"):
                    # campo OPCIONAL sem resposta segura (matrícula, "se outra, informe"...): deixa em branco, não trava a leva
                    out[f.id] = Answer(None, "skip", 0)
                    continue
                pending.append({"field": f, "suggested": a.get("suggested") or value, "topic": topic})
                continue
            out[f.id] = Answer(value, "llm", conf, topic)
            if (not company_specific and f.kind not in ("textarea", "password") and value not in (None, "", [])
                    and not self.GENERIC_LABEL_RE.search(f.label) and not SALARY_RE.search(normalize(f.label))):   # genérico/salário não vão pro banco
                self.bank.add(f.label, value, "llm", topic, f.options)

        if pending:
            if self.interactive and self.ask_user:
                for p in pending:
                    f = p["field"]
                    ans = self.ask_user(f, p.get("suggested"))
                    if ans is None or ans == "":
                        out[f.id] = Answer(None, "skip", 0)
                        continue
                    if f.kind in ("select", "radio", "combobox"):
                        ans = self._match_option(ans, f.options)
                    elif f.kind == "multiselect":
                        ans = [self._match_option(x.strip(), f.options) for x in str(ans).split(",") if x.strip()]
                    out[f.id] = Answer(ans, "user", 1.0, p.get("topic", ""))
                    self.bank.add(f.label, ans, "user", p.get("topic", ""), f.options)
            else:
                qs = []
                for p in pending:
                    f = p["field"]
                    qid = self.db.add_pending_question(job.id, f.label, f.kind, f.options, str(p.get("suggested") or ""))
                    qs.append({"id": qid, "question": f.label, "suggested": p.get("suggested")})
                raise NeedsUserInput(qs)
        return out
