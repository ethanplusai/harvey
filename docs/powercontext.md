# Embedded customer memory

Harvey optionally runs PowerContext inside its lifecycle, using durable SQLite
and its existing Brain. No PowerContext server, service URL, access token,
second model login or embedding account is required. Memory is disabled by default.

## Install and enable

From the Harvey checkout:

```bash
python -m pip install -e '.[powercontext]'
```

The extra installs published `powercontext[builtin]==1.4.0` and `pycasbin`, which
that release's embedded initialization imports. A default Harvey installation
does not import the optional SDK or create its database.

Add to `harvey.local.yaml`:

```yaml
powercontext:
  enabled: true
  namespace: my-harvey-workspace
```

Enabled memory defaults to `memory_families: [profile, topic]`:

| Family | Purpose |
| --- | --- |
| `profile` | Stable customer/contact background and communication preferences supported by Sources. |
| `topic` | Evolving business projects, including goals, requirements, progress and decisions. |
| `atomic` | Optional independently extracted facts and constraints. |

Select `[profile, topic, atomic]` to add Atomic Memory, or `[atomic]` to retain
Atomic-only mode. Each family processes the same original Sources using its own
cursor. Topic and Profile are not summaries of Atomic output. Enabling more
families adds model work; all three are not required for the default setup.

The default database is `powercontext.db` beside Harvey's database. Optional
`database_path` selects another persistent file; relative paths resolve against
the Harvey database directory. Back up both files. `:memory:` is rejected.
Run one Harvey process per data directory.
Existing local Sources can be processed by newly enabled families.

## Model ownership and processing

The heartbeat opens one provider and shares it with Writer and Handler. Profile,
Topic, optional Atomic generation and email drafting use the same Brain object,
provider, model, reasoning settings and login. No credentials are copied into
memory configuration. Usage appears in Harvey's ledger under `agent=powercontext`:
`generate_profile`, `topic_*`, and, when selected, `extract_memory` / `reconcile_memory`.

Profile uses a standalone Harvey customer-profile prompt with the SDK's typed
generation contract. It retains supported identity, responsibilities, organization
and lasting preferences, including their explicit business context; project-specific
requirements, progress and action items belong to Topic. It also removes project
material from an existing Profile while preserving lasting responsibilities such
as owning POCs. These instructions replace the generic Profile prompt rather than
appending to it. They are model guidance, not a deterministic semantic filter.
The prompt is defined in the adapter; Harvey does not expose managed Prompt
customization. Changing it does not automatically rebuild existing Profiles:
incremental generation applies the instructions when new Sources arrive.
Topic uses all seven official stages and PowerContext's supervised processing,
fencing, Source cursor and
atomic publication. Its host-supplied launcher runs a cooperative asynchronous
task in the Harvey process. It does not provide an OS process sandbox. Shutdown
cancels the task and waits for model/resource cleanup; the SDK retains ownership
if cooperative termination cannot finish in time.

Topic stage calls disable hidden Brain retries to match their one-request budget.
The adapter applies the SDK input budget before inference and rejects estimated
over-budget output before publication. Brain has no provider output-token limit
parameter: this is not a hard provider billing cap. The 32768-token local work
budget is not a claim about the selected model's context capacity.

`timeout` defaults to 300 seconds and bounds synchronization/preparation work,
including processing-lock waits, lazy initialization, generation, readiness and
retrieval. A short database operation registers delivery intent before this timer;
cancellation waits for that registration to commit, then propagates. Startup and
Topic workers also have bounded lifetimes. Harvey's outer cycle timeout still
applies. Inference adds latency and usage to the existing model account.

## Evidence, profiles and project topics

Only saved incoming customer messages become Sources. Each full message retains
its conversation, position, timestamp and stable event identity. The adapter does
not save unsent drafts or model guesses as customer evidence, and does not copy
all CRM records, attachments or outgoing promises. Prospect fields such as role
or company are not automatically converted into Profile facts; an authoritative
CRM Source adapter would be separate work.

Each namespace/contact ID has an isolated durable Scope. Profile maintains stable
information for that contact; a temporary POC requirement must not become an
unconditional long-term preference. Topic generation separates business projects
and evolves their revisions with Source lineage. Contacts at one company do not automatically share
information or become a single company Profile.

Before generating a reply, Handler captures the conversation's saved incoming
messages and finishes processing the selected families. The current incoming
message is already present directly in the reply prompt and is captured after
Harvey saves the conversation. Profile policy is created once with automatic
activation; existing disabled or review-required policies are not overwritten.

Topic flush acceptance only means work was queued. The adapter snapshots the
Source frontier and waits for the committed Topic cursor to reach it; a window
that produces no useful Topic can still complete. Profile flush similarly checks
cursor progress and rejects incomplete/conflicted states. All selected families
must finish before a pending capture is acknowledged.

Harvey's pending queue stores conversation IDs and registration versions, not
message bodies. Registration precedes Runtime work, so failures before Source
creation can recover. Successful attempts acknowledge only their own versions,
preserving concurrent new deliveries. Replayed Sources are idempotent; processed
windows are not re-extracted after restart. Capture still visits the supplied
conversation's messages; it does not scan the full CRM for historical backfill.

## Reply context and retrieval limits

Context preparation requests the current Profile and up to three relevant Topics.
An optional Atomic section adds up to three facts, or six in Atomic-only mode.
Topic results contain their title, summary and matching detail snippet, rather
than every full project document. Exact artifact/revision references accompany
the evidence. `max_bytes` defaults to 8192 UTF-8 bytes, configurable from 512 to
32768. The SDK shares this total across sections and caps each displayed item at
2000 bytes, marking truncation. A large Profile or Topic may be incomplete.

Profile is read independently of query matches. Topic/Atomic recall uses SQLite
full-text search. With Topic enabled, the SDK recall-sufficiency policy permits
one bounded expansion of lexical admission for weak matches. It does not increase
the item count or byte budget and adds no inference call. Its local probes may
scan contact-scoped records; large-history cost has not been benchmarked. It is
not semantic or cross-language retrieval and does not guarantee complete recall.

Prepared content is wrapped in Harvey's untrusted-data fence and presented as
historical evidence, with instructions to keep internal references out of customer
email. Multiple retrieved Topics retain their project
identity; ambiguous requests still need clarification rather than mixing their
requirements. Writer uses private memory only for individual personal emails,
not shared campaign templates.

## Failure and approval behavior

Enabled memory that cannot initialize stops startup visibly. A preparation failure
before generation leaves the incoming message retryable and blocks that reply.
Failures must not become apparently successful empty context.

If synchronization fails after the conversation is saved, Handler marks it for
human attention. Native mail keeps the draft pending review even with automatic
approval; the legacy direct-send path does not send it. Pending delivery intent
and Sources remain available for recovery. Opt-outs and escalation handling do
not wait for memory processing.

A memory failure during first-touch personalization pauses the campaign to prevent
Sender from substituting a generic template. Restore memory, regenerate and review
missing personal drafts before resuming. Do not resume an incomplete campaign
directly into sending.

## Verification

The `tests/test_powercontext*.py` suite covers real SQLite/SDK processing with
deterministic generation decisions: contact and project separation, Profile
updates, Topic revisions, short follow-ups, restart/idempotency, delivery recovery,
supervisor cancellation, model bridge validation, untrusted memory evidence and
host mail policy.
