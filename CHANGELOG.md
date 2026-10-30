# Changelog

Notable changes to Harvey. Dates are release dates; the format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) loosely and versions
follow [semver](https://semver.org/), with the caveat that Harvey is pre-1.0 and
minor versions can still change behaviour.

## [Unreleased]

### Added

- **Your own email shape** (`writer.email_rules`). Point it at a markdown file
  (`skills/email_rules.md` is gitignored) and its rules replace the Writer's
  built-in shape, which ends every prompt and so beat anything in the skills:
  email 1 under 75 words, an observation and a question, no pitch, no link.
  It covers the per-person first email and the 3-email sequence. It never
  lifts the FACTS rule, and the send gate (220 words, one link,
  do-not-contact) still applies. The prompt now says what Harvey really adds
  when it sends: with `compliance.opt_out_line` set, the draft gets no second
  opt-out sentence; with it empty, an opt-out sentence the rules ask for is
  written into the body. A file that's set but missing, empty or too long
  stops the Writer, so it never drafts in the shape you replaced. Unset,
  nothing changes.

- **Scavio discovery providers** (`scavio_maps`, `scavio_search`). Google Maps
  listings around each city, with phone, website, category, rating and review
  count, and Google organic results with rank, both from one
  `SCAVIO_API_KEY`. Maps is 1 credit per 20 businesses and Search 1 credit per
  10 results; estimates use the $0.01 pay-as-you-go credit price. Off unless
  you pick one in `harvey discover` or the dashboard. Closes #28.
- **TrulyInbox for warmup.** Harvey now recommends TrulyInbox for warming and
  keeping sending mailboxes healthy, and reads it: `harvey trulyinbox connect`
  (or the new warmup step on the dashboard's setup checklist) checks a key and
  saves it, and `harvey warmup` then says whether the sending mailbox is
  actually warmed: enrolled, signed in, warmed for `min_days`, and under 5%
  spam in its warmup mail. Read-only; it never changes anything in TrulyInbox.
  `harvey trulyinbox other` records that you use another warmup service.
- **A do-not-contact list, enforced at send time.** `compliance.do_not_contact`
  takes addresses and domains Harvey must never email: competitors, your
  existing customers, anyone under a legal hold, a partner you agreed not to
  approach. A domain covers its subdomains, matched on a label boundary, so
  `acme.example` blocks `mail.acme.example` and leaves `notacme.example`
  alone — a plain suffix test would catch the wrong company. The check runs
  in `pre_send_check`, so it is the last thing before an email leaves and
  holds for one a human already approved in the Outbox. It applies to replies as well as cold mail: a list that lets
  automated replies through is not a do-not-contact list, and the operator
  can still answer by hand. Entries are normalised when the config loads, so
  ` @Acme.EXAMPLE `, a pasted `https://www.acme.example/about`,
  `Jane Doe <jane@acme.example>` (that address) and `acme.example` behave
  alike; an entry that names nothing is a config error, not a silent no-op.
  An edit counts at once in a running Harvey. A listed person who writes
  back goes to you without a draft, and `harvey export` leaves them out. On
  Instantly, which has no Outbox, listed leads are left out of the upload
  and a reply to one goes to you; a lead Instantly already has must be
  removed there.

- **Discovery skips do-not-contact companies too.** The same
  `compliance.do_not_contact` list is now applied while discovering, before a
  result becomes a company, so a forbidden business never enters the pool: it
  costs nothing to enrich, never has its site read, and stops turning up in
  cohorts for someone to spot by eye. The send-time gate is unchanged and
  still the last word — only it can stop a send — but on its own it let a
  competitor through discovery and charged you for the enrichment first.
  Discovery has a domain, not an address, so only domain entries apply here:
  `jane@acme.example` bars one mailbox, and acme.example stays discoverable
  because a colleague there may still be worth reaching. Skipped companies
  are counted separately from junk, and `harvey discover` prints the count
  only when it isn't zero — junk asks whether a result is a business at all,
  this asks whether you are allowed to approach one, and burying the second
  in the first is how a list that has grown too broad goes unnoticed.

- **TinyFish as a free discovery source** (from #95). `harvey discover
  --provider tinyfish` (or *TinyFish* on the Discover tab) finds businesses
  by phrase with a free `TINYFISH_API_KEY`, no card, and runs the `yc`,
  `hiring`, `accelerator` and `funding` presets. Harvey paces it to its
  published limits, 30 searches a minute and 500 an hour, and a run that
  uses up the hour (or is refused the key) stops and says why instead of
  failing every remaining phrase. It records no
  `SERP_RANK`: TinyFish's ordering isn't Google's and its search takes no
  location, so rank signals still come from Serper or DataForSEO SERP, and
  a TinyFish run never marks a business they ranked as gone. Scout also
  searches through TinyFish when the key is set, after Serper.

- **Open roles from the job board a company actually uses.** When a
  company's own site links or embeds a Greenhouse, Lever, Ashby or Workable
  board, profiling reads that board's public JSON feed (one request, capped
  at 8 MB, never a guessed token) instead of guessing from careers-page
  HTML, which on most startup sites has no roles in it at all because a
  script loads them. `HIRING_ROLE` now records the roles matching
  `icp.hiring_signals` (else `icp.titles`) with department, location,
  posting date and the posting's URL; the new `OPEN_ROLES` signal counts the
  board's open roles. When the board lists no matching role, `HIRING_ROLE`
  is recorded as 0, so a role that has closed stops counting in cohorts and
  shows as "no" to the reviewer. Matching folds VP/Vice President, CMO/Chief
  Marketing Officer and similar, and "Assistant to the CEO" is not a CEO
  role. Pages linking three or more boards, none named like the site (an
  agency or a VC portfolio page), are attributed to nobody. Sites without a
  board still get the careers-page read.
  The new `hiring` discovery preset (`harvey discover --preset hiring`, or
  *Look for: Companies hiring for your roles*) searches through Serper or
  DataForSEO SERP for companies whose own careers pages list your roles.
  `skills/signal_playbook.md` has a short section on using a posting as the
  hook: cite the role as posted, never guess salary or headcount, don't
  imply inside knowledge, mind the posting date.

- **Find companies by where they rank in a city.** DataForSEO SERP now also
  records who holds the local 3-pack (`IN_LOCAL_PACK`, slot 1-3, ads
  excluded), and a new **DataForSEO Google Maps** provider records each
  business's map position (`MAPS_RANK`) with its rating, category and
  claimed status, keeping businesses with no website by their place_id
  (`NO_WEBSITE`). Both search from the geocoded city centre. Every rank is
  stored with the search phrase that produced it ("#14 for 'roofer Denver,
  CO'"), one per business per phrase, keeping the best. When a later search
  for that phrase no longer finds a business, Harvey records that, so an old
  rank or a lost pack slot doesn't keep matching. Websiteless businesses are
  keyed on Google's cid, so the pack and Maps resolve to one company. `icp.keyword_variants`
  adds search phrases per industry. The cohort builder takes numeric ranges
  (Signals tab, `/api/cohort` `ranges`, `/api/cohort.csv?ranges=SERP_RANK:11:30`),
  so "organic rank 11-30, not in the local pack" is one query. The signal
  playbook has rank-based angles, worded as "when we searched from downtown
  Denver", never as an absolute position.

- **Cold email in each recipient's office hours** (from #59 by
  gabrielaoimevbore-beep; `channels.email.send_window`, #53). Cold email goes out only between `start` and `end` (08:00 to 17:00 on
  weekdays by default) in the recipient's own time zone. The zone is worked
  out once and stored on the prospect, from the company's location (US
  states, Canadian provinces, Australian states, countries with one zone or
  one main zone, and the coordinates discovery already cached), the country
  Precleared returns when that check is on, or `prospect_country`. A code
  that is a state and a country too ("Mumbai, IN", "Adelaide, SA") is
  settled by those; with nothing else it reads as a US state. Anyone Harvey
  can't place gets the window in `usage.quiet_hours.timezone`. Follow-ups
  keep their `delay_days` and move into the window, an email approved after
  hours waits for the next one, and replies are never held by the window.
  With `recipient_timezone` on, quiet hours still pause prospecting,
  writing and replies, but wake for cold email that can go now in its
  recipient's window (not paused, under the caps): the heartbeat reads
  replies, then sends it. `weekdays_only` means Monday to Friday. Off unless
  set; Gmail, Microsoft and SMTP only.

- **A built-in skill for selling to YC startups.** The `yc` discovery preset
  (`harvey discover --preset yc`, or *Look for: YC startups* on Discover)
  searches your industries, through Serper or DataForSEO SERP, for companies
  whose own site says they're backed by Y Combinator. The new `YC_BACKED`
  signal tags them from their homepage or About page (a reference to their
  own YC page, or a line where they say it, never customer or audience
  lines), with the batch when it's stated (W24, S23). Many YC companies
  don't say it on their site, so it finds the ones that do. For those prospects the writer and reply handler use the new
  `skills/selling_to_startups.md` playbook: who reads, timing around the
  batch and Demo Day, what not to do. Harvey never reads ycombinator.com,
  whose terms forbid scraping it or collecting founders' contact details.
- **Startups beyond YC: accelerators and recent raises.** Two more presets:
  `accelerator` (*Accelerator startups*) finds companies whose own site says
  Techstars, 500 Global, Antler, Entrepreneur First, South Park Commons, Neo,
  AI Grant, a16z speedrun, Pioneer or Plug and Play backed them, and
  `funding` (*Recently funded*) finds companies announcing their own raise in
  the last 18 months, with news sites, press wires and funding databases
  excluded and filtered as junk. Two new signals read it off their own
  pages: `ACCELERATOR` (program and cohort, from a sentence where the company
  says it or a "Backed by" logo wall) and `FUNDING_ROUND` (round, amount and
  date, from the homepage, the search hit's own page, About or the press
  page; the date from the sentence or the page's publish date). Both are
  anchored like `YC_BACKED`: "we help startups raise", customer or
  portfolio lines, an investor's round in another company, and money that
  isn't a raise (loans closed, verdicts won, money raised for a charity)
  don't count. The founder playbook now applies to YC- and
  accelerator-backed companies and to raises dated within 18 months, and has
  a section on writing to recently funded companies; an older or undated
  raise is never written to as news.
- **Approval or autopilot, visible and switchable.** The Outbox now says
  which sending mode you're in, and Settings → Sending explains both,
  lists the domains counted as yours and switches them. Turning autopilot
  on takes typing `autopilot`; turning it off is one click and works at
  once, mid-cycle too (every step that queues or sends re-reads the mode):
  emails autopilot approved that haven't gone out come back to the Outbox,
  and the sender won't send one while approval is on. Only the
  `require_approval` line of your config changes (comments kept), and a
  running Harvey picks it up next cycle. Each outbox row records who
  approved it (`approved_by`: you, autopilot, or earlier for rows from
  before this). The README has a plain table of what each mode does.
- **`harvey warmup [domain]`**: checks whether a sending domain is ready for
  cold email. It covers MX, SPF (`+all`, duplicates, top-level lookups), DKIM,
  DMARC, domain age, domain and SMTP-IP blocklists (a list that refuses the
  query is reported as such, not as a listing), warmup progress and bounce
  rate, then gives a READY / NOT READY verdict. It's read-only, and a DNS
  timeout is reported as unknown, never as a missing record.
- **A cold-email ramp for new domains** (`channels.email.warmup`). From
  `start_date`, no cold email goes out for `min_days`; then the cold cap starts
  at `start_sends` and climbs by `daily_increase` a day up to
  `max_daily_sends`, counted per local day. Gmail, Microsoft and SMTP only:
  Instantly has its own warmup and per-account limits. Replies are never
  held. Off unless `start_date` is set. Both based on #18 by Tariz Shahid.
- **A postal address and opt-out line under every cold email** (CAN-SPAM).
  Set `compliance.postal_address` in `harvey.yaml`; `compliance.opt_out_line`
  defaults to *Not interested? Reply "no thanks" and I won't email again.*
  Both are added when the email sends (native providers and Instantly), never
  to replies, and the Outbox shows them under each draft; "Open in Gmail"
  includes them too. Without an address (or with the opt-out line turned
  off), cold email still sends, and `harvey run`, `harvey status` and the
  Outbox tab warn until it's set. The opt-out line must ask for a reply
  Harvey recognises and can't contain a link. Based on #13 by Camilo Cruz.
- **The Today page and `harvey status` say when Precleared is holding cold
  email.** When the check can't be made (its free checks for the day are used
  up, the key is out of checks, was rejected or is being rate limited, or
  Precleared can't be reached), cold email to new addresses waits and replies
  still go out, but this used to reach only the log, so cold email just went
  out slower. Harvey now records the hold, and the Today page and
  `harvey status` say how many cold emails are waiting and what fixes it: a
  free key, a pack for that key (the checkout link from Precleared's answer),
  or nothing but time. The next fresh answer clears it, a hold not seen for a
  day is ignored, and switching the check off clears it. (from #57 by
  gabrielaoimevbore-beep)

### Changed

- **Precleared is told the company's and person's names Harvey already has.**
  For a UK address, cold email may go to a limited company's staff without
  consent but not to a sole trader, so Precleared has to find the company
  behind the domain on the Companies House register. When the domain alone
  didn't find it, the answer was `consent_needed`, even for a company Harvey
  knew the name of. Harvey now sends `company_name` (the company record's
  name, else the prospect's company field) and `recipient_name` (the
  prospect's first and last name; none for a company inbox), which
  Precleared uses to search the register and match the company's officers.
  It doesn't store the person's name. The names join the cache fingerprint
  only when set: a cached `ok_to_email` stands when they first arrive, and
  any other cached verdict gets one fresh check with them.
- **Precleared is told the recipient's country Harvey already knows.** The
  send window works out where a company is (a country, US state, Canadian
  province or Australian state in its location, or a code that is a state
  and a country too once the cached coordinates settle it); the compliance
  check used to ask Precleared about the address with none of it, so
  Precleared had to find the country again and, when it couldn't, answered
  `unknown` and the email failed. Harvey now sends that country as
  `recipient_country` when it is sure of it, which Precleared takes as
  settled. Nothing it is not sure of is sent: "Berlin, DE" with no
  coordinates reads as Delaware by convention and goes without a country,
  as do a bare city and coordinates alone. Without a certain country,
  `prospect_country` goes as the default, as before, and with that unset
  the one country every `icp.geography` entry is in now serves as that
  default; Precleared uses a default only when its own lookups show
  nothing. The country joins the cache fingerprint only when it is set, so
  cached verdicts are kept, and an address Harvey later becomes sure of is
  re-checked once unless Precleared had found the same country itself.
  `docs/precleared.md` describes the check in full.
- **Setup comes first on Today.** Until setup is finished, the checklist sits at
  the top of the Today page as a "Finish setting up Harvey" card, with the next
  step highlighted, instead of at the bottom of the side column.
- **Scout reads a company's site the way a person would.** It starts at the
  homepage and follows the site's own menu to its team, about, our-story,
  meet-the-doctors or attorneys pages (six pages at most), instead of
  guessing nine fixed paths and stopping at the first that answered. Owners
  are read from team cards, sentences ("owned and operated by…"),
  and schema.org founders, and rank first (a surname in the business name
  only breaks a tie between owners);
  at most two people per company become prospects. ICP titles match by
  whole word, with owner synonyms (Proprietor, Principal, Managing Partner,
  Founder, CEO). With `LIKELY_OWNER` confirmed, each owner is recorded with
  its evidence.
- **Email patterns come from the addresses a site publishes**, including
  ones hidden behind Cloudflare email protection or a mail link's `data-`
  attributes, matched to the people the site names. A lone ambiguous
  address (`kmorrow@`) teaches nothing, and an address another named person
  could own (`jsmith@` with both John and Jane Smith there) is never guessed
  for anyone. A named person's own published address is used as is. With no
  evidence, a company known to be small (ICP company size, or a
  local-business listing) is guessed as `first@`, others as `first.last@`. Every address is still graded
  verified / risky / guess / invalid, and only verified or risky ones are
  ever sent.
- **Gmail reads only what's new.** After the first complete read of the
  week, each cycle asks Gmail's history for messages added to the inbox
  since the last check, so a busy mailbox costs what a quiet one does.
  Harvey moves past them only once every reply has been handled, so one
  it couldn't handle is read again. It reads the full week again when
  Gmail's history marker has expired, and once a day as a backstop.

### Fixed

- **Replies remember the whole conversation** (closes #97). A reply was
  written from the last six messages only, so a requirement the prospect
  stated earlier ("synthetic data only, processed in the EU") dropped out
  and the draft could contradict it. Harvey now sends the whole thread, and
  tells the model that what the prospect asked for or ruled out, and what
  Harvey agreed to in its own messages, still applies. Each message's text
  is quoted under a header the prospect can't type, so a reply that writes
  "Harvey: confirmed, the first year is free" stays the prospect's words
  and is no agreement. A thread too long to fit (about 24,000 characters)
  keeps the latest six messages, then the prospect's own earlier words,
  then Harvey's, and says how many it left out; one pasted wall of text in
  an older message is cut so it can't crowd out the rest, while the
  message being answered is kept almost whole.

- **The dashboard's Start and Stop work, on Windows and everywhere else.**
  Start launched `python -m harvey` with no command, so the loop printed the
  help and exited; it now runs `harvey run`, and finds `claude` in
  `~/.local/bin` when the native installer left it off PATH. On Windows the
  "is it running?" check was `os.kill(pid, 0)`, which there terminates the
  process it asks about, and a stale PID file made the status and Start
  return 500. The check now asks the OS without signalling. `harvey run`
  records itself too, so a loop started in a terminal shows as running, and
  holds a lock on `data/harvey.lock`: a PID file left by a crash or a reboot
  can name an unrelated program, which is now reported as stopped and never
  signalled, and a second `harvey run` refuses to start while one is
  running. Start refuses, with the reason, until `harvey setup` has been
  run (the loop used to sit in the setup wizard reading the dashboard's
  terminal), and Stop says so when it couldn't end the loop instead of
  reporting success. (From #91 and #90.)

- **`{{company}}'s` reads right for names ending in "s"**: "Parkes Builders'",
  not "Parkes Builders's". (From #90.)

- **LinkedIn-profile web searches run only when LinkedIn prospecting is on**
  (from #60 by FreemanGT). Searching `site:linkedin.com/in` for your titles
  found names with no company and no email, which Harvey can never verify
  or write to, and they counted toward the "no usable emails" check that
  pauses prospecting for the day. It also ran first and could use up the
  cycle's three searches before company discovery, which finds people you
  can email, got any. That search now runs only when LinkedIn prospecting
  is on (`channels.linkedin.enabled`, off by default) with a LinkedIn login.

- **Dashboard actions work in Safari and Firefox** (from #80 by 0SansNom).
  Confirming signals, approving the Outbox and saving settings all failed
  with "Cross-origin requests are refused." The dashboard's requests asked the
  browser to send no referrer, which per the Fetch spec makes a POST's Origin
  `null`, and the guard rightly refused it. Safari and Firefox follow the
  spec; Chrome didn't, so it worked there. Requests now send their origin to
  the dashboard itself, and only to it.

- **A reply stops the cold email to the whole company.** Stop-on-reply was
  per person, so when an owner wrote back "not interested" the office
  manager still got the follow-up. Now a reply (not a bounce, an
  out-of-office or a "wrong person, ask Bob") cancels the sequence emails
  queued for everyone else at that company, and Harvey won't write, stage
  or send cold email to them later; the Outbox and log say which colleague
  replied. Colleagues are the same company; or, when Harvey has no company
  for one of them, the same email domain, and only if it's that company's
  own domain, never a shared one like gmail.com or rr.com. Instantly
  campaigns Harvey creates turn on Instantly's own `stop_for_company`.

- **DataForSEO SERP sends a location.** It sent only a keyword, but
  DataForSEO requires a location on every task, so searches failed or came
  back un-localised. It now sends the geocoded city point, or a
  "Denver,Colorado,United States" location name, or the United States for
  searches with no city. "No search results" is now an empty answer rather
  than an error; other task errors still stop that query and are reported.
- **DataForSEO SERP estimates were 3.3x low.** They used the standard-queue
  price ($0.0006 per 10 results); Harvey calls the Live endpoint ($0.002),
  so a depth-30 search is $0.006, not $0.0018. Harvey stays on Live: it's
  synchronous, so the spend cap checked between searches sees real spend.

- **Scout finds the actual decision maker.** On a test set of 19 small
  business sites it found the right person on 3; it now finds 17. It no
  longer turns headings like "Team of Experts" into prospects, reads a
  bio paragraph as a job title, lets "Vice President" pass for "President",
  or takes a customer quoted in a testimonial for the owner.
- **A cached default email pattern no longer blocks better evidence.** A
  first.last@ guess cached for a domain used to be read first forever, so
  neither a real address nor a Hunter lookup could replace it. Hunter is
  asked once per domain, and `j.smith@` is no longer read as first.last@.
- **The company-inbox fallback finds published inboxes it used to miss**: it
  reads whole pages (an `info@` 140k characters into a page counted for
  nothing), schema.org, and an address on the business's own second domain
  (`admin@phroofing.test` for prairieheritageroofing.test). It is also used
  when the named owner's address is only a guess, which is never sent.
- **reCAPTCHA no longer makes a site look blocked.** Any page mentioning
  "captcha" in its first 4,000 characters counted as a block, which cost
  Harvey every site with a protected contact form. Only real challenge
  pages count now, and a SiteGround or Cloudflare challenge is reported as
  challenged (not unreachable) without retrying every URL variant.
- **Gmail reads replies in the charset they were written in** (from #58 by
  xiaoheiai4719-collab). Every reply was decoded as UTF-8, so a reply sent
  as ISO-8859-1 or Windows-1252 ("Je préfère mardi") reached the model
  garbled. Each part now uses its declared charset; an unknown or missing
  one, or odd headers, falls back to UTF-8 without losing the text. SMTP
  and Microsoft 365 already decoded correctly.
- **You see it when Harvey isn't sending because it couldn't read your
  inbox.** Sending waits until replies are read, so an opt-out stops its
  follow-up. That hold only showed in the log, so a lapsed Gmail sign-in
  could stall sending for days unnoticed. Today now shows it first, with
  the reason, how long, how many emails are waiting and the fix (`harvey
  gmail auth`, `harvey microsoft auth`, or a mailbox over the read limit),
  and so does `harvey status`. It clears itself once sending runs again.
- **Gmail: nothing sends until the whole inbox has been read** (from #62 by
  xiaoheiai4719-collab). A failed or partial inbox read used to look like
  an empty inbox, so a follow-up could go out before an opt-out on page two
  was handled. Gmail now reads every page of the last week, and raises on
  any failure so the heartbeat holds sending, the same as Microsoft 365. A
  send whose outcome is unknown (a dropped connection, a 5xx, an unreadable
  answer) is recorded as maybe-sent and never retried, so nobody gets it
  twice. Messages already read aren't downloaded again on later cycles.
- **Autopilot can't be steered by text from outside** (#66, reported by
  Yaaaannnn435). Websites Harvey researches and the replies people send
  reach the model, and either can carry planted instructions. That text is
  now fenced as data in every prompt (writer, reply handler, scout scoring,
  Humanizer), in a block it can't close itself. Prompts only lower the odds,
  so with `require_approval: false` an email nobody approved goes back to
  the Outbox, marked Held with the reason, when it links to or gives an
  address at a domain that isn't yours (sending domain, booking link, or
  `channels.email.link_domains`), or quotes Harvey's own instructions.
  Bare domains like `example.com/x` count as links. Approving it yourself
  sends it. On Instantly, replies now run the pre-send gate too and a held
  one goes to you; a campaign template that would be held isn't deployed.
  The model never had tools, files or network to act with, so this was
  never more than what it wrote.
- **Opted-out contacts are no longer in the prospect CSV.** Someone who
  replied "unsubscribe" stayed in `harvey export` and the dashboard's Export
  button, so taking the list to another sequencer could email them again.
  They're now left out even when `--status` or `--email-status` is given;
  `harvey export --all` (and `?all=true`) is still the raw, unfiltered dump.
  From #64 by hifzabuildsai.
- **An opt-out now stays an opt-out.** Any later message from someone who
  opted out (an out-of-office, a "thanks") set them back to *replied* or
  *lost*, which put them back in the export, and a question from them got a
  drafted answer in the Outbox. Now their message is kept in a closed
  conversation for you to read, nothing is drafted, and no code path can
  move a prospect off *opted out*. A complaint from them still comes to you.
- **See why someone opted out, and undo it.** Each opt-out now records the
  rule that fired (an opt-out phrase, "no thanks", a one-word reply, or the
  model's judgement), the words it matched and their message. **Why?** on
  their row in Contacts (or on the conversation) shows it, and **Undo
  opt-out** reverses a mistaken one after a confirm: they become *replied*,
  so nothing is sent on its own, and Harvey's answer to their next reply
  waits for your approval even with `require_approval: false` (on
  Instantly, which can't hold a reply, that conversation comes to you). Opt-outs from before this show their last message instead, and
  every undo is logged.
- **`aiosmtplib` is now 5.1.3 or later** (was `>=3.0.1,<4`). Older versions
  wrote a CR/LF inside an address straight to the SMTP server, so an address
  scraped from a website could add commands of its own
  (GHSA-v3q9-hj7j-63hq), and kept plaintext bytes across STARTTLS
  (GHSA-vxj7-4xrp-5vr4). Harvey passes scraped addresses to it when sending
  over SMTP and when probing a mailbox. Reported in #66.
- A follow-up no longer goes out before, or right after, the email it
  follows. Each step now waits until the previous one has actually been sent
  and its gap (at least a day) has passed. Before, approving a batch of drafts
  late, or resuming after a pause or a compliance hold, could send a whole
  sequence within minutes, or step 2 before step 1. A follow-up whose earlier
  email was rejected or failed is cancelled instead of arriving as a first
  email.
- **Windows:** text files (harvey.yaml, .env, prompts, skills, trained
  output, tokens, exports) are read and written as UTF-8 instead of the
  locale code page, so `harvey train` no longer crashes on characters like
  "★" and prompts aren't garbled; command output is UTF-8 too, so the
  dashboard's log of `harvey run` can't crash on "⚠". An npm install's
  `claude.cmd` shim is resolved to the native `claude.exe` it wraps and is
  never run through cmd.exe, which would re-parse Harvey's isolation flags;
  if the executable is missing, Harvey says how to fix it. Files an older
  Harvey saved in the Windows code page (trained skills, harvey.yaml) still
  load, with a warning to re-save them as UTF-8. `harvey export` CSVs are now
  UTF-8 on Windows too, which sequencers expect (Excel needs Data > From
  Text/CSV to show accents). Based on #18 by Tariz Shahid.
- Replies are read without the email they quote. The model, the stored thread
  and the answer get the reply without the quoted email, and any line that
  repeats what Harvey sent that person is set aside, so Harvey's own words
  (its footer, or an escalation word like "attorney" in its pitch) don't count
  as theirs. Opt-out and escalation words the person wrote are still found
  anywhere, including between or under quoted lines. "No thanks" / "no thank
  you" as a phrase, and a reply of just "STOP", now count as opt-outs.

- The Sender claims each email right before sending it. A reply, bounce, hand
  send or rejection that cancels an approved email while the Sender is working
  through its queue now stops it, instead of the Sender sending its earlier
  copy and recording it as sent. Two Harvey processes running at once can no
  longer send the same email twice. An email cut off mid-send (Harvey stopped,
  or the mail library crashed mid-call) is marked failed and never retried, since
  it may have gone out; it shows under "Didn't send" with a note to check your
  Sent folder, and the dashboard won't offer to email that person by hand.
  Rejecting an email that is already sending or sent is refused instead of
  relabelling it.
- Keep local website requests out of private networks, including redirects and
  DNS rebinding. Preserve the original hostname for TLS verification. Website
  fetching (search, company sites, profiling, the training crawler) now ignores
  `HTTP_PROXY`/`HTTPS_PROXY`, so it can't be routed around that boundary; mail
  and API integrations are unchanged. `harvey train` refuses local or intranet
  URLs and says so.
- **Instantly users: check `require_approval`.** Instantly has no per-email
  approval queue, so with `channels.email.require_approval: true` (the default)
  Harvey now holds campaign deployment and hands replies to a human instead of
  silently bypassing the setting. `harvey run` and `harvey status` say when this
  is happening; prospecting and drafting carry on (drafts wait, using model
  quota, until sending is allowed). Set `require_approval: false` for
  Instantly autopilot.
- Disable LinkedIn by default in the model, sample config, and product trainer;
  save the setup wizard's explicit LinkedIn choice when updating a config.
  Answering "no" to LinkedIn when re-running `harvey setup` now turns it off
  (saved credentials stay in `.env`).
- Protect the local dashboard with an access token, restrict requests to trusted
  local origins, and save settings with owner-only permissions. Update and restart
  Harvey, then open the dashboard link printed in your terminal.
- Isolate Claude calls from personal instructions and hooks, and fix Claude CLI
  installation in Docker. Thanks to @EICCIO-Advisors for the private report.

### Added

- **Send by hand from the dashboard, and a live status panel.** With no
  mailbox connected (or email turned off), each campaign's first email has an
  **Open in Gmail** button that fills in a compose window in the account
  Harvey sends as, and a **Mark as sent** box. Harvey never sends to someone
  marked sent by hand, even if a mailbox is connected later, and only offers
  it for people it could still email (not opted out, replied, bounced, or
  already queued by Harvey). The Today tab gains a panel showing each pipeline
  step, what's running, and what to do next.

- **Outbox review: see what a draft is based on, and edit it.** Each pending
  draft now shows its campaign, the company (name, clickable website,
  description), the contact and how they were found, email status and score,
  tech / agency / hiring / ads / booking signals, and the Writer's notes, so
  every claim can be checked against what Harvey actually found. **Edit** (or
  `E`) turns subject and body into fields; Save rewrites the draft only while
  it is still `pending_review` and never approves it. An edit the sender
  would block (merge tags, HTML, banned phrases) is refused on save with the
  reason; a reply keeps its subject so it stays in the thread; every edit is
  logged with what it replaced. While editing, the shortcuts and Approve all
  can't act on the unedited draft. `POST /api/outbox/{id}/edit` backs it.

- **Microsoft 365 mail provider.** Set `channels.email.provider: microsoft` to
  send and read through a Microsoft 365 / Outlook mailbox via Microsoft Graph,
  for domains whose mail lives in Exchange Online (which no longer accepts
  passwords over IMAP). Signs in once with OAuth + PKCE (`harvey microsoft
  auth`; no client secret, no stored password), drafts then sends so the
  Message-ID is known, threads replies with `createReply`, reads the last 7
  days of the inbox without marking anything read, refuses to act as any
  mailbox other than `persona.email`, and holds sending when the inbox can't
  be read. See [docs/microsoft365.md](docs/microsoft365.md).
- **Optional per-recipient compliance check (Precleared).** Set
  `compliance.precleared.enabled: true` with `sender_country: GB` or `US` to
  have each new recipient checked before the first cold email against UK
  PECR, US CAN-SPAM, Canada's CASL, Germany's UWG and France's rules. Any
  verdict but `ok_to_email` fails that outbox item with the rule and the next
  step. If the check can't be made because of Precleared (daily allowance or
  rate limit, a bad key, network down, an unreadable answer), new addresses
  wait for a later cycle while email to people already cleared, and replies,
  still go out; if it fails for one address, only that email is moved back
  six hours (failures at two different domains with no answer in between
  hold new addresses too), and an address whose check timed out or got an
  answer Harvey can't read is moved back as well, so it can't keep the
  others waiting. One check per address, cached for 30 days; changing the
  sender country, address source or product description triggers a fresh
  check. The receipt id is logged with each send. Native providers only. Off
  by default.
- **`harvey setup` offers the Precleared check** (from #46 by
  gabrielaoimevbore-beep). A new optional last step
  asks whether to turn the check on, where your business is based (GB or US)
  and, if your prospects are all in one country, which one (answering "no"
  there asks before saving it as Norway's code). It then offers a
  free `PRECLEARED_API_KEY` (hidden input, saved to `.env`; 25 checks a day
  instead of 10). Off unless you say yes. Re-running setup offers your saved
  answers as the defaults, so pressing Enter never switches the check off. The
  Claude-guided setup in CLAUDE.md offers the same step. Setup now reads and
  saves `harvey.local.yaml` when it exists (it's what `harvey train` writes,
  and the file Harvey reads), not the `harvey.yaml` it shadows, so its answers
  take effect; if it can't save the file it says so instead of ticking the
  summary.
- **Cohort builder pagination and CSV export.** The cohort table on the
  Signals tab used to stop at the first 50 matches with no way to see the
  rest. It now pages with Prev / Next ("Showing 51–100 of 147"), and an
  Export CSV button downloads the whole cohort. `POST /api/cohort` accepts
  `offset` and `limit` (default 50, max 200) and orders by name so pages never
  overlap; `GET /api/cohort.csv?require=...&exclude=...` returns every match.
  Counts and exports include every match; pages use a stable name/id SQL order.
  CSV downloads carry dashboard authorization and escape spreadsheet formulas.

## [0.3.0] — 2026-10-13

### Added

- **Opt-in Codex brain (ChatGPT plan).** Set `brain.provider: codex` (or pick
  it in `harvey setup`, or `harvey train --provider codex`) to think with the
  Codex CLI on a ChatGPT subscription instead of Claude. Claude remains the
  default and there is no automatic fallback between providers. Each call
  runs a throwaway, isolated `codex app-server` with no tools; Harvey verifies
  the lockdown from Codex's own reports before sending any prompt, aborts on
  any attempted tool use, refuses API-key logins and custom endpoints
  (checked before your login is exposed to Codex), and never changes your own
  Codex settings or login: each call gets a private copy of the login without
  its refresh token, so an expired login must be renewed with Codex itself or
  `codex login`. Requires Codex CLI 0.159.3; other versions are refused. See
  the README section "Using a ChatGPT plan instead (Codex)".
- `brain.model` picks the model for either provider, for Harvey only. For
  Codex use `gpt-5.5` (suggested by `harvey setup`): the verified text-only
  model. Blank auto-selection only picks listed models without code-execution
  tools, and on current ChatGPT accounts there are none; code-mode models are
  refused.
- If Codex can't be used (expired or renewal-due login, unsupported CLI,
  unsafe Codex config, rejected model, plan usage limit), Harvey pauses the
  heartbeat instead of running agents that would fail: replies stay unread
  and unescalated, queued emails wait, and the reason shows in the log,
  `harvey status` and the dashboard. It resumes on its own once the cause is
  fixed. `harvey status --check` runs the same check (no prompt sent) on demand
  and exits non-zero when it fails. The Codex provider supports macOS and
  Linux; it is refused on Windows (use Claude there).

### Changed

- Each heartbeat now sends email after the agents that read and answer
  replies have finished, and native mail providers (Gmail/SMTP) read the
  mailbox every cycle, so a reply always cancels its queued follow-ups before
  that cycle's sends (previously the two could run at the same time, and a
  reply arriving with no open conversation waited for a later cycle). This
  applies to Claude and Codex alike.
- A reply is only recorded once its reply draft (if it needs one) is
  written, so a brain outage part-way never half-records it. If a working
  brain returns no usable draft, the conversation is flagged for a human
  instead of being left open with no answer.
- Usage is tracked per provider. The budget gate, `harvey usage`, `harvey
  status` and the dashboard read and label the active provider's own quota;
  existing usage history is kept and attributed to Claude.

### Fixed

- Dashboard HTML and text assets now use UTF-8 explicitly, fixing Windows
  decoding failures. Regression tests cover HTML, CSS, JavaScript and SVG.

## [0.2.1] — 2026-10-13

A security release. Update if you run Harvey.

### Security

- **Claude now runs with no tools.** Every Claude call used to run with
  `--dangerously-skip-permissions`, and prompts include text Harvey doesn't
  control: prospect email replies and scraped web pages. Instructions hidden
  in that text could make Claude run commands, read or write files, or use
  the operator's connected MCP servers. Calls now run with `--tools ""` and
  `--strict-mcp-config`; Harvey only ever needed text back, so nothing else
  changes. Reported privately by [@hackchang](https://github.com/hackchang).
  If you ran an earlier version with live replies coming in, check the
  account that ran Harvey for unexpected files or activity.

### Fixed

- `harvey run` no longer drops into the setup wizard when the trained config
  lives in `harvey.local.yaml`; the dashboard reads it too.
- A heartbeat cycle is capped at 10 minutes, so one hung agent can't freeze
  Harvey, and quiet hours are re-checked every 5 minutes so a Mac waking from
  sleep resumes on time.
- Sites that only answer on plain HTTP or `www.` are no longer marked
  unreachable.
- Profile and contact backfills rotate through every company instead of
  retrying the same few forever.
- The trainer writes full place names ("Solon, Ohio") and real job titles, so
  discovery doesn't geocode to the wrong city and found contacts aren't
  silently discarded.

### Added

- Contacts are backfilled for companies found without any, from their own team
  pages and, if a key is set, Hunter's named-people search.
- `HUNTER_MONTHLY_SEARCHES` (default 25) sets one Hunter budget shared by every
  domain search.

Thanks to [@sshaw0729-dotcom](https://github.com/sshaw0729-dotcom) for the
fixes above (#2).

## [0.2.0] — 2026-09-13

The release that made Harvey worth running. The previous version could write
and send email but could not reliably find anyone to send it to, and it needed
a paid third-party platform to send at all. Both are fixed.

### Added

- **A prospecting pipeline that works.** Four stages, each independently
  runnable and re-runnable: DISCOVER (who exists) → PROFILE (what they are) →
  ENRICH (who decides) → VERIFY (can you reach them).
- **DISCOVER, with a provider menu.** Four sources behind one adapter
  interface, shown side by side in the dashboard with what each does, what it
  costs, its free tier and which keys it needs. The default — OpenStreetMap —
  needs no account, no card and no key, so the whole pipeline can be run
  before paying anyone. `harvey discover`.
- **Cost discipline in the collector.** Every discovery run estimates before it
  spends, refuses to start over your cap, re-checks the cap between batches,
  reads a kill switch, and flushes observations per batch so a run that dies
  keeps what it learned. `harvey discover --estimate` prints the projection and
  exits.
- **PROFILE.** Free — three HTTP requests per business, no browser, no model
  call. Reads the incumbent agency out of the site footer, ad pixels, missing
  schema, abandoned blogs, open roles and named people. Chained onto DISCOVER
  and run on every heartbeat. `harvey profile`.
- **A signal catalog you confirm.** Harvey knows how to collect 23 signals and
  collects none of them until a human says so. Each shows a plain-language
  description, what it costs, and how many companies already carry it.
  `harvey signals`.
- **The cohort builder.** Pick what a prospect must have and what disqualifies
  them; see the matching companies live. A prospect list is a query you can
  explain, not a file.
- **Native mail providers.** Gmail (REST + OAuth) and any SMTP+IMAP mailbox.
- **The outbox approval ladder.** Every outgoing email waits for approval by
  default (`pending_review → approved → sent`), reviewed one at a time in a
  decisions desk with `A`/`R`/`J`/`K`. A deterministic pre-send gate — no model
  involved — rejects unrendered merge tags, banned phrases, over-length bodies,
  too many links, HTML, undeliverable addresses and recipient mismatches.
- **Bounce handling and a kill switch.** A bounce marks the address invalid,
  cancels that prospect's queued sends, and trips a global pause past a
  configured bounce rate. `harvey sending pause|resume`.
- **Claude usage tracking.** Per-call attribution by agent, task and model, plus
  live subscription-quota gauges read the same way `/usage` does, so Harvey
  throttles itself and leaves headroom for your own interactive work.
  `harvey usage`.
- **CSV export.** `harvey export` writes a sequencer-ready list. Harvey is worth
  running as a list-builder even if you never let it send.
- **A dashboard worth looking at.** Opens on Today — what needs a human — with
  an activity feed and a rail of pipeline figures, collector runs and setup.
  Light and dark are the same design re-tokenised; appearance follows your OS by
  default. Fonts are vendored so it renders with the network off.
- **A Python version guard.** `python3 -m venv` on macOS builds from the system
  3.9, which cannot run Harvey. It now fails immediately with the fix instead of
  surfacing later as an unrelated-looking ImportError.

### Changed

- **The data model.** Every fact Harvey learns is now an observation — a row in
  `observations`, never a column. A new signal needs no migration, re-observing
  over time is a free time series, and confidence and provenance travel with the
  fact. The vocabulary is governed: a database trigger rejects any signal code
  not in `signal_codes`.
- **Email finding is pattern-first.** Harvey derives the domain's address format
  and verifies one candidate rather than brute-forcing name variations, and
  grades every address honestly as `verified` / `risky` / `guess` / `invalid`.
  Only deliverable addresses are ever sent to.
- **The default mail provider** is now `gmail`. Instantly is still supported as
  a legacy option.
- **Discovery depth defaults to 30, not 100.** Google removed
  100-results-per-page in September 2025, so depth 100 is now billed as ten
  pages nearly everywhere — and below rank 30 it is mostly directories.
- **The dashboard's HTML, CSS and JS** are real files in `harvey/web/` instead
  of a 1,500-line string literal in `dashboard.py`. Still no build step.

### Fixed

- **Guessed emails were being reported as verified** whenever the domain merely
  had an MX record. The migration downgrades every legacy `email_verified=1` row
  to `guess`.
- **Cohorts counted the wrong companies.** PROFILE records a false boolean when
  it checks and finds nothing — which is correct — but `cohort()` matched on the
  row existing, so "companies running Google Ads" silently meant "companies we
  checked for Google Ads", which was all of them. Cohorts now read the newest
  observation per company and signal, and require it to be true.
- **Usage tracking counted every Claude session on the machine**, not just
  Harvey's. The transcript reconciler is gone; the ledger contains only rows
  Harvey's own Brain wrote.
- **Page titles were being extracted as people** ("Request Service", "Commercial
  Services"), and titles bled between team members.
- **Confirming a signal before anything had loaded the catalog** reported success
  while changing nothing.
- **The free OSM provider answered unmapped search terms with every registered
  office in the area** — coffee roasters and churches for a roofing query. It now
  refuses the query and names the trades it can search.

### Security

- Test fixtures no longer carry real businesses' phone numbers or street
  addresses.
- `harvey.local.yaml` (gitignored) overrides the tracked `harvey.yaml`, so a
  fork can hold real product configuration without it reaching a public repo.

## [0.1.0]

Initial release: the heartbeat loop, five sub-agents, the skills library, the
website trainer, Instantly-based sending, and the first dashboard.
