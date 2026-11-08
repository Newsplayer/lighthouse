"""Build lighthouse/data/places.tsv.gz: the cities of the US, Canada and Mexico with their state or
province, population, coordinates and metro area, for normalizing posting locations
(lighthouse/places.py). Data: GeoNames (geonames.org), CC BY 4.0.

    curl -O https://download.geonames.org/export/dump/cities1000.zip     (unzip it)
    curl -O https://download.geonames.org/export/dump/admin1CodesASCII.txt
    python scripts/make_places.py cities1000.txt admin1CodesASCII.txt lighthouse/data/places.tsv.gz

Rows (tab-separated):
    M  slug  label  country  region  aliases (";"-separated)     a metro area
    C  name  country  region  population  lat  lon  metro        a city of 1,000+ people
    A  alias  country  region  city                              another name for a city

Regions are postal codes in the US and Canada (TX, ON) and ISO 3166-2 codes in Mexico (NLE, CMX).
A city is in a metro area when it is within the area's radius of one of its anchor cities (the
nearest, relative to radius, when several areas reach it). Neighbourhoods (PPLX) are left out;
New York City's boroughs become the city itself, and it is called "New York" (its usual name in postings).
"""

import gzip
import io
import math
import sys

CA_REGIONS = {"01": "AB", "02": "BC", "03": "MB", "04": "NB", "05": "NL", "07": "NS", "08": "ON", "09": "PE",
              "10": "QC", "11": "SK", "12": "YT", "13": "NT", "14": "NU"}
MX_REGIONS = {"01": "AGU", "02": "BCN", "03": "BCS", "04": "CAM", "05": "CHP", "06": "CHH", "07": "COA", "08": "COL",
              "09": "CMX", "10": "DUR", "11": "GUA", "12": "GRO", "13": "HID", "14": "JAL", "15": "MEX", "16": "MIC",
              "17": "MOR", "18": "NAY", "19": "NLE", "20": "OAX", "21": "PUE", "22": "QUE", "23": "ROO", "24": "SLP",
              "25": "SIN", "26": "SON", "27": "TAB", "28": "TAM", "29": "TLA", "30": "VER", "31": "YUC", "32": "ZAC"}

# slug, label, aliases, [(country, region, anchor city as GeoNames names it, radius km)]
METROS = [
    ("nyc", "New York City area", "nyc;new york metro;greater new york;tri-state area;new york metropolitan area",
     [("US", "NY", "New York", 55)]),
    ("la", "Los Angeles area", "greater los angeles;los angeles metro;southern california;socal;orange county",
     [("US", "CA", "Los Angeles", 50), ("US", "CA", "Irvine", 20)]),
    ("chicago", "Chicago area", "chicagoland;greater chicago", [("US", "IL", "Chicago", 55)]),
    ("dfw", "Dallas–Fort Worth area", "dfw;dallas fort worth;dallas/fort worth;metroplex",
     [("US", "TX", "Dallas", 40), ("US", "TX", "Fort Worth", 30)]),
    ("houston", "Houston area", "greater houston", [("US", "TX", "Houston", 50)]),
    ("dc", "Washington, D.C. area", "dmv;washington dc metro;washington metropolitan area;northern virginia;nova",
     [("US", "DC", "Washington", 40)]),
    ("philadelphia", "Philadelphia area", "greater philadelphia;philly", [("US", "PA", "Philadelphia", 40)]),
    ("miami", "South Florida (Miami area)", "south florida;greater miami",
     [("US", "FL", "Miami", 30), ("US", "FL", "Fort Lauderdale", 25), ("US", "FL", "West Palm Beach", 25)]),
    ("atlanta", "Atlanta area", "greater atlanta;metro atlanta", [("US", "GA", "Atlanta", 50)]),
    ("boston", "Boston area", "greater boston", [("US", "MA", "Boston", 45)]),
    ("phoenix", "Phoenix area", "greater phoenix;valley of the sun", [("US", "AZ", "Phoenix", 45)]),
    ("sf-bay", "San Francisco Bay Area", "bay area;sf bay area;silicon valley;san francisco bay area;south bay;east bay",
     [("US", "CA", "San Francisco", 30), ("US", "CA", "Oakland", 25), ("US", "CA", "San Jose", 30)]),
    ("inland-empire", "Inland Empire (Riverside–San Bernardino)", "inland empire",
     [("US", "CA", "Riverside", 25), ("US", "CA", "San Bernardino", 20)]),
    ("detroit", "Detroit area", "metro detroit;greater detroit", [("US", "MI", "Detroit", 45)]),
    ("seattle", "Seattle area", "greater seattle;greater seattle area;puget sound",
     [("US", "WA", "Seattle", 40), ("US", "WA", "Tacoma", 20)]),
    ("twin-cities", "Minneapolis–St. Paul area", "twin cities;minneapolis st paul;msp",
     [("US", "MN", "Minneapolis", 35), ("US", "MN", "Saint Paul", 25)]),
    ("san-diego", "San Diego area", "greater san diego", [("US", "CA", "San Diego", 40)]),
    ("tampa", "Tampa Bay area", "tampa bay", [("US", "FL", "Tampa", 35), ("US", "FL", "St. Petersburg", 20)]),
    ("denver", "Denver area", "greater denver;denver metro", [("US", "CO", "Denver", 40), ("US", "CO", "Boulder", 15)]),
    ("st-louis", "St. Louis area", "greater st louis", [("US", "MO", "St. Louis", 40)]),
    ("baltimore", "Baltimore area", "greater baltimore", [("US", "MD", "Baltimore", 35)]),
    ("orlando", "Orlando area", "greater orlando;central florida", [("US", "FL", "Orlando", 35)]),
    ("charlotte", "Charlotte area", "greater charlotte", [("US", "NC", "Charlotte", 40)]),
    ("san-antonio", "San Antonio area", "greater san antonio", [("US", "TX", "San Antonio", 35)]),
    ("portland", "Portland area (Oregon)", "greater portland", [("US", "OR", "Portland", 35)]),
    ("sacramento", "Sacramento area", "greater sacramento", [("US", "CA", "Sacramento", 35)]),
    ("pittsburgh", "Pittsburgh area", "greater pittsburgh", [("US", "PA", "Pittsburgh", 35)]),
    ("austin", "Austin area", "greater austin;central texas", [("US", "TX", "Austin", 40)]),
    ("las-vegas", "Las Vegas area", "las vegas valley", [("US", "NV", "Las Vegas", 30)]),
    ("cincinnati", "Cincinnati area", "greater cincinnati", [("US", "OH", "Cincinnati", 35)]),
    ("kansas-city", "Kansas City area", "greater kansas city", [("US", "MO", "Kansas City", 35)]),
    ("columbus", "Columbus area (Ohio)", "central ohio", [("US", "OH", "Columbus", 35)]),
    ("indianapolis", "Indianapolis area", "greater indianapolis", [("US", "IN", "Indianapolis", 35)]),
    ("cleveland", "Cleveland area", "greater cleveland;northeast ohio", [("US", "OH", "Cleveland", 35)]),
    ("nashville", "Nashville area", "greater nashville;middle tennessee", [("US", "TN", "Nashville", 40)]),
    ("raleigh-durham", "Raleigh–Durham area", "research triangle;the triangle;triangle area;rtp;research triangle park",
     [("US", "NC", "Raleigh", 25), ("US", "NC", "Durham", 20)]),
    ("salt-lake", "Salt Lake City area", "wasatch front;silicon slopes;greater salt lake",
     [("US", "UT", "Salt Lake City", 40), ("US", "UT", "Provo", 25)]),
    ("milwaukee", "Milwaukee area", "greater milwaukee", [("US", "WI", "Milwaukee", 30)]),
    ("jacksonville", "Jacksonville area", "", [("US", "FL", "Jacksonville", 35)]),
    ("oklahoma-city", "Oklahoma City area", "", [("US", "OK", "Oklahoma City", 30)]),
    ("memphis", "Memphis area", "", [("US", "TN", "Memphis", 30)]),
    ("louisville", "Louisville area", "", [("US", "KY", "Louisville", 30)]),
    ("richmond", "Richmond area (Virginia)", "", [("US", "VA", "Richmond", 30)]),
    ("new-orleans", "New Orleans area", "greater new orleans", [("US", "LA", "New Orleans", 30)]),
    ("hartford", "Hartford area", "", [("US", "CT", "Hartford", 25)]),
    ("buffalo", "Buffalo area", "", [("US", "NY", "Buffalo", 25)]),
    ("birmingham", "Birmingham area (Alabama)", "", [("US", "AL", "Birmingham", 30)]),
    ("rochester-ny", "Rochester area (New York)", "", [("US", "NY", "Rochester", 25)]),
    ("tucson", "Tucson area", "", [("US", "AZ", "Tucson", 30)]),
    ("honolulu", "Honolulu area", "oahu", [("US", "HI", "Honolulu", 30)]),
    ("omaha", "Omaha area", "", [("US", "NE", "Omaha", 25)]),
    ("albuquerque", "Albuquerque area", "", [("US", "NM", "Albuquerque", 25)]),
    ("huntsville", "Huntsville area", "", [("US", "AL", "Huntsville", 25)]),
    ("boise", "Boise area", "treasure valley", [("US", "ID", "Boise", 25)]),
    ("madison", "Madison area", "", [("US", "WI", "Madison", 20)]),
    ("space-coast", "Space Coast (Melbourne–Titusville)", "space coast",
     [("US", "FL", "Melbourne", 20), ("US", "FL", "Titusville", 15)]),
    ("grand-rapids", "Grand Rapids area", "", [("US", "MI", "Grand Rapids", 25)]),
    ("tulsa", "Tulsa area", "", [("US", "OK", "Tulsa", 25)]),
    ("fresno", "Fresno area", "", [("US", "CA", "Fresno", 25)]),
    ("knoxville", "Knoxville area", "", [("US", "TN", "Knoxville", 25)]),
    ("greenville-sc", "Greenville area (South Carolina)", "upstate south carolina", [("US", "SC", "Greenville", 25)]),
    ("dayton", "Dayton area", "", [("US", "OH", "Dayton", 25)]),
    ("el-paso", "El Paso area", "", [("US", "TX", "El Paso", 25)]),
    ("charleston-sc", "Charleston area (South Carolina)", "lowcountry", [("US", "SC", "Charleston", 25)]),
    ("colorado-springs", "Colorado Springs area", "", [("US", "CO", "Colorado Springs", 20)]),
    ("spokane", "Spokane area", "", [("US", "WA", "Spokane", 20)]),
    ("des-moines", "Des Moines area", "", [("US", "IA", "Des Moines", 20)]),
    ("toronto", "Toronto area (GTA)", "gta;greater toronto area;greater toronto", [("CA", "ON", "Toronto", 40)]),
    ("montreal", "Montreal area", "greater montreal;grand montreal", [("CA", "QC", "Montréal", 30)]),
    ("vancouver", "Vancouver area", "metro vancouver;greater vancouver;lower mainland", [("CA", "BC", "Vancouver", 35)]),
    ("calgary", "Calgary area", "", [("CA", "AB", "Calgary", 25)]),
    ("edmonton", "Edmonton area", "", [("CA", "AB", "Edmonton", 25)]),
    ("ottawa", "Ottawa–Gatineau area", "ottawa gatineau", [("CA", "ON", "Ottawa", 25)]),
    ("quebec-city", "Quebec City area", "", [("CA", "QC", "Québec", 20)]),
    ("winnipeg", "Winnipeg area", "", [("CA", "MB", "Winnipeg", 20)]),
    ("halifax", "Halifax area", "", [("CA", "NS", "Halifax", 20)]),
    ("kitchener-waterloo", "Kitchener–Waterloo area", "waterloo region;kitchener waterloo;kw",
     [("CA", "ON", "Kitchener", 12), ("CA", "ON", "Waterloo", 10)]),
    ("hamilton-on", "Hamilton area (Ontario)", "", [("CA", "ON", "Hamilton", 15)]),
    ("london-on", "London area (Ontario)", "", [("CA", "ON", "London", 15)]),
    ("victoria-bc", "Victoria area (British Columbia)", "", [("CA", "BC", "Victoria", 15)]),
    ("cdmx", "Mexico City area", "cdmx;greater mexico city;valle de mexico;zona metropolitana del valle de mexico",
     [("MX", "CMX", "Mexico City", 40)]),
    ("monterrey", "Monterrey area", "mty;area metropolitana de monterrey;zona metropolitana de monterrey",
     [("MX", "NLE", "Monterrey", 25)]),
    ("guadalajara", "Guadalajara area", "gdl;zona metropolitana de guadalajara", [("MX", "JAL", "Guadalajara", 25)]),
    ("queretaro", "Querétaro area", "", [("MX", "QUE", "Santiago de Querétaro", 25)]),
    ("tijuana", "Tijuana area", "", [("MX", "BCN", "Tijuana", 20)]),
    ("juarez", "Ciudad Juárez area", "", [("MX", "CHH", "Ciudad Juárez", 20)]),
    ("puebla", "Puebla area", "", [("MX", "PUE", "Puebla", 20)]),
    ("leon", "León area", "", [("MX", "GUA", "León de los Aldama", 20)]),
    ("saltillo", "Saltillo area", "", [("MX", "COA", "Saltillo", 20)]),
    ("toluca", "Toluca area", "", [("MX", "MEX", "Toluca", 20)]),
    ("chihuahua", "Chihuahua area", "", [("MX", "CHH", "Chihuahua", 15)]),
    ("hermosillo", "Hermosillo area", "", [("MX", "SON", "Hermosillo", 15)]),
    ("merida", "Mérida area", "", [("MX", "YUC", "Mérida", 20)]),
    ("aguascalientes", "Aguascalientes area", "", [("MX", "AGU", "Aguascalientes", 15)]),
    ("san-luis-potosi", "San Luis Potosí area", "", [("MX", "SLP", "San Luis Potosí", 15)]),
    ("cancun", "Cancún area", "", [("MX", "ROO", "Cancún", 20)]),
    ("reynosa", "Reynosa area", "", [("MX", "TAM", "Reynosa", 15)]),
    ("mexicali", "Mexicali area", "", [("MX", "BCN", "Mexicali", 15)]),
]

NYC_BOROUGHS = ("Manhattan", "Brooklyn", "Queens", "The Bronx", "Bronx", "Staten Island")
# Other names postings use for a city: (alias, country, region, city).
ALIASES = [
    ("nyc", "US", "NY", "New York"), ("new york city", "US", "NY", "New York"), ("ny city", "US", "NY", "New York"),
    *[(b.lower(), "US", "NY", "New York") for b in NYC_BOROUGHS],
    ("sf", "US", "CA", "San Francisco"), ("washington dc", "US", "DC", "Washington"),
    ("washington d c", "US", "DC", "Washington"), ("district of columbia", "US", "DC", "Washington"),
    ("cdmx", "MX", "CMX", "Mexico City"), ("ciudad de mexico", "MX", "CMX", "Mexico City"),
    ("mexico df", "MX", "CMX", "Mexico City"), ("mexico d f", "MX", "CMX", "Mexico City"),
    ("distrito federal", "MX", "CMX", "Mexico City"), ("df", "MX", "CMX", "Mexico City"),
    ("quebec city", "CA", "QC", "Québec"), ("ville de quebec", "CA", "QC", "Québec"),
    ("queretaro", "MX", "QUE", "Santiago de Querétaro"), ("leon", "MX", "GUA", "León de los Aldama"),
    ("juarez", "MX", "CHH", "Ciudad Juárez"), ("mty", "MX", "NLE", "Monterrey"), ("gdl", "MX", "JAL", "Guadalajara"),
    ("san pedro", "MX", "NLE", "San Pedro Garza García"), ("garza garcia", "MX", "NLE", "San Pedro Garza García"),
    ("tlaquepaque", "MX", "JAL", "Tlaquepaque"), ("san pedro tlaquepaque", "MX", "JAL", "Tlaquepaque"),
]
# The name people use, where GeoNames' differs.
RENAME = {("US", "NY", "New York City"): "New York"}
# Populated places, without neighbourhoods (PPLX) or abandoned/historical ones.
KEEP_FEATURES = {"PPL", "PPLA", "PPLA2", "PPLA3", "PPLA4", "PPLC", "PPLG", "PPLS", "PPLL", "PPLF", "PPLQ"}


def km(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 12742 * math.asin(math.sqrt(h))


def main(cities_path, admin1_path, out):
    names = {}
    for line in open(admin1_path, encoding="utf-8"):
        code, name = line.split("\t")[:2]
        names[code] = name
    cities = []
    for line in open(cities_path, encoding="utf-8"):
        r = line.rstrip("\n").split("\t")
        cc, feature, a1 = r[8], r[7], r[10]
        if cc not in ("US", "CA", "MX") or feature not in KEEP_FEATURES:
            continue
        region = a1 if cc == "US" else (CA_REGIONS if cc == "CA" else MX_REGIONS).get(a1)
        if not region:
            continue
        if cc == "US" and region == "NY" and r[1] in NYC_BOROUGHS:
            continue                                     # aliases of New York City below
        name = RENAME.get((cc, region, r[1]), r[1])
        cities.append([name, cc, region, int(r[14] or 0), round(float(r[4]), 3), round(float(r[5]), 3), ""])
    by_key = {}
    for c in cities:
        by_key.setdefault((c[1], c[2], c[0]), []).append(c)
    # one row per (country, region, name): the largest
    cities = [max(v, key=lambda c: c[3]) for v in by_key.values()]
    index = {(c[1], c[2], c[0]): c for c in cities}
    anchors = []
    for slug, _, _, points in METROS:
        for cc, region, name, radius in points:
            c = index.get((cc, region, name))
            if not c:
                raise SystemExit(f"anchor not found: {name}, {region}, {cc}")
            anchors.append((slug, cc, (c[4], c[5]), radius))
    for c in cities:
        best = None
        for slug, cc, at, radius in anchors:
            if cc != c[1]:
                continue
            d = km((c[4], c[5]), at)
            if d <= radius and (best is None or d / radius < best[0]):
                best = (d / radius, slug)
        c[6] = best[1] if best else ""
    for alias, cc, region, name in ALIASES:
        if (cc, region, name) not in index:
            raise SystemExit(f"alias target not found: {name}, {region}, {cc}")
    cities.sort(key=lambda c: (c[1], c[2], c[0]))
    with open(out, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=9, mtime=0) as gz, \
            io.TextIOWrapper(gz, encoding="utf-8", newline="\n") as f:      # the same bytes every run
        f.write("# Cities of the US, Canada and Mexico (1,000+ people) with region, population, coordinates and metro\n"
                "# area. Data: GeoNames (geonames.org), CC BY 4.0. Built by scripts/make_places.py\n")
        for slug, label, aliases, points in METROS:
            f.write(f"M\t{slug}\t{label}\t{points[0][0]}\t{points[0][1]}\t{aliases}\n")
        for c in cities:
            f.write("C\t" + "\t".join(str(x) for x in c) + "\n")
        for alias, cc, region, name in ALIASES:
            f.write(f"A\t{alias}\t{cc}\t{region}\t{name}\n")
    in_metro = sum(1 for c in cities if c[6])
    print(len(cities), "cities,", in_metro, "in", len(METROS), "metro areas,", len(ALIASES), "aliases")


if __name__ == "__main__":
    main(*sys.argv[1:4])
