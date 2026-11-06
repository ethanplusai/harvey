# Legal and deliverability

## Legal and deliverability

Sellbird automates outreach, but **you are the sender.** Cold email is legal in most places when done right and expensive when done wrong — CAN-SPAM penalties run to $53,088 per email. Sellbird ships with compliant defaults. Keep them.

**CAN-SPAM (US).** Truthful subject line and accurate from-address — Sellbird's copywriting rules forbid fake "re:" threads and impersonation. A working opt-out, honored fast: every cold email ends with an opt-out line (`compliance.opt_out_line`, default *Not interested? Reply "no thanks" and I won't email again.*), and Sellbird treats any opt-out wording as immediate and permanent, wherever in the reply the person wrote it (including a bare "STOP" or "No thanks"). If the same reply threatens legal action or complains ("stop emailing me, my attorney will be in touch"), the opt-out still stands and the conversation goes to you as well. Nothing Sellbird does later moves someone off *opted out*: a later message from them is kept for you to read, never answered, and a complaint still goes to you. To see why someone was opted out (the rule that fired, the words, and their message), click **Why?** on their row in Contacts. If it was a mistake, **Undo opt-out** there marks them as replied: Sellbird sends nothing on its own, and their next reply gets a draft for your approval. Your physical mailing address in the footer: set `compliance.postal_address` in `sellbird.yaml` before your first campaign. Sellbird adds both lines under every cold email when it sends (replies don't get them), shows them under each draft in the Outbox, and warns on `sellbird run`, `sellbird status` and the Outbox tab until the address is set. Text in a reply that repeats what Sellbird wrote for that person (its emails, campaign templates, and every footer it has used) is set aside as quoted, not read as their words, so quoting Sellbird's own footer isn't an opt-out; the model also reads replies without the quoted email. The opt-out line must ask for a reply Sellbird recognises (`Reply "no thanks"`, `Reply STOP`, also "unsubscribe", "remove me", "opt out") and can't contain a link or email address. Hebrew works the same way: a line such as `השיבו "הסר"` or `תגידו לי "לא תודה"` is accepted, and Sellbird catches Hebrew opt-outs without the model: a bare "הסר", "תורידו אותי מהרשימה", "תפסיקו לשלוח", "אל תכתבו לי", "לא מעוניין, תודה". A Hebrew legal threat or complaint ("עורכת הדין שלי", "אתבע אתכם", "תלונה על ספאם", "חוק הספאם") comes to you the same way, with no draft, while "תביעות ביטוח" or "סליחה על ההטרדה" are read as ordinary replies. Spanish too: `Responda BAJA` or `Responda "no, gracias"` is accepted, and Sellbird catches "no, gracias", a bare "BAJA", "denme de baja", "no me escriba más", "dejen de enviarme correos", "sáquenme de su lista" and "no me interesa" said on its own, but not "¡cómo no, gracias!", "queremos dar de baja a nuestro proveedor actual" or "no me interesa el plan anual, pero sí el mensual", which go to the model. Spanish legal threats aren't matched by rule yet; the model reads those.

**Do not contact.** List competitors, existing customers, or anyone else Sellbird must never email under `compliance.do_not_contact`, one address or domain per line, in the config file Sellbird reads: `sellbird.local.yaml` if you trained Sellbird (that file wins), otherwise `sellbird.yaml`. A domain covers its subdomains (`acme.example` also blocks `mail.acme.example`, not `notacme.example`), an address covers its plus-tags (`jane@` also blocks `jane+news@`), and a pasted link such as `https://www.acme.example/about` or `Jane Doe <jane@acme.example>` counts as what it names. An entry that is neither an address nor a domain stops Sellbird from starting, with the entry named, rather than quietly protecting nobody. A running Sellbird picks up an edit at once, and the send gate checks the list as each email leaves, cold or reply, so it holds even for an email you already approved. If someone on the list writes back, Sellbird doesn't draft an answer: the conversation comes to you. `sellbird export` and the dashboard's Export buttons leave them out too. On Instantly, listed leads are left out of the upload and a reply to one comes to you, but Sellbird can't take back a lead Instantly already has: remove it there.

**GDPR / PECR (EU & UK).** B2B cold email needs a defensible legitimate interest: the pitch must be genuinely relevant to that person's role, you must know where the data came from, and objecting must be effortless. If you can't say why a specific person would care, Sellbird shouldn't email them — and its qualification rules say so.

**Per-recipient check (optional).** Those rules turn on facts about each recipient: a UK sole trader needs consent where a limited company doesn't, Germany wants prior consent even for business email, and Canada's CASL depends on whether the address was published. With `compliance.precleared.enabled: true`, Sellbird asks [Precleared](https://precleared.dev) about each new recipient before the first cold email. Anything other than `ok_to_email` holds that email back, with the rule and the next step shown in the Outbox, and the signed receipt id is logged with every send. One check per address, cached for 30 days (changing the sender country, address source, product description or the country sent with it triggers a fresh check); replies are never checked; native providers (Gmail, Microsoft 365, SMTP) only. Sellbird tells Precleared the recipient's country when it is sure of it from the company's location (a country, a US state, a Canadian province or an Australian state named there, as for the send window), and Precleared takes that as settled; otherwise Precleared works the country out from the address, company records or the company's website. If all your prospects are in one country, also set `prospect_country` (for example `US`), which Precleared uses only when none of those show it; without it, the one country every `icp.geography` entry is in serves as that default. Precleared receives the recipient's address, your sender country, the address source, your product description and the recipient's country when Sellbird is sure of it, else your prospects' country if there is one. It also receives the company's name and the person's name when Sellbird has them, which it uses to find a UK company on the Companies House register when the domain alone doesn't (it doesn't store the person's name). Without a key it allows 10 checks a day. A free key (`PRECLEARED_API_KEY`) allows 100 a day for its first 14 days, then 25, and `sellbird setup` or `sellbird precleared-key` gets one with just your email address. Put the setting in `sellbird.yaml`, or in `sellbird.local.yaml` if you trained Sellbird, since that file wins. [docs/precleared.md](precleared.md) has the details, verdict by verdict.

**If the check is held up.** When Precleared can't answer (the day's free checks are used up, the key is out of checks, was rejected or is being rate limited, or Precleared can't be reached), cold email to new addresses waits and replies still go out; email to people Precleared has already cleared keeps going. If Precleared can't check one address, that email is tried again six hours later and the rest carry on, unless that happens at two different domains with no answer in between, which holds new addresses as above. The Today page and `sellbird status` say how many cold emails are waiting on the check and what fixes it.

**Bot disclosure.** Some jurisdictions require disclosing automation. Sellbird is instructed to answer truthfully, always, if a prospect asks whether they're talking to an AI. Never configure it otherwise.

**LinkedIn.** Browser automation violates LinkedIn's ToS and can get the account restricted. Off by default. If you turn it on, use an account you can afford to lose.

LinkedIn requires both `channels.linkedin.enabled: true` and configured
credentials. Training a product does not enable it; the setup wizard records
your explicit choice.

**Deliverability — warm up or burn out.** Sending cold email from your main domain, or at volume on day one, lands you in spam permanently.

1. Buy a **dedicated sending domain** (`getacme.com`, not `acme.com`).
2. Set up **SPF, DKIM and DMARC** on it. Without all three, Gmail junks you. `sellbird warmup` checks them, plus domain age, blocklists, your warmup progress and bounce rate, and ends with READY / NOT READY. It only reads public DNS and Sellbird's own records.
3. **Warm up for 2–4 weeks** before real volume, and keep warmup running after. We recommend [TrulyInbox](https://trulyinbox.com/?via=sellbird); see [Warmup with TrulyInbox](#warmup-with-trulyinbox).
4. **Ramp slowly** — 10–20/day per inbox, adding ~5/day. The `max_daily_sends: 50` default is a ceiling, not a target. Set `channels.email.warmup.start_date` and Sellbird does this for you (Gmail, Microsoft, SMTP; Instantly has its own warmup): no cold email for `min_days`, then a cold-email cap that climbs by `daily_increase` a day. Replies always go out.
5. **Watch bounces.** Above ~3%, stop and fix list quality. Sellbird trips its own kill switch past your configured threshold.

*None of this is legal advice. Sending at scale or into regulated industries? Talk to a lawyer.*

### Warmup with TrulyInbox

Whether a mailbox is warmed isn't something DNS can tell you: only the warmup
service sees where its mail lands. We recommend [TrulyInbox](https://trulyinbox.com/?via=sellbird), and Sellbird
connects to it so `sellbird warmup` can say whether your mailbox is actually ready:

1. **Get TrulyInbox** at [trulyinbox.com](https://trulyinbox.com/?via=sellbird) and add your sending mailbox.
   Connecting Sellbird needs API access, which starts on the Starter plan.
2. **Create a key.** In TrulyInbox, open Settings > API keys and create a
   workspace key with `accounts:read`, `warmup:read`, `analytics:read` and
   `workspace:read`.
3. **Connect it**: `sellbird trulyinbox connect`, or paste it into the warmup step
   on the dashboard's Today page. Sellbird checks the key before saving it to `.env`.

`sellbird warmup` then reads the mailbox from TrulyInbox and calls it ready only
when it's enrolled, signed in, has warmed for `min_days` (14 by default), and
less than 5% of its warmup mail landed in spam. Sellbird only reads: it never
enrolls, pauses or changes anything in your TrulyInbox account.

Already warming with another service? `sellbird trulyinbox other` (or *I use
another service* on the dashboard) and Sellbird stops suggesting it.
