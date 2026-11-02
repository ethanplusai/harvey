# Sending from a Microsoft 365 mailbox

Harvey can send and read mail through a Microsoft 365 / Outlook mailbox
(Exchange Online) using Microsoft Graph. Use it when your sending domain's
mail lives in Microsoft 365.

## Why not SMTP + IMAP?

Exchange Online no longer accepts a password for IMAP, and SMTP AUTH is off
by default in most tenants. The `smtp` provider can sometimes still send, but
it can't read the inbox. Harvey has to read replies to stop sequences, honor
opt-outs and catch bounces. If it can send but can't read, it keeps emailing
people who already answered.

The `microsoft` provider signs in with OAuth (authorization code + PKCE)
instead. Harvey never stores a password or a client secret.

## Setup

### 1. Register an app in Entra ID

At [entra.microsoft.com](https://entra.microsoft.com), go to
**Applications → App registrations → New registration**:

| Field | Value |
|---|---|
| Name | `Harvey` |
| Supported account types | *Accounts in this organizational directory only* |
| Redirect URI, platform | **Public client/native (mobile & desktop)** |
| Redirect URI, value | `http://localhost:8766` |

From the app's **Overview**, copy the **Application (client) ID** and the
**Directory (tenant) ID**.

Don't create a client secret. Harvey is a public client.

### 2. Configure Harvey

`.env`:

```
M365_CLIENT_ID=<Application (client) ID>
M365_TENANT_ID=<Directory (tenant) ID>
```

`harvey.yaml` (or `harvey.local.yaml`):

```yaml
persona:
  email: "you@yourdomain.com"   # must be the mailbox you sign in with
channels:
  email:
    provider: "microsoft"
```

### 3. Sign in once

```bash
harvey microsoft auth   # opens the browser; sign in as persona.email
harvey microsoft test   # → ✓ Connected as you@yourdomain.com
```

Harvey asks for four **delegated** permissions:

| Permission | Why it's needed |
|---|---|
| `Mail.ReadWrite` | read replies, and create drafts (reply threading) |
| `Mail.Send` | send them |
| `User.Read` | check which mailbox the token belongs to |
| `offline_access` | keep working without signing in again |

The refresh token is stored in `data/m365_token.json` (gitignored). On macOS
and Linux the file is mode 600 (owner only). Windows has no such mode bits:
the file is protected only by the access permissions (ACLs) of the folder
Harvey lives in, so keep Harvey in a folder only you can read.
Microsoft rotates it on every use, and Harvey saves the new one each time.

### If you see "Need admin approval"

Your tenant doesn't let users consent to apps themselves. An admin has to
approve the app once:

1. In Entra, open **App registrations → Harvey → API permissions**. Add the
   four Microsoft Graph delegated permissions above, then click
   **Grant admin consent**.
2. Run `harvey microsoft auth` again and sign in as the **sending mailbox**,
   not as the admin. Harvey always acts as whoever signs in.

To make sure nobody else in the tenant can use the app, go to
**Enterprise applications → Harvey**:

1. **Users and groups**: assign only the sending mailbox.
2. **Properties**: set **Assignment required?** to **Yes**. Do this after
   step 1, or the sending mailbox is locked out too.

## How it behaves

- **Sending.** Harvey creates a draft and then sends it, so the email's
  RFC `Message-ID` is known. Bounces and replies are matched on it. Bodies
  are plain text, unless `channels.email.signature.logo` is set: then the
  body is HTML and the logo is added to the draft as an inline attachment
  before it's sent. Outlook's own signature isn't added to mail sent
  through Graph, so this is the way to put a logo under Harvey's email.
- **Replies are threaded.** Harvey answers with `createReply` on the
  prospect's own message, so `In-Reply-To` and `References` are correct. If
  that message was deleted, the reply goes out as a new email.
- **Your inbox is left alone.** Each cycle, Harvey reads the last 7 days of
  the inbox, newest first, all of it (page by page), and skips your own mail. It never marks anything
  read, moves it or deletes it. It keeps its own record of what it has
  handled.
- **It only acts as the right mailbox.** If the signed-in mailbox isn't
  `persona.email`, every read and send is refused with a message saying so.
  This catches the common mistake of signing in as the admin who approved
  the app.
- **Failures hold sending.** If the inbox can't be read (expired login,
  Graph outage, throttling), the cycle sends nothing. A reply nobody read
  can't have stopped its follow-ups. Graph throttling (429/503 with a short
  `Retry-After`) is retried once. A read that fails part-way through counts
  as a failed read: Harvey never acts on part of the inbox.
- **Unsent drafts are cleaned up.** If a send fails, its draft is deleted
  so it doesn't sit in your Drafts folder.
- **The sender name is the mailbox's.** Mail goes out under the mailbox's
  display name in Microsoft 365, not `persona.name`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Timed out waiting for the browser round-trip` | The browser never came back to `localhost:8766`. Check that nothing else uses the port, and that the redirect URI is `http://localhost:8766` under *Mobile and desktop applications* (not *Web*). |
| `AADSTS50011` (redirect URI mismatch) | Same as above: the URI and its platform must match exactly. |
| `Need admin approval` | See [If you see "Need admin approval"](#if-you-see-need-admin-approval). |
| `signed in as X, but persona.email is Y` | Run `harvey microsoft auth` again and pick the right account. Use "Use another account" if the browser is already signed in as someone else. |
| `token refresh rejected` | The login expired (90 days unused) or was revoked. Run `harvey microsoft auth` again. |

To revoke Harvey's access, go to
[myaccount.microsoft.com](https://myaccount.microsoft.com) → **Apps**, or
delete the app registration. Then delete `data/m365_token.json`.

## Deliverability notes

- **Enable DKIM** for your domain in Microsoft Defender (Email
  authentication → DKIM). Without it, mail from Exchange Online isn't
  signed with your domain.
- **Mind the tenant.** Exchange Online watches outbound spam for the whole
  tenant. If you share it with an organization, its sending reputation
  depends on yours: keep `max_daily_sends` low and `require_approval: true`.

## For developers

| File | What it is |
|---|---|
| `harvey/integrations/microsoft.py` | `MicrosoftProvider` (a `MailProvider`) and `run_auth_flow` |
| `harvey/integrations/mail_provider.py` | Registers `microsoft` in `NATIVE_PROVIDERS` and `get_mail_provider` |
| `harvey/cli.py` | `harvey microsoft auth\|test` |
| `tests/test_microsoft.py` | Graph and login faked with `httpx.MockTransport`; no network |

`get_replies` **raises** `GraphError` when the inbox can't be read, instead
of returning `[]`. The handler turns that into a failed read, and the
heartbeat holds sending. It returns the whole 7-day window or nothing:

- **Every page.** `limit` is only the page size (`$top`). Harvey follows
  `@odata.nextLink` until Graph stops sending one, using the link exactly
  as given, without re-adding the first request's parameters or touching
  `$skip`, as Microsoft asks. Messages repeated across pages (new mail
  shifts `$skip` mid-read) are returned once.
- **Links are checked before the token is attached.** A `nextLink` must be
  `https://graph.microsoft.com` (default port), on the path
  `/v1.0/me/mailFolders/inbox/messages`, with a query, and no credentials,
  fragment, backslash or control characters. A link that repeats an earlier
  page is a loop. Either way the read fails without sending the request.
- **Bounded.** More than `MAX_INBOX_PAGES` (40) pages, i.e. 2,000 messages
  in a week at the default page size, fails the read rather than truncating.
- **Malformed data fails.** Any Graph response must be a JSON object; only
  `202`/`204` (send, delete) may be empty. A page whose `value` is missing
  or isn't a list, or a message with the wrong shape (no `id`, `from` or
  `body` not an object, non-string fields, headers not a list) raises. A
  message with no sender address is skipped, as before.

Limitations: a mailbox with more than 2,000 inbox messages in 7 days holds
sending every cycle until the volume drops (raise `MAX_INBOX_PAGES` or the
page size). If Graph ever returns a `nextLink` in another shape (say
`/users/{id}/...`), reads fail closed until the check is widened. Mail that
arrives while pages are being read can shift `$skip` so one message is
missed that cycle; the next cycle reads it, since the window is 7 days.

The loopback callback server listens on both `127.0.0.1` and `::1`, because
`localhost` resolves to both and browsers differ in which one they try.
