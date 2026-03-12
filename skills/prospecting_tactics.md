# DIY Prospecting Tactics

Harvey's playbook for finding leads without expensive tools like Apollo, ZoomInfo, or Clearbit.

---

## Strategy 1: Google Dorking

### Find LinkedIn Profiles
```
site:linkedin.com/in "VP Marketing" "SaaS" "San Francisco"
site:linkedin.com/in "Head of Growth" "marketing agency"
site:linkedin.com/in "{{title}}" "{{industry}}" "{{location}}"
```

### Find Company Team Pages
```
site:{{domain}} "team" OR "about" OR "leadership"
"{{company name}}" "team" "{{title}}"
```

### Find Companies in an Industry
```
"{{industry}}" "Series A" OR "Series B" site:crunchbase.com
"{{industry}}" company "{{location}}" "employees" site:linkedin.com/company
```

### Find Email Patterns
```
"@{{domain}}" email
site:{{domain}} "contact" OR "email" OR "@"
"{{first_name}} {{last_name}}" "{{company}}" email
```

---

## Strategy 2: LinkedIn Search

### People Search Operators
- **Title**: Search "VP Marketing" to find exact title matches
- **Company**: Filter by current company
- **Location**: Filter by geographic region
- **Industry**: Filter by LinkedIn industry categories
- **Connections**: 2nd-degree connections have higher accept rates

### Sales Navigator (if available)
- Boolean search: "VP Marketing" AND "SaaS" NOT "consultant"
- Saved searches with alerts for new matches
- Lead lists for organized outreach

### Without Sales Navigator
- Use LinkedIn's basic search with keyword combinations
- Browse "People Also Viewed" on relevant profiles
- Check company pages → "People" tab for employees
- Search within relevant LinkedIn Groups

---

## Strategy 3: Company Website Scraping

### Team/About Pages
Most companies list leadership on /team, /about, /about-us, /our-team, /people, /leadership pages.

**Extract:**
- Name + title from team cards
- Sometimes email or LinkedIn links directly listed
- Company size indicator from number of team members

### Job Postings
- Companies hiring for roles your product supports = high-intent prospects
- Check /careers, /jobs pages
- Also check job boards: Indeed, LinkedIn Jobs, Wellfound (for startups)

### Tech Stack Discovery
- **BuiltWith/Wappalyzer**: Identify their current tools
- Useful for: "I see you're using {{competitor}} — here's how we compare"
- Free browser extensions available

---

## Strategy 4: Email Pattern Discovery

### Common Email Formats (in order of prevalence)
1. `first.last@domain.com` (most common)
2. `first@domain.com`
3. `firstlast@domain.com`
4. `flast@domain.com` (first initial + last name)
5. `firstl@domain.com`
6. `f.last@domain.com`
7. `last.first@domain.com`
8. `first_last@domain.com`
9. `first-last@domain.com`

### Verification Methods (Free)
1. **MX Record Check**: Verify the domain accepts email (dns lookup)
2. **SMTP RCPT TO**: Ask the mail server if the address exists
3. **Hunter.io Free Tier**: 25 searches/mo + 50 verifications/mo
4. **Email format from known addresses**: If you know one person's email at a company, the pattern applies to everyone

### Catch-All Domains
Some domains accept all addresses (catch-all). SMTP verification won't work for these. Rely on pattern + domain verification instead.

---

## Strategy 5: Trigger Events

High-value timing signals that indicate a prospect might be ready to buy:

1. **New hire in relevant role** — they're building the function you support
2. **Recent funding** — they have budget to spend
3. **New product launch** — they need marketing/growth support
4. **Job posting for your product's domain** — they're investing in the area
5. **Competitor using your product** — social proof angle
6. **Company expansion** — growing teams need new tools
7. **Leadership change** — new leaders bring new tools
8. **Tech stack change** — they're evaluating alternatives

### Where to find trigger events (free)
- Google News alerts for target companies
- LinkedIn: follow target companies for updates
- Crunchbase (free tier): funding announcements
- Company blogs and press release pages
- Job boards: new postings in relevant departments

---

## Strategy 6: Referral Mining

When a prospect says "I'm not the right person":
1. Ask who IS the right person (name + title)
2. Ask if they'd be open to making an intro
3. If no intro, use their name as social proof: "{{name}} on your team suggested I reach out..."

When a deal closes:
1. Ask for referrals to similar companies
2. Ask if they know peers in other companies with similar challenges
3. LinkedIn: check their connections for ICP matches

---

## Daily Prospecting Quotas

For sustainable pipeline building:
- **Research**: 20-30 companies per day
- **New prospects identified**: 10-15 per day
- **Emails verified**: 10-15 per day
- **Total prospects added to DB**: 10-15 per day
- **Quality check**: Score every prospect before adding to outreach
