"""Tech-stack detection — what tools a company's website reveals.

A curated subset of Wappalyzer-style fingerprints matched against HTML
Harvey already downloads while researching a company. Zero extra network
requests, zero dependencies. Knowing a prospect runs Shopify / HubSpot /
Intercom (or conspicuously lacks something) is a concrete, verifiable
personalization hook — the kind that actually earns replies.

Patterns are intentionally conservative: match distinctive script hosts,
cookie names, or generator tags — not generic words — so false positives
stay rare.
"""

import re

# Lovable's own markers, which survive into the published site: its default
# social-card image and author tag, its older editor script, and the path it
# serves uploaded images from. A site can carry any one of them.
LOVABLE_FINGERPRINTS: tuple[str, ...] = (
    r"lovable\.dev/opengraph-image",
    r'<meta[^>]+name=["\']author["\'][^>]+content=["\']Lovable["\']',
    r"cdn\.gpteng\.co/gptengineer\.js",
    r"/lovable-uploads/",
    r"[\w-]+\.lovable\.app",
)
# A project's API host (a 20-character ref), as it appears in a preconnect or
# a storage image URL. Not the bare word, which marketing copy uses.
SUPABASE_FINGERPRINTS: tuple[str, ...] = (r"\b[a-z0-9]{20}\.supabase\.co\b",)

# name -> list of regexes; any match counts. Compiled lazily, matched
# case-insensitively against the raw HTML.
FINGERPRINTS: dict[str, list[str]] = {
    # CMS / site builders
    "WordPress": [r"wp-content/", r"wp-includes/", r'<meta name="generator" content="WordPress'],
    "Shopify": [r"cdn\.shopify\.com", r"myshopify\.com"],
    "Wix": [r"static\.parastorage\.com", r"wix\.com/website-builder"],
    "Squarespace": [r"static1\.squarespace\.com", r'<!-- This is Squarespace'],
    "Webflow": [r"assets\.website-files\.com", r'data-wf-domain'],
    "Framer": [r"framerusercontent\.com"],
    "Drupal": [r'<meta name="generator" content="Drupal', r"sites/default/files"],
    "Ghost": [r'<meta name="generator" content="Ghost'],
    "Next.js": [r"/_next/static/"],
    "Gatsby": [r"/page-data/app-data\.json", r"id=\"___gatsby\""],

    # Analytics / tracking
    "Google Analytics": [r"googletagmanager\.com/gtag", r"google-analytics\.com/analytics\.js"],
    "Google Tag Manager": [r"googletagmanager\.com/gtm\.js"],
    "Segment": [r"cdn\.segment\.com/analytics\.js"],
    "Mixpanel": [r"cdn\.mxpnl\.com"],
    "Hotjar": [r"static\.hotjar\.com"],
    "Plausible": [r"plausible\.io/js"],
    "Meta Pixel": [r"connect\.facebook\.net/[^\"']*fbevents\.js"],

    # Marketing / CRM
    "HubSpot": [r"js\.hs-scripts\.com", r"js\.hsforms\.net", r"hubspot\.com/api"],
    "Marketo": [r"munchkin\.marketo\.net"],
    "Mailchimp": [r"chimpstatic\.com", r"list-manage\.com/subscribe"],
    "Klaviyo": [r"static\.klaviyo\.com"],
    "ActiveCampaign": [r"trackcmp\.net"],
    "Salesforce": [r"force\.com", r"salesforce\.com/embeddedservice"],

    # Chat / support
    "Intercom": [r"widget\.intercom\.io", r"js\.intercomcdn\.com"],
    "Drift": [r"js\.driftt\.com"],
    "Zendesk": [r"static\.zdassets\.com", r"zendesk\.com/embeddable"],
    "Crisp": [r"client\.crisp\.chat"],
    "Tidio": [r"code\.tidio\.co"],
    "LiveChat": [r"cdn\.livechatinc\.com"],

    # Scheduling / payments
    "Calendly": [r"assets\.calendly\.com", r"calendly\.com/[\w\-]+/"],
    "Stripe": [r"js\.stripe\.com"],
    "Typeform": [r"embed\.typeform\.com"],

    # E-commerce (non-Shopify)
    "WooCommerce": [r"woocommerce", r"wc-ajax"],
    "BigCommerce": [r"cdn\d*\.bigcommerce\.com"],

    # Hosting/infra hints
    "Cloudflare": [r"/cdn-cgi/"],
    "Vercel": [r"vercel\.app", r"x-vercel"],

    # AI app builders
    "Lovable": list(LOVABLE_FINGERPRINTS),
    "Supabase": list(SUPABASE_FINGERPRINTS),
}

_compiled: dict[str, list[re.Pattern]] | None = None


def _get_compiled() -> dict[str, list[re.Pattern]]:
    global _compiled
    if _compiled is None:
        _compiled = {
            name: [re.compile(p, re.IGNORECASE) for p in patterns]
            for name, patterns in FINGERPRINTS.items()
        }
    return _compiled


def detect_tech(html: str, max_bytes: int = 300_000) -> list[str]:
    """Return the tools detectable in a page's HTML, alphabetized."""
    if not html:
        return []
    sample = html[:max_bytes]
    found = []
    for name, patterns in _get_compiled().items():
        if any(p.search(sample) for p in patterns):
            found.append(name)
    return sorted(found)
