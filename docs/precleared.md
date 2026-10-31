# The cold-email check (Precleared)

Before the first cold email to anyone new, Harvey can ask
[Precleared](https://precleared.dev) whether cold email may go to that
address. The answer turns on facts the address does not show: a UK sole
trader needs consent where a limited company does not. Precleared looks them
up live (Companies House, the domain, the website) and applies the rules of
the recipient's country.

Off by default. One check per address, for mail sent through Gmail,
Microsoft 365 or SMTP. Instantly campaigns and replies are never checked.

## Turning it on

In `harvey.yaml`, or `harvey.local.yaml` if you trained Harvey (that file
wins):

```yaml
compliance:
  precleared:
    enabled: true
    sender_country: "GB"        # GB or US
    address_source: "unknown"   # unless copied from the businesses' own sites
    prospect_country: ""        # e.g. "US" if all prospects are in one country
```

Quote `prospect_country` (YAML reads an unquoted `NO` as false).
`harvey setup` asks the same questions.

## What is sent

One request per new address: the address, `sender_country`,
`address_source`, `existing_customer: false`, the first 200 characters of
`product.description`, and the recipient's country, in one of two ways:

- **`recipient_country`**, when Harvey is sure of it. That is a country
  named in the company's location ("Oslo, Norway", "London, UK"), a US
  state, Canadian province or Australian state named there ("Austin, TX",
  "Toronto, ON"), or a code that is a state and a country too ("Berlin, DE")
  once the coordinates discovery cached for that place settle it. Precleared
  takes it as settled, so a reading Harvey is not sure of is never sent: that
  same code with no coordinates (Delaware by convention), a city on its own,
  or coordinates alone. A stored time zone alone never establishes the
  country, because it may come from approximate or stale location data.
  Coordinates far from both readings of an ambiguous code do not settle it.
- **`default_recipient_country`**, otherwise, if there is one:
  `prospect_country`, or the one country every entry in `icp.geography` is
  certainly in ("Austin, TX" and "Denver, Colorado" give `US`; "Denver, CO"
  on its own does not, since `CO` is Colombia's code too, unless
  `icp.geo_coordinates` has it). Precleared uses a default only when its own
  lookups show no country.

Never both.

Also, when Harvey has them, the company's and the person's names:

- **`company_name`**, the company record's name, else the prospect's own
  company field.
- **`recipient_name`**, the prospect's first and last name. A company inbox
  (info@, hello@) has no person, so no name goes with it.

For a UK recipient, Precleared searches the Companies House register by the
company's name when the domain alone does not find the company, and checks
the person's name against the company's officers. A company found that way
counts only when other evidence ties it to the recipient, such as an officer
with the person's name or the company's own website naming it. Without a
company, a UK address can't be told from a sole trader's, so the answer is
`consent_needed`; the names turn many of those into a confirmed company.
Precleared does not store the person's name. Both are cut to Precleared's
limits (160 and 120 characters).

The key goes as a bearer header. Not sent: the subject, the body, the
person's title, or the company's location itself (only the country read
from it).

## What each verdict does

- **ok_to_email.** The email sends. The receipt id is logged with every send
  (`precleared_receipt` in the action log).
- **consent_needed, blocked.** The outbox item is marked failed, with the
  rule, its source and the next step as the reason. Its follow-ups are
  cancelled, and a later first email to the same address meets the cached
  verdict, with no second check.
- **unknown.** A fact could not be established, usually the recipient's
  country. The item fails the same way, but the verdict is not cached, so a
  re-queued address is asked again.

## When the check is held up

If Precleared cannot answer (free checks used up, key out of checks or
rejected, or it cannot be reached), new checks wait for a later cycle.
Valid cached verdicts still apply, using the same recipient-country facts
as a fresh check. Replies still go out. Address-specific failures postpone
that email six hours; failures at two distinct domains without a fresh answer
pause further checks. The Today page and `harvey status` say how many cold
emails are waiting and what fixes it. The next answered check clears the
notice, and so does switching the check off.

## Keys and allowances

| Key | Checks a day |
|---|---|
| None | 10, shared per public IP address |
| Free key | 25 |
| Any new key, first 14 days | 100 |

Beyond that a key draws on a prepaid pack, and Harvey shows the checkout
link when one runs out. `harvey precleared-key` gets a free key for your email
address and saves it in `.env` as `PRECLEARED_API_KEY` without showing it;
`harvey setup` offers the same. Precleared receives that address to make the
key. You can also get one at
[precleared.dev/get-key](https://precleared.dev/get-key) and add it yourself.

## Caching

A definite verdict is kept for 30 days under `precleared:<address>`, with a
fingerprint of the sender country, address source, product description, the
country sent with it (the recipient's when Harvey was sure, else the default
in use) and the names sent with it. Change any of those and the next email
gets a fresh check. A three-step sequence costs one check. An address Harvey
later becomes sure of gets one fresh check, unless Precleared had already
found that same country itself, in which case the cached verdict stands. When
the names first come with an address whose verdict was cached without them,
an `ok_to_email` stands, since the names only help Precleared find the
company, and anything else gets one fresh check with them. The country
Precleared found is kept with the verdict and feeds the send window.

## FAQ

**Does it work with Instantly?** No. Instantly deploys whole campaigns, so
Harvey cannot hold single emails there.

**Why do I get unknown?** Usually the recipient's country could not be worked
out, and Harvey was not sure of it either (see "What is sent"). If your
prospects are all in one country, set `prospect_country`.

**Why do so many UK addresses come back consent_needed?** Often Precleared
could not find the company behind the domain on the register, so it could not
rule out a sole trader. Harvey sends the company's and person's names to help
it; a company record with the business's real name helps most.

**Can I prove a check was made?** Each send logs the receipt id, and
Precleared can show the signed receipt for that id later.
