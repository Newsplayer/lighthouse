"""Free, rule-based labels for search filters: job function, seniority, employment type, skills.

All of it runs in the job-index Action (extract.derive), so readers just filter on stored columns.
Function and seniority come mostly from the title (so they work even for postings whose description
hasn't been fetched yet); skills come from the description text. Nothing is guessed: no match = None.
"""

from __future__ import annotations

import re

DERIVE_VERSION = "d1"   # bump when these rules change, so every row is re-labelled once

# ------------------------------------------------------------------ job function
# Checked in order; the first match wins ("Machine Learning Engineer" is ML, not Software).
FUNCTIONS: list[tuple[str, re.Pattern]] = [(n, re.compile(p, re.I)) for n, p in (
    ("Machine Learning & AI", r"\b(machine learning|ml|ai|artificial intelligence|deep learning|llm|nlp|computer vision|"
                              r"applied scientist|research scientist,? (ai|ml)|ml ?ops|generative ai|genai)\b"),
    ("Data & Analytics", r"\b(data (scientist|science|engineer|engineering|analyst|analytics|architect|platform|"
                         r"governance|steward|modeler|warehouse)|analytics|business intelligence|bi (developer|analyst|"
                         r"engineer)|statistician|quantitative analyst|quant(itative)? (researcher|developer|trader)|"
                         r"decision scientist|insights analyst|reporting analyst)\b"),
    ("Security", r"\b(security|cyber|infosec|soc analyst|penetration|appsec|threat|vulnerability|identity and access|iam)\b"),
    ("Design & UX", r"\b(designer|design (lead|manager|director|systems)|ux|ui/ux|user experience|user research(er)?|"
                    r"interaction design|visual design|graphic design|creative director|art director)\b"),
    ("Product Management", r"\b(product (manager|management|owner|lead|director|operations|marketing manager)|"
                           r"(group|senior|principal|staff|technical) product manager|head of product|vp,? product|"
                           r"chief product officer)\b"),
    ("Software Engineering", r"\b(software|developer|programmer|full[- ]?stack|front[- ]?end|back[- ]?end|"
                             r"(web|mobile|ios|android|platform|backend|frontend|cloud|devops|site reliability|sre|"
                             r"infrastructure|systems|application|java|python|\.net|embedded software|firmware|qa|test|"
                             r"automation|build|release|solutions?) engineer(ing)?|engineering manager|swe|sdet|devops|"
                             r"site reliability|member of technical staff|technical lead|tech lead)\b"),
    ("IT & Infrastructure", r"\b(it (support|specialist|manager|director|analyst|administrator|technician|operations)|"
                            r"help ?desk|service desk|desktop support|system administrator|sysadmin|network "
                            r"(engineer|administrator|technician)|database administrator|dba|erp|sap (consultant|"
                            r"analyst|developer)|salesforce (administrator|developer)|servicenow|technical support)\b"),
    ("Hardware & Electrical Engineering", r"\b(hardware|electrical engineer|electronics|asic|fpga|rtl|silicon|"
                                          r"semiconductor|chip|analog|rf engineer|power electronics|pcb|"
                                          r"verification engineer|design verification|physical design|layout engineer)\b"),
    ("Engineering (Mechanical, Civil, Manufacturing)", r"\b(mechanical|civil|structural|manufacturing engineer|process "
                                                       r"engineer|industrial engineer|chemical engineer|aerospace engineer|"
                                                       r"propulsion|materials engineer|reliability engineer|quality "
                                                       r"engineer|project engineer|design engineer|engineer)\b"),
    ("Science & Research", r"\b(scientist|research(er)? associate|postdoc|chemist|biologist|biostatistician|"
                           r"laboratory|lab (technician|manager)|clinical research|research and development|r&d|"
                           r"physicist|toxicologist|pharmacologist)\b"),
    ("Healthcare & Clinical", r"\b(nurse|rn|lpn|cna|physician|doctor|md|pharmacist|pharmacy|therapist|clinical|"
                              r"medical (assistant|director|technologist)|radiolog|sonograph|phlebotom|surgeon|"
                              r"dentist|dental|veterinar|paramedic|emt|caregiver|patient care|health coach|dietitian)\b"),
    ("Sales", r"\b(sales|account (executive|manager|director)|business development representative|bdr|sdr|"
              r"inside sales|territory manager|sales engineer|solutions consultant|presales|pre-sales|"
              r"relationship manager|client advisor|financial advisor|wealth advisor|broker)\b"),
    ("Business Development & Partnerships", r"\b(business development|partnerships?|alliances|channel manager|"
                                            r"corporate development|m&a)\b"),
    ("Marketing & Communications", r"\b(marketing|brand|communications|public relations|pr manager|social media|"
                                   r"content (strategist|marketing|manager)|seo|sem|growth|demand generation|"
                                   r"advertising|media (planner|buyer)|copywriter|events? (manager|coordinator))\b"),
    ("Customer Success & Support", r"\b(customer (success|support|service|experience|care)|client (success|services|"
                                   r"support)|support (specialist|engineer|representative|agent)|call center|"
                                   r"contact center|implementation (manager|consultant|specialist)|onboarding)\b"),
    ("Finance & Accounting", r"\b(finance|financial (analyst|planning|reporting|controller)|fp&a|accountant|"
                             r"accounting|controller|treasury|tax|audit(or)?|payroll|accounts (payable|receivable)|"
                             r"bookkeep|investment (banking|analyst|associate)|credit (analyst|risk)|underwriter|"
                             r"actuar|portfolio manager|trader|equity research|cfo|chief financial officer)\b"),
    ("Legal & Compliance", r"\b(legal|counsel|attorney|lawyer|paralegal|compliance|regulatory affairs|privacy|"
                           r"contracts? (manager|specialist|administrator)|general counsel|aml|kyc|risk (manager|"
                           r"analyst|management))\b"),
    ("People & Recruiting", r"\b(recruit|talent (acquisition|partner|sourcer)|sourcer|human resources|hr |hrbp|"
                            r"people (partner|operations|business partner)|benefits|compensation|learning and "
                            r"development|l&d|diversity|chro|chief people officer)\b"),
    ("Consulting & Strategy", r"\b(consultant|consulting|strategy|strategic|management analyst|transformation|"
                              r"advisory|business analyst|process improvement)\b"),
    ("Project & Program Management", r"\b(project manager|program manager|programme manager|pmo|project coordinator|"
                                     r"scrum master|agile coach|delivery manager|technical program manager|tpm)\b"),
    ("Operations & Supply Chain", r"\b(operations|supply chain|logistics|procurement|purchasing|buyer|sourcing|"
                                  r"inventory|warehouse|distribution|fulfillment|planner|planning|demand planning|"
                                  r"production (manager|supervisor|planner)|plant manager|facilities|fleet|"
                                  r"transportation|dispatcher)\b"),
    ("Skilled Trades & Technicians", r"\b(technician|mechanic|electrician|plumber|hvac|welder|machinist|carpenter|"
                                     r"maintenance|installer|operator|lineman|lineworker|tool and die|fabricator|"
                                     r"inspector|field service)\b"),
    ("Writing & Content", r"\b(writer|editor|journalist|reporter|technical writer|content (creator|producer|designer)|"
                          r"producer|video|photographer|animator)\b"),
    ("Education & Training", r"\b(teacher|instructor|trainer|training|tutor|professor|lecturer|educator|curriculum|"
                             r"instructional design)\b"),
    ("Administrative", r"\b(administrative|admin assistant|executive assistant|office (manager|coordinator|assistant)|"
                       r"receptionist|secretary|coordinator|clerk|data entry)\b"),
    ("Quality & Regulatory", r"\b(quality (assurance|control|specialist|manager)|qa/qc|qc analyst|validation|"
                             r"regulatory)\b"),
)]
FUNCTION_NAMES = [n for n, _ in FUNCTIONS]


def job_function(title: str, departments: str = "") -> str | None:
    t = f" {title or ''} "
    for name, pat in FUNCTIONS:
        if pat.search(t):
            return name
    d = departments or ""
    if d and d not in ("[]", "null"):
        for name, pat in FUNCTIONS:
            if pat.search(d):
                return name
    return None


# ------------------------------------------------------------------ seniority
SENIORITY = ["intern", "entry", "mid", "senior", "lead", "manager", "director", "executive"]
SENIORITY_LABEL = {"intern": "Intern / co-op", "entry": "Entry level", "mid": "Mid level", "senior": "Senior",
                   "lead": "Staff / principal / lead", "manager": "Manager", "director": "Director",
                   "executive": "VP / executive"}
_SEN = [(lvl, re.compile(p, re.I)) for lvl, p in (
    ("intern", r"\b(intern|internship|co-?op|apprentice(ship)?|summer (analyst|associate))\b"),
    ("executive", r"\b(chief|ceo|cfo|cto|coo|cio|ciso|cmo|chro|president|vice president|svp|evp|vp|partner)\b"),
    ("director", r"\b(director|head of)\b"),
    ("manager", r"\b(manager|supervisor|superintendent)\b"),
    ("lead", r"\b(principal|staff|lead|distinguished|fellow|architect)\b"),
    ("senior", r"\b(senior|sr\.?|iii|iv|level 3|l5)\b"),
    ("entry", r"\b(junior|jr\.?|entry[- ]level|associate|new grad(uate)?|graduate|early career|trainee|assistant|i)\b"),
    ("mid", r"\b(ii|level 2|mid[- ]level|intermediate)\b"),
)]


def seniority(title: str, yoe: int | None = None, yoe_source: str | None = None) -> str | None:
    t = title or ""
    for lvl, pat in _SEN:
        if pat.search(t):
            if lvl == "manager" and re.search(r"\b(account|product|project|program|case|property|territory)\s+manager\b",
                                               t, re.I) and not re.search(r"\b(senior|sr|lead)\b", t, re.I):
                return "mid"   # "Account Manager" is an individual role, not people management
            return lvl
    if yoe is not None and yoe_source == "stated":
        return "entry" if yoe <= 1 else "mid" if yoe <= 4 else "senior" if yoe <= 7 else "lead"
    return None


# ------------------------------------------------------------------ employment type
EMP_TYPES = ["full_time", "part_time", "contract", "internship", "temporary"]
EMP_LABEL = {"full_time": "Full-time", "part_time": "Part-time", "contract": "Contract",
             "internship": "Internship", "temporary": "Temporary / seasonal"}
_EMP_TEXT = [(k, re.compile(p, re.I)) for k, p in (
    ("internship", r"\b(internship|intern|co-?op)\b"),
    ("contract", r"\b(contract(or)?|freelance|1099|c2c|corp[- ]to[- ]corp|fixed[- ]term)\b"),
    ("temporary", r"\b(temporary|temp\b|seasonal)\b"),
    ("part_time", r"\bpart[- ]?time\b"),
    ("full_time", r"\b(full[- ]?time|permanent|regular)\b"),
)]


def employment_type(ats_value: str | None, title: str = "", text_hint: str | None = None) -> str | None:
    """ATS field first ("Full-time", "FULL_TIME", "Regular"...), then the title, then a phrase from the text."""
    for source in (ats_value, title):
        v = (source or "").replace("_", " ")
        for key, pat in _EMP_TEXT:
            if v and pat.search(v):
                if source is title and key == "full_time":
                    continue   # "Regular" in a title isn't an employment type
                return key
    return text_hint


_EMP_PHRASE = re.compile(r"\b(this is an? |position type:\s*|employment type:\s*|job type:\s*|schedule:\s*)"
                         r"(full[- ]?time|part[- ]?time|contract|temporary|seasonal|internship)", re.I)


def employment_from_text(text: str) -> str | None:
    m = _EMP_PHRASE.search(text or "")
    if not m:
        return None
    w = m.group(2).lower().replace(" ", "").replace("-", "")
    return {"fulltime": "full_time", "parttime": "part_time", "seasonal": "temporary"}.get(w, w)


# ------------------------------------------------------------------ skills
# canonical -> aliases (lowercase; multi-word allowed). Ambiguous words are matched only in the
# forms listed (e.g. "spring boot", not "spring"; "golang", not "go").
SKILLS: dict[str, tuple[str, ...]] = {
    "python": ("python",), "java": ("java",), "javascript": ("javascript", "js", "ecmascript"),
    "typescript": ("typescript",), "c++": ("c++", "cpp"), "c#": ("c#", "csharp"), "go": ("golang",),
    "rust": ("rust lang", "rustlang"), "ruby": ("ruby",), "php": ("php",), "scala": ("scala",),
    "kotlin": ("kotlin",), "swift": ("swiftui",), "objective-c": ("objective-c", "objective c"),
    "matlab": ("matlab",), "sql": ("sql", "t-sql", "pl/sql", "tsql"), "bash": ("bash", "shell scripting"),
    "perl": ("perl",), "haskell": ("haskell",), "elixir": ("elixir",), "dart": ("dart",), "julia": ("julia lang",),
    "sas": ("sas programming", "sas enterprise"), "vba": ("vba",), "solidity": ("solidity",),
    "verilog": ("verilog", "systemverilog"), "vhdl": ("vhdl",), "labview": ("labview",), "plc": ("plc", "plcs"),
    "react": ("react", "react.js", "reactjs"), "react native": ("react native",), "angular": ("angular", "angularjs"),
    "vue": ("vue", "vue.js", "vuejs"), "next.js": ("next.js", "nextjs"), "node.js": ("node.js", "nodejs", "node"),
    "django": ("django",), "flask": ("flask",), "fastapi": ("fastapi",), "spring": ("spring boot", "spring framework"),
    ".net": (".net", "dotnet", "asp.net"), "rails": ("ruby on rails", "rails"), "graphql": ("graphql",),
    "grpc": ("grpc",), "rest api": ("rest api", "restful", "rest apis"), "microservices": ("microservices",),
    "kafka": ("kafka",), "spark": ("spark", "pyspark", "apache spark"), "hadoop": ("hadoop",),
    "airflow": ("airflow",), "dbt": ("dbt",), "snowflake": ("snowflake",), "databricks": ("databricks",),
    "bigquery": ("bigquery",), "redshift": ("redshift",), "postgresql": ("postgresql", "postgres"),
    "mysql": ("mysql",), "sql server": ("sql server", "mssql"), "oracle database": ("oracle database", "oracle db"),
    "mongodb": ("mongodb", "mongo"), "redis": ("redis",), "elasticsearch": ("elasticsearch", "opensearch"),
    "cassandra": ("cassandra",), "dynamodb": ("dynamodb",), "tableau": ("tableau",), "power bi": ("power bi", "powerbi"),
    "looker": ("looker",), "excel": ("microsoft excel", "ms excel", "advanced excel", "excel spreadsheets"),
    "pandas": ("pandas",), "numpy": ("numpy",), "pytorch": ("pytorch",), "tensorflow": ("tensorflow",),
    "scikit-learn": ("scikit-learn", "sklearn"), "keras": ("keras",), "hugging face": ("hugging face", "huggingface"),
    "langchain": ("langchain",), "llms": ("llm", "llms", "large language models", "large language model"),
    "nlp": ("nlp", "natural language processing"), "computer vision": ("computer vision",),
    "machine learning": ("machine learning",), "deep learning": ("deep learning",), "statistics": ("statistics",),
    "aws": ("aws", "amazon web services"), "azure": ("azure",), "gcp": ("gcp", "google cloud"),
    "kubernetes": ("kubernetes", "k8s"), "docker": ("docker",), "terraform": ("terraform",), "ansible": ("ansible",),
    "jenkins": ("jenkins",), "ci/cd": ("ci/cd", "cicd", "continuous integration"), "github actions": ("github actions",),
    "linux": ("linux", "unix"), "git": ("git",), "salesforce": ("salesforce",), "sap": ("sap",),
    "workday": ("workday hcm", "workday financials"), "servicenow": ("servicenow",), "jira": ("jira",),
    "figma": ("figma",), "sketch": ("sketch app",), "adobe creative suite": ("adobe creative suite", "adobe creative cloud"),
    "photoshop": ("photoshop",), "illustrator": ("adobe illustrator",), "autocad": ("autocad",),
    "solidworks": ("solidworks",), "catia": ("catia",), "ansys": ("ansys",), "revit": ("revit",),
    "gaap": ("gaap", "us gaap"), "ifrs": ("ifrs",), "cpa": ("cpa",), "cfa": ("cfa",),
    "financial modeling": ("financial modeling", "financial modelling"), "quickbooks": ("quickbooks",),
    "netsuite": ("netsuite",), "hyperion": ("hyperion",), "bloomberg terminal": ("bloomberg terminal",),
    "six sigma": ("six sigma", "lean six sigma"), "pmp": ("pmp",), "agile": ("agile", "scrum"),
    "hipaa": ("hipaa",), "epic ehr": ("epic ehr", "epic systems"), "bls": ("bls certification",),
    "acls": ("acls",), "gdpr": ("gdpr",), "soc 2": ("soc 2", "soc2"), "iso 27001": ("iso 27001",),
    "cissp": ("cissp",), "splunk": ("splunk",), "siem": ("siem",), "penetration testing": ("penetration testing",),
    "ios": ("ios development", "ios"), "android": ("android",), "unity": ("unity3d", "unity engine"),
    "unreal engine": ("unreal engine",), "cuda": ("cuda",), "embedded systems": ("embedded systems", "embedded c"),
    "rtos": ("rtos",), "fpga": ("fpga",), "pcb design": ("pcb design", "pcb layout"),
}
_TOKEN = re.compile(r"[a-z0-9.][a-z0-9+#.-]*[a-z0-9+#]|[a-z0-9]")


def _tokens(text: str) -> list[str]:
    return [t.rstrip(".") for t in _TOKEN.findall((text or "").lower()) if t.rstrip(".")]


_SKILL_INDEX: dict[tuple[str, ...], str] = {}
for _canon, _aliases in SKILLS.items():
    for _a in _aliases:
        _SKILL_INDEX[tuple(_tokens(_a))] = _canon
_MAX_N = max(len(k) for k in _SKILL_INDEX)
# A few short names that are also ordinary words are matched case-sensitively in the original text.
_CASED = [("go", re.compile(r"\bGo(?=\s*(?:,|/|\)|\s(?:and|or)\s|\s(?:programming|language|lang)\b))")),
          ("rust", re.compile(r"\bRust\b")), ("swift", re.compile(r"\bSwift\b(?!\s*(?:code|transfer|payment|network))")),
          ("r", re.compile(r"(?<![\w.])R(?=\s*[,/)]|\s(?:and|or)\s|\sprogramming|\sStudio)")),
          ("c", re.compile(r"(?<![\w.])C(?=\s*[,/]\s*C\+\+|\s(?:programming|language)\b)|\bANSI C\b|\bembedded C\b")),
          ("sas", re.compile(r"\bSAS\b(?!\s*\d)")), ("tableau", re.compile(r"\bTableau\b")),
          ("excel", re.compile(r"\bExcel\b(?!\s+(?:in|at|as|with|and grow)\b)"))]


def skills(text: str) -> list[str]:
    """Canonical skill names mentioned in the text, in a stable order."""
    toks = _tokens(text)
    found = set()
    for i in range(len(toks)):
        for n in range(1, _MAX_N + 1):
            key = tuple(toks[i:i + n])
            if len(key) < n:
                break
            hit = _SKILL_INDEX.get(key)
            if hit:
                found.add(hit)
    for canon, pat in _CASED:
        if canon not in found and pat.search(text or ""):
            found.add(canon)
    order = {k: i for i, k in enumerate(list(SKILLS) + ["r", "c"])}
    return sorted(found, key=lambda k: order.get(k, 999))


def encode_skills(items: list[str]) -> str | None:
    return ("," + ",".join(items) + ",") if items else None


def decode_skills(value: str | None) -> list[str]:
    return [x for x in (value or "").split(",") if x]
