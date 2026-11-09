"""Posting locations -> places people can filter by: country, state or province, metro area, city.

Careers systems write the same city a dozen ways ("New York, NY", "NY-New York", "New York City",
"US, NY, New York", "New York, New York, United States"). This reads them into one normalized place
per location, from a table of the 26k cities of the US, Canada and Mexico with 1,000+ people
(lighthouse/data/places.tsv.gz, GeoNames, built by scripts/make_places.py), so the website can offer
"Austin, TX", "Austin area", "Texas" and "United States" as choices and match every spelling.

Places elsewhere get only their country (lighthouse/geo.py). Nothing is guessed: a name that several
cities share ("Springfield", "Richmond") with nothing saying which one stays at the state or country
level the text gives, or is left out.
"""

from __future__ import annotations

import gzip
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from . import geo

VERSION = 1          # bump when parsing changes: the website then writes a new base with the new places
NA = ("US", "CA", "MX")
_FILE = Path(__file__).with_name("data") / "places.tsv.gz"

COUNTRY_NAMES = {"US": "United States", "CA": "Canada", "MX": "Mexico"}
REGION_NAMES = {
    "US": {code: ("District of Columbia" if code == "DC" else name.title()) for code, name in geo.US_STATES.items()},
    "CA": {"AB": "Alberta", "BC": "British Columbia", "MB": "Manitoba", "NB": "New Brunswick",
           "NL": "Newfoundland and Labrador", "NS": "Nova Scotia", "ON": "Ontario", "PE": "Prince Edward Island",
           "QC": "Quebec", "SK": "Saskatchewan", "YT": "Yukon", "NT": "Northwest Territories", "NU": "Nunavut"},
    "MX": {"AGU": "Aguascalientes", "BCN": "Baja California", "BCS": "Baja California Sur", "CAM": "Campeche",
           "CHP": "Chiapas", "CHH": "Chihuahua", "COA": "Coahuila", "COL": "Colima", "CMX": "Mexico City",
           "DUR": "Durango", "GUA": "Guanajuato", "GRO": "Guerrero", "HID": "Hidalgo", "JAL": "Jalisco",
           "MEX": "State of Mexico", "MIC": "Michoacán", "MOR": "Morelos", "NAY": "Nayarit", "NLE": "Nuevo León",
           "OAX": "Oaxaca", "PUE": "Puebla", "QUE": "Querétaro", "ROO": "Quintana Roo", "SLP": "San Luis Potosí",
           "SIN": "Sinaloa", "SON": "Sonora", "TAB": "Tabasco", "TAM": "Tamaulipas", "TLA": "Tlaxcala",
           "VER": "Veracruz", "YUC": "Yucatán", "ZAC": "Zacatecas"},
}
# Other ways postings name a region (normalized: see _norm).
_REGION_ALIASES = {
    "US": {"washington state": "WA", "d c": "DC"},
    "CA": {"newfoundland": "NL", "labrador": "NL", "pei": "PE", "nwt": "NT", "quebec province": "QC",
           "province of quebec": "QC"},
    "MX": {"estado de mexico": "MEX", "edo de mexico": "MEX", "edo mex": "MEX", "edomex": "MEX", "mexico state": "MEX",
           "coahuila de zaragoza": "COA",
           "veracruz de ignacio de la llave": "VER", "michoacan de ocampo": "MIC", "queretaro de arteaga": "QUE",
           "nuevo leon": "NLE", "san luis potosi": "SLP"},
}
# Mexican abbreviations ("Qro.", "N.L.", "Chih."): read only when something else says Mexico.
_MX_ABBREV = {"ags": "AGU", "bc": "BCN", "b c": "BCN", "bcs": "BCS", "camp": "CAM", "chis": "CHP", "chih": "CHH",
              "coah": "COA", "dgo": "DUR", "gto": "GUA", "gro": "GRO", "hgo": "HID", "jal": "JAL", "mich": "MIC",
              "mor": "MOR", "nay": "NAY", "nl": "NLE", "n l": "NLE", "oax": "OAX", "pue": "PUE", "qro": "QUE",
              "q roo": "ROO", "qroo": "ROO", "slp": "SLP", "sin": "SIN", "son": "SON", "tab": "TAB", "tamps": "TAM",
              "tlax": "TLA", "ver": "VER", "yuc": "YUC", "zac": "ZAC", "edomex": "MEX"}
_COUNTRY_WORDS = {
    "US": ("united states", "united states of america", "usa", "us", "u s", "u s a", "america", "estados unidos",
           "eeuu", "unitedstates"),
    "CA": ("canada", "can"),
    "MX": ("mexico", "mx", "mexique"),
}
# Words that are never a city on their own, and never part of a city found inside longer text.
_NOT_A_CITY = {
    "remote", "hybrid", "virtual", "home", "home based", "work from home", "wfh", "anywhere", "nationwide", "multiple",
    "multiple locations", "various", "various locations", "flexible", "field", "field based", "onsite", "on site",
    "in office", "office", "headquarters", "hq", "corporate", "campus", "main campus", "main site", "site", "plant",
    "store", "warehouse", "distribution", "distribution center", "center", "centre", "clinic", "hospital", "building",
    "remote first", "telecommute", "travel", "region", "area", "metro", "north", "south", "east", "west", "central",
    "northeast", "northwest", "southeast", "southwest", "midwest", "downtown", "midtown", "uptown", "global", "worldwide",
    "international", "other", "tbd", "n a", "na", "none", "location", "locations", "us remote", "remote us",
}
# Words that are also towns, but in longer free text with no state given are ordinary words
# ("Mobile Unit", "Orange Campus").
_COMMON_WORDS = {
    "mobile", "orange", "independence", "surprise", "normal", "commerce", "industry", "union", "liberty", "temple",
    "hope", "reading", "victoria", "deal", "sale", "march", "nice", "split", "bath", "aurora", "columbia", "lincoln",
    "jackson", "madison", "clinton", "franklin", "marion", "warren", "troy", "florence", "paris", "athens", "rome",
    "london", "dublin", "berlin", "manchester", "cambridge", "oxford", "kent", "bend", "moore", "lawrence", "salem",
    "auburn", "eagle", "golden", "university", "college", "university park", "mission", "harmony", "energy",
    "enterprise", "progress", "opportunity", "security", "freedom", "providence", "unity", "medical", "sun", "pharmacy",
    "service", "services", "health", "care", "life", "lab", "labs", "tech", "data", "sales", "retail", "support",
    "operations", "manufacturing", "logistics", "engineering", "research", "park", "valley", "lake", "river", "beach",
    "springs", "hills", "heights", "city", "town", "village", "county", "fort", "port", "mount", "saint", "san", "santa",
    "new", "old", "big", "little", "grand", "green", "white", "black", "red", "blue", "rock", "stone", "bay", "harbor",
}
_DIGITS = re.compile(r"[^\s\-,~]*\d[^\s\-,~]*")     # a word with a digit: zip code, store number, address
_STREET = {"st", "street", "ave", "avenue", "rd", "road", "blvd", "boulevard", "dr", "drive", "ln", "lane", "hwy",
           "highway", "pkwy", "parkway", "way", "ct", "court", "pl", "place", "cir", "ter", "trl", "pike", "sq"}
_TAIL = {"office", "offices", "site", "campus", "hq", "headquarters", "branch", "location", "facility", "remote",
         "hybrid", "onsite"}
_SPACED_DASH = re.compile(r"\s+[-–—]+\s+|[–—]+")
_PARENS = re.compile(r"[()\[\]{}>~]")
_SPLIT = re.compile(r"\s*(?:;|\||/|\n|\s+or\s+)\s*", re.I)
_TRAIL_CODE = re.compile(r"^(.*[a-z].*?)\s+([A-Z]{2})$")
_LEAD_CODE = re.compile(r"^([A-Z]{2})\s+([A-Za-z].*)$")
_AREA = re.compile(r"^(?:the )?(?:greater )?(.+?)(?: metro(?:politan)?(?: area)?| area| region| metroplex)$")


def _norm(s: str) -> str:
    """Lowercase ASCII words: accents, dots, apostrophes and hyphens removed; St/Ft/Mt spelled out."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"['’`]", "", s)
    s = re.sub(r"[^a-z0-9&]+", " ", s).strip()
    s = re.sub(r"^(st|ste|ft|mt)\b", lambda m: {"st": "saint", "ste": "sainte", "ft": "fort", "mt": "mount"}[m.group(1)], s)
    return s


@dataclass(frozen=True)
class Place:
    country: str
    region: str = ""     # TX, ON, NLE ("" when only the country is known)
    city: str = ""       # GeoNames name, e.g. "Austin", "Montréal"
    metro: str = ""      # metro area slug, e.g. "austin" (a city's, or the place is the metro area itself)

    def entry(self) -> str:
        """As the website's place list stores it: "US|TX|Austin|austin"."""
        return f"{self.country}|{self.region}|{self.city}|{self.metro}"

    def keys(self) -> set[str]:
        """Every choice that includes this place: country, region, metro area and city."""
        out = {self.country}
        if self.region:
            out.add(f"{self.country}-{self.region}")
        if self.metro:
            out.add(f"@{self.metro}")
        if self.city:
            out.add(f"{self.country}-{self.region}-{self.city}")
        return out


@dataclass(frozen=True)
class _City:
    name: str
    country: str
    region: str
    pop: int
    metro: str


@lru_cache(maxsize=1)
def _tables():
    """name -> cities; metro slug -> (label, country, region, aliases); metro alias -> slug;
    city key -> its other names."""
    names: dict[str, list[_City]] = {}
    metros: dict[str, tuple] = {}
    metro_alias: dict[str, str] = {}
    by_key: dict[tuple, _City] = {}
    aliases = []
    with gzip.open(_FILE, "rt", encoding="utf-8") as f:
        for line in f:
            if line[:1] == "#":
                continue
            r = line.rstrip("\n").split("\t")
            if r[0] == "C":
                c = _City(r[1], r[2], r[3], int(r[4]), r[7])
                names.setdefault(_norm(c.name), []).append(c)
                by_key[(c.country, c.region, c.name)] = c
            elif r[0] == "M":
                al = [a for a in r[5].split(";") if a]
                metros[r[1]] = (r[2], r[3], r[4], al)
                for a in al + [r[2]]:
                    metro_alias[_norm(a)] = r[1]
            elif r[0] == "A":
                aliases.append(r[1:])
    nicknames: dict[str, list[str]] = {}
    for alias, cc, region, name in aliases:
        c = by_key[(cc, region, name)]
        nicknames.setdefault(f"{cc}-{region}-{name}", []).append(alias)
        lst = names.setdefault(_norm(alias), [])
        if c not in lst:
            lst.insert(0, c)
    # "Ciudad Apodaca" is "Apodaca" in postings, unless that region has another place by that name
    for (cc, region, name), c in list(by_key.items()):
        if cc == "MX" and name.startswith("Ciudad "):
            short = _norm(name[7:])
            lst = names.setdefault(short, [])
            if not any(x.region == region for x in lst):
                lst.append(c)
    # a city's own name ("NYC", "CDMX") means the city, not its metro area
    metro_alias = {a: slug for a, slug in metro_alias.items() if a not in names}
    return names, metros, metro_alias, nicknames


def metros() -> dict[str, tuple]:
    """slug -> (label, country, region, aliases)."""
    return _tables()[1]


def nicknames() -> dict[str, list[str]]:
    """City key ("US-NY-New York") -> other names postings use for it ("nyc", "brooklyn")."""
    return _tables()[3]


@lru_cache(maxsize=1)
def _region_words():
    """normalized region name -> {(country, code)}; and the same for country words."""
    regions: dict[str, set] = {}
    for cc, table in REGION_NAMES.items():
        for code, name in table.items():
            regions.setdefault(_norm(name), set()).add((cc, code))
        for name, code in _REGION_ALIASES[cc].items():
            regions.setdefault(name, set()).add((cc, code))
    regions.pop("mexico city", None)          # the city (and its region, through the city)
    countries = {w: cc for cc, ws in _COUNTRY_WORDS.items() for w in ws}
    inside = sorted([n for n in regions if len(n) > 3] + [w for w in countries if len(w) > 3], key=len, reverse=True)
    inside_re = re.compile(r"\b(" + "|".join(re.escape(n) for n in inside) + r")\b")
    return regions, countries, inside_re


class _Part:
    __slots__ = ("raw", "n", "regions", "inner", "countries", "city_ok", "mx_codes", "foreign")

    def __init__(self, raw: str):
        self.raw, self.n = raw, _norm(raw)
        self.regions: set = set()      # the part names a region: (country, code); ("CA", "*") = somewhere in Canada
        self.inner: set = set()        # regions named inside longer text ("Tampa Florida")
        self.countries: set = set()
        self.city_ok = True            # may name a city
        self.mx_codes: set = set()     # Mexican abbreviations, used only if something says Mexico
        self.foreign = ""              # the part is a country outside North America ("Brazil")


def _classify(raw_part: str) -> _Part:
    p = _Part(raw_part)
    words = p.n.split()
    while len(words) > 1 and words[-1] in _TAIL:
        words.pop()                              # "New York City Office", "Austin Campus"
    p.n = " ".join(words)
    regions, countries, _ = _region_words()
    code = raw_part.strip()
    if len(code) == 2 and code.isupper():
        if code in geo.US_STATES:
            p.regions.add(("US", code))
        if code in REGION_NAMES["CA"]:
            p.regions.add(("CA", code))
        if code == "CA":
            p.regions.add(("CA", "*"))           # Workday "CA-QC-Longueuil"; "Barrie, CA"
        if code in ("NL", "BC"):
            p.mx_codes.add(_MX_ABBREV[code.lower()])
        if code in ("US", "MX"):
            p.countries.add(code)
        p.city_ok = False
    elif code == "MEX":
        p.countries.add("MX")
        p.city_ok = False
    elif len(code) == 3 and code.isupper() and code in REGION_NAMES["MX"]:
        p.mx_codes.add(code)                     # "MX-NLE-Apodaca"
        p.city_ok = False
    if p.n in countries:
        p.countries.add(countries[p.n])
        p.city_ok = False
    elif geo._NAME_TO_CODE.get(p.n, "US") not in NA:
        p.foreign = geo._NAME_TO_CODE[p.n]
        p.city_ok = False
    elif p.n in regions:
        p.regions |= regions[p.n]
    elif p.n in _MX_ABBREV:
        p.mx_codes.add(_MX_ABBREV[p.n])
    if p.n in _NOT_A_CITY or not p.n or p.n.rsplit(" ", 1)[-1] in _STREET:
        p.city_ok = False                        # "Remote", "Main Site", "Alhambra St"
    return p


def _dominant(cands: list[_City], min_pop: int = 0) -> _City | None:
    """The city a bare name means: the only one, or one 3x bigger than any namesake."""
    if not cands:
        return None
    cands = sorted(set(cands), key=lambda c: -c.pop)
    top = cands[0]
    if top.pop < min_pop:
        return None
    if len(cands) == 1 or top.pop >= 3 * max(cands[1].pop, 1):
        return top
    return None


def _choose(cands: list[_City], regions: set, countries: set, ctx: set, min_pop: int = 0) -> _City | None:
    if not cands:
        return None
    if regions:
        hit = [c for c in cands if (c.country, c.region) in regions or (c.country, "*") in regions]
        return max(hit, key=lambda c: c.pop) if hit else None
    if countries:
        return _dominant([c for c in cands if c.country in countries], min_pop)
    if ctx:
        hit = [c for c in cands if (c.country, c.region) in ctx]
        if hit:
            return max(hit, key=lambda c: c.pop)
    return _dominant(cands, min_pop)


def _parts(piece: str) -> list[_Part]:
    """Comma, dash and ">" separated parts, with Workday codes ("TX-Dallas") and trailing state
    codes ("Peoria AZ") split off, and numbers (zip codes, store numbers, addresses) dropped."""
    s = _PARENS.sub(",", piece)
    s = _SPACED_DASH.sub(",", s)
    out: list[_Part] = []
    regions, countries, _ = _region_words()
    names = _tables()[0]
    for chunk in s.split(","):
        chunk = " ".join(_DIGITS.sub(" ", chunk).replace("_", " ").split())
        if not chunk:
            continue
        segs = [x.strip() for x in chunk.split("-") if x.strip()]
        if len(segs) > 1 and any((len(x) in (2, 3) and x.isupper()) or _norm(x) in regions or _norm(x) in countries
                                 for x in segs):
            pieces = segs                        # "TX-Dallas", "CA-QC-Longueuil", "United States-Utah-Roy"
        else:
            pieces = [chunk]                     # "Winston-Salem"
        for x in pieces:
            m = _TRAIL_CODE.match(x)
            if m and (m.group(2) in geo.US_STATES or m.group(2) in REGION_NAMES["CA"]):
                out += [_classify(m.group(1)), _classify(m.group(2))]       # "Peoria AZ", "Pittsburgh PA"
                continue
            m = _LEAD_CODE.match(x)
            if m and (m.group(1) in geo.US_STATES or m.group(1) in REGION_NAMES["CA"]) and _norm(m.group(2)) in names:
                out += [_classify(m.group(1)), _classify(m.group(2))]       # "ID Boise"
                continue
            out.append(_classify(x))
    return out


def _only_in(name: str, country: str) -> bool:
    cands = _tables()[0].get(name, ())
    return bool(cands) and all(c.country == country for c in cands)


def _groups(parts: list[_Part]) -> list[list[_Part]]:
    """"Houston, TX, Milford, UT": one group per city and its state, when the parts are only that."""
    groups, cur = [], []
    for p in parts:
        cur.append(p)
        if p.regions and len(cur) > 1 and not any(q.regions or q.countries for q in cur[:-1]):
            groups.append(cur)
            cur = []
    if cur or len(groups) < 2:
        return [parts]
    return groups


def _piece(piece: str, ctx: set) -> list[Place]:
    parts = _parts(piece)
    if not parts:
        return []
    out: list[Place] = []
    for group in _groups(parts):
        out += _resolve(group, piece, ctx)
    return out


def _resolve(parts: list[_Part], piece: str, ctx: set) -> list[Place]:
    names, metro_table, metro_alias, _ = _tables()
    regions_words, country_words, inside_re = _region_words()
    # Region and country names inside longer text ("Tampa Florida United States"), unless the whole
    # text is a city ("New York City", "Kansas City").
    for p in parts:
        if p.city_ok and not p.regions and " " in p.n and p.n not in names and p.n not in metro_alias:
            found = inside_re.findall(p.n)
            if found:
                for w in found:
                    if w in country_words:
                        p.countries.add(country_words[w])
                    else:
                        p.inner |= regions_words[w]
                p.n = " ".join(inside_re.sub(" ", p.n).split())
                p.city_ok = bool(p.n) and p.n not in _NOT_A_CITY
                if not p.n:                      # "New York New York United States": the city of that name
                    p.n = next((w for w in found if w in names and w not in country_words), "")
                    p.city_ok = bool(p.n)
    countries = set().union(*(p.countries for p in parts))
    foreign = {p.foreign for p in parts if p.foreign}
    if foreign and not countries:
        return [Place(cc) for cc in sorted(foreign)]     # "Campo Largo, PR, Brazil"
    mexico = "MX" in countries or any(r[0] == "MX" for p in parts for r in p.regions | p.inner) \
        or any(p.city_ok and _only_in(p.n, "MX") for p in parts)
    if mexico:
        for p in parts:
            p.regions |= {("MX", c) for c in p.mx_codes}
    if "US" in countries and "CA" not in countries:
        for p in parts:                          # "US, CA, Santa Clara": California, not Canada
            p.regions.discard(("CA", "*"))
    if countries:
        for p in parts:                          # "Monterrey, NL, Mexico": Nuevo León, not Newfoundland
            if any(r[0] in countries for r in p.regions):
                p.regions = {r for r in p.regions if r[0] in countries}
    all_regions = set().union(*(p.regions | p.inner for p in parts))

    def around(i: int) -> set:
        return parts[i].inner.union(*(q.regions | q.inner for j, q in enumerate(parts) if j != i))

    # 1. a metro area by name: "Greater Seattle Area", "Bay Area", "GTA"
    for i, p in enumerate(parts):
        slug = metro_alias.get(p.n)
        if not slug:
            m = _AREA.match(p.n)
            if m and m.group(1) != p.n:
                core = _choose(names.get(m.group(1), []), around(i), countries, ctx)
                slug = core.metro if core else ""
        if slug:
            _, cc, region, _ = metro_table[slug]
            return [Place(cc, region, "", slug)]
    # 2. a city, in order ("Austin, TX"; "Toronto, Ontario, Canada"; "Ontario, CA" is the city in California)
    for i, p in enumerate(parts):
        if not p.city_ok:
            continue
        near = around(i)
        if p.regions and not near and p.n != "new york":
            continue                             # a region name alone is the region: "Ontario", "Texas, US"
        c = _choose(names.get(p.n) or names.get("the " + p.n, []), near, countries, ctx)   # "Woodlands": The Woodlands
        if c:
            return [Place(c.country, c.region, c.name, c.metro)]
    # 3. a city inside longer text: "Cleveland Clinic Main Campus", "Store Peoria"
    for i, p in enumerate(parts):
        words = p.n.split()
        if not p.city_ok or len(words) < 2:
            continue
        near = around(i)
        for size in range(min(4, len(words) - 1), 0, -1):
            for k in range(len(words) - size + 1):
                gram = " ".join(words[k:k + size])
                if gram not in names or gram in _NOT_A_CITY or (gram in _COMMON_WORDS and not near):
                    continue
                c = _choose(names[gram], near, countries, ctx, min_pop=0 if near else 20000)
                if c:
                    return [Place(c.country, c.region, c.name, c.metro)]
    # 4. a city abroad whose letters looked like a state code: "Bangalore, KA, IN", "Marktoberdorf, DE"
    abroad = geo._gazetteer()
    for p in parts:
        cc = (geo._CITY_TO_CODE.get(p.n) or abroad.get(p.n)) if p.city_ok and not p.regions else None
        if cc and cc not in NA:
            return [Place(cc)]
    # 5. the region, then the country
    exact = {r for r in all_regions if r[1] != "*"}
    if countries and len(exact) > 1:
        exact = {r for r in exact if r[0] in countries} or exact
    if exact == {("US", "WA")} and any(p.n == "washington" for p in parts) \
            and not any(p.raw.strip() == "WA" or "state" in p.n for p in parts):
        return [Place("US")]                     # "Washington" alone: the state or the capital
    if len(exact) == 1:
        cc, code = next(iter(exact))
        return [Place(cc, code)]
    if exact and len({r[0] for r in exact}) == 1:
        countries = countries | {next(iter(exact))[0]}
    if countries:
        return [Place(cc) for cc in sorted(countries)]
    return [Place(cc) for cc in sorted(geo.countries(piece))]


@lru_cache(maxsize=200_000)
def parse(text: str) -> tuple[Place, ...]:
    """The places a location string names, in its order, without repeats."""
    if not text or not text.strip():
        return ()
    found = geo.countries(text)
    if found and not found & set(NA):
        return tuple(Place(cc) for cc in sorted(found))          # only abroad: "London, UK", "Bengaluru"
    pieces = [x for x in _SPLIT.split(text) if x and x.strip()]
    first = [_piece(x, set()) for x in pieces]
    ctx = {(p.country, p.region) for ps in first for p in ps if p.region}
    out: list[Place] = []
    for x, ps in zip(pieces, first):
        if len(pieces) > 1 and (not ps or not ps[0].city) and ctx:
            ps = _piece(x, ctx) or ps            # "Charlottesville/Harrisonburg, VA": the region next door
        for p in ps:
            if p not in out:
                out.append(p)
    # a country is redundant next to a place in it
    return tuple(p for p in out if p.region or p.metro or not any(q.country == p.country and (q.region or q.metro) for q in out))


def keys_of(locations) -> set[str]:
    """Every place choice that includes any of these location strings."""
    out: set[str] = set()
    for loc in locations:
        for p in parse(loc):
            out |= p.keys()
    return out
