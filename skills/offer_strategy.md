# Offer Strategy & Closing Mechanics

## Core Rule: Earn the Right to Ask

Never lead with an offer. Never pitch in a cold email. The first outreach exists to start a conversation — not to close a deal.

Offers are only presented after a prospect has shown genuine interest:
- They replied positively
- They asked a question about the product
- They expressed a pain point you can solve
- They asked about pricing or next steps

If none of these have happened, you are still in the "earn attention" phase. Stay there.

## The Offer Ladder

Not every interested prospect gets the same offer. Match the offer to their level of engagement:

| Engagement Level | Signal | Offer |
|-----------------|--------|-------|
| **Curious** | Asked a question, clicked a link | Answer their question + soft CTA ("Happy to walk you through it if useful") |
| **Interested** | Expressed a pain point, asked about features | Share a relevant resource or case study + suggest a quick call |
| **Ready** | Asked about pricing, timeline, or next steps | Present the specific offer with clear next step |
| **Urgent** | Has a deadline, lost their current vendor, hiring | Move fast — present the offer immediately with urgency framing |

## Crafting Offers from Product Knowledge

When Harvey learns about a product (via website crawl or manual training), it should identify:

1. **Primary offer**: What's the main thing we're selling? (e.g., software subscription, service engagement, audit/assessment)
2. **Entry offers**: Lower-commitment ways to start (e.g., free trial, pilot program, free audit, strategy session)
3. **Proof offers**: Ways to demonstrate value before asking for money (e.g., sample report, free assessment, ROI calculator)

### Offer Construction Framework

For each product, determine:
- **What they get**: Specific deliverable or access (not vague "value")
- **What it costs**: Price, time commitment, or "free"
- **What they risk**: Nothing (free trial), money-back guarantee, or month-to-month
- **What happens next**: The exact mechanic — click a link, reply with times, fill out a form

### Example Offer Structures

**SaaS Product:**
- Entry: "We have a 14-day free trial — no credit card needed. Want me to set one up for you?"
- Primary: "Plans start at $X/mo. Want me to walk you through which tier makes sense for your team?"

**Service/Agency:**
- Entry: "We usually start with a free 30-minute audit of your current setup. Worth doing?"
- Primary: "Our typical engagement starts at $X/mo. I can put together a custom scope if you share more about what you're working with."

**Consulting:**
- Entry: "I put together a quick analysis of [their specific situation]. Want me to send it over?"
- Primary: "We do this as a [fixed/retainer] engagement. Want to jump on a 20-minute call to see if it makes sense?"

## Closing Mechanics: How to Book

The way you ask someone to take the next step matters. Always confirm with the user which mechanic to use:

### Option 1: Calendar Link
Best when: High volume, self-service feel, user has a scheduling tool set up.
```
"Here's my calendar if you want to grab 15 minutes: [LINK]"
```
- Use when the product config includes a `booking_url`
- Always specify the time commitment ("15 minutes", "quick 20-min call")
- Never say "book a demo" — say "grab time" or "jump on a call"

### Option 2: Suggest Specific Times
Best when: More personal feel, higher-touch sale, no calendar tool.
```
"Would Thursday at 2pm or Friday morning work for a quick call?"
```
- Offer exactly 2-3 options
- Always include day + time
- Keep the window narrow (same week)

### Option 3: Ask for Their Preference
Best when: Enterprise prospects, respecting their schedule, senior decision-makers.
```
"What does your calendar look like this week for a quick 15 minutes?"
```
- Deferential but still time-boxed
- Good for C-suite where you don't want to seem presumptuous

### Option 4: Next Step Without a Call
Best when: Product sells itself, prospect just needs access.
```
"Want me to set up a trial account for you? Takes 2 minutes."
```
- For self-serve products with free trials
- Skip the call if the product can speak for itself

## What Harvey Should Confirm with the User

During setup or training, Harvey needs to know:

1. **What's the primary offer?** (subscription, service, trial, audit, etc.)
2. **Is there an entry offer?** (free trial, free audit, sample, etc.)
3. **What's the goal of outreach?** (book a call, start a trial, get a reply, etc.)
4. **How should meetings be booked?**
   - Calendar link (provide URL)
   - Suggest times
   - Ask for their preference
5. **Who takes the meeting?** (the user, a sales team member, Harvey books and someone else shows up)
6. **Any qualification before booking?** (company size, budget, specific need)

These answers go into `harvey.yaml` under the product config and inform how Harvey writes emails and handles replies.

## Offer Timing in the Sequence

### Cold Email Sequence (3 emails)
- **Email 1**: No offer. Start a conversation. Ask a question. Share an insight.
- **Email 2**: Soft mention only if relevant. "We help teams like yours [do thing]. Worth a conversation?"
- **Email 3**: Break-up email. Slightly more direct but still no hard pitch. "If this isn't a priority right now, no worries. But if [pain point] comes up, I'm here."

### Reply Handling (after interest)
- **First interested reply**: Acknowledge, answer their question, then present the entry offer or suggest a call.
- **Follow-up after interest**: If they went quiet after showing interest, one gentle nudge. Never more than one.
- **Ready to book**: Present the specific closing mechanic. Be direct. "Let's get something on the calendar."

## Language Rules

**Do say:**
- "Worth a quick conversation?"
- "Happy to walk you through it"
- "Would it make sense to grab 15 minutes?"
- "Want me to send over [specific thing]?"

**Never say:**
- "Book a demo" (sounds like a sales pitch)
- "Schedule a call with our team" (impersonal)
- "I'd love to show you" (self-serving framing)
- "Let me know if you're interested" (passive, no clear next step)
- "When are you free?" (too open-ended)

## Handling "What's the Price?"

When a prospect asks about pricing before you've qualified them:

1. Give a real answer — never dodge the question
2. Frame it with context: "Plans start at $X/mo, depending on [variable]. Most teams your size are on the [tier] plan."
3. Pivot to qualification: "To give you an exact number, it'd help to know [specific question about their needs]."
4. If there's a free entry offer, lead with that: "There's a free trial so you can see if it fits before committing to anything."
