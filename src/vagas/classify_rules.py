"""Classificador DETERMINÍSTICO de vagas (sem IA).

Motivo: classificar cada vaga com o gpt-4.1-mini custava ~US$ 0,40/dia (~US$ 12/mês) e o teto
de gasto do usuário é R$ 50/mês. Aqui a triagem sai de heurísticas + léxico calibrado nas 742
vagas que o LLM já tinha rotulado (`jobs.classification` no SQLite; validação no scratchpad
`valida_classificador.py`).

`classify_job_rules` devolve `(classificação, confiança)` no MESMO formato do JSON de
`llm.classify_job`. Com confiança abaixo de `settings.llm.rules_min_confidence`, quem chama cai
para a IA. Campo que a regra não sabe sai no valor neutro que o pipeline tolera
("unspecified", [], None, "") e derruba a confiança.

Reaproveita o que já existe e está calibrado:
  - senioridade: `qa.job_seniority` (o título manda)
  - idioma da descrição: `pipeline.guess_language`
  - vocabulário de tecnologias: `resume.profile.all_skills` + sinônimos de `TECH_ALIASES`
"""
from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

from .resume.profile import all_skills


# ======================================================================================
# normalização
# ======================================================================================
def _fold(s: str) -> str:
    """Sem acento, minúsculo. Mantém # + . / porque fazem parte de nomes (c#, c++, .net, ci/cd)."""
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()


def _squash(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _norm_title(title: str) -> str:
    """Título dobrado sem flexão de gênero: 'Engenheiro(a) de Dados' -> 'engenheiro de dados'."""
    t = _fold(title)
    t = re.sub(r"\((?:a|o|as|os|e|es|x)\)", "", t)
    t = re.sub(r"(?<=[a-z])/(?:a|as|o|os)\b", "", t)
    return _squash(t)


def _key(s: str) -> str:
    """Chave de tecnologia: letras, dígitos, # e + (node.js == nodejs; c# != c++ != c)."""
    return re.sub(r"[^a-z0-9#\+]+", "", _fold(s))


# Lixo das páginas do LinkedIn que confunde o léxico ("show less" casava com LESS/CSS)
BOILERPLATE_RE = re.compile(r"\b(show more|show less|mostrar mais|mostrar menos|exibir mais|exibir menos|ver mais|ver menos)\b")


# ======================================================================================
# léxico de tecnologias: canônico -> sinônimos EXATOS que casam no texto dobrado
# Os canônicos seguem a grafia que o gpt-4.1-mini já usava no banco (compatível com o
# dashboard "stacks mais pedidas" e com `tailor.adaptation_context`).
# ======================================================================================
TECH_ALIASES: dict[str, tuple[str, ...]] = {
    # linguagens
    "python": ("python",),
    "java": ("java",),
    "javascript": ("javascript", "js", "ecmascript"),
    "typescript": ("typescript", "ts"),
    "c#": ("c#", "csharp", "c sharp"),
    ".net": (".net", "dotnet", "dot net", "net core", ".net core", "asp.net", "aspnet", ".net framework", "asp.net core"),
    "php": ("php",),
    "go": ("golang", "go lang"),              # "go" solto só por contexto (TITLE_ONLY_TECH / GO_DESC_RE)
    "rust": ("rust",),
    "ruby": ("ruby",),
    "kotlin": ("kotlin",),
    "swift": ("swift",),
    "swiftui": ("swiftui",),
    "scala": ("scala",),
    "elixir": ("elixir",),
    "dart": ("dart",),
    "c++": ("c++", "cpp"),
    "c": ("linguagem c",),
    "r": ("linguagem r", "rstudio", "r studio"),
    "delphi": ("delphi", "object pascal"),
    "cobol": ("cobol",),
    "abap": ("abap",),
    "advpl": ("advpl", "tlpp"),
    "protheus": ("protheus",),
    "perl": ("perl",),
    "vb.net": ("vb.net", "visual basic"),
    "groovy": ("groovy",),
    "bash": ("bash", "shell script", "shellscript"),
    "pl/sql": ("pl/sql", "plsql", "pl sql"),
    "t-sql": ("t-sql", "tsql"),
    # front
    "react": ("react", "react.js", "reactjs"),
    "next.js": ("next.js", "nextjs", "next js"),
    "angular": ("angular", "angularjs", "angular.js"),
    "vue.js": ("vue.js", "vuejs", "vue", "vue js"),
    "nuxt": ("nuxt", "nuxtjs", "nuxt.js"),
    "svelte": ("svelte", "sveltekit"),
    "html": ("html", "html5"),
    "css": ("css", "css3", "sass", "scss"),
    "tailwind": ("tailwind", "tailwindcss"),
    "bootstrap": ("bootstrap",),
    "jquery": ("jquery",),
    "redux": ("redux",),
    "webpack": ("webpack",),
    "vite": ("vite",),
    "storybook": ("storybook",),
    "mvvm": ("mvvm",),
    # back / frameworks
    "node.js": ("node.js", "nodejs", "node js", "node"),
    "nestjs": ("nestjs", "nest.js", "nest js"),
    "express": ("express.js", "expressjs", "express"),
    "spring boot": ("spring boot", "springboot"),
    "spring security": ("spring security",),
    "spring": ("spring", "spring framework", "spring cloud", "spring mvc"),
    "quarkus": ("quarkus",),
    "micronaut": ("micronaut",),
    "django": ("django",),
    "flask": ("flask",),
    "fastapi": ("fastapi", "fast api"),
    "laravel": ("laravel",),
    "symfony": ("symfony",),
    "codeigniter": ("codeigniter",),
    "ruby on rails": ("ruby on rails", "rails"),
    "hibernate": ("hibernate", "jpa"),
    "entity framework": ("entity framework", "entityframework", "ef core"),
    "asp.net mvc": ("asp.net mvc", "aspnet mvc"),
    "prisma": ("prisma",),
    "typeorm": ("typeorm",),
    "sequelize": ("sequelize",),
    "maven": ("maven",),
    "gradle": ("gradle",),
    "json": ("json",),
    "oauth2": ("oauth2", "oauth 2.0", "oauth"),
    "jwt": ("jwt",),
    # mobile
    "react native": ("react native", "react-native"),
    "flutter": ("flutter",),
    "android": ("android",),
    "ios": ("ios",),
    "xamarin": ("xamarin", "maui"),
    "ionic": ("ionic",),
    # dados
    "sql": ("sql",),
    "postgresql": ("postgresql", "postgres", "postgre"),
    "mysql": ("mysql",),
    "sql server": ("sql server", "sqlserver", "ms sql", "mssql"),
    "oracle": ("oracle", "oracle db", "oracle database"),
    "db2": ("db2",),
    "mongodb": ("mongodb", "mongo"),
    "redis": ("redis",),
    "dynamodb": ("dynamodb",),
    "cassandra": ("cassandra",),
    "elasticsearch": ("elasticsearch", "elastic search", "opensearch"),
    "firebase": ("firebase",),
    "nosql": ("nosql", "no-sql"),
    "snowflake": ("snowflake",),
    "databricks": ("databricks",),
    "bigquery": ("bigquery", "big query"),
    "redshift": ("redshift",),
    "spark": ("spark", "apache spark"),
    "pyspark": ("pyspark",),
    "airflow": ("airflow", "apache airflow"),
    "dbt": ("dbt",),
    "etl": ("etl", "elt"),
    "data lake": ("data lake", "datalake", "lakehouse", "delta lake"),
    "data warehouse": ("data warehouse", "datawarehouse"),
    "pandas": ("pandas",),
    "power bi": ("power bi", "powerbi"),
    "dax": ("dax",),
    "excel": ("excel",),
    "tableau": ("tableau",),
    "looker": ("looker",),
    "qlik": ("qlik", "qlikview", "qlik sense"),
    "ssis": ("ssis",),
    # nuvem / infra
    "aws": ("aws", "amazon web services"),
    "s3": ("s3", "amazon s3"),
    "lambda": ("lambda", "aws lambda"),
    "azure": ("azure", "microsoft azure"),
    "gcp": ("gcp", "google cloud", "google cloud platform"),
    "pub/sub": ("pub/sub", "pubsub"),
    "docker": ("docker",),
    "containers": ("containers", "conteineres", "containerizacao", "conteinerizacao"),
    "kubernetes": ("kubernetes", "k8s", "eks", "aks", "gke"),
    "terraform": ("terraform",),
    "iac": ("iac", "infraestrutura como codigo", "infrastructure as code"),
    "ansible": ("ansible",),
    "jenkins": ("jenkins",),
    "github actions": ("github actions",),
    "gitlab ci": ("gitlab ci", "gitlab-ci", "gitlab ci/cd"),
    "azure devops": ("azure devops", "azure pipelines", "tfs"),
    "ci/cd": ("ci/cd", "ci cd", "cicd", "integracao continua", "continuous integration", "continuous delivery", "entrega continua"),
    "linux": ("linux", "unix"),
    "git": ("git", "github", "gitlab", "bitbucket"),
    "jira": ("jira",),
    "grafana": ("grafana",),
    "prometheus": ("prometheus",),
    "datadog": ("datadog", "data dog"),
    "new relic": ("new relic", "newrelic"),
    "opentelemetry": ("opentelemetry", "open telemetry", "otel"),
    "elk": ("elk", "kibana", "logstash"),
    "openshift": ("openshift",),
    "serverless": ("serverless",),
    "nginx": ("nginx",),
    "helm": ("helm",),
    "argocd": ("argocd", "argo cd"),
    # mensageria / integração
    "kafka": ("kafka", "apache kafka", "confluent"),
    "rabbitmq": ("rabbitmq", "rabbit mq"),
    "sqs": ("sqs", "amazon sqs"),
    "sns": ("sns", "amazon sns"),
    "activemq": ("activemq",),
    "apis rest": ("api rest", "apis rest", "rest api", "rest apis", "restful", "apis restful", "api restful", "rest",
                  "api", "apis", "web api", "web apis"),
    "graphql": ("graphql", "graph ql"),
    "soap": ("soap",),
    "grpc": ("grpc",),
    "webhooks": ("webhook", "webhooks"),
    "microsserviços": ("microsservicos", "microservicos", "microservices", "micro servicos", "microsservico", "microservico", "micro-servicos"),
    "kong": ("kong", "apigee"),
    "mulesoft": ("mulesoft",),
    # testes / qualidade
    "junit": ("junit",),
    "jest": ("jest",),
    "mockito": ("mockito",),
    "pytest": ("pytest",),
    "cypress": ("cypress",),
    "playwright": ("playwright",),
    "selenium": ("selenium",),
    "cucumber": ("cucumber",),
    "tdd": ("tdd", "test driven"),
    "bdd": ("bdd", "behavior driven"),
    "sonarqube": ("sonarqube", "sonar"),
    "testes unitários": ("testes unitarios", "teste unitario", "unit tests", "unit test"),
    "testes automatizados": ("testes automatizados", "automated tests", "automacao de testes"),
    "clean code": ("clean code", "codigo limpo"),
    # arquitetura / práticas (servem para decidir "é dev?", quase nunca viram stack)
    "clean architecture": ("clean architecture", "arquitetura limpa"),
    "ddd": ("ddd", "domain driven"),
    "solid": ("solid",),
    "design patterns": ("design patterns", "padroes de projeto"),
    "event driven": ("event driven", "orientada a eventos", "orientado a eventos", "event-driven"),
    "observabilidade": ("observabilidade", "observability"),
    "mensageria": ("mensageria", "message broker", "filas de mensagem"),
    "scrum": ("scrum",),
    "agile": ("metodologias ageis", "metodologia agil", "agile", "kanban"),
    "devops": ("devops", "dev ops"),
    "cloud": ("cloud", "computacao em nuvem"),
    "ia": ("ia", "inteligencia artificial", "artificial intelligence"),
    # plataformas / low-code / ia
    "salesforce": ("salesforce", "sfdc"),
    "apex": ("apex",),
    "lightning web components": ("lightning web components", "lwc", "lightning components", "lightning"),
    "sap": ("sap",),
    "servicenow": ("servicenow", "service now"),
    "sharepoint": ("sharepoint",),
    "dynamics": ("dynamics 365", "microsoft dynamics"),
    "outsystems": ("outsystems",),
    "mendix": ("mendix",),
    "power apps": ("power apps", "powerapps"),
    "power automate": ("power automate",),
    "power platform": ("power platform",),
    "n8n": ("n8n",),
    "zapier": ("zapier",),
    "make": ("make.com",),
    "wordpress": ("wordpress",),
    "magento": ("magento",),
    "vtex": ("vtex",),
    "shopify": ("shopify",),
    "unity": ("unity", "unity3d"),
    "unreal": ("unreal engine", "unreal"),
    "rpa": ("rpa", "uipath", "automation anywhere", "blue prism"),
    "ia generativa": ("ia generativa", "genai", "gen ai", "generative ai", "ai generativa"),
    "llms": ("llm", "llms", "large language model", "large language models"),
    "langchain": ("langchain", "langgraph"),
    "rag": ("rag", "retrieval augmented"),
    "machine learning": ("machine learning", "aprendizado de maquina", "mlops"),
    "tensorflow": ("tensorflow",),
    "pytorch": ("pytorch",),
    "opencv": ("opencv",),
    "openai": ("openai", "chatgpt", "gpt-4", "copilot"),
}

# Genéricas: entram na stack, mas no fim da fila (mesma ideia de pipeline._GENERIC_STACKS)
GENERIC_TECH = {"sql", "git", "html", "css", "linux", "apis rest", "etl", "nosql", "json", "testes automatizados",
                "clean code", "ci/cd", "containers", "jira", "excel"}
# Práticas/temas que o LLM quase nunca listava como stack (concordância < 20%): só ajudam a decidir "é dev?"
NEVER_OUTPUT = {"observabilidade", "agile", "scrum", "mensageria", "testes unitários", "design patterns", "event driven",
                "solid", "cloud", "ia", "devops", "openai"}
# Citadas de passagem com frequência ("integração com SAP", "app iOS e Android"): só entram com
# a tecnologia no título ou citada 2+ vezes
LOW_SIGNAL = {"sap", "salesforce", "machine learning", "rag", "firebase", "ios", "android", "ia generativa", "llms",
              "nosql", "ddd", "clean architecture", "sqs", "sns", "kong", "elk", "data warehouse", "gitlab ci",
              "serverless", "bdd", "webhooks", "power platform", "openshift", "looker", "langchain", "tdd", "s3",
              "json", "oauth2", "jwt", "excel", "jira", "containers", "lambda", "iac"}
# O sinônimo do "pai" casa dentro do nome do "filho": só vale a menção avulsa do pai
SUBSUMES = {
    "spring boot": ("spring",), "spring security": ("spring",), "react native": ("react",),
    "sql server": ("sql",), "pl/sql": ("sql",), "t-sql": ("sql",), "azure devops": ("azure", "devops"),
    "github actions": ("git",), "gitlab ci": ("git",), "ruby on rails": ("ruby",), "ia generativa": ("ia",),
    "asp.net mvc": (".net",), "aws lambda": ("aws",), "power bi": (), "google cloud": ("cloud",),
}

# Linguagens de programação de verdade: peso alto na decisão "é vaga de dev?"
PROGRAMMING_LANGS = {"python", "java", "javascript", "typescript", "c#", ".net", "php", "go", "rust", "ruby",
                     "kotlin", "swift", "scala", "elixir", "dart", "c++", "c", "delphi", "cobol", "abap",
                     "perl", "vb.net", "groovy", "apex", "advpl", "r"}
TITLE_FRAMEWORKS = {"react", "angular", "vue.js", "next.js", "node.js", "nestjs", "spring boot", "spring", "django",
                    "flask", "fastapi", "laravel", "symfony", "ruby on rails", "react native", "flutter", "android",
                    "ios", "xamarin", "ionic", "quarkus", "express"}


@lru_cache(maxsize=1)
def _tech_patterns() -> list[tuple[str, re.Pattern]]:
    out: list[tuple[str, re.Pattern]] = []
    for canon, aliases in TECH_ALIASES.items():
        body = "|".join(re.escape(a) for a in sorted(set(aliases), key=len, reverse=True))
        out.append((canon, re.compile(r"(?<![a-z0-9\.#\+\-/])(?:" + body + r")(?![a-z0-9\+#]|\.[a-z])")))
    return out


@lru_cache(maxsize=4096)
def _tech_counts(text: str) -> dict[str, int]:
    counts = {canon: n for canon, pat in _tech_patterns() if (n := len(pat.findall(text)))}
    for child, parents in SUBSUMES.items():
        if child in counts:
            for p in parents:
                if p in counts:
                    counts[p] -= counts[child]
                    if counts[p] <= 0:
                        del counts[p]
    return counts


# "Go" e "R" soltos são ambíguos demais; no título ("Desenvolvedor Go") ou ao lado de outra
# linguagem na descrição ("Java, Go ou Python", "Python e R") valem.
_LANG_NEAR = r"(java|python|node(?:\.js)?|kotlin|rust|c#|\.net|php|ruby|elixir|scala|typescript|c\+\+)"
GO_TITLE_RE = re.compile(r"(?<![a-z0-9\.])go(?![a-z0-9])(?!\s*-\s*[a-z]{2}\b)")
GO_DESC_RE = re.compile(r"(linguagem go\b|\bem go\b|go \(golang\)|" + _LANG_NEAR + r"\s*(?:,|e|ou|/)\s*go(?![a-z0-9])"
                        r"|(?<![a-z0-9\.])go\s*(?:,|e|ou|/)\s*" + _LANG_NEAR + r")")
R_DESC_RE = re.compile(r"((python|sql|sas|stata|julia)\s*(?:,|e|ou|/)\s*r(?![a-z0-9\$#\+\.])"
                       r"|(?<![a-z0-9\$])r\s*(?:,|e|ou|/)\s*(python|sql|sas)\b)")


def _title_tech(t: str) -> dict[str, int]:
    out = dict(_tech_counts(t))
    if GO_TITLE_RE.search(re.sub(r",\s*go\b", "", t)):     # ", GO" = Goiás
        out["go"] = out.get("go", 0) + 1
    return out


def _desc_tech(d: str) -> dict[str, int]:
    out = dict(_tech_counts(d))
    for canon, rx in (("go", GO_DESC_RE), ("r", R_DESC_RE)):
        n = len(rx.findall(d))
        if n:
            out[canon] = out.get(canon, 0) + n
    return out


@lru_cache(maxsize=1)
def _alias_index() -> dict[str, str]:
    idx: dict[str, str] = {}
    for canon, aliases in TECH_ALIASES.items():
        idx.setdefault(_key(canon), canon)
    for canon, aliases in TECH_ALIASES.items():
        for a in aliases:
            idx.setdefault(_key(a), canon)
    return idx


@lru_cache(maxsize=2048)
def canonical_tech(name: str) -> str:
    """Nome canônico de uma tecnologia escrita de qualquer jeito ('NodeJS' -> 'node.js')."""
    k = _key(name)
    if not k:
        return ""
    return _alias_index().get(k) or _squash(_fold(name))


# ======================================================================================
# título: é papel de desenvolvimento?
# ======================================================================================
# Sinal de dev "fraco" no título
TITLE_DEV_RE = re.compile(
    r"(desenvolvedor|desenvolvedora|pessoa desenvolvedora|programador|programadora|\bdeveloper\b|\bdev\b|\bdevs\b"
    r"|engenheir[oa] de software|software engineer|engenharia de software|engineering manager"
    r"|full[ \-]?stack|fullstack|back[ \-]?end|front[ \-]?end|backend|frontend"
    r"|engenheir[oa] de dados|data engineer|engenheir[oa] de plataforma|platform engineer"
    r"|engenharia de (dados|plataforma|machine learning|ia|inteligencia artificial|analytics)"
    r"|analytics engineer|engenheir[oa] de analytics"
    r"|engenheir[oa] de machine learning|machine learning engineer|\bml engineer\b|\bai engineer\b"
    r"|\bsre\b|site reliability|devops|cloud engineer|engenheir[oa] de nuvem"
    r"|arquitet[oa] de software|arquitet[oa] de solucoes|solutions architect|software architect|arquitet[oa] de dados"
    r"|tech lead|technical lead|lider tecnic[oa]|staff engineer|principal engineer"
    r"|analista desenvolvedor|analista de desenvolvimento|analista programador"
    r"|engenheir[oa] de qa|qa engineer|engenheir[oa] de testes|sdet"
    r"|mobile engineer|engenheir[oa] mobile|android developer|ios developer"
    r"|especialista em desenvolvimento|especialista de desenvolvimento"
    r"|\bapplication engineer\b|\bsoftware\b|\bcoding\b|\bprogramacao\b|\bengineer\b|engenheir[oa]\b)",
    re.I,
)

# Sinal de dev "forte" no título: papel inequívoco de escrever código
DEV_CORE_RE = re.compile(
    r"(desenvolvedor|desenvolvedora|pessoa desenvolvedora|programador|programadora|\bdeveloper\b"
    r"|engenheir[oa] de software|software engineer|engenharia de software"
    r"|full[ \-]?stack|fullstack|back[ \-]?end|front[ \-]?end|backend|frontend"
    r"|engenheir[oa] de dados|data engineer|engenheir[oa] de plataforma|platform engineer"
    r"|engenharia de (dados|plataforma|machine learning|ia|inteligencia artificial|analytics)"
    r"|analytics engineer|engenheir[oa] de analytics"
    r"|engenheir[oa] de machine learning|machine learning engineer"
    r"|\bsre\b|site reliability|devops|cloud engineer"
    r"|arquitet[oa] de software|arquitet[oa] de solucoes|solutions architect|software architect"
    r"|tech lead|technical lead|lider tecnic[oa]|staff engineer|principal engineer"
    r"|analista desenvolvedor|analista de desenvolvimento|analista programador"
    r"|mobile engineer|engenheir[oa] mobile|android developer|ios developer"
    r"|engenheir[oa] de (ia|inteligencia artificial|ai|integracao|integracoes|aplicac)|\bai engineer\b|\bml engineer\b"
    r"|arquitet[oa] (de )?(java|\.net|cloud|devops|integracao|integracoes|sistemas|aplicac|aws|azure|backend|front)"
    r"|\bsoftware engineering\b)",
    re.I,
)

# Sinal de NÃO-dev "fraco" no título
TITLE_NONDEV_RE = re.compile(
    r"(analista de dados|analista de bi\b|analista bi\b|analista de business|business intelligence"
    r"|analista de b\.?i|data analyst|\bbi\b(?! *dev)|cientista de dados|data scientist"
    r"|\bdba\b|administrador de banco|administradora de banco|database administrator|administrador de bancos"
    r"|product manager|product owner|gerente de produto|\bpo\b|\bpm\b|project manager|gerente de projet"
    r"|scrum master|agilista|agile coach|\bdesigner\b|\bdesign\b|\bux\b|\bui\b(?!/)|product design"
    r"|marketing|social media|comercial|vendas|\bcrm\b|e-?commerce|ecommerce|midia|conteudo"
    r"|recrutamento|recrutador|talent|people|rh\b|financeiro|contabil|contador|juridic"
    r"|suporte tecnic|help ?desk|service ?desk|\bn1\b|\bn2\b|infraestrutura|infra\b"
    r"|professor|instrutor|docente|mentor\b|monitor\b"
    r"|consultor|consultora|consultant|funcional|especialista funcional"
    r"|analista de negocio|business analyst|analista de requisito|analista de processo"
    r"|analista de qualidade|analista de teste|analista de sistemas de qualidade"
    r"|analista de seguranca|seguranca da informacao|pentest|red team"
    r"|analista de suporte|analista de infra|analista de redes|analista fiscal"
    r"|analista de custos|analista de planejamento|analista de precificacao|analista de marketplace"
    r"|analista de e-?commerce|analista de crm|analista de midia|analista de automacao de marketing"
    r"|tecnic[oa] de|operador|assistente|auxiliar|estagi|trainee|aprendiz"
    r"|banco de talentos|\btalentos\b"
    r"|arquitetura de interiores|arquitet[oa] urbanista)",
    re.I,
)

# NÃO-dev "duro": mesmo com palavra de dev no título, o papel não é de escrever código
HARD_NONDEV_RE = re.compile(
    r"(analista de bi\b|analista bi\b|analista de b\.?i\b|analista de business"
    r"|analista de inteligencia de dados"
    r"|\bdba\b|administrador[a]? de banco|administrador[a]? de bancos|database administrator|administradora sr - banco"
    r"|consultor|consultora|consultant"
    r"|product manager|product owner|gerente de produto|project manager|gerente de projet|coordenador|coordenadora"
    r"|scrum master|agilista|agile coach"
    r"|\bdesigner\b|web design|product design|\bux\b|\bui/ux\b|design educacional|design instrucional"
    r"|marketing|social media|comercial|vendas|analista de crm|analista crm|\be-?commerce\b|ecommerce|marketplace"
    r"|recrutamento|recrutador|\brh\b|financeir|contabil|juridic|analista de custos|analista de precificacao"
    r"|suporte tecnic|help ?desk|service ?desk|analista de suporte"
    r"|professor|instrutor|docente"
    r"|analista de negocio|business analyst|analista de requisito|analista de processo|analista funcional"
    r"|analista de qualidade|analista de teste[s]? de software|analista de seguranca|red team"
    r"|analista de infraestrutura|analista de redes|tecnic[oa] de|operador|assistente|auxiliar"
    r"|estagi|trainee|aprendiz|\bintern\b"
    r"|banco de talentos|analista de implementacao|implementador"
    r"|analista sap|especialista sap|\bsap (mm|sd|fi|co|pp|qm|bw|hybris|tax|s/4|practice|td|sac)\b|consultor sap"
    r"|transformacao digital|analista de planejamento|analista de visualizacao"
    r"|especialista em ti\b|especialista de ti\b|analista power bi|analista de power bi"
    r"|arquitet[oa] enterprise|arquitetura enterprise|arquitet[oa] de solucoes enterprise"
    r"|programador de clp|automacao industrial)",
    re.I,
)

# Arquiteto de prédio: título "Arquiteto" pelado + descrição de obra/Revit
PHYSICAL_ARCH_RE = re.compile(r"^\s*arquitet[oa]\s*($|\(|-|\|)|arquitetura de interiores|urbanista", re.I)
PHYSICAL_ARCH_DESC_RE = re.compile(r"(revit|autocad|sketchup|projetos? arquitetonic|obras?\b|interiores|urbanismo|\bcau\b|luminotecnic)", re.I)
SAP_TITLE_RE = re.compile(r"\bsap\b", re.I)
SAP_DEV_RE = re.compile(r"(abap|desenvolvedor|developer|programador|btp|fiori|ui5|arquitet|architect|hybris|commerce cloud|fullstack|full stack)", re.I)
INTERN_RE = re.compile(r"(estagi|\bintern\b|internship|trainee|aprendiz)", re.I)
# Famílias em que o próprio LLM se dividia (~50% dev): a regra decide, com confiança baixa -> IA
AMBIGUOUS_TITLE_RE = re.compile(r"(analista de dados|data analyst|cientista de dados|data scientist|ciencia de dados"
                                r"|especialista (de|em) dados|specialist of data|analista (de )?sistemas|analista de ti\b"
                                r"|especialista (de|em) ti\b|analista de automac|analista de integrac|consultor)", re.I)

# Descrição: mão na massa em código
DEV_VERBS_RE = re.compile(
    r"(desenvolver|desenvolvimento de (software|sistemas|aplicac|solucoes|api)|codificar|codificacao"
    r"|escrever codigo|escrita de codigo|qualidade de codigo|revisao de codigo|code review|codigo limpo"
    r"|pull request|merge request|versionamento|refatorac|refatorar|deploy|pipeline de ci"
    r"|programar|programacao|implementar (api|servic|funcionalidad|endpoint|integrac)"
    r"|manutencao de sistemas|sustentacao de sistemas|novas funcionalidades|construir aplicac"
    r"|testes unitarios|testes automatizados|arquitetura de software|clean code|boas praticas de desenvolvimento)",
    re.I,
)
# Descrição: papel analítico/funcional/comercial (sem código)
NONDEV_TERMS_RE = re.compile(
    r"(dashboards?|relatorios|indicadores|kpis?|insights|tomada de decisao|analises de negocio"
    r"|power bi|business intelligence|visualizacao de dados|storytelling"
    r"|levantamento de requisitos|mapeamento de processos|documentacao funcional"
    r"|parametrizacao|customizacao funcional|modulos sap|consultoria funcional"
    r"|atendimento ao cliente|prospeccao|campanhas|leads|funil de vendas"
    r"|desenvolvimento de negocios|business development|novos negocios|metas comerciais)",
    re.I,
)

# ======================================================================================
# idioma exigido
# ======================================================================================
_LEVELS = r"(avancad[oa]|fluente|fluencia|intermediari[oa]|tecnic[oa]|basic[oa]|conversacao|nativ[oa]|profissional|b2|c1|c2|proficien\w*)"
ENGLISH_RE = re.compile(
    r"(ingles[^.;\n]{0,60}?" + _LEVELS
    + r"|" + _LEVELS + r"[^.;\n]{0,30}?em ingles"
    + r"|" + _LEVELS + r"\s+(de|do|no|em)\s+ingles"
    + r"|ingles (obrigatori|necessari|requerid|imprescind|sera um requisito|e um requisito)"
    r"|(nivel de |dominio do |dominio de )?ingles\b[^.;\n]{0,40}(obrigatori|requisito|necessari)"
    r"|\benglish\b[^.;\n]{0,40}(required|fluent|advanced|proficien|mandatory|level|speaking|skills|is a must)"
    r"|(fluent|advanced|proficient|intermediate|conversational)[^.;\n]{0,20}\benglish\b"
    r"|(communicat\w*|written|oral|spoken|speak)[^.;\n]{0,40}\bin english\b"
    r"|(ler|leitura|comunicar|comunicacao|conversar|escrever|falar|reunioes)[^.;\n]{0,30}em ingles"
    r"|ingles para (leitura|conversacao|comunicacao|reunioes|documentac|interpretac|consumo|colaborac|interacao)"
    r"|ingles escrito e falado|ingles falado e escrito)",
    re.I,
)
SPANISH_RE = re.compile(
    r"(espanhol[^.;\n]{0,60}?" + _LEVELS + r"|" + _LEVELS + r"\s+(de|do|em)\s+espanhol"
    + r"|espanhol (obrigatori|necessari|requerid)"
    r"|\bspanish\b[^.;\n]{0,40}(required|fluent|advanced|proficien|mandatory)"
    r"|espanhol para (leitura|conversacao|comunicacao|interacao))",
    re.I,
)
OPTIONAL_RE = re.compile(r"(desejavel|diferencial|plus\b|nice to have|opcional|sera um diferencial|bonus|valorizado|nao (e|eh) obrigatori)", re.I)
# idioma citado como BENEFÍCIO ("convênio com escola de inglês/espanhol") não é exigência
LANG_BENEFIT_RE = re.compile(r"(convenio|parceria|escola de idiomas|cursos? de (ingles|idiomas|espanhol)|aulas de|bolsa|subsidio|desconto"
                             r"|beneficio|auxilio|incentivo|plataforma de idiomas|ensino de idiomas)", re.I)
# cabeçalho de seção: "Diferenciais:" / "Requisitos desejáveis" x "Requisitos:" / "O que buscamos"
OPT_HEADER_RE = re.compile(r"^(requisitos |qualificacoes |conhecimentos |skills |sera )?(diferencia|desejave|nice to have|bonus|pontos extras|um (plus|diferencial)|plus\b)")
REQ_HEADER_RE = re.compile(r"^(requisitos|requirements|qualificacoes|o que (buscamos|esperamos|voce precisa|procuramos)|must have|obrigatori"
                           r"|responsabilidades|atividades|o que voce (vai|fara)|beneficios|sobre|quem (buscamos|procuramos))")

# ======================================================================================
# modelo de trabalho
# ======================================================================================
HYBRID_RE = re.compile(r"\b(hibrid[oa]s?|hybrid|semi[ \-]?presencial|modelo misto)\b", re.I)
ONSITE_ONLY_RE = re.compile(r"\b(presencial|100% presencial|totalmente presencial|no escritorio|on ?site|alocado no cliente)\b", re.I)
REMOTE_STRONG_RE = re.compile(r"(100% remot|totalmente remot|remoto integral|full remote|fully remote|home ?office integral|anywhere office"
                              r"|trabalho remoto|modelo remoto|remoto \(|\(remoto\)|remote first|remoto: sim)", re.I)
REMOTE_ANY_RE = re.compile(r"\b(remot[oa]s?|home\s?office|remote|anywhere|de qualquer lugar)\b", re.I)
MODEL_LINE_RE = re.compile(r"((modelo|regime|formato|modalidade)( de (trabalho|atuacao|contratacao))?|local de trabalho|location)\s*[:\-]?\s*\(?"
                           r"(100% |totalmente )?(remot[oa]|hibrid[oa]|presencial|home ?office|hybrid|remote|on ?site)[^\n\.;]{0,60}", re.I)
# "remoto" que NÃO é modelo de trabalho: benefício, produto, termo técnico
NOT_WORK_REMOTE_RE = re.compile(
    r"((auxilio|ajuda de custo|subsidio|bolsa|kit|vale|reembolso|beneficio)[^\n;]{0,30}(home ?office|trabalho remoto|remoto)[^\n;]{0,120}"
    r"|(um|uma|1|dois|duas|2|tres|3|\d+) (dias?|semanas?) (por semana |na semana |por mes |no ano )?de (home ?office|trabalho remoto)"
    r"|home ?office (1|2|3|uma|duas|tres) ?(x|vezes|dias)"
    r"|remote ?configs?|remote ?debug\w*"
    r"|(gerenciamento|acesso|monitoramento|diagnostico|suporte tecnico local e|suporte|atendimento|controle)( tecnico)? remot[oa]"
    r"|(configurar|monitorar|diagnosticar|gerenciar|operar)[^\n\.;]{0,60}de forma remota)",
    re.I,
)
HYBRID_HINT_RE = re.compile(
    r"((\d+|um|uma|dois|duas|tres) ?(x|dias?|vezes)( por| na| a cada)? (semana|mes)[^\n\.;]{0,30}(presencia|no escritorio|na sede|home ?office)"
    r"|(presencia\w*|escritorio|na sede)[^\n\.;]{0,30}(\d+|um|uma|dois|duas|tres) ?(x|dias?|vezes)( por| na)? semana"
    r"|remot\w*[^\n\.;]{0,15}(ate )?(\d+|um|uma|dois|duas|tres) ?(x|vezes|dias?)( por| na)? semana"
    r"|um dia de home ?office)",
    re.I,
)
# "a vaga NÃO é remota", "sem home office": vale mais que qualquer "remoto" do texto
NEGATED_REMOTE_RE = re.compile(
    r"(nao (e|eh|sera|se trata de|oferece|oferecemos|temos|trabalhamos com|possui|permite|aceita)[^\n\.;]{0,25}(remot|home ?office)"
    r"|sem (possibilidade de )?(home ?office|trabalho remoto|remoto))", re.I)
# "remoto, mas comparecer presencialmente 1x ao mês" = híbrido (o LLM marcava assim)
PERIODIC_ONSITE_RE = re.compile(
    r"(presencia\w*|no escritorio|na sede)[^\n\.;]{0,60}?(\d+|um|uma|dois|duas|tres) ?(x|vez|vezes|dias?)( por| ao| na| a cada)? ?(semana|mes|quinzena|15 dias|trimestre)", re.I)
RELOCATION_RE = re.compile(r"(mudanca de (cidade|estado)|disponibilidade para (mudanca|residir|morar)|residir em|morar em|residencia em|relocation|relocate)", re.I)

# ======================================================================================
# agência / salário / contrato / anos
# ======================================================================================
STAFFING_NAME_RE = re.compile(r"(consultoria|consulting|it services|staffing|\brh\b|recruit|talent|outsourc|allocation|tech ?people|people ?tech)", re.I)
STAFFING_TEXT_RE = re.compile(r"(nosso cliente|alocacao|alocad[oa]|body ?shop|para atuar em um de nossos clientes|projeto do cliente"
                              r"|em nossos clientes|prestacao de servicos de ti|banco de talentos|cliente do segmento|cliente final)", re.I)
SALARY_RE = re.compile(
    r"(salari\w*|remunerac\w*|faixa salarial|pretensao|pacote|valor (da vaga|hora)|bolsa)[^\n\.;]{0,60}?"
    r"(r\$ ?\d[\d\.\, ]{2,}(?:\s*(?:a|-|ate|e)\s*r?\$? ?\d[\d\.\, ]{2,})?)",
    re.I,
)
YEARS_RE = re.compile(r"(?:(?:no minimo|minimo de|pelo menos|acima de|mais de|a partir de)\s*)?(\d{1,2})\s*\+?\s*(?:a|ate|-)?\s*(\d{1,2})?\s*anos?[^\n\.;]{0,40}?(experiencia|atuacao|vivencia)", re.I)
YEARS_EN_RE = re.compile(r"(\d{1,2})\+?\s*years?[^\n\.;]{0,30}(experience)", re.I)
CLT_RE = re.compile(r"\b(clt|carteira assinada|regime clt|contratacao clt)\b", re.I)
PJ_RE = re.compile(r"\b(pj\b|pessoa juridica|cnpj|contrato pj)\b", re.I)


# ======================================================================================
# API
# ======================================================================================
def classify_job_rules(title: str, company: str, location: str, description: str,
                       profile: dict, settings) -> tuple[dict, float]:
    """Classifica uma vaga sem IA.

    Devolve (classificação no formato de `llm.classify_job`, confiança 0..1). Confiança abaixo de
    `settings.llm.rules_min_confidence` = a heurística não se decidiu; quem chama usa o LLM.
    `settings` pode ser None (só é lido para informação; nenhum limiar é aplicado aqui).
    """
    title = title or ""
    description = description or ""
    location = location or ""
    t = _norm_title(title)
    d = BOILERPLATE_RE.sub(" ", _fold(description)[:20000])
    floc = _fold(location)

    stack, stack_conf = _detect_stack(t, d, profile)
    raw_stack = _raw_stack(t, d)
    dev, dev_conf, dev_reason = _is_dev_role(t, d, raw_stack)
    work_model, wm_conf, city = _work_model(t, floc, location, d)
    seniority = _seniority(title, d)
    language = _language(description)
    req_langs = _required_languages(f"{t}\n{d}")
    relocation = bool(work_model in ("onsite", "hybrid") and RELOCATION_RE.search(d))

    cls = {
        "is_dev_role": dev,
        "role_family": _role_family(t, d, stack),
        "seniority": seniority,
        "stack": stack,
        "language": language,
        "work_model": work_model,
        "job_city": city,
        "requires_relocation": relocation,
        "required_languages": req_langs,
        "hard_requirements": _hard_requirements(d, work_model, city, req_langs),
        "min_years_required": _min_years(d),
        "salary_offered": _salary_offered(description),
        "contract_type": _contract_type(d),
        "is_staffing_agency": bool(STAFFING_TEXT_RE.search(d)) or bool(STAFFING_NAME_RE.search(_fold(company or ""))),
        "fit_score": _fit_score(stack, profile, dev),
        "hard_reject": False,     # o pipeline já decide pelos campos acima; sem IA não arriscamos aqui
        "reason": dev_reason,
        "_rules": True,           # marca que veio do classificador determinístico
    }
    return cls, _confidence(dev_conf, wm_conf, stack_conf, language, d)


# ======================================================================================
# stack
# ======================================================================================
def _raw_stack(t: str, d: str) -> list[str]:
    """Todas as tecnologias citadas (inclusive práticas); só para decidir "é dev?"."""
    return list({**_desc_tech(d), **_title_tech(t)})


def _detect_stack(t: str, d: str, profile: dict, limit: int = 14) -> tuple[list[str], float]:
    """Tecnologias pedidas, ordenadas por evidência. Precisão importa: a stack alimenta a
    adaptação do currículo (`tailor.adaptation_context`)."""
    in_title = _title_tech(t)
    in_desc = _desc_tech(d)
    if not in_title and not in_desc:
        return [], 0.3
    known = {canonical_tech(s) for s in all_skills(profile)}
    first = {}
    for canon, pat in _tech_patterns():
        if canon in in_desc:
            m = pat.search(d)
            first[canon] = m.start() if m else 99999
    scored: list[tuple[float, int, str]] = []
    for canon in set(in_title) | set(in_desc):
        if canon in NEVER_OUTPUT:
            continue
        nt, nd = in_title.get(canon, 0), in_desc.get(canon, 0)
        if canon in LOW_SIGNAL and not nt and nd < 2:
            continue
        score = 4.0 * nt + min(nd, 5)
        if canon in GENERIC_TECH:
            score *= 0.5
        if canon in PROGRAMMING_LANGS:
            score *= 1.4
        if canon in known:
            score *= 1.15      # desempate a favor do que o usuário tem no perfil
        scored.append((-score, first.get(canon, 99999), canon))
    scored.sort()
    stack = [c for _, _, c in scored[:limit]]
    specific = [c for c in stack if c not in GENERIC_TECH]
    conf = 0.9 if len(specific) >= 3 else (0.7 if specific else 0.4)
    return stack, conf


# ======================================================================================
# é vaga de desenvolvimento?
# ======================================================================================
def _is_dev_role(t: str, d: str, stack: list[str]) -> tuple[bool, float, str]:
    core = bool(DEV_CORE_RE.search(t))
    soft = bool(TITLE_DEV_RE.search(t))
    hard_non = bool(HARD_NONDEV_RE.search(t))
    soft_non = bool(TITLE_NONDEV_RE.search(t))
    # só linguagem/framework no título "salva" um título não-dev ("Consultor(a) Mobile | React Native")
    title_tech = [c for c in _title_tech(t) if c in PROGRAMMING_LANGS or c in TITLE_FRAMEWORKS]
    if hard_non and title_tech and not SAP_TITLE_RE.search(t) and not INTERN_RE.search(t):
        hard_non, soft_non = False, True
    if SAP_TITLE_RE.search(t) and not SAP_DEV_RE.search(t):
        hard_non = True                        # consultoria/funcional SAP (SD, MM, FI, TAX...) não é dev
    if PHYSICAL_ARCH_RE.search(t) and (PHYSICAL_ARCH_DESC_RE.search(d) or not stack):
        hard_non = True

    langs = [s for s in stack if s in PROGRAMMING_LANGS]
    verbs = len(DEV_VERBS_RE.findall(d))
    anti = len(NONDEV_TERMS_RE.findall(d))
    score = 0.0
    if core:
        score += 3.2
    elif soft:
        score += 1.4
    if hard_non:
        score -= 3.4
    elif soft_non:
        score -= 1.6
    score += min(len(langs), 3) * 0.7
    score += min(verbs, 6) * 0.32
    score -= min(anti, 8) * 0.30
    if not langs and not core:
        score -= 0.8
    if len(d) < 200:                     # sem descrição: decide pelo título (como o prompt do LLM)
        score = 3.0 if core else (1.0 if soft else -1.5)

    dev = score > 0
    conf = min(1.0, 0.5 + abs(score) / 4.2)
    if AMBIGUOUS_TITLE_RE.search(t) and not core:
        conf = min(conf, 0.6)
    if INTERN_RE.search(t):
        dev, conf = False, max(conf, 0.85)   # estágio fica fora (settings: exclude_roles)
    if dev:
        reason = "regras: título/descrição de desenvolvimento" + (f" ({', '.join(langs[:3])})" if langs else "")
    else:
        reason = "regras: papel não-dev (análise, consultoria, BI, gestão, suporte ou estágio)"
    return dev, conf, reason


# ======================================================================================
# senioridade (qa.job_seniority) / idioma (pipeline.guess_language)
# ======================================================================================
class _JobShim:
    """Só o que `qa.job_seniority` lê de um Job."""
    __slots__ = ("title", "classification")

    def __init__(self, title: str, classification: dict):
        self.title = title
        self.classification = classification


DESC_SENIOR_RE = re.compile(r"\b(senior|sr\.?|especialista|staff|principal)\b", re.I)
DESC_MID_RE = re.compile(r"\b(pleno|mid[ \-]?level|pl\.)\b", re.I)
DESC_JUNIOR_RE = re.compile(r"\b(junior|jr\.?|entry[ \-]?level|primeiro emprego)\b", re.I)
# rodapé das vagas do LinkedIn; "Pleno-sênior" e "Não aplicável" não dizem nada (o LLM se dividia)
FOOTER_JUNIOR_RE = re.compile(r"(nivel de experiencia|seniority level)\s*\n?\s*(assistente|junior|estagio|entry level|internship)")


def _seniority(title: str, d: str) -> str:
    """O título manda (qa.job_seniority). Título mudo: a palavra de nível mais citada na descrição;
    se empatar, os anos mínimos pedidos (>=5 sênior, 3-4 pleno, <=2 júnior); senão 'unspecified'."""
    from .qa import job_seniority
    counts = {"senior": len(DESC_SENIOR_RE.findall(d)), "mid": len(DESC_MID_RE.findall(d)),
              "junior": len(DESC_JUNIOR_RE.findall(d))}
    top = max(counts.values())
    guess = "unspecified"
    if FOOTER_JUNIOR_RE.search(d):
        guess = "junior"
    elif top and list(counts.values()).count(top) == 1:
        guess = max(counts, key=counts.get)
    else:
        years = _min_years(d)
        if years is not None:
            guess = "senior" if years >= 5 else ("mid" if years >= 3 else "junior")
    return job_seniority(_JobShim(title, {"seniority": guess}))


def _language(description: str) -> str:
    from .pipeline import guess_language     # import tardio: o pipeline importa este módulo
    return guess_language(description or "")


# ======================================================================================
# idioma exigido
# ======================================================================================
def _sentence_around(d: str, m: re.Match) -> str:
    a = max(d.rfind(".", 0, m.start()), d.rfind("\n", 0, m.start()), d.rfind(";", 0, m.start()))
    ends = [x for x in (d.find(".", m.end()), d.find("\n", m.end()), d.find(";", m.end())) if x != -1]
    return d[a + 1:min(ends) if ends else len(d)].strip()


OPTIONAL_LOOKBACK_LINES = 1      # cabeçalho "Diferenciais:" vale só para a linha logo abaixo (varredura: melhor recall)


def _in_optional_section(d: str, pos: int) -> bool:
    """A menção está logo abaixo de um cabeçalho 'Diferenciais/Desejável'? Janela curta de propósito:
    na dúvida é exigência (o usuário prefere perder a vaga a aplicar para vaga que pede inglês)."""
    lines = [x.strip(" -•*:\t") for x in d[max(0, pos - 600):pos].split("\n")[:-1]]
    lines = [x for x in lines if x][-OPTIONAL_LOOKBACK_LINES:] if OPTIONAL_LOOKBACK_LINES else []
    for line in reversed(lines):
        if len(line) > 60:
            continue
        if OPT_HEADER_RE.search(line):
            return True
        if REQ_HEADER_RE.search(line):
            return False
    return False


def _required_languages(d: str) -> list[str]:
    """Exigência de idioma estrangeiro. O usuário NÃO aplica para vaga que exige inglês: erramos para
    o lado de detectar. Menção em frase ou seção de 'desejável/diferencial' sai marcada '(desejável)',
    que o pipeline já trata como não obrigatório. Idioma como benefício (convênio) é ignorado."""
    out: list[str] = []
    for name, rx in (("inglês", ENGLISH_RE), ("espanhol", SPANISH_RE)):
        found: list[str] = []
        for m in rx.finditer(d):
            sent = _sentence_around(d, m)
            if LANG_BENEFIT_RE.search(d[max(0, m.start() - 60):m.start()]):
                continue                   # "convênio com escolas de: inglês/espanhol"
            lv = re.search(_LEVELS, m.group(0), re.I)
            level = ""
            if lv:
                level = {"fluencia": "fluente", "conversacao": "para conversação"}.get(lv.group(0), lv.group(0))
                level = "fluente" if level.startswith("proficien") else level
            label = f"{name} {level}".strip()
            if OPTIONAL_RE.search(sent[:400]) or _in_optional_section(d, m.start()):
                label += " (desejável)"
            found.append(label)
            if "desejável" not in label:
                break                      # uma exigência firme basta
        if found:
            firm = [f for f in found if "desejável" not in f]
            out.append(firm[0] if firm else found[0])
    return out


# ======================================================================================
# modelo de trabalho
# ======================================================================================
def _city_from_location(location: str) -> str:
    loc = _squash(location)
    if not loc or _fold(loc) in ("brasil", "brazil", "remoto", "remote", "home office", "brasil (remoto)"):
        return ""
    loc = re.sub(r"\s*\(.*?\)\s*", " ", loc).strip()
    loc = re.sub(r",?\s*(Brasil|Brazil)$", "", loc, flags=re.I).strip()
    loc = re.sub(r"\s+e Regi[aã]o$", "", loc, flags=re.I).strip()
    return loc.strip(" ,-")


def _work_model(t: str, floc: str, location: str, d: str) -> tuple[str, float, str]:
    """remote|hybrid|onsite|unspecified, confiança e cidade (só para presencial/híbrido)."""
    city = _city_from_location(location)
    dw = NOT_WORK_REMOTE_RE.sub(" ", d)       # "auxílio home office", "remote config"... não contam

    def mark(model: str, conf: float) -> tuple[str, float, str]:
        return model, conf, (city if model in ("onsite", "hybrid") else "")

    # 0) negação explícita e ida periódica ao escritório vencem qualquer "remoto"
    if NEGATED_REMOTE_RE.search(d):
        return mark("hybrid" if HYBRID_RE.search(dw) else "onsite", 0.85)
    if PERIODIC_ONSITE_RE.search(dw):
        return mark("hybrid", 0.8)
    # 1) linha explícita "Modelo de trabalho: X"
    m = MODEL_LINE_RE.search(dw)
    if m:
        line = m.group(0)
        if HYBRID_RE.search(line):
            # "híbrido: 3 dias presenciais + 2 home office" e "híbrido em SP ou remoto nas demais
            # regiões" = híbrido (o LLM também marcava assim); na dúvida, o lado seguro
            return mark("hybrid", 0.92)
        if REMOTE_ANY_RE.search(line) and not ONSITE_ONLY_RE.search(line):
            return mark("remote", 0.92)
        if ONSITE_ONLY_RE.search(line) and not REMOTE_ANY_RE.search(line):
            return mark("onsite", 0.9)
    # 2) título
    if HYBRID_RE.search(t):
        return mark("hybrid", 0.9)
    if ONSITE_ONLY_RE.search(t) and not REMOTE_ANY_RE.search(t):
        return mark("onsite", 0.9)
    if REMOTE_ANY_RE.search(t):
        return mark("remote", 0.9)
    # 3) campo de local
    if REMOTE_ANY_RE.search(floc):
        return mark("remote", 0.9)
    # 4) corpo da descrição
    strong_remote = bool(REMOTE_STRONG_RE.search(dw))
    hybrid = bool(HYBRID_RE.search(dw)) or bool(HYBRID_HINT_RE.search(d))
    onsite = bool(ONSITE_ONLY_RE.search(dw))
    remote_any = bool(REMOTE_ANY_RE.search(dw))
    if strong_remote and not hybrid:
        return mark("remote", 0.88)
    if hybrid:
        # "100% remoto" + "híbrido" em outro ponto: o LLM ficava com híbrido; confiança menor
        return mark("hybrid", 0.8 if not strong_remote else 0.65)
    if onsite and not remote_any:
        return mark("onsite", 0.85)
    if remote_any and not onsite:
        return mark("remote", 0.75 if REMOTE_ANY_RE.search(dw[:1200]) else 0.6)
    if onsite and remote_any:
        return mark("onsite" if city else "remote", 0.5)
    # 5) texto mudo: o campo de local decide (cidade => presencial, como o LLM fazia; o hard_reject
    #    dele era "presencial implícito pela localização"). Sem descrição não dá para inferir.
    if city and len(d) >= 200:
        return mark("onsite", 0.8 if _fold(city.split(",")[0]) in d else 0.6)
    return mark("unspecified", 0.45)


# ======================================================================================
# demais campos
# ======================================================================================
DATA_RE = re.compile(r"(engenheir[oa] de dados|data engineer|pipeline de dados|etl|databricks|airflow|spark|data lake|analytics)", re.I)


def _role_family(t: str, d: str, stack: list[str]) -> str:
    if re.search(r"(tech lead|technical lead|lider tecnic|engineering manager|head de|coordenador tecnic)", t):
        return "lead"
    if re.search(r"(arquitet|architect)", t):
        return "architect"
    if re.search(r"(mobile|android|\bios\b|flutter|react native)", t):
        return "mobile"
    if re.search(r"(devops|\bsre\b|site reliability|platform engineer|cloud engineer|infra)", t):
        return "devops"
    if re.search(r"(dados|\bdata\b|\bbi\b|analytics|machine learning|\bml\b)", t):
        return "data"
    if re.search(r"(full[ \-]?stack|fullstack)", t):
        return "fullstack"
    if re.search(r"(back[ \-]?end|backend)", t):
        return "backend"
    if re.search(r"(front[ \-]?end|frontend)", t):
        return "frontend"
    s = set(stack)
    front = {"react", "angular", "vue.js", "next.js", "html", "css", "nuxt", "svelte"} & s
    back = {"java", "python", "node.js", "c#", ".net", "php", "go", "spring boot", "django", "laravel", "nestjs",
            "kotlin", "ruby", "rust", "elixir", "scala"} & s
    if front and back:
        return "fullstack"
    if back:
        return "backend"
    if front:
        return "frontend"
    if DATA_RE.search(d):
        return "data"
    return "other"


def _min_years(d: str) -> int | None:
    best: int | None = None
    for rx in (YEARS_RE, YEARS_EN_RE):
        for m in rx.finditer(d[:9000]):
            try:
                n = int(m.group(1))
            except (TypeError, ValueError):
                continue
            if 1 <= n <= 15 and (best is None or n < best):
                best = n
    return best


def _salary_offered(description: str) -> str:
    """Só com 'salário/remuneração/faixa' perto do valor (vale-refeição de R$ 500 não é salário)."""
    # travessão some no _fold ("R$ 3.000–R$ 7.500" viraria só "R$ 3.000" e o qa responderia o piso)
    m = SALARY_RE.search(_fold(re.sub(r"[\u2013\u2014\u2212]", "-", description or "")))
    if not m:
        return ""
    return _squash(re.sub(r"r\$\s*", "R$ ", m.group(3))).strip(" ,.")


def _contract_type(d: str) -> str:
    clt, pj = bool(CLT_RE.search(d)), bool(PJ_RE.search(d))
    if clt and not pj:
        return "CLT"
    if pj and not clt:
        return "PJ"
    return "unspecified"


def _hard_requirements(d: str, work_model: str, city: str, req_langs: list[str]) -> list[str]:
    out: list[str] = []
    if work_model in ("onsite", "hybrid") and city:
        out.append(f"trabalho {'presencial' if work_model == 'onsite' else 'híbrido'} em {city}")
    out += [r for r in req_langs if "desejável" not in r]
    if re.search(r"(ensino superior completo (e )?(obrigatori|sera exigido)|formacao superior completa obrigatori|diploma obrigatori)", d):
        out.append("formação superior completa obrigatória")
    if re.search(r"(certificacao [a-z0-9 \-]{2,30} obrigatori|certificacao obrigatori)", d):
        out.append("certificação obrigatória")
    if re.search(r"(\bcnh\b|carteira de habilitacao)", d):
        out.append("CNH")
    return out[:5]


def _fit_score(stack: list[str], profile: dict, dev: bool) -> int:
    """Aderência = quanto das 3 tecnologias específicas mais citadas o usuário tem no perfil
    (skills.* + stack das experiências). Calibrado contra o gpt-4.1-mini no corte que o pipeline usa
    (`apply.min_fit_score: 70`, 84% de acordo): >= 1 das 3 no perfil passa; nenhuma, ou vaga sem
    stack, fica abaixo. Presencial e idioma já descartam a vaga por outros campos."""
    if not dev:
        return 20
    known = {canonical_tech(s) for s in all_skills(profile)}
    specific = [s for s in stack if s not in GENERIC_TECH] or stack
    if not specific:
        return 55
    top = specific[:3]
    return int(round(58 + 37 * (sum(1 for s in top if s in known) / len(top))))


def _confidence(dev_conf: float, wm_conf: float, stack_conf: float, language: str, d: str) -> float:
    """O que pesa é acertar "é dev?" (candidatura errada) e o modelo de trabalho (remoto)."""
    conf = 0.60 * dev_conf + 0.25 * wm_conf + 0.15 * stack_conf
    if language != "pt-BR":
        conf = min(conf, 0.9)
    if len(d) < 200:
        conf -= 0.05
    return round(max(0.0, min(1.0, conf)), 3)
