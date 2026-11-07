# Using DeepSeek's API instead (DeepSeek)

Claude is the default and needs no change. If you don't have a Claude or ChatGPT plan, Sellbird can think through [DeepSeek's API](https://api-docs.deepseek.com/) instead. Unlike the other two, this **is** per-token billing: you top up a prepaid balance and each call is paid from it. Sellbird's prompts are short (a cold email, a reply, a score), so a normal day usually costs cents, and a daily dollar cap stops Sellbird before it spends more than you allow.

## Set it up

1. **Get a key.** Create an account at [platform.deepseek.com](https://platform.deepseek.com), top up a few dollars, and create an API key.
2. **Put the key in `.env`** (never in `sellbird.yaml`, which you might share):

   ```bash
   DEEPSEEK_API_KEY=sk-...
   ```

3. **Choose DeepSeek in your config** (`sellbird.local.yaml` if you trained Sellbird, else `sellbird.yaml`):

   ```yaml
   brain:
     provider: deepseek
     model: deepseek-flash   # or deepseek-v4-pro; blank means deepseek-flash
     max_daily_usd: 0.50     # stop for the day at this estimated spend; 0 = no dollar cap
   ```

   Training works the same way: `sellbird train <url> --provider deepseek`.

4. **Check it:**

   ```bash
   sellbird status --check   # "DeepSeek (deepseek-flash) ready", or what's missing
   sellbird usage            # today's estimated spend against your cap
   ```

   The check sends nothing to DeepSeek: it confirms the key is set and the model is a DeepSeek one. A wrong key or an empty balance shows up on the first real call (see below).

## What to know

- **It's one or the other.** Sellbird uses exactly the provider you configure. If DeepSeek fails, the call fails with a clear log message; it never falls back to Claude or Codex. A DeepSeek-only machine doesn't need either CLI installed.
- **No tools.** Each call is one HTTPS request to DeepSeek's chat-completions endpoint: the prompt as a single user message, and no `tools` in the request, so the model can only answer with text. It can't read files, run commands or browse, whatever a scraped page or a reply asks for. A reply that comes back as a tool call anyway is refused.
- **Your data goes to DeepSeek.** Prompts carry what Sellbird works with: text from prospects' websites, their names and roles, and the replies they send you. With this provider that text is sent to DeepSeek's API and handled under [DeepSeek's terms and privacy policy](https://platform.deepseek.com), not Anthropic's or OpenAI's. Read them, and stay with Claude or Codex if they don't fit your business or the people you write to.
- **Models.** `deepseek-flash` (the default) is cheap and fast; `deepseek-v4-pro` writes better and costs about four times as much. Any other model name is refused by `sellbird status --check`. DeepSeek's thinking (reasoning) mode is its default and bills extra tokens; Sellbird turns it off for every call, since a short email doesn't need it.
- **Cost and the daily cap.** Every call records its tokens and an estimated cost, priced at DeepSeek's **peak** rates. Off-peak rates are half, so the real charge is the same or lower than the estimate. Before each heartbeat, Sellbird adds up today's estimates (by your local date) and stops for the day once they reach `brain.max_daily_usd` (default `0.50`). `0` turns the dollar cap off. The daily call limit still applies either way. Prices change: DeepSeek's [pricing page](https://api-docs.deepseek.com/quick_start/pricing) is the authority, and your real balance is on platform.deepseek.com. `sellbird usage` and the dashboard's Usage tab show today's spend against the cap instead of a subscription quota.
- **When DeepSeek can't be used, Sellbird pauses.** A missing key or an unknown model stops each heartbeat before any work, with the reason in the log, `sellbird status` and the dashboard. A rejected key (401) or an empty balance (402) pauses DeepSeek calls too. Sellbird tries again as soon as the key in `.env` changes (no restart needed), or after 30 minutes, which covers a top-up. While paused, no reply is read or marked handled and nothing is drafted or sent, as with Codex. Rate limits (429) and server errors (500, 503) are brief: Sellbird retries the call a couple of times, then leaves it for the next cycle. A request DeepSeek refuses as malformed (400, 422) isn't retried.
- **Usage is recorded per provider.** DeepSeek calls count separately from Claude and Codex calls, so switching providers never uses up another one's budget.

## Troubleshooting

| Message | What to do |
|---|---|
| `DEEPSEEK_API_KEY is not set in .env.` | Add the key to `.env` in the Sellbird folder. |
| `DeepSeek rejected the API key (401).` | Copy the key again from platform.deepseek.com; Sellbird resumes once `.env` changes. |
| `DeepSeek balance is empty (402).` | Top up at platform.deepseek.com; Sellbird tries again within 30 minutes. |
| `brain.model '…' isn't a DeepSeek model.` | Use `deepseek-flash` or `deepseek-v4-pro`, or leave `model` blank. |
| `DeepSeek daily cap reached` in the log | Working as intended: it resumes tomorrow. Raise `brain.max_daily_usd` to allow more. |
