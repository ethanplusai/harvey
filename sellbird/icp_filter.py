"""Who is worth emailing: the ICP filter that keeps the wrong companies out.

Sellbird discovers businesses by searching the web. A search for "manufacturing
company Pune" returns what Google ranks — and Google ranks blog posts,
directories, marketplaces and listicles alongside real factories. Worse, a
search for "plastic manufacturer" happily returns an ERP vendor's blog about
plastic manufacturing, and that vendor then enters the pipeline as though it
were a prospect.

So two things have to be true before a company earns a place in the list:

1. It is a company, not a page about companies. A URL that is a blog post,
   a news item or a directory listing is not a business.
2. It is the kind of company we sell to. An ERP vendor, a software house or
   a trading company does not buy a manufacturing ERP — it already builds
   one. Those are competitors or bystanders, not buyers.

Both checks live here so every path into the pipeline — discovery, the
scout's backfill, and the writer's selection — applies the same rule. A
filter in only one place is a filter that leaks.

Three facts about the data shape these rules.

**The domain is the identity.** A result titled "Hydraulic Cylinder
Manufacturers in Nagpur" on deltahydraulics.in is Delta Hydraulics, a
manufacturer with an SEO-heavy page title — throwing it away would lose a
good prospect. The same title on nagpuronline.in is a news portal's guide,
and the portal is not a prospect. So a list-shaped title is only evidence
against a company when the domain and the URL agree that it is a list.

**The path says what the result was.** A manufacturer's own site has
company pages (/about-us, /products) and content pages (/blog,
/news); a directory's result is a search or listing path. The path is
the evidence that tells the two apart, so it is read as carefully as the
name.

**The search phrase is not a classification.** For a company found through
a web search, the ``industry`` field is the *phrase that found it* — so
"trading company" means "Google ranked this for the trading-company
query", which is not the same as "this is a trading company" (CERA
Sanitaryware, a manufacturer, has a dealer-locator page and lands there).
A phrase is therefore only ever a hint, and only ever used together with
evidence from the company's own name, domain or description.
"""

import re

# ── 1. Words that say what a company does ──────────────────────────────
#
# Three vocabularies, and the whole filter turns on telling them apart.
# A company whose name carries a maker word makes something; one whose
# name carries a trading word moves goods; one whose name carries a
# software word sells a system. The overlap is small, and where it exists
# the direct evidence — the name itself — wins.

# Evidence that the company makes something.
MAKER_WORDS = (
    r"manufactur\w*|fabricat\w*|engineer\w*|polymer\w*|plastics?\b|"
    r"rubber\w*|hydraulic\w*|electrical\b|electronic\w*|"
    r"industrial\b|industr(?:y|ies)\b|enterprises?\b|machin\w*|tooling\b|tools?\b|"
    r"foundr\w*|casting\w*|forging\w*|forge\b|welding\b|mould\w*|mold\w*|"
    r"powder\b|metals?\b|steel\b|stainless\b|alumini\w*|copper\b|"
    r"zinc\b|granite\b|marble\b|ceramic\w*|sanitary\w*|tiles?\b|"
    r"glass\w*|cement\b|bricks?\b|chemical\w*|pharma\w*|drug\w*|"
    r"textile\w*|garment\w*|clothing\b|apparel\b|fashion\b|"
    r"food\w*|snacks?\b|beverage\w*|dairy\b|"
    r"spice\w*|paper\w*|board\b|printing\b|packag\w*|bottl\w*|"
    r"containers?\b|pipes?\b|valves?\b|bearings?\b|motors?\b|pumps?\b|"
    r"gears?\b|springs?\b|fasteners?\b|bolts?\b|wires?\b|cables?\b|"
    r"optical\w*|lenses?\b|compressors?\b|generators?\b|batter\w*|"
    r"switches?\b|lamps?\b|fans?\b|furniture\b|sofas?\b|upholst\w*|"
    r"building\w*|construction\b|roofing\b|paints?\b|coatings?\b|"
    r"adhesives?\b|gaskets?\b|filters?\b|kilns?\b|furnaces?\b|"
    r"embroid\w*|weav\w*|spinning\b|dyeing\b|tanning\b|sewing\b|"
    r"assembly\b|extruders?\b|sheet metal\b|laser\b|plasma\b|"
    r"rolling\b|milling\b|grinding\b|calibrat\w*|dies?\b|jigs?\b|"
    r"turbines?\b|washers?\b|dryers?\b|mixers?\b|blenders?\b|"
    r"ovens?\b|boilers?\b|elevators?\b|tyres?\b|tires?\b|tractors?\b|"
    r"weld\w*|autos?\b|automotiv\w*|vehicles?\b|trailers?\b|"
    r"vessels?\b|ships?\b|marine\b|aerospace\b|defen[cs]e\b|"
    r"poultr\w*|feed\b|seed\w*|fertili[sz]\w*|tea\b|coffee\b|"
    r"sugar\b|refiner\w*|petro\w*|stationer\w*|plywood\b|"
    r"veneer\b|laminate\w*|doors?\b|faucet\w*|brass\b|"
    r"iron\b|lead\b|nickel\b|titanium\b|stamping\b|press\b|"
    r"harness\b|connector\w*|appliances?\b|gearbox\w*|"
    r"screw\w*|hinge\b|seal\b|corrugated\b|laminating\b|bottling\b|"
    r"tetra\b|thermoform\w*|filtration\b|bakery\b|rice\b|flour\b|"
    r"brewery\b|distillery\b|winery\b|tobacco\b|cigarette\w*|"
    r"pencil\w*|mattress\w*|foam\b|quarry\w*|refractor\w*|"
    r"abrasiv\w*|surgical\b|medical\b|dental\b|diagnostic\b|"
    r"solar\b|wind\b|weighing\b|labell?ing\b|conveyor\b|carcass\b"
)
MAKER_RE = re.compile(r"\b(?:" + MAKER_WORDS + r")", re.I)

# Evidence that the company moves goods rather than making them. A trading
# house has no shopfloor to schedule. "Supplier", "dealer", "distributor"
# and "agent" all mean the same thing: a middleman.
#
# Two forms, because Indian company names run their words together:
# "Taj Trading Company" and "shyamtradingcompany.com" are the same kind
# of business written two ways.
MIDDLEMAN_STRICT_RE = re.compile(
    r"\b(?:trading|traders?|trade|wholesalers?|wholesale|distributors?|"
    r"distribution|dealers?|retailers?|retail|stockists?|brokers?|"
    r"commission\s+agents?|stores?|supermarkets?|marts?|bazaars?|malls?|"
    r"shops?|agencies|agency|agents?|logistics?|warehouses?|freight|"
    r"courier|transport|shipping|cargo|market)\b",
    re.I,
)
MIDDLEMAN_LOOSE_RE = re.compile(
    r"(?:trading|trader|trade|wholesal|distribut|supplier|stockist|warehous|"
    r"logistic|freight|courier|commodit|emporium|retail)",
    re.I,
)

# A company that only exports or imports is not necessarily a factory —
# but many real factories export, so these words alone never exclude a
# company whose name also says it makes something.
EXPORT_RE = re.compile(r"\b(?:export\w*|import\w*)\b", re.I)

# A page whose title is a dealer or supplier locator for the brand whose
# own site it is — "GRABO® Dealer in India". The company is the brand,
# and the page is one of its dealers.
DEALER_PAGE_RE = re.compile(
    r"\b(?:dealer|supplier|stockist|distributor)s?\s+(?:in|at)\b", re.I)

# Evidence that the company sells software, IT or services. These are
# competitors or bystanders, not buyers. Matched against the *name* only:
# a hosting subdomain ends in ".app" or ".netlify.app", and a real
# manufacturer can live on one.
VENDOR_WORDS = (
    r"software|softwares\b|erp\b|crm\b|saas\b|"
    r"portals?\b|apps?\b|apis?\b|hosting\b|domains?\b|"
    r"staffing\b|manpower\b|recruit\w*|payroll\b|"
    r"accounting\b|bookkeeping\b|fintech\b|wallets?\b|"
    r"credits?\b|loans?\b|lending\b|insurance\b|banking\b|"
    r"payments?\b|finances?\b|financial\b|ventures\b|"
    r"consultanc\w*|advisory\b|advisors?\b|"
    r"seo\b|cyber\s*(?:security|services|tech)|"
    r"web\s*(?:design|development|services|solutions|hosting|app)|"
    r"data\s*(?:science|analytics|engineering|services|solutions|"
    r"systems|processing|ware)|"
    r"digital\s*(?:marketing|solutions|platform|transformation)|"
    r"marketing\s*(?:agency|services|solutions)|"
    r"advertis\w*|media\b"
)
VENDOR_RE = re.compile(r"\b(?:" + VENDOR_WORDS + r")", re.I)

# Industries that mean "this company sells software" with no ambiguity.
# Only used for the query phrases that mean exactly that — the broader
# "trading company" and "Retail and Distribution" phrases are search
# terms, not classifications, and are never trusted on their own.
IT_INDUSTRY_RE = re.compile(
    r"^(?:software|erp|information technology|it services|it consultancy|"
    r"web development|saas|it support|it solutions)\b"
    # "Accounting Software", "Manufacturing Software": the head noun still
    # says what the company sells. It is the industry phrase itself, not a
    # search term, so it is safe to trust.
    r"|^\w[\w &/+.-]*\bsoftware\b",
    re.I,
)

# ── 2. Is this a company, or a page about companies? ────────────────────
#
# Discovery records the URL a result came from and a name derived from the
# search result's title. When the title is a list — "Manufacturing
# Companies in Nagpur" — the publisher of that list is not the business
# being listed, and it must not be treated as a prospect.

# Domains that belong to a publisher, a portal, a directory or an
# institution rather than to a business. Checked as a suffix, so
# "nashik.idbf.in" is caught by "idbf.in".
KNOWN_PUBLISHER_DOMAINS = (
    "idbf.in",
    "infoisinfo.co.in",
    "getmanufacturers.com",
    "getbusiness.co.in",
    "clothwala.in",
    "justdial.com",
    "sulekha.com",
    "indiamart.com",
    "tradeindia.com",
    "exportindia.com",
    "connect2india.com",
    "go4worldbusiness.com",
    "europages.co.uk",
    "kompass.com",
    "indiabiz.com",
    "thehindu.com",
    "indiatimes.com",
    "economictimes.indiatimes.com",
    "business-standard.com",
    "aninews.in",
    "indianretailer.com",
    "mtdcnc.com",
    "technavio.org",
    "shipbob.com",
    "builtin.com",
    "mapsofindia.com",
    "mydigisence.com",
    "revenuebase.ai",
    "fliarbi.com",
    "scanx.trade",
    "moglix.com",
    "businessdeals.in",
    "beststartup.in",
    "vocal.media",
    "infomediasearch.com",
    "textileinfomedia.com",
    "dcmsme.gov.in",
    "nagpuronline.in",
    "indoreonline.in",
    "gurugramonline.in",
    "lonionline.in",
    "jaipuronline.in",
    "pimprichinchwadonline.in",
    "electricalsinformed.com",
    "india-briefing.com",
    "india2west.com",
    "newprojectstracker.com",
    "wypages.com",
    "oswalgroup.com",
    "okcredit.in",
)

# Host labels that are a publisher's own name for itself. Matched as a
# whole label, so "blog.bigsauda.com" is a publisher but "bigsauda.com"
# is a company. ("hindustan" is not "hindu" — a substring match here
# would throw away half of the manufacturing belt.)
PUBLISHER_LABEL_WORDS = frozenset({
    "news", "blogs", "blog", "media", "magazine", "journal", "newsletter",
    "directory", "directories", "listings", "portal", "businesses",
    "business", "review", "reviews", "times", "express", "tribune",
    "post", "daily", "today", "standard", "updates", "update", "guide",
    "guides",
})

# Host labels that end in one of these are content farms: "nagpuronline",
# "electricalsinformed". Indian local guides are named this way.
CONTENT_FARM_LABEL_SUFFIXES = ("online", "informed")

# Host labels that name a directory rather than a business: "123coimbatore",
# "sdamanufacturer", "wspcrane" (a crane directory). A domain that says
# "manufacturers" or "suppliers" is a list of them, not one of them.
DIRECTORY_DOMAIN_WORDS = (
    "manufacturers", "manufactures", "suppliers", "traders", "companies",
    "directory", "directories", "listings", "marketplace", "yellowpages",
    "b2b",
)

# Top-level domains that are a publisher's own.
PUBLISHER_TLDS = frozenset({"media", "news", "directory", "today"})

# Colleges, universities, chambers, trade bodies: never a manufacturer.
INSTITUTION_TLD_RE = re.compile(r"\.(ac\.in|edu\.in|gov\.in|nic\.in)$", re.I)

INSTITUTION_NAME_RE = re.compile(
    r"\b(college|university|institute|polytechnic|school\s+of|academy|"
    r"foundation|association|chamber|federation|society|hospital|"
    r"museum|library|airport|port\s+trust|railway|ministry|department|"
    r"council|committee|trust|mission|temple|municipal\s+corporation)\b",
    re.I,
)

# Titles that are a list of companies, an article, or a page.
LISTICLE_NAME_RE = re.compile(
    r"^(?:the\s+)?\d+\s+best\b"
    r"|^#\d+\b"
    r"|^(?:top|best)\b"
    r"|^list\s+of\b"
    r"|^(?:major|key|leading)\s+companies\b"
    r"|\b(?:companies|industries|manufacturers?|suppliers?|vendors?|"
    r"factories|distributors?|dealers?|traders?|exporters?|importers?|"
    r"plants?|units?|clusters?|locations?|businesses|enterprises|"
    r"products?|services?)\s+(?:in|near|around|and\s+around|across|from)\b"
    r"|\b(?:economy|industry|market|overview|landscape|scenario|"
    r"ecosystem|directory|portal)\s+of\b"
    r"|\b(?:newspaper|magazine|journal|newsletter|blog|weekly|"
    r"fortnightly|quarterly)\b"
    r"|\bpage\s+\d+\b"
    r"|\b\d+\s+found\b"
    r"|\bdirectory\b"
    r"|\bguide\s+(?:to|for)\b"
    r"|\bhow\s+to\b"
    r"|\bwhat\s+is\b",
    re.I,
)

# Words in a title that describe a *category* rather than a brand. They
# are ignored when deciding whether a domain belongs to the company.
GENERIC_TITLE_WORDS = frozenset({
    "manufacturing", "manufacturer", "manufacturers", "company",
    "companies", "industry", "industries", "industrial", "supplier",
    "suppliers", "vendor", "vendors", "dealer", "dealers", "trader",
    "traders", "trading", "factory", "factories", "plant", "plants",
    "unit", "units", "business", "businesses", "enterprise",
    "enterprises", "best", "top", "leading", "major", "key", "list",
    "directory", "portal", "india", "world", "wide", "global",
    "services", "service", "solution", "solutions", "system", "systems",
    "found", "products", "product", "works", "work", "exporter",
    "exporters", "importer", "importers", "page",
})

# URL paths that are plainly a directory of businesses.
LIST_PATH_RE = re.compile(
    r"/(?:business-directory|all-companies|companies-in-|manufacturers?-in-|"
    r"suppliers?-in-|vendors?-in-|dealers?-in-|traders?-in-|factories-in-|"
    r"industries-in-|list-of-|top-\d+|best-\d+|whitepages?|listings?|"
    r"directories|directory|search/manufacturer|manufacturing-companies)"
    r"(?:/|$|\?|-|\.)",
    re.I,
)

# URL paths that are an article or a news item rather than a company page.
PUBLICATION_PATH_RE = re.compile(
    r"/(?:news|blog|blogs|article|articles|post|posts|press-release|"
    r"pressroom|media|updates?|magazine|resources?|whitepapers?|"
    r"case-stud(?:y|ies)|blogdetails?)(?:/|-|$|\.)",
    re.I,
)

# URL paths that are a company's own pages. A result on one of these is
# the company, whatever the page title said.
COMPANY_PATH_SEGMENTS = frozenset({
    "about", "about-us", "aboutus", "contact", "contact-us", "contactus",
    "profile", "company-profile", "company", "our-story", "who-we-are",
    "story", "team", "products", "manufacturing", "services", "factory",
    "capabilities", "history", "infrastructure", "facilities", "machinery",
    "process", "quality", "certifications", "overview", "introduction",
    "management", "leadership", "our-story", "why-us", "what-we-do",
})

# ── 3. Where we sell ────────────────────────────────────────────────────
#
# Maharashtra first: the delivery model is Nashik-based, and the first
# wave of mail goes to Maharashtra companies. Only then the rest of India.

MAHARASHTRA_CITIES = (
    "nashik", "pune", "mumbai", "thane", "chhatrapati sambhajinagar",
    "sambhajinagar", "aurangabad", "nagpur", "kolhapur", "satara",
    "sangli", "solapur", "ahmednagar", "jalgaon", "washim", "yavatmal",
    "amravati", "akola", "bhandara", "gondia", "wardha", "chandrapur",
    "gadchiroli", "latur", "osmanabad", "beed", "parbhani", "hingoli",
    "ratnagiri", "sindhudurg", "palghar", "dhule", "nandurbar",
)
MAHARASHTRA_RE = re.compile(
    r"(?<![a-z])(" + "|".join(MAHARASHTRA_CITIES) + r")(?![a-z])", re.I)


def is_maharashtra(location: str) -> bool:
    """Is this location in Maharashtra?

    The location field is what we trust — a company named "Pune Enterprises"
    in Ludhiana is a Ludhiana company. A city name alone is enough because
    discovery stamps the searched city onto the result.
    """
    return bool(MAHARASHTRA_RE.search(location or ""))


def place_key(place: str) -> str:
    """A place's comparable name: the city, without the state or country.

    "Pune, India" and "Pune" are the same place. Comparing the whole
    string would make a config list and a data column fail to match
    purely because one of them carries ", India".
    """
    return (place or "").split(",")[0].strip().lower()


def location_matches_priority(location: str, priority: list[str]) -> bool:
    """Is this location one of the priority places?"""
    if not location:
        return False
    key = place_key(location)
    return any(place_key(p) == key for p in priority if p and p.strip())


def maharashtra_priority(location: str) -> int:
    """Sort key for Maharashtra-first ordering: 0 = Maharashtra, 1 = elsewhere."""
    return 0 if is_maharashtra(location) else 1


def _text(*parts) -> str:
    return " ".join(str(p or "") for p in parts if p)


def _host(domain: str) -> str:
    """The bare host of a domain or URL, without www."""
    value = (domain or "").strip().lower()
    if "//" in value:
        value = value.split("//", 1)[1]
    value = value.split("/", 1)[0]
    value = value.split("?", 1)[0]
    if value.startswith("www."):
        value = value[4:]
    return value


def _domain_of(url: str) -> str:
    return _host(url)


def _path_of(url: str) -> str | None:
    """The path of a URL, without the host and the query string.

    Returns "" for a homepage, and None when there is no URL to read —
    the difference matters, because a result on a homepage is the company
    itself and a result with no URL is simply unknown.
    """
    value = (url or "").strip()
    if not value:
        return None
    if "//" in value:
        value = value.split("//", 1)[1]
    if "/" not in value:
        return ""
    path = value.split("/", 1)[1]
    path = path.split("?", 1)[0].split("#", 1)[0]
    return path.strip("/")


def _labels(host: str) -> list[str]:
    """The host's labels, without the top-level domain."""
    parts = [p for p in (host or "").split(".") if p]
    return parts[:-1] if len(parts) > 1 else parts


def _known_publisher(host: str) -> bool:
    """Is this domain one of the publishers or directories we know?"""
    if not host:
        return False
    return any(host == d or host.endswith("." + d)
               for d in KNOWN_PUBLISHER_DOMAINS)


def _content_farm(host: str) -> bool:
    """Does the host look like a content farm rather than a business?

    A content farm names itself after what it publishes: "nagpuronline",
    "electricalsinformed", "123coimbatore".
    """
    for label in _labels(host):
        if label in PUBLISHER_LABEL_WORDS:
            return True
        if label[0].isdigit():
            return True
        if label.endswith(CONTENT_FARM_LABEL_SUFFIXES):
            return True
    parts = [p for p in (host or "").split(".") if p]
    if len(parts) >= 2 and parts[-1] in PUBLISHER_TLDS:
        return True
    return False


def _directory_domain(host: str) -> bool:
    """Does the host name itself after a list of businesses?"""
    label = "".join(_labels(host))
    if not label:
        return False
    return any(word in label for word in DIRECTORY_DOMAIN_WORDS)


def _distinctive_tokens(name: str) -> set[str]:
    """The words in a name that identify a brand or a speciality.

    Category words are left out: "Hydraulic Cylinder Manufacturers" says
    what the company does, and "manufacturers" tells us nothing about
    whose domain we are looking at. Middleman words are left out for the
    same reason, and with a sharper edge — "getdistributors" contains
    "distributors", but that is the directory's name for its trade, not
    the brand of the company listed on it.
    """
    tokens = re.findall(r"[a-z0-9]+", (name or "").lower())
    return {
        t for t in tokens
        if len(t) >= 4
        and t not in GENERIC_TITLE_WORDS
        and not MIDDLEMAN_STRICT_RE.fullmatch(t)
    }


def _owns_its_domain(name: str, host: str) -> bool:
    """Does the domain look like this company's own website?

    A brand name usually shows up in the domain: "shivalayajaipur.com" is
    Shivalaya Jaipur, "deltahydraulics.in" is Delta Hydraulics. When the
    title names the speciality and the domain carries the same word, the
    title came from the company's own page and the company is real.
    """
    label = "".join(_labels(host))
    if not label:
        return False
    tokens = _distinctive_tokens(name)
    if not tokens:
        return False
    return any(token in label for token in tokens)


def _is_company_page(path: str) -> bool:
    """Is this path one of a company's own pages?

    "/about-us", "/contact", "/products/hydraulic-cylinders" — a result
    on one of these is the company, whatever the page title said.
    """
    if not path:
        return False
    first = path.split("/")[0].lower()
    for suffix in (".html", ".htm", ".php", ".aspx"):
        if first.endswith(suffix):
            first = first[: -len(suffix)]
    return first in COMPANY_PATH_SEGMENTS


# ── The three questions ────────────────────────────────────────────────


def is_list_title(name: str) -> bool:
    """Does this name read like a page *about* companies ("Top 50 ... in
    Nagpur", "Manufacturing Companies in Nagpur")?

    The name of a discovered company is its page title, so a list-shaped
    name is strong evidence when there is no page on record to check. A
    real company's own homepage can carry such a title too, which is why
    the caller only uses this when it has no source URL.
    """
    return bool(LISTICLE_NAME_RE.search((name or "").strip()))


def is_content_page(name: str, source_url: str = "", domain: str = "") -> bool:
    """Is this result a page about companies rather than a company?

    A blog post titled "Manufacturing Companies in Nagpur" is a list of
    businesses, not one. Google ranks it; Sellbird must not call it.

    The test is domain-led, because the domain is the company's identity.
    A manufacturer's own website has blog posts and contact pages, and a
    page on a manufacturer's site is still evidence of a manufacturer:

    1. The domain is a known publisher, directory or institution, or
       looks like a content farm — that is decisive on its own.
    2. The *name* is a list, an article or a magazine — the name came
       from the page title, so this is strong evidence.
    3. The *name* is list-shaped and the URL is a listing path, with
       nothing to say the domain is the company's own.
    """
    name = (name or "").strip()
    host = _host(domain) or _domain_of(source_url)
    path = _path_of(source_url)

    if INSTITUTION_NAME_RE.search(name):
        return True
    if host:
        if _known_publisher(host):
            return True
        if _content_farm(host):
            return True
        if INSTITUTION_TLD_RE.search(host):
            return True
        if _directory_domain(host):
            # The domain names itself after a list of businesses — unless
            # it is plainly the company's own brand and the name says the
            # company makes something.
            if not (MAKER_RE.search(name) and _owns_its_domain(name, host)):
                return True

    # A name that is plainly a publication — "Retail India News",
    # "Anaxee Blog's" — is never a manufacturer's name.
    if re.search(r"\b(?:newspaper|magazine|journal|newsletter|blog|"
                 r"weekly|fortnightly|quarterly)\b", name, re.I):
        return True

    # A "portal" is a place that lists businesses: "MSME B2B Portal". A
    # factory does not call itself one, whatever its customer login is.
    if re.search(r"\bportals?\b", name, re.I):
        return True

    if path and PUBLICATION_PATH_RE.search(path):
        # An article, on someone's site. It is only worth keeping when
        # the site is plainly the factory's own page.
        if not (host and MAKER_RE.search(name) and _owns_its_domain(name, host)):
            return True

    if LISTICLE_NAME_RE.search(name):
        # The domain and the URL have the last word. A list-shaped title
        # on the company's own site is still the company.
        if host and _owns_its_domain(name, host) and MAKER_RE.search(name):
            return False
        if path is None or path == "":
            return False           # a homepage, or nothing to go on
        if _is_company_page(path):
            return False
        if LIST_PATH_RE.search("/" + path):
            return True
        # Not a listing path — an ordinary page of a real company whose
        # title happens to read like a list.
        return False

    if path and LIST_PATH_RE.search("/" + path):
        # The result itself is a listing page. Whoever published it is
        # not the business being listed — unless the site is plainly a
        # factory's own page about its own plants.
        if host and MAKER_RE.search(name) and _owns_its_domain(name, host):
            return False
        return True

    return False


def is_it_vendor(name: str, industry: str = "", description: str = "",
                 source_url: str = "", domain: str = "") -> bool:
    """Does this company sell software, IT or ERP rather than make things?

    The name is trusted outright. The description is only trusted for
    phrasing that only a vendor uses — a manufacturer that says "we use
    software for billing" is not a software company.
    """
    if IT_INDUSTRY_RE.search(industry or ""):
        return True

    if name and VENDOR_RE.search(name):
        return True

    desc = description or ""
    if desc:
        # "We run on Odoo" is a buyer talking. Nothing in the description
        # can make it a vendor.
        if SOFTWARE_USER_RE.search(desc):
            return False
        if VENDOR_DESCRIPTION_STRONG_RE.search(desc):
            return True
        if VENDOR_DESCRIPTION_RE.search(desc):
            return True

    # A product-shaped name is the tell even with no description at all:
    # "Delta Sales App", "QuickCommerceMap".
    if name and SAAS_NAME_RE.search(name):
        return True
    if name and " " not in name.strip() and PRODUCT_NAME_RE.match(name.strip()):
        return True

    return False


def is_trading_or_retail(name: str, industry: str = "", description: str = "",
                          source_url: str = "", domain: str = "") -> bool:
    """Is this a trading house, distributor or retailer rather than a maker?

    The name, domain and description are the evidence. The ``industry``
    field is only ever a hint, and for a company found by a web search it
    is the search phrase — so it is never used to exclude on its own.
    """
    identity = _text(name, domain)

    middleman = (MIDDLEMAN_STRICT_RE.search(identity)
                 or MIDDLEMAN_LOOSE_RE.search(identity))
    if middleman:
        # A name that says it makes things is a factory that happens to
        # trade as well — "Exporter & Manufacturer of Industrial Shafts".
        if re.search(r"manufactur", name or "", re.I):
            return False
        # A dealer-locator page on the brand's own site: the company is
        # the brand, and the page is one of its dealers.
        if DEALER_PAGE_RE.search(name or "") and _owns_its_domain(name, _host(domain)):
            return False
        # A name that is only a trading identity — "Taj Trading Company",
        # "shyamtradingcompany.com" — is out.
        return True

    # Exports and imports on their own mean the company moves goods, but
    # plenty of factories export, so a maker word rescues it.
    if EXPORT_RE.search(identity):
        if not MAKER_RE.search(_text(name, description, domain)):
            return True

    if description and (MIDDLEMAN_STRICT_RE.search(description)
                        or MIDDLEMAN_LOOSE_RE.search(description)):
        if not MAKER_RE.search(name or ""):
            return True

    return False


def exclusion_reason(company: dict) -> str:
    """Why this company must not be a prospect. "" means it may be one.

    ``company`` is a dict with any of: name, industry, description,
    source_url, domain, location.
    """
    name = str(company.get("name") or "")
    industry = str(company.get("industry") or "")
    description = str(company.get("description") or "")
    source_url = str(company.get("source_url") or "")
    domain = str(company.get("domain") or "")

    if is_content_page(name, source_url, domain):
        return "not a company (a blog post, directory or listicle)"
    if is_it_vendor(name, industry, description, source_url, domain):
        return "IT / ERP / software vendor"
    if is_trading_or_retail(name, industry, description, source_url, domain):
        return "trading / retail / distribution, not manufacturing"
    return ""


def is_excluded(company: dict) -> bool:
    """Should this company be kept out of the pipeline entirely?"""
    return bool(exclusion_reason(company))


# ── Description patterns ───────────────────────────────────────────────

# Description phrasing that says "we sell software", not "we use software".
VENDOR_DESCRIPTION_RE = re.compile(
    r"(is a |are |provides |offers |builds |develops |supplies |"
    r"specialises in |specializes in |helps |enables ).{0,70}?"
    r"(software|platform|app|erp|crm|saas|system|api|portal|"
    r"automation|analytics|integration|extension|plugin|add-?on|"
    r"consulting|services|consultancy|hosting|infrastructure)",
    re.I,
)
VENDOR_DESCRIPTION_STRONG_RE = re.compile(
    r"(software company|software firm|software solutions provider|"
    r"software services|software development company|"
    r"saas company|saas platform|it company|it services|tech company|"
    r"technology company|technology solutions|it solutions|"
    r"digital solutions|online platform|web application|"
    r"custom software|software development|"
    r"erp solutions provider|erp software company|erp implementation partner)",
    re.I,
)

# A company saying it *runs on* software is a buyer, not a vendor. This is
# checked first and suppresses the vendor rules: "We use ERP software for
# billing" is the single most common sentence on a manufacturer's own
# website, and reading it as a vendor would throw away exactly the
# companies we are looking for.
SOFTWARE_USER_RE = re.compile(
    r"\b(?:we|our(?:\s+\w+)?|they|their(?:\s+\w+)?|"
    r"the\s+(?:plant|factory|shop\s?floor|unit))\s+"
    r"(?:use|uses|used|using|run|runs|running|run\s+on|runs\s+on|"
    r"adopted|have\s+adopted|has\s+adopted|"
    r"migrated\s+(?:to|from)|migrate\s+to)\b"
    r"|\b(?:powered|built)\s+on\b",
    re.I,
)

# Product-shaped names: the company sells software even with no description.
SAAS_NAME_RE = re.compile(
    r"\bapp\b|\bsaas\b|\bplatform\b|\bportal\b|\bsoftware\b|"
    r"\bdashboard\b|\bapi\b|\bapp\s?based\b|"
    r"\bmarketplace\b|\blogistics platform\b|"
    r"\bdelivery management\b|\bwarehouse management\b|"
    r"\bshipping aggregator\b|\btracking app\b",
    re.I,
)

# A single compound word that ends in a product word — "QuickCommerceMap",
# "RouteApp", "InventoryCloud". These are products, not factories. Matched
# only on a one-word name so that "The Steel Map Company" is not caught.
PRODUCT_NAME_RE = re.compile(
    r"^(?:[A-Za-z]+)?(?:App|Portal|Platform|Suite|Desk|Hub|Cloud|Map|Api|"
    r"Saas|Software)$",
)
