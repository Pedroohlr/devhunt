"""Assistente de configuração: `vagas setup`.

Pergunta cada dado que a ferramenta usa e escreve `config/profile.yaml` e `.env`.

Regra do projeto: NADA é inventado. O que você não responder fica vazio, e quando
um formulário pedir aquele dado a candidatura vira pendência em vez de sair um dado
errado. Por isso todo campo aceita Enter para pular.

Dá para rodar quantas vezes quiser: os valores atuais viram o padrão de cada pergunta,
então `vagas setup --secao preferencias` serve para mexer só na pretensão salarial.
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Any, Callable

import yaml
from rich.console import Console
from rich.prompt import Confirm, Prompt

from .config import CONFIG_DIR, DATA_DIR, ROOT

console = Console()

SECOES = ["pessoal", "documentos", "diversidade", "carreira", "skills", "formacao", "preferencias", "chaves"]

# Validador: recebe o texto digitado e devolve (ok, valor_normalizado) ou (False, motivo).
Validador = Callable[[str], tuple[bool, str]]


# ============================================================
# perguntas
# ============================================================
def _cabecalho(passo: str, titulo: str, ajuda: str = "") -> None:
    console.print(f"\n[bold cyan]━━━ {passo} · {titulo} [/]")
    if ajuda:
        console.print(f"[dim]{ajuda}[/]")


def _ask(label: str, *, default: Any = "", ajuda: str = "", validar: Validador | None = None,
         obrigatorio: bool = False, senha: bool = False) -> str:
    """Uma pergunta. Enter mantém o valor atual (ou pula, se não houver)."""
    if ajuda:
        console.print(f"   [dim]{ajuda}[/]")
    default = "" if default is None else str(default)
    while True:
        try:
            v = Prompt.ask(f"   [bold]{label}[/]", default=default, show_default=bool(default),
                           password=senha).strip()
        except (EOFError, KeyboardInterrupt):
            raise
        if not v:
            if obrigatorio:
                console.print("   [yellow]este campo é obrigatório[/]")
                continue
            return ""
        if validar:
            ok, resultado = validar(v)
            if not ok:
                console.print(f"   [yellow]{resultado}[/]")
                if Confirm.ask("   usar assim mesmo?", default=False):
                    return v
                continue
            return resultado
        return v


def _ask_lista(label: str, *, default: list | None = None, ajuda: str = "") -> list[str]:
    """Lista separada por vírgula."""
    atual = ", ".join(str(x) for x in (default or []))
    v = _ask(label, default=atual, ajuda=ajuda)
    return [x.strip() for x in v.split(",") if x.strip()]


def _ask_sim_nao(label: str, *, default: bool = True, ajuda: str = "") -> bool:
    if ajuda:
        console.print(f"   [dim]{ajuda}[/]")
    return Confirm.ask(f"   [bold]{label}[/]", default=default)


def _ask_opcao(label: str, opcoes: list[str], *, default: str = "", ajuda: str = "") -> str:
    """Escolha por número, mas aceita texto livre (a lista é sugestão, não camisa de força)."""
    if ajuda:
        console.print(f"   [dim]{ajuda}[/]")
    for i, o in enumerate(opcoes, 1):
        console.print(f"     [dim]{i}.[/] {o}")
    v = _ask(label, default=default, ajuda="número da opção ou escreva a sua")
    if v.isdigit() and 1 <= int(v) <= len(opcoes):
        return opcoes[int(v) - 1]
    return v


# ============================================================
# validadores
# ============================================================
def v_email(v: str) -> tuple[bool, str]:
    return (True, v.lower()) if re.fullmatch(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}", v) else (False, "não parece um e-mail")


def v_telefone(v: str) -> tuple[bool, str]:
    d = re.sub(r"\D", "", v)
    if d.startswith("55") and len(d) in (12, 13):
        d = d[2:]
    if len(d) not in (10, 11):
        return False, "telefone deve ter DDD + número (10 ou 11 dígitos)"
    return True, f"+55 {d[:2]} {d[2:-4]}-{d[-4:]}"


def _cpf_valido(d: str) -> bool:
    if len(d) != 11 or len(set(d)) == 1:
        return False
    for tam in (9, 10):                                   # cada dígito verificador
        soma = sum(int(d[i]) * (tam + 1 - i) for i in range(tam))
        if (soma * 10) % 11 % 10 != int(d[tam]):
            return False
    return True


def v_cpf(v: str) -> tuple[bool, str]:
    d = re.sub(r"\D", "", v)
    if len(d) != 11:
        return False, "CPF deve ter 11 dígitos"
    if not _cpf_valido(d):
        return False, "CPF com dígito verificador inválido (confira a digitação)"
    return True, f"{d[:3]}.{d[3:6]}.{d[6:9]}-{d[9:]}"


def v_cep(v: str) -> tuple[bool, str]:
    d = re.sub(r"\D", "", v)
    return (True, f"{d[:5]}-{d[5:]}") if len(d) == 8 else (False, "CEP deve ter 8 dígitos")


def v_data(v: str) -> tuple[bool, str]:
    """Aceita 12/05/1990 ou 1990-05-12; guarda sempre em ISO (o motor de formulários converte)."""
    v = v.strip()
    m = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", v)
    if m:
        return True, f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", v)
    if m:
        return True, v
    return False, "use AAAA-MM-DD ou DD/MM/AAAA"


def v_mes(v: str) -> tuple[bool, str]:
    """Mês de início/fim de experiência: AAAA-MM (aceita MM/AAAA)."""
    v = v.strip()
    m = re.fullmatch(r"(\d{2})/(\d{4})", v)
    if m:
        return True, f"{m.group(2)}-{m.group(1)}"
    if re.fullmatch(r"\d{4}-\d{2}", v):
        return True, v
    if re.fullmatch(r"\d{4}", v):
        return True, f"{v}-01"
    return False, "use AAAA-MM (ex.: 2021-03)"


def v_uf(v: str) -> tuple[bool, str]:
    v = v.strip().upper()
    ufs = {"AC", "AL", "AP", "AM", "BA", "CE", "DF", "ES", "GO", "MA", "MT", "MS", "MG", "PA", "PB",
           "PR", "PE", "PI", "RJ", "RN", "RS", "RO", "RR", "SC", "SP", "SE", "TO"}
    return (True, v) if v in ufs else (False, "use a sigla do estado (ex.: SP)")


def v_url(dominio: str = "") -> Validador:
    def _v(v: str) -> tuple[bool, str]:
        v = v.strip()
        if not v.startswith("http"):
            v = "https://" + v.lstrip("/")
        if dominio and dominio not in v:
            return False, f"esperava um endereço do {dominio}"
        return True, v
    return _v


def v_inteiro(minimo: int = 0, maximo: int = 100) -> Validador:
    def _v(v: str) -> tuple[bool, str]:
        d = re.sub(r"\D", "", v)
        if not d or not (minimo <= int(d) <= maximo):
            return False, f"informe um número entre {minimo} e {maximo}"
        return True, d
    return _v


def v_dinheiro(v: str) -> tuple[bool, str]:
    """'8000', '8.000', 'R$ 8000' -> 'R$ 8.000'."""
    d = re.sub(r"\D", "", v)
    if not d:
        return False, "informe um valor em reais (ex.: 8000)"
    return True, "R$ " + f"{int(d):,}".replace(",", ".")


# ============================================================
# seções
# ============================================================
def secao_pessoal(prof: dict) -> None:
    p = prof.setdefault("personal", {})
    _cabecalho("1/8", "Dados pessoais",
               "Vão para o cabeçalho do currículo e para os campos de contato dos formulários.")
    p["full_name"] = _ask("Nome completo", default=p.get("full_name"), obrigatorio=True)
    partes = p["full_name"].split()
    p["first_name"] = _ask("Primeiro nome", default=p.get("first_name") or (partes[0] if partes else ""))
    p["last_name"] = _ask("Sobrenome", default=p.get("last_name") or (" ".join(partes[1:]) if len(partes) > 1 else ""))
    p["email"] = _ask("E-mail", default=p.get("email"), validar=v_email, obrigatorio=True)
    p["phone"] = _ask("Telefone/WhatsApp", default=p.get("phone"), validar=v_telefone,
                      ajuda="com DDD; é gravado como +55 DD NNNNN-NNNN e enviado só com dígitos quando o site exige")
    p["city"] = _ask("Cidade", default=p.get("city"))
    p["state"] = _ask("Estado (UF)", default=p.get("state"), validar=v_uf)
    p["country"] = _ask("País", default=p.get("country") or "Brasil")
    p["linkedin"] = _ask("URL do LinkedIn", default=p.get("linkedin"), validar=v_url("linkedin.com"))
    p["github"] = _ask("URL do GitHub", default=p.get("github"), validar=v_url("github.com"))
    p["portfolio"] = _ask("Portfólio/site", default=p.get("portfolio"), validar=v_url())
    p["birth_date"] = _ask("Data de nascimento", default=p.get("birth_date"), validar=v_data,
                           ajuda="alguns formulários exigem; fica só no seu computador")
    p["nationality"] = _ask("Nacionalidade", default=p.get("nationality") or "Brasileira")
    p["gender"] = _ask("Gênero", default=p.get("gender"), ajuda="campo simples de formulário (ex.: Masculino/Feminino)")

    foto = _ask("Caminho de uma foto para o currículo", default="",
                ajuda="ex.: /mnt/c/Users/voce/Desktop/foto.jpg — Enter pula (o currículo sai sem foto)")
    if foto:
        origem = Path(foto).expanduser()
        if origem.exists():
            destino = CONFIG_DIR / ("foto" + (origem.suffix or ".jpg"))
            shutil.copy(origem, destino)
            p["photo"] = str(destino.relative_to(ROOT))
            console.print(f"   [green]foto copiada para {p['photo']}[/]")
        else:
            console.print(f"   [yellow]arquivo não encontrado: {origem} — seguindo sem foto[/]")


def secao_documentos(prof: dict) -> None:
    p = prof.setdefault("personal", {})
    _cabecalho("2/8", "Documentos e endereço",
               "ATS brasileiro (Gupy, Indeed Apply, sites de empresa) costuma exigir CPF e endereço completo.\n"
               "Sem isso a candidatura para nesses campos e vira pendência. Enter pula qualquer um.")
    if not _ask_sim_nao("Quer preencher documentos e endereço agora?", default=True):
        return
    p["cpf"] = _ask("CPF", default=p.get("cpf"), validar=v_cpf)
    p["rg"] = _ask("RG", default=p.get("rg"))
    p["zip"] = _ask("CEP", default=p.get("zip"), validar=v_cep)
    p["street"] = _ask("Rua/logradouro", default=p.get("street"))
    p["address_number"] = _ask("Número", default=p.get("address_number"))
    p["neighborhood"] = _ask("Bairro", default=p.get("neighborhood"))
    if p.get("street"):
        p["address"] = f"{p['street']}, {p['address_number']}" if p.get("address_number") else p["street"]
    console.print("   [dim]Nome dos pais: pedido por alguns formulários de admissão. Enter pula.[/]")
    p["mother_name"] = _ask("Nome completo da mãe", default=p.get("mother_name"))
    p["father_name"] = _ask("Nome completo do pai", default=p.get("father_name"))


def secao_diversidade(prof: dict) -> None:
    p = prof.setdefault("personal", {})
    f = prof.setdefault("facts", {})
    _cabecalho("3/8", "Diversidade e acessibilidade",
               "Responde sozinho os formulários de diversidade. O que ficar vazio vira pendência,\n"
               "e a ferramenta nunca marca sozinha uma caixa que declara um fato sobre você.")
    if not _ask_sim_nao("Quer responder agora?", default=True):
        return
    pcd = _ask_sim_nao("Você é pessoa com deficiência (PCD)?", default=False)
    p["disability"] = _ask("Qual deficiência?", default=p.get("disability")) if pcd else "Não"
    f["gender_identity"] = _ask_opcao("Identidade de gênero",
                                      ["Homem cisgênero", "Mulher cisgênero", "Homem transgênero",
                                       "Mulher transgênero", "Não binário", "Prefiro não responder"],
                                      default=f.get("gender_identity", ""))
    f["sexual_orientation"] = _ask_opcao("Orientação sexual",
                                         ["Heterossexual", "Homossexual", "Bissexual", "Prefiro não responder"],
                                         default=f.get("sexual_orientation", ""))
    f["race_color"] = _ask_opcao("Cor/raça", ["Branca", "Preta", "Parda", "Amarela", "Indígena", "Prefiro não responder"],
                                 default=f.get("race_color", ""))
    f["ethnicity"] = f["race_color"]
    grupos = _ask_lista("Grupos de diversidade dos quais você faz parte",
                        default=f.get("diversity_groups_member") or [],
                        ajuda="separados por vírgula (ex.: LGBTQIAP+, pessoa negra, PCD). Enter = nenhum.\n"
                              "   Isto decide se a ferramenta aplica em vaga afirmativa: vaga exclusiva de um grupo\n"
                              "   que não é o seu é pulada automaticamente.")
    f["diversity_groups_member"] = grupos
    f["diversity_groups"] = ", ".join(grupos) if grupos else "Não pertenço a nenhum dos grupos"


def _cadastra_experiencia(atual: dict | None = None) -> dict:
    atual = atual or {}
    e: dict[str, Any] = {}
    e["company"] = _ask("Empresa", default=atual.get("company"), obrigatorio=True)
    e["title"] = _ask("Cargo", default=atual.get("title"), obrigatorio=True)
    e["start"] = _ask("Início (AAAA-MM)", default=atual.get("start"), validar=v_mes)
    e["end"] = _ask("Fim (AAAA-MM)", default=atual.get("end"), validar=v_mes,
                    ajuda="Enter se for o emprego atual")
    e["location"] = _ask("Local", default=atual.get("location") or "Remoto")
    e["employment_type"] = _ask_opcao("Contrato", ["CLT", "PJ", "Estágio", "Freelance"],
                                      default=atual.get("employment_type", ""))
    e["stack"] = _ask_lista("Tecnologias usadas nessa empresa", default=atual.get("stack") or [],
                            ajuda="separadas por vírgula. Os bullets desta experiência só podem citar estas.")
    console.print("   [dim]Agora o que você fez lá. Escreva frases concretas, com números quando existirem.\n"
                  "   Escreva várias (5+ se lembrar): por vaga o gerador escolhe e reescreve as mais relevantes,\n"
                  "   trocando tecnologias pelas da vaga — mas só pelas que estão na lista acima.\n"
                  "   Enter vazio encerra.[/]")
    bullets = list(atual.get("bullets") or [])
    for b in bullets:
        console.print(f"     [dim]· {b[:100]}[/]")
    while True:
        b = _ask(f"Realização {len(bullets) + 1}", default="")
        if not b:
            break
        bullets.append(b)
    e["bullets"] = bullets
    if _ask_sim_nao("Proteger a stack original desta experiência?", default=False,
                    ajuda="'sim' impede o gerador de trocar as tecnologias dos bullets desta empresa\n"
                          "   (útil quando o trabalho era de uma stack bem específica, tipo Salesforce)"):
        e["restack"] = False
    # `end` fica mesmo vazio (= emprego atual) e na posição certa; o resto vazio sai do YAML
    return {k: v for k, v in e.items() if v not in ("", [], None) or k == "end"}


def secao_carreira(prof: dict) -> None:
    _cabecalho("4/8", "Carreira",
               "O currículo de cada vaga é montado SÓ com o que entrar aqui. Quanto mais detalhe, melhor a adaptação.")
    prof["headline"] = _ask("Título profissional", default=prof.get("headline"),
                            ajuda="ex.: Desenvolvedor Full Stack Sênior — o gerador adapta por vaga")
    console.print("   [dim]Resumo em 2-4 linhas. O gerador reescreve por vaga mantendo os fatos.[/]")
    prof["summary"] = _ask("Resumo", default=prof.get("summary", "").strip())
    prof["years_experience_total"] = int(_ask("Anos de experiência no total", default=prof.get("years_experience_total"),
                                              validar=v_inteiro(0, 60)) or 0)

    experiencias = list(prof.get("experiences") or [])
    if experiencias:
        console.print(f"\n   [dim]Já cadastradas ({len(experiencias)}):[/]")
        for e in experiencias:
            console.print(f"     [dim]· {e.get('company')} — {e.get('title')} ({e.get('start')} → {e.get('end') or 'atual'})[/]")
        escolha = _ask_opcao("O que fazer com as experiências?",
                             ["manter como estão", "acrescentar outra", "começar do zero"], default="1")
        if escolha.startswith("começar"):
            experiencias = []
        elif escolha.startswith("manter"):
            prof["experiences"] = experiencias
            return

    if not experiencias and _ask_sim_nao("Importar do PDF do LinkedIn (Perfil > Mais > Salvar como PDF)?", default=False,
                                         ajuda="usa a IA para transformar o PDF em rascunho; precisa da OPENAI_API_KEY.\n"
                                               "   Depois rode `vagas profile show` e revise — é um rascunho."):
        caminho = _ask("Caminho do PDF", default="")
        if caminho and Path(caminho).expanduser().exists():
            console.print("   [dim]rode depois: [bold]vagas profile import "
                          f"{Path(caminho).expanduser()}[/][/]")
            console.print("   [yellow]O import sobrescreve o profile.yaml, então faça isso ANTES de terminar este setup\n"
                          "   ou depois rode `vagas setup` de novo para completar o resto.[/]")
            return
        console.print("   [yellow]arquivo não encontrado; seguindo no cadastro manual[/]")

    console.print("\n   [dim]Cadastro das experiências, da mais recente para a mais antiga.[/]")
    while True:
        console.print(f"\n   [bold cyan]Experiência {len(experiencias) + 1}[/]")
        experiencias.append(_cadastra_experiencia())
        if not _ask_sim_nao("Adicionar outra experiência?", default=True):
            break
    prof["experiences"] = experiencias


def secao_skills(prof: dict) -> None:
    sk = prof.setdefault("skills", {})
    _cabecalho("5/8", "Tecnologias",
               "O currículo só pode citar tecnologia que estiver aqui (ou na stack de uma experiência).")
    sk["primary"] = _ask_lista("Principais (você domina e usa profissionalmente)",
                               default=sk.get("primary") or [],
                               ajuda="viram destaque em Competências e no título adaptado")
    sk["secondary"] = _ask_lista("Secundárias (sabe trabalhar, não é o forte)", default=sk.get("secondary") or [])
    sk["also_experienced"] = _ask_lista("Outras com que você tem experiência", default=sk.get("also_experienced") or [],
                                        ajuda="entram em Competências quando a vaga pede, mas nunca nos bullets de\n"
                                              "   uma empresa onde você não as usou")
    sk["familiar"] = _ask_lista("Conhece/estudou (nível básico)", default=sk.get("familiar") or [])

    prof["skill_years_default"] = int(_ask("Anos de experiência para as tecnologias não detalhadas",
                                           default=prof.get("skill_years_default") or 5,
                                           validar=v_inteiro(0, 60)) or 5)
    anos = dict(prof.get("skill_years") or {})
    if sk["primary"] and _ask_sim_nao("Informar anos por tecnologia principal?", default=True,
                                      ajuda="usado nas perguntas 'quantos anos de experiência com X'.\n"
                                            "   Vaga pleno usa metade e júnior um quarto, automaticamente."):
        for tec in sk["primary"]:
            v = _ask(f"anos com {tec}", default=anos.get(tec, ""), validar=v_inteiro(0, 60))
            if v:
                anos[tec] = int(v)
    prof["skill_years"] = anos
    prof.setdefault("adaptation", {"min_stack_share": 0.5, "restack_all": True,
                                   "allowed_from": ["primary", "secondary", "also_experienced"]})


def secao_formacao(prof: dict) -> None:
    _cabecalho("6/8", "Formação, idiomas e cursos")
    educacao = list(prof.get("education") or [])
    if educacao:
        for e in educacao:
            console.print(f"   [dim]· {e.get('degree')} — {e.get('institution')} ({e.get('status')})[/]")
    if not educacao or _ask_sim_nao("Recadastrar a formação?", default=False):
        educacao = []
        while True:
            ed: dict[str, Any] = {}
            ed["institution"] = _ask("Instituição", default="")
            if not ed["institution"]:
                break
            ed["degree"] = _ask("Curso", default="", ajuda="ex.: Bacharelado em Engenharia de Software")
            ed["start"] = _ask("Início (ano)", default="")
            ed["end"] = _ask("Fim/previsão (ano)", default="")
            ed["status"] = _ask_opcao("Situação", ["concluído", "cursando", "trancado"], default="2")
            educacao.append(ed)
            if not _ask_sim_nao("Adicionar outra formação?", default=False):
                break
    prof["education"] = educacao

    idiomas = list(prof.get("languages") or [])
    atual = {str(l.get("name", "")).lower(): l.get("level", "") for l in idiomas}
    console.print("\n   [dim]Idiomas. O nível informado é o que a ferramenta declara nos formulários.[/]")
    novos = [{"name": "Português", "level": _ask("Português", default=atual.get("português") or "Nativo")}]
    ing = _ask_opcao("Inglês", ["Avançado", "Intermediário", "Básico", "Fluente", "não falo"],
                     default=atual.get("inglês") or "")
    if ing and ing != "não falo":
        novos.append({"name": "Inglês", "level": ing})
    esp = _ask_opcao("Espanhol", ["Básico", "Intermediário", "Avançado", "não falo"],
                     default=atual.get("espanhol") or "")
    if esp and esp != "não falo":
        novos.append({"name": "Espanhol", "level": esp})
    prof["languages"] = novos
    prof.setdefault("facts", {})["english_level"] = ing if ing and ing != "não falo" else "Não falo inglês"

    console.print("\n   [dim]Cursos e certificações que você JÁ FEZ. A ferramenta escolhe por vaga os que casam\n"
                  "   com a stack; o catálogo de cursos por stack (config/courses.yaml) já vem preenchido,\n"
                  "   mas todos marcados como 'a fazer' — marque os seus com `vagas courses done <nome>`.[/]")
    certs = list(prof.get("certifications") or [])
    for c in certs:
        console.print(f"   [dim]· {c.get('name')}[/]")
    if _ask_sim_nao("Cadastrar certificações agora?", default=not certs):
        while True:
            nome = _ask("Nome do curso/certificação", default="")
            if not nome:
                break
            certs.append({
                "name": nome,
                "issuer": _ask("Emissor", default="", ajuda="ex.: Udemy, Alura, AWS"),
                "year": _ask("Ano", default=""),
                "tags": _ask_lista("Tecnologias relacionadas", ajuda="usado para casar com a vaga"),
            })
            if not _ask_sim_nao("Adicionar outra?", default=True):
                break
    prof["certifications"] = certs
    prof.setdefault("max_certifications", 4)


def secao_preferencias(prof: dict) -> None:
    f = prof.setdefault("facts", {})
    _cabecalho("7/8", "Pretensão e preferências",
               "Respondem sozinhas as perguntas de formulário. A pretensão por nível existe porque a\n"
               "ferramenta aplica para júnior, pleno e sênior — e o nível sai do título da vaga.\n"
               "Quando a vaga informa salário, a resposta segue a vaga, não estes valores.")
    f["salary_expectation_senior_clt"] = _ask("Pretensão sênior CLT", default=f.get("salary_expectation_senior_clt"), validar=v_dinheiro)
    f["salary_expectation_senior_pj"] = _ask("Pretensão sênior PJ", default=f.get("salary_expectation_senior_pj"), validar=v_dinheiro)
    f["salary_expectation_mid_clt"] = _ask("Pretensão pleno CLT", default=f.get("salary_expectation_mid_clt"), validar=v_dinheiro)
    f["salary_expectation_mid_pj"] = _ask("Pretensão pleno PJ", default=f.get("salary_expectation_mid_pj"), validar=v_dinheiro)
    f["salary_expectation_junior_clt"] = _ask("Pretensão júnior CLT", default=f.get("salary_expectation_junior_clt"), validar=v_dinheiro)
    f["salary_expectation_junior_pj"] = _ask("Pretensão júnior PJ", default=f.get("salary_expectation_junior_pj"), validar=v_dinheiro)
    f["salary_expectation_clt"] = f.get("salary_expectation_senior_clt") or ""
    f["salary_expectation_pj"] = f.get("salary_expectation_senior_pj") or ""

    f["availability"] = _ask("Disponibilidade para início", default=f.get("availability") or "Imediata")
    f["notice_period"] = _ask("Aviso prévio", default=f.get("notice_period") or "Sem aviso prévio; disponibilidade imediata")
    f["currently_employed"] = _ask_opcao("Está trabalhando atualmente?", ["Não", "Sim"], default=f.get("currently_employed", ""))
    f["work_models_accepted"] = _ask("Modelos de trabalho aceitos", default=f.get("work_models_accepted") or "Somente remoto",
                                     ajuda="a ferramenta só busca vaga remota (regra fixa em código)")
    f["willing_to_relocate"] = _ask_opcao("Aceita mudar de cidade?", ["Não", "Sim"], default=f.get("willing_to_relocate", ""))
    f["accepts_clt"] = _ask_opcao("Aceita CLT?", ["Sim", "Não"], default=f.get("accepts_clt", ""))
    f["accepts_pj"] = _ask_opcao("Aceita PJ?", ["Sim", "Não"], default=f.get("accepts_pj", ""))
    f["has_cnpj"] = _ask("Tem CNPJ?", default=f.get("has_cnpj") or "", ajuda="ex.: Sim (ME) / Não")
    f["contract_preference"] = _ask("Preferência de contrato", default=f.get("contract_preference") or "CLT ou PJ, sem preferência")
    f["has_drivers_license"] = _ask("Tem CNH?", default=f.get("has_drivers_license") or "")
    f["travel_availability"] = _ask("Disponibilidade para viajar", default=f.get("travel_availability") or "")
    f["authorized_to_work_brazil"] = f.get("authorized_to_work_brazil") or "Sim"
    f["needs_visa_sponsorship"] = f.get("needs_visa_sponsorship") or "Não"
    f["veteran"] = f.get("veteran") or "Não"
    f["how_did_you_hear"] = f.get("how_did_you_hear") or "LinkedIn"
    f["led_teams"] = _ask("Já liderou time? Onde?", default=f.get("led_teams") or "")
    f["international_experience"] = _ask("Experiência internacional?", default=f.get("international_experience") or "")
    f["notable_clients"] = _ask("Clientes conhecidos com quem trabalhou", default=f.get("notable_clients") or "",
                                ajuda="usado em bullets e respostas; só cite o que é verdade e não é confidencial")

    console.print("\n   [dim]Como você escreve — o gerador imita para o currículo não ter cara de IA.[/]")
    voz = prof.setdefault("voice", {})
    voz["notes"] = _ask("Estilo de escrita", default=(voz.get("notes") or "").strip() or
                        "Direto e concreto. Verbos de ação no início. Sem adjetivos vazios. "
                        "Números quando existirem. Português do Brasil.")


def secao_chaves(prof: dict) -> None:
    _cabecalho("8/8", "Chaves e senhas (.env)",
               "Ficam só no arquivo .env desta pasta, que nunca vai para o git.")
    env = _le_env()
    env["OPENAI_API_KEY"] = _ask("OPENAI_API_KEY", default=env.get("OPENAI_API_KEY", ""), senha=True,
                                 ajuda="de platform.openai.com. Sem ela a ferramenta ainda roda pelo caminho sem IA,\n"
                                       "   mas não escreve currículo novo nem responde pergunta aberta.")
    if _ask_sim_nao("Preencher as senhas de login agora?", default=False,
                    ajuda="opcionais: a ferramenta usa o navegador com a sua sessão já logada (`vagas login`).\n"
                          "   A senha do LinkedIn só é usada para entrar na Gupy via 'Entrar com LinkedIn'."):
        env["LINKEDIN_PASSWORD"] = _ask("Senha do LinkedIn", default=env.get("LINKEDIN_PASSWORD", ""), senha=True)
        env["GUPY_PASSWORD"] = _ask("Senha da Gupy", default=env.get("GUPY_PASSWORD", ""), senha=True)
        env["SIGNUP_EMAIL"] = _ask("E-mail para cadastros em ATS", default=env.get("SIGNUP_EMAIL", ""), validar=v_email,
                                   ajuda="conta separada da sua principal; a ferramenta lê o e-mail de confirmação nela")
        env["ATS_SIGNUP_PASSWORD"] = _ask("Senha padrão para esses cadastros", default=env.get("ATS_SIGNUP_PASSWORD", ""), senha=True)
    if _ask_sim_nao("Receber avisos no Telegram?", default=False):
        env["TELEGRAM_BOT_TOKEN"] = _ask("TELEGRAM_BOT_TOKEN", default=env.get("TELEGRAM_BOT_TOKEN", ""), senha=True)
        env["TELEGRAM_CHAT_ID"] = _ask("TELEGRAM_CHAT_ID", default=env.get("TELEGRAM_CHAT_ID", ""))
    _grava_env(env)
    console.print(f"   [green]salvo em {(ROOT / '.env')}[/]")


# ============================================================
# arquivos
# ============================================================
def _le_env() -> dict[str, str]:
    env: dict[str, str] = {}
    caminho = ROOT / ".env"
    if caminho.exists():
        for linha in caminho.read_text(encoding="utf-8").splitlines():
            if "=" in linha and not linha.strip().startswith("#"):
                k, _, v = linha.partition("=")
                env[k.strip()] = v.strip()
    return env


def _grava_env(env: dict[str, str]) -> None:
    ordem = ["OPENAI_API_KEY", "LINKEDIN_PASSWORD", "GUPY_PASSWORD", "SIGNUP_EMAIL",
             "ATS_SIGNUP_PASSWORD", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]
    linhas = ["# Chaves e senhas. Arquivo pessoal: não vai para o git (está no .gitignore).",
              "# Gerado por `vagas setup` — dá para editar à mão.", ""]
    for k in ordem:
        if env.get(k):
            linhas.append(f"{k}={env[k]}")
    for k, v in env.items():                       # o que já estava lá e não é conhecido
        if k not in ordem and v:
            linhas.append(f"{k}={v}")
    (ROOT / ".env").write_text("\n".join(linhas) + "\n", encoding="utf-8")


class _Dumper(yaml.SafeDumper):
    """Mapas sempre em bloco (o arquivo é para editar à mão); listas curtas de texto ficam em linha."""


def _lista_em_linha(dumper: yaml.SafeDumper, data: list):
    cabe = (all(isinstance(x, (str, int, float)) for x in data)
            and sum(len(str(x)) + 2 for x in data) <= 88
            and not any("\n" in str(x) for x in data))
    return dumper.represent_sequence("tag:yaml.org,2002:seq", data, flow_style=cabe)


_Dumper.add_representer(list, _lista_em_linha)


def _yaml(valor: Any) -> str:
    # default_flow_style=False é o que impede `{headline: ...}` no topo, que deixaria o arquivo ilegível
    # para o YAML (mistura de bloco e flow na raiz) além de ruim de editar.
    return yaml.dump(valor, Dumper=_Dumper, allow_unicode=True, sort_keys=False,
                     default_flow_style=False, width=100)


def dump_profile(prof: dict) -> str:
    """profile.yaml com os comentários de sempre (safe_dump sozinho comeria todos)."""
    blocos = [
        ("# ============================================================\n"
         "# PERFIL MESTRE — a única fonte de verdade sobre você.\n"
         "# O currículo de cada vaga é montado SÓ com o que está aqui: nada é inventado\n"
         "# (resume/tailor.py:validate_resume barra o que não tiver lastro neste arquivo).\n"
         "# Editável à mão; `vagas setup` reescreve mantendo o que já existe.\n"
         "# ============================================================", None),
        ("# Dados pessoais e de contato (cabeçalho do currículo + campos de formulário)", "personal"),
        ('# Título base. O gerador adapta por vaga (ex.: "Desenvolvedor PHP Sênior").', "headline"),
        ("# Resumo base. O gerador reescreve por vaga mantendo os fatos.", "summary"),
        ('# Anos totais (perguntas "quantos anos de experiência").', "years_experience_total"),
        ("# O currículo só pode citar tecnologia listada aqui ou na stack de uma experiência.", "skills"),
        ("# Meta de adaptação: fração mínima de bullets/competências citando a stack da vaga.", "adaptation"),
        ("# Anos por tecnologia. Vaga pleno usa metade; júnior, um quarto.", "skill_years_default"),
        (None, "skill_years"),
        ("# Experiências, da mais recente para a mais antiga.\n"
         "# bullets: o gerador escolhe e reescreve os mais relevantes por vaga, trocando\n"
         "# tecnologias pelas da vaga — só pelas que estão em `stack` desta experiência.\n"
         "# restack: false protege a stack original desta experiência.", "experiences"),
        ("# Formação", "education"),
        ("# Cursos/certificações já feitos (o pool por stack fica em config/courses.yaml)", "max_certifications"),
        (None, "certifications"),
        ("# Idiomas (o nível é o declarado nos formulários)", "languages"),
        ("# Fatos e preferências para responder formulários. Pode acrescentar chave: valor à vontade.", "facts"),
        ("# Como você escreve (o gerador imita).", "voice"),
    ]
    partes: list[str] = []
    for comentario, chave in blocos:
        if chave is None:
            partes.append(comentario)
            continue
        if chave not in prof:
            continue
        if comentario:
            partes.append(("\n" if partes else "") + comentario)
        partes.append(_yaml({chave: prof[chave]}).rstrip())
    restantes = [k for k in prof if k not in {c for _, c in blocos if c}]
    if restantes:
        partes.append("\n# Outros campos")
        partes.append(_yaml({k: prof[k] for k in restantes}).rstrip())
    return "\n".join(partes) + "\n"


def _salva_profile(prof: dict, caminho: Path) -> None:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_text(dump_profile(prof), encoding="utf-8")


def _carrega_profile(caminho: Path) -> dict:
    if caminho.exists():
        return yaml.safe_load(caminho.read_text(encoding="utf-8")) or {}
    return {}


# ============================================================
# entrada
# ============================================================
def run_setup(secao: str = "tudo") -> None:
    caminho = CONFIG_DIR / "profile.yaml"
    prof = _carrega_profile(caminho)

    if secao == "tudo":
        console.print("\n[bold]Configuração do auto-aplicador[/]")
        console.print("[dim]São 8 blocos de perguntas. Enter aceita o valor entre colchetes ou pula o campo.\n"
                      "Nada é inventado: o que ficar em branco vira pendência quando um formulário pedir.\n"
                      "Dá para parar no meio (Ctrl+C) — cada bloco é salvo assim que termina.\n"
                      "Depois, `vagas setup --secao preferencias` mexe só num bloco.[/]")
        if caminho.exists():
            console.print(f"[dim]Perfil existente em {caminho.relative_to(ROOT)}: os valores atuais vêm como padrão.[/]")

    blocos = {
        "pessoal": secao_pessoal, "documentos": secao_documentos, "diversidade": secao_diversidade,
        "carreira": secao_carreira, "skills": secao_skills, "formacao": secao_formacao,
        "preferencias": secao_preferencias, "chaves": secao_chaves,
    }
    escolhidas = SECOES if secao == "tudo" else [secao]
    for nome in escolhidas:
        try:
            blocos[nome](prof)
        except (KeyboardInterrupt, EOFError):
            if nome != "chaves":
                _salva_profile(prof, caminho)
            console.print(f"\n[yellow]Interrompido. O que já foi respondido está salvo em "
                          f"{caminho.relative_to(ROOT)}.[/]\nRetome com [bold]vagas setup --secao {nome}[/].")
            return
        if nome != "chaves":
            _salva_profile(prof, caminho)
            console.print(f"   [green]✓ salvo em {caminho.relative_to(ROOT)}[/]")

    for d in ("resumes", "profile", "learn", "evidence", "logs"):
        (DATA_DIR / d).mkdir(parents=True, exist_ok=True)
    respostas = CONFIG_DIR / "answers.yaml"
    if not respostas.exists():
        shutil.copy(CONFIG_DIR / "answers.example.yaml", respostas)

    if secao == "tudo":
        _resumo(prof)


def _resumo(prof: dict) -> None:
    p = prof.get("personal") or {}
    faltando = [rot for rot, v in [
        ("CPF", p.get("cpf")), ("endereço", p.get("address")), ("CEP", p.get("zip")),
        ("telefone", p.get("phone")), ("experiências", prof.get("experiences")),
        ("tecnologias principais", (prof.get("skills") or {}).get("primary")),
        ("chave da OpenAI", _le_env().get("OPENAI_API_KEY")),
    ] if not v]
    console.print("\n[bold green]Pronto.[/] Perfil em [bold]config/profile.yaml[/], chaves em [bold].env[/].")
    if faltando:
        console.print(f"[yellow]Em branco:[/] {', '.join(faltando)} "
                      "[dim]— formulário que pedir isso vira pendência; complete com `vagas setup --secao <bloco>`.[/]")
    console.print("\nPróximos passos:\n"
                  "  1. [bold]vagas doctor[/] — confere chave, navegador e perfil\n"
                  "  2. [bold]vagas login[/] — abre o navegador para você logar no LinkedIn/Indeed/Gupy\n"
                  "  3. [bold]vagas search[/] — descobre as vagas do dia\n"
                  "  4. [bold]vagas apply --interactive --dry-run[/] — preenche sem enviar, para você conferir\n"
                  "  5. [bold]vagas dashboard --serve[/] — painel em http://localhost:8765")
