---
name: cold-email-playbook
description: Sellbird's rules for writing cold emails, follow-ups and replies that sound like a person, start conversations and stay out of spam folders. Use whenever you write or edit outreach copy for Sellbird (campaigns_create, campaigns_add_prospects, replies_draft, outbox_edit_draft).
---

# Writing cold email for Sellbird

The goal of a cold email is to start a conversation, not to close a deal. Every email
you write waits in the Outbox for the customer's approval, so write the version you
would be proud to have them read.

Before writing, know what the customer sells: run `setup_status` (the product step) or
ask them. Every claim must trace back to what they told you or what Sellbird stores.
If it isn't there, don't say it.

## The rules

1. Never use em dashes. Use periods or commas.
2. Never use: "picture this", "here's what I keep seeing", "imagine if", "what if I told
   you", "in today's fast-paced world", "game-changer", "unlock", "supercharge",
   "elevate", "seamless", "leverage", "delve", "navigate the landscape".
3. No before/after lists, no feature dumps. Each email makes one point.
4. Plain text only: no bullets, bold, italics or formatting.
5. Never say "worth a quick look/chat/call?". Ask a real, specific question.
6. Never say "I'd love to", "just wanted to", "reaching out because", "just following
   up", "hope this finds you well", "I know you're busy".
7. Never start an email with "I" or "We". Start with them.
8. No exclamation marks.
9. Email 1 under 75 words, email 2 under 75, email 3 under 40. Count them.
10. Email 1 doesn't pitch or name the product. It makes an observation and asks a question.
11. Subject lines: lowercase, 2 to 5 words, no name, never "quick question".
12. Exactly one question or call to action per email.
13. No "no pressure", "no obligation" or "no contracts" in email 1.
14. Each campaign must feel different in tone, structure and angle, not the same
    template with a new pain point.

## Sounds-human checks

Reread every email and rewrite any sentence that does one of these:

- "Not X, but Y" contrasts, including across two sentences, or "less X, more Y". Just say Y.
- One-line dramatic closers ("That's the gap."). End on the question.
- Sayings that sound deep and say nothing.
- A run-up before the point ("Here's the thing:", "The truth is"). Start with the point.
- Forced groups of three when one or two would do.
- Stacked qualifiers and hyphenated buzz-pairs ("always-on, done-for-you").
- Inflated language ("transform", "a whole new level", "crucial", "powerful", "robust").
- "..., helping you grow" riders. Cut them.
- Curly quotes. Straight quotes only.
- Every sentence the same length. Mix short and long.

## Spam filters

Never use: "free", "guarantee(d)", "risk-free", "no cost", "act now", "limited time",
"exclusive deal", "special offer", "discount", "% off", "winner", "congratulations",
"urgent", "don't miss", "buy now", "click here", "sign up now", "make money",
"increase sales", "double your", "100%", "$$$". No ALL CAPS words. At most one
question mark. No links in email 1, at most one link in a sequence. Never a fake
"Re:" or "Fwd:", never a made-up prior conversation.

## Personal, and true

- Email 1 has at least one specific, verifiable detail about the person: their role's
  real day-to-day problem, their industry's current pressure, or their personalization
  notes. When notes exist, they are the hook.
- Never invent funding rounds, mutual connections, posts they wrote, or case-study numbers.
- Never promise results or numbers the customer didn't give you.
- Merge fields sit naturally mid-sentence. Sellbird fills `{{first_name}}`,
  `{{last_name}}`, `{{company}}`, `{{title}}`, `{{personalization}}` and
  `{{sender_name}}`; any other merge field is refused.

## Writing for many people at once

With `campaigns_create`, write a shared template for each step, then give the people
who deserve it their own email in `emails` (`{prospect_id, step, subject, body}`). A
person-specific email that breaks a rule is skipped with the reason; fix it and add
that person with `campaigns_add_prospects`.

## Replies

When answering a reply (`replies_draft`): answer their question first, in their
register, in under 80 words, with one next step. Never argue with a no. If they ask
not to be contacted, don't answer: call `suppressions_add` and
`outcome_record` with `not_interested`.

More frameworks and examples: [references/frameworks.md](references/frameworks.md).
