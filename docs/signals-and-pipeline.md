# Signals and the prospecting pipeline

## You confirm what it looks for

This is the part that makes Sellbird different from a black box, and it is deliberately not skippable.

Sellbird knows how to collect **29 signals** about a business. It does not collect any of them until you say so. On first run it *proposes* the catalog; you confirm, skip, or reject each one in the dashboard or from the terminal:

```bash
sellbird signals                       # review the catalog
sellbird signals --confirm free        # everything that costs nothing (26 of them)
sellbird signals --confirm SERP_RANK   # a paid one, opted into explicitly
```

Every signal shows what it is in plain language, **what it costs**, and how many companies already carry it.

| | Signal | What it tells you |
|---|---|---|
| **Discovery** | `SERP_RANK` | Where they rank for their own core search, stored with the search phrase. Positions 11–30 are the sweet spot: visible enough to be trying, and losing. |
| | `IN_LOCAL_PACK` / `MAPS_RANK` | Whether they hold one of the three map slots above the results, and where they sit in Google Maps. |
| | `NO_WEBSITE` | They have a listing and a phone and no site. If you sell websites, the highest-intent signal that exists. |
| | `UNCLAIMED_LISTING` | Nobody has claimed their Google Business Profile. |
| | `REVIEW_RATING` / `REVIEW_COUNT` | Good but invisible, or visible but struggling. |
| **Profile** | `INCUMBENT_AGENCY` | Who currently has the account, credited in their own footer. Every detection builds a roster you can target as a whole book of business. |
| | `RUNNING_GOOGLE_ADS` / `RUNNING_META_ADS` | They are spending on acquisition *today*. The strongest budget signal available for free. |
| | `NO_ONLINE_BOOKING` | Leads have to phone in, so after-hours demand is lost. |
| | `NO_SCHEMA_MARKUP` | A concrete, checkable SEO gap you can name in an email. |
| | `BLOG_STALE` | Content marketing started and abandoned. |
| | `SITE_PAGE_COUNT`, `TECH_STACK`, `BLOCKS_AI_CRAWLERS` | Size, platform, AI-search visibility. |
| | `HIRING_ROLE` / `OPEN_ROLES` | What they're hiring for, and how many roles are open. When their site links a Greenhouse, Lever, Ashby or Workable board, Sellbird reads that board's public feed (one request): roles matching `icp.hiring_signals` (else `icp.titles`), with department, location, posting date and the posting's URL; when the board lists none, it records "not hiring" so a closed role stops counting. Otherwise it reads the careers page. Set `hiring_signals` to the roles that mean they're buying: the `titles` fallback names who you email, so "CEO" only matches a CEO opening (never "Assistant to the CEO"), which is rarely the signal you want. |
| | `YC_BACKED`, `ACCELERATOR`, `FUNDING_ROUND` | A startup, as its own site says: YC or another accelerator (with the batch or cohort), or a raise it announced (round, amount, date). |
| **People** | `CONTACT_FOUND`, `DECISION_MAKER_TITLE`, `LIKELY_OWNER`, `REGISTRY_VERIFIED` | Who decides, and whether they own the place. |
| **Contact** | `EMAIL_PATTERN`, `EMAIL_STATUS`, `CONTACT_FORM_URL` | Whether you can actually reach them. |

### Then a cohort is a query, not a list

Once signals are confirmed, the dashboard's cohort builder turns them into a target set — pick what a good prospect **must have**, what **disqualifies** them, and a **range** for numeric signals (organic rank 11 to 30), and it counts the matches live:

```
Has a marketing agency  AND  running Google Ads  AND NOT  already has online booking
→ 23 companies
```

That is a campaign with a reason behind it. You know exactly why each of those 23 is on the list, and so does the email Sellbird writes them.

---

## The prospecting pipeline

Four stages. Each one runs independently, re-runs safely, and states its cost.

### 1. DISCOVER — who exists

The only stage that spends money, so it always estimates first and never calls anything until you press go.

```bash
sellbird discover --providers   # the menu, with real prices
sellbird discover --estimate    # projected spend, then exits
sellbird discover               # go
```

| Source | Cost | Free tier | Best for |
|---|---|---|---|
| **OpenStreetMap** *(default)* | free | unlimited, no account | Trying the whole pipeline before paying anyone. Coverage is thin for businesses without a storefront: OSM maps premises, so whole trades are barely present (under 2,000 roofers and under 2,300 plumbers in the entire US). Sellbird says so in the UI rather than quietly under-delivering. |
| **TinyFish** | free | 30 searches a minute, 500 an hour, no card | Finding businesses by phrase (including the startup and hiring presets) at no cost. Records no search rank: its ordering isn't Google's and it takes no location, so use Serper or DataForSEO SERP for rank signals. |
| **DataForSEO Business Listings** | $0.372 / 1,000 | $1 credit | Local trades, clinics, contractors. Phone, domain, rating, claimed status — and it can filter for businesses with **no website at all**. |
| **DataForSEO SERP** | $0.006 / search at depth 30 (Live, $0.002 per 10 results) | $1 credit + free sandbox | Organic rank as the buying signal, plus who holds the local 3-pack, searched from the city centre. |
| **DataForSEO Google Maps** | $0.002 / search (up to 100 results) | $1 credit + free sandbox | Map rank, rating, category and claimed status, including businesses with no website. |
| **Serper** | ~$0.30–1.00 / 1,000 | 2,500 free, no card | The easiest paid one to try. |
| **Scavio Google Maps** | $0.01 / page (up to 20 listings) | 50 credits, no card | Google Maps listings around each city: phone, website, category, rating and reviews, including businesses with no website. |
| **Scavio Google Search** | ~$0.03 / depth-30 search | same 50 credits | Rank as the signal, on the same key as Scavio Google Maps. |

**Selling to startups?** Sellbird has a built-in startup skill, made of three
parts:

- **The searches** (each a web search, so Serper or DataForSEO SERP):

  | Preset | Finds | Signal |
  |---|---|---|
  | `yc` (*YC startups*) | Companies whose own site says they're backed by Y Combinator | `YC_BACKED`, with the batch (W24, S23) |
  | `accelerator` (*Accelerator startups*) | Ones backed by Techstars, 500 Global, Antler, Entrepreneur First, South Park Commons, Neo, AI Grant, a16z speedrun or Plug and Play (two searches per industry; the signal also reads Pioneer) | `ACCELERATOR`, with the program and cohort ("Techstars 2024") |
  | `funding` (*Recently funded*) | Companies announcing their own raise ("we raised", "announces $X Series A") in the last 18 months, news sites and funding databases excluded | `FUNDING_ROUND`: round, amount and date |

  `sellbird discover --preset accelerator --provider serper`, or the *Look for*
  menu on the Discover tab.
- **The signals:** turn them on in the Signals tab. They read the homepage and
  About page (and for a raise, the page the search found on their own site and
  their press or news page) for a line where the company says it about itself
  ("we went through Y Combinator", "Acme (Techstars '24)", "we raised a $4M
  seed round"), a "Backed by" logo wall, or a link to its own YC page. Customer
  and audience lines ("trusted by YC-backed startups", "we help founders raise
  their seed round") don't count. Many startups never say it on their site, so
  these find the ones that do; a YC or accelerator preset hit counts as a yes too.
  A raise's date comes from its own clause, or from the publish date of a
  press, news or blog post. Money that isn't a raise (loans closed, verdicts
  won, money raised for a charity, an investor's round in another company)
  doesn't count.
- **The writing:** the writer and reply handler use
  `skills/selling_to_startups.md`, a playbook for writing to founders with a
  section on companies that just raised, for YC- and accelerator-backed
  companies and for raises dated within the last 18 months. An older or
  undated raise alone doesn't bring the playbook, and is never written to as
  news.

Sellbird never reads ycombinator.com, the accelerators' directories, Crunchbase,
PitchBook or LinkedIn, whose terms forbid scraping them; everything comes from
the companies' own sites and your search provider.

**Looking for companies hiring a role?** `sellbird discover --preset hiring
--provider serper` (or *Look for: Companies hiring for your roles*) searches
your industries for companies whose own careers pages list one of your
`icp.hiring_signals` roles (else `icp.titles`). Hosted boards and job sites
are left out of the search, since a board page names a board, not a website
to profile. Confirm `HIRING_ROLE` and profiling reads the board their site links
to and records the role; the search hit alone records nothing.

Cities geocode themselves (cached forever, one lookup each). Directories, aggregators and institutions are filtered by pattern, not a blocklist — `en.wikipedia.org` and `biz.yelp.com` never make it in.

> **Depth 20–30, not 100.** Google removed 100-results-per-page in September 2025, so depth 100 is now billed as ten pages nearly everywhere. Below rank 30 it is mostly directories anyway.

### 2. PROFILE — what they are

**Free.** Three HTTP requests per business. No browser, no model call. This is the highest value-per-effort stage in the whole system, because it produces the facts that make an email specific.

Website requests from Scout, Profile, and the local training crawler are limited
to public HTTP(S) destinations. Every DNS answer and redirect is checked; local,
private, link-local, and metadata addresses are rejected. Connections use a
validated IP while preserving the website's TLS identity. These website clients
ignore environment proxies, so proxy routing cannot bypass that boundary.

Homepage, `robots.txt`, `sitemap.xml`, plus the team and careers pages when they exist — then regex over HTML already in hand. Out comes the incumbent agency, the ad pixels, the missing schema, the abandoned blog, the open roles, the named humans.

Agency detection is the compounding one. Almost every agency credits itself in the footer, and Sellbird scores the wording: *"Powered by"* / *"Website by"* → 0.9 confidence; a bare descriptive link → 0.75; an unlabelled link → 0.35 and **not recorded**. A wrong incumbent in an email is worse than saying nothing.

### 3. ENRICH — who decides

The owner, from the company's own site. Sellbird reads the homepage and follows the site's own menu to the pages where people are named — team, about, our story, meet the dentists, attorneys, contact — six pages at most, skipping links that land back on the homepage. On those pages it reads:

- **cards and lists**: a name with a title under it, even when the title is only a `<br>` away inside a span;
- **sentences**: "owned and operated by Gary Pruitt", "led today by President Ilan Brooks", "Ines Varga started Countwise";
- **schema.org**: the business's own `founder` and `employee` entries (a blog post's author is not staff);
- **the name on the door**: when two people both have owner titles, the one whose surname is in the business name comes first. A surname alone never makes anyone the owner: Linda Smith, Office Manager, at Smith & Sons isn't one.

Owners rank first, and at most two people per company become prospects. Headings made of ordinary words ("Team of Experts"), customers quoted in testimonials or tweets ("Founder at Quillnest"), other organisations' officers ("KTR President Vic Hale"), former or late owners ("Past President", "the late Harold Smith"), and taglines are not people. Titles match your ICP by whole word: "Vice President" doesn't count as "President" unless you list it, and Proprietor, Principal, Managing Partner, Founder and CEO all count as an owner. With `LIKELY_OWNER` confirmed, each owner is recorded as an observation with the evidence that says so.

When a company's site names nobody, or names an owner whose address is only a guess, and Sellbird has no contact there yet — common for small trades — it also uses a general address the company prints on its own pages (only general mailboxes such as `owner@`, `office@`, `info@` or `hello@`, never a person's own address). Sellbird reads whole pages for it, including schema.org and addresses hidden from scrapers, and accepts an address on another domain only when that domain leads to the company's own site. It is never guessed, it is verified like any other address, and those emails greet the company's team instead of a first name.

A site behind a bot challenge (SiteGround's "sgcaptcha", Cloudflare's "Just a moment…") is reported as challenged, not unreachable, and isn't retried.

### 4. VERIFY — can you reach them

Sellbird learns each company's **address pattern** and verifies one candidate, rather than brute-forcing name variations. Any address the site publishes can teach it: `nvale@` next to Nora Vale is `{first initial}{last}`, and `j.smith@` is an initial and surname. A lone dotless address no named person owns (`kmorrow@`: Kmorrow, or K. Morrow?) is ambiguous and teaches nothing. An address two named people could own (`jsmith@` when John and Jane Smith both work there) is format evidence only, and Sellbird never guesses or verifies an address another named person could own: a verifier would confirm *their* mailbox. It decodes the usual hiding tricks (Cloudflare email protection, a mail link's `data-user`/`data-domain` attributes, `name [at] domain [dot] com`) and never invents an address from ordinary text. A person's own published address is used as is. With no evidence at all, it guesses `first.last@`, or `first@` when something besides the website says the company is small (your ICP's company size, or a local-business listing as its source). A cached guess never stops a real address or a Hunter lookup from replacing it, and Hunter is asked once per domain (a timeout or rate limit doesn't count). Raw SMTP probing is not viable from a laptop — outbound port 25 is usually blocked, and Google Workspace and Microsoft 365 accept everything from an unknown IP.

Every address is tagged honestly: `verified` / `risky` (catch-all) / `guess` / `invalid`. **Only deliverable ones are ever sent to.** A guess is never treated as a win.

> Sellbird is worth running even if you never let it send. `sellbird export` writes a sequencer-ready CSV of everything it found.
