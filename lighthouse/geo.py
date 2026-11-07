"""Location text -> ISO country codes, without any API.

Handles the shapes ATSs actually emit: "San Francisco, CA", "US, CA, Santa Clara" (Workday),
"Remote - US", "New York City, NY; Seattle, WA", "London, UK", "Toronto, Ontario, Canada",
"Bengaluru, India". Returns an empty set when it can't tell; callers treat that as "keep".

Used for the Countries filter and, when you pick countries on the Profile page, to skip
storing and processing postings located only elsewhere (the biggest saving on global employers).
"""

from __future__ import annotations

import gzip
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

US_STATES = {
    "AL": "alabama", "AK": "alaska", "AZ": "arizona", "AR": "arkansas", "CA": "california", "CO": "colorado",
    "CT": "connecticut", "DE": "delaware", "FL": "florida", "GA": "georgia", "HI": "hawaii", "ID": "idaho",
    "IL": "illinois", "IN": "indiana", "IA": "iowa", "KS": "kansas", "KY": "kentucky", "LA": "louisiana",
    "ME": "maine", "MD": "maryland", "MA": "massachusetts", "MI": "michigan", "MN": "minnesota",
    "MS": "mississippi", "MO": "missouri", "MT": "montana", "NE": "nebraska", "NV": "nevada",
    "NH": "new hampshire", "NJ": "new jersey", "NM": "new mexico", "NY": "new york", "NC": "north carolina",
    "ND": "north dakota", "OH": "ohio", "OK": "oklahoma", "OR": "oregon", "PA": "pennsylvania",
    "RI": "rhode island", "SC": "south carolina", "SD": "south dakota", "TN": "tennessee", "TX": "texas",
    "UT": "utah", "VT": "vermont", "VA": "virginia", "WA": "washington", "WV": "west virginia",
    "WI": "wisconsin", "WY": "wyoming", "DC": "district of columbia", "PR": "puerto rico",
}
CA_PROVINCES = {"ON": "ontario", "QC": "quebec", "BC": "british columbia", "AB": "alberta", "MB": "manitoba",
                "SK": "saskatchewan", "NS": "nova scotia", "NB": "new brunswick", "NL": "newfoundland",
                "PE": "prince edward island", "YT": "yukon", "NT": "northwest territories", "NU": "nunavut"}
# Mexican states (names that are also common US place names, e.g. Hidalgo or Durango, are left out).
MX_STATES = ("aguascalientes", "baja california", "baja california sur", "campeche", "chiapas", "chihuahua",
             "coahuila", "colima", "guanajuato", "jalisco", "michoacan", "michoacán", "nuevo leon", "nuevo león",
             "oaxaca", "puebla", "queretaro", "querétaro", "quintana roo", "san luis potosi", "san luis potosí",
             "sinaloa", "sonora", "tabasco", "tamaulipas", "tlaxcala", "veracruz", "yucatan", "yucatán", "zacatecas",
             "ciudad de mexico", "ciudad de méxico", "estado de mexico", "estado de méxico")

COUNTRIES = {
    "US": ("united states", "united states of america", "usa", "u.s.", "u.s.a.", "us", "america"),
    "CA": ("canada",),
    "GB": ("united kingdom", "uk", "u.k.", "england", "scotland", "wales", "great britain", "britain", "northern ireland"),
    "IE": ("ireland",), "DE": ("germany", "deutschland"), "FR": ("france",), "NL": ("netherlands", "the netherlands", "holland"),
    "ES": ("spain",), "PT": ("portugal",), "IT": ("italy",), "CH": ("switzerland",), "AT": ("austria",),
    "BE": ("belgium",), "SE": ("sweden",), "NO": ("norway",), "DK": ("denmark",), "FI": ("finland",),
    "PL": ("poland",), "CZ": ("czech republic", "czechia"), "RO": ("romania",), "HU": ("hungary",),
    "GR": ("greece",), "TR": ("turkey", "türkiye"), "IL": ("israel",), "AE": ("united arab emirates", "uae"),
    "SA": ("saudi arabia",), "IN": ("india",), "SG": ("singapore",), "JP": ("japan",), "KR": ("south korea", "korea"),
    "CN": ("china",), "HK": ("hong kong",), "TW": ("taiwan",), "AU": ("australia",), "NZ": ("new zealand",),
    "BR": ("brazil", "brasil"), "MX": ("mexico", "méxico"), "AR": ("argentina",), "CO": ("colombia",),
    "CL": ("chile",), "PH": ("philippines",), "MY": ("malaysia",), "ID": ("indonesia",), "VN": ("vietnam",),
    "TH": ("thailand",), "ZA": ("south africa",), "NG": ("nigeria",), "KE": ("kenya",), "EG": ("egypt",),
    "PK": ("pakistan",), "UA": ("ukraine",), "LT": ("lithuania",), "LV": ("latvia",), "EE": ("estonia",),
    "BG": ("bulgaria",), "RS": ("serbia",), "HR": ("croatia",), "LU": ("luxembourg",), "CR": ("costa rica",),
}
# Cities that often appear without a country.
CITIES = {
    "US": ("new york", "nyc", "san francisco", "bay area", "seattle", "austin", "boston", "chicago", "los angeles",
           "denver", "atlanta", "dallas", "houston", "miami", "washington dc", "washington, d.c.", "palo alto",
           "mountain view", "menlo park", "sunnyvale", "san jose", "santa clara", "redmond", "cambridge, ma",
           "pittsburgh", "philadelphia", "phoenix", "san diego", "salt lake city", "minneapolis", "detroit",
           "nashville", "raleigh", "charlotte", "portland", "st. louis", "columbus", "arlington", "reston",
           "huntsville", "el segundo", "hawthorne", "brooklyn", "manhattan", "silicon valley"),
    "GB": ("london", "manchester", "edinburgh", "cambridge, uk", "oxford", "bristol", "belfast", "glasgow", "leeds"),
    "CA": ("toronto", "vancouver", "montreal", "montréal", "ottawa", "calgary", "waterloo", "edmonton"),
    "IE": ("dublin", "cork"), "DE": ("berlin", "munich", "münchen", "hamburg", "frankfurt", "stuttgart", "cologne"),
    "FR": ("paris", "lyon"), "NL": ("amsterdam", "rotterdam", "eindhoven"), "ES": ("madrid", "barcelona"),
    "PT": ("lisbon", "porto"), "IT": ("milan", "rome"), "CH": ("zurich", "zürich", "geneva"),
    "SE": ("stockholm",), "DK": ("copenhagen",), "PL": ("warsaw", "krakow", "kraków"), "RO": ("bucharest",),
    "CZ": ("prague",), "AT": ("vienna",), "IL": ("tel aviv",), "AE": ("dubai", "abu dhabi"),
    "IN": ("bangalore", "bengaluru", "hyderabad", "mumbai", "pune", "chennai", "gurgaon", "gurugram", "noida",
           "new delhi", "delhi"),
    "SG": ("singapore",), "JP": ("tokyo", "osaka"), "KR": ("seoul",), "CN": ("shanghai", "beijing", "shenzhen"),
    "AU": ("sydney", "melbourne", "brisbane", "perth"), "BR": ("são paulo", "sao paulo", "rio de janeiro"),
    "MX": ("mexico city", "guadalajara", "monterrey", "querétaro", "queretaro", "tijuana", "ciudad juárez",
           "ciudad juarez", "juárez", "saltillo", "zapopan", "apodaca", "mérida", "león, gto"),
    "AR": ("buenos aires",), "CO": ("bogotá", "bogota", "medellín"),
    "TW": ("taipei",), "HK": ("hong kong",), "PH": ("manila",), "TH": ("bangkok",), "NZ": ("auckland", "wellington"),
    "BE": ("brussels",), "FI": ("helsinki",), "NO": ("oslo",), "HU": ("budapest",), "GR": ("athens",),
}
_ABBREV = {"sf": "US", "nyc": "US", "ny": "US", "dc": "US", "cdmx": "MX", "ldn": "GB"}

_NAME_TO_CODE: dict[str, str] = {}
for code, names in COUNTRIES.items():
    for n in names:
        _NAME_TO_CODE[n] = code
_CITY_TO_CODE = {c: code for code, cities in CITIES.items() for c in cities}
_STATE_NAMES = {v: k for k, v in US_STATES.items()}
_PROV_NAMES = set(CA_PROVINCES.values())
_SPLIT = re.compile(r"\s*(?:;|\||/|\band\b|\n| or )\s*", re.I)
_MX_STATES = set(MX_STATES)
# Whole-word names inside free text ("Tampa Florida United States", "Mississauga Ontario Canada"),
# longest first so "new mexico" wins over "mexico".
_WORD_NAMES = sorted({**{n: c for n, c in _NAME_TO_CODE.items() if len(n) > 3 and "." not in n},
                      **{n: "US" for n in _STATE_NAMES}, **{n: "CA" for n in _PROV_NAMES},
                      **{n: "MX" for n in MX_STATES}}.items(), key=lambda kv: -len(kv[0]))
_WORD_RE = re.compile(r"\b(" + "|".join(re.escape(n) for n, _ in _WORD_NAMES) + r")\b", re.I)
_WORD_CODE = dict(_WORD_NAMES)
_STATE_ZIP = re.compile(r"\b([A-Z]{2})\s*,?\s+\d{5}(?:-\d{4})?\b")      # "Erie PA, 16502" / "TX 75001"
_CA_POSTAL = re.compile(r"\b[ABCEGHJ-NPRSTVXY]\d[ABCEGHJ-NPRSTV-Z]\s?\d[ABCEGHJ-NPRSTV-Z]\d\b")
_PREFIX = re.compile(r"^\s*([A-Z]{2})\s*[-–]\s*([A-Z]{2,3})?")            # Workday "CA-QC-Longueuil", "GB - Luton"
_ISO_CODES = set(COUNTRIES)
_CITIES_FILE = Path(__file__).with_name("data") / "cities.tsv.gz"


@lru_cache(maxsize=1)
def _gazetteer() -> dict[str, str]:
    """City name -> country for 77k cities of 5,000+ people (GeoNames, CC BY 4.0), loaded on first
    use. Only names one country clearly owns; never a foreign country for a name a North American
    city also has."""
    try:
        with gzip.open(_CITIES_FILE, "rt", encoding="utf-8") as f:
            return dict(line.rstrip("\n").split("\t", 1) for line in f if line[:1] != "#" and "\t" in line)
    except OSError:
        return {}


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return " ".join(s.lower().replace("-", " ").replace("'", "").split())
_TOKEN = re.compile(r"[a-zà-ÿ.']+(?:\s+[a-zà-ÿ.']+)*", re.I)


def _one(loc: str) -> set[str]:
    raw = loc.strip()
    low = raw.lower()
    if not low:
        return set()
    out: set[str] = set()
    # Workday style "US, CA, Santa Clara" / "USA - Remote" / "Remote - US"
    head = re.split(r"[\s,\-–()]+", raw)
    for tok in head:
        t = tok.strip(".").upper()
        if t in ("US", "USA"):
            out.add("US")
        elif t == "UK":
            out.add("GB")
    # Country-code prefix: "CA-QC-Longueuil", "GB - Luton", "MX-NLE-Apodaca". A code that is also a
    # US state (CA, IN, DE...) counts only when a province of that country follows.
    m = _PREFIX.match(raw)
    if m:
        cc, nxt = m.group(1), m.group(2) or ""
        if cc == "CA" and nxt in CA_PROVINCES:
            out.add("CA")
        elif cc in _ISO_CODES and cc not in US_STATES:
            out.add(cc)
    parts = [p.strip().lower() for p in re.split(r"[,\-–()~]", raw) if p.strip()]
    for p in parts:
        if p in _ABBREV:
            out.add(_ABBREV[p])
        elif p in _NAME_TO_CODE:
            out.add(_NAME_TO_CODE[p])
        elif p in _STATE_NAMES:
            out.add("US")
        elif p in _PROV_NAMES:
            out.add("CA")
        elif p in _MX_STATES:
            out.add("MX")
    city = {code for c, code in _CITY_TO_CODE.items() if c in low}
    # ", CA" after a city: a US state code unless the text names another country. A province code
    # that is also a Mexican state code (NL, BC) yields to a named Mexican city ("Monterrey, NL").
    for code in re.findall(r",\s*([A-Z]{2})\b", raw):
        if code in US_STATES and not (code == "CA" and "canada" in low):
            out.add("US")
        elif code in CA_PROVINCES and "MX" not in city and "mexico" not in low:
            out.add("CA")
    for code in _STATE_ZIP.findall(raw):
        if code in US_STATES:
            out.add("US")
    if _CA_POSTAL.search(raw) and ("canada" in low or any(f" {c}" in raw for c in CA_PROVINCES)):
        out.add("CA")
    if not out:
        for m in _WORD_RE.finditer(raw):       # names inside free text
            name = m.group(1).lower()
            if name == "mexico" and "new mexico" in low:
                continue
            out.add(_WORD_CODE[name])
    if not out and city:
        out.add(sorted(city)[0] if len(city) == 1 else next(iter(city)))
    if not out:                               # last resort: a known city name as one part
        gaz = _gazetteer()
        for p in [raw, *raw.split(","), *parts]:
            code = gaz.get(_norm(p))
            if code:
                out.add(code)
                break
    if {"US", "CA"} <= out and "canada" not in low:   # "Ontario, CA" is in California
        out.discard("CA")
    return out


@lru_cache(maxsize=65536)
def countries(text: str) -> frozenset[str]:
    """All country codes a location string mentions (several for multi-location postings)."""
    out: set[str] = set()
    for piece in _SPLIT.split(text or ""):
        out |= _one(piece)
    return frozenset(out)


def countries_of(locations: list[str]) -> frozenset[str]:
    out: set[str] = set()
    for l in locations:
        out |= countries(l)
    return frozenset(out)


def in_scope(locations: list[str], allowed: set[str] | frozenset[str]) -> bool:
    """Keep a posting unless every location is known and outside `allowed`. Unknown or
    country-less "Remote" postings are kept."""
    if not allowed:
        return True
    found = countries_of([l for l in locations if l])
    return not found or bool(found & set(allowed))


def encode(codes) -> str:
    """Stored as ',US,GB,' so SQL can match one code with LIKE '%,US,%'."""
    return "," + ",".join(sorted(codes)) + "," if codes else ""


CHOICES = [("US", "United States"), ("CA", "Canada"), ("GB", "United Kingdom"), ("IE", "Ireland"), ("DE", "Germany"),
           ("FR", "France"), ("NL", "Netherlands"), ("ES", "Spain"), ("CH", "Switzerland"), ("SE", "Sweden"),
           ("PL", "Poland"), ("IL", "Israel"), ("IN", "India"), ("SG", "Singapore"), ("JP", "Japan"),
           ("AU", "Australia"), ("BR", "Brazil"), ("MX", "Mexico")]
