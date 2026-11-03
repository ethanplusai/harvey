# Getting started: step by step

This is a beginner's guide to downloading and running this AI sales agent. It
is a fork of [Harvey](https://github.com/ethanplusai/harvey) by Ethan Rogers
(MIT licence), with a few additions:

- **Email Review window**: go through drafted emails one at a time and send them by hand
- **Mobile and WhatsApp numbers** read from each company's own website (office landlines are never shown as the number to call)
- **LinkedIn button** on each contact when their profile is available

Setup takes about 15 minutes. You don't need to know how to code.

---

## What you need first

| | What | Where to get it |
|---|---|---|
| 1 | **Claude Pro or Max** subscription | https://claude.ai |
| 2 | **Claude Code** (the app that runs the agent) | https://claude.com/claude-code |
| 3 | **Python 3.11 or newer** | https://www.python.org/downloads (on Windows, tick **"Add Python to PATH"** when installing) |
| 4 | **Git** | https://git-scm.com/downloads |
| 5 | A mailbox to send from, ideally on a **separate domain** from your main one | Google Workspace or Microsoft 365 |

Don't send cold email from your main company domain. If those emails get
marked as spam, your everyday email suffers too.

---

## Step 1: Download the agent

Open a terminal (on Windows, **PowerShell**; on Mac, **Terminal**) and run:

```bash
git clone https://github.com/rashad1st/RASH.git
cd RASH
```

## Step 2: Log in to Claude Code

```bash
claude login
```

A browser window opens. Log in with your Claude account.

## Step 3: Let Claude set everything up

Still in the `RASH` folder, start Claude Code:

```bash
claude
```

Then type:

> **set up Harvey for me**

Claude takes it from there. It installs everything, asks one question at a
time, and learns about your business from your website. Have these ready:

- your company website
- who you sell to (industry, job titles, cities or countries)
- the name and email the agent should send as

## Step 4: Connect your mailbox and an email checker

Claude asks for these during setup. Type keys into the `.env` file yourself,
never into a chat.

- **Mailbox:** Gmail/Google Workspace, Microsoft 365, or any SMTP mailbox
- **Email checker:** a free [Reoon](https://www.reoon.com) or
  [ZeroBounce](https://www.zerobounce.net) key. Without one, addresses the agent
  finds are treated as guesses and **never emailed**.

## Step 5: Warm up the mailbox

A new mailbox needs **2 to 4 weeks of warmup** before cold email, or it lands in
spam. Use a warmup service (Harvey recommends
[TrulyInbox](https://trulyinbox.com/?via=harvey); that's an affiliate link).
Check where you stand with:

```bash
harvey warmup
```

## Step 6: Find prospects

```bash
harvey signals --confirm free   # choose what makes a good prospect (free signals)
harvey discover                 # find businesses (free source, no account needed)
```

## Step 7: Open the dashboard

```bash
harvey dashboard
```

Open the private link it prints. You'll see:

- **Today**: anything waiting for you to decide
- **Contacts**: each person's email, mobile/WhatsApp number and LinkedIn button
- **Outbox / Email Review**: every email the agent writes **waits for your
  approval**. Nothing is sent until you approve it.

## Step 8: Start the agent

```bash
harvey run
```

Every 15 minutes it checks what needs doing: finding prospects, writing emails,
sending the ones you approved, and reading replies.

---

## Tips

- **Start small:** 10 emails a day at first, and approve each one yourself.
- **Just want a list?** `harvey export` saves the prospects to a CSV file.
- **Stop everything at once:** `harvey sending pause`
- **Stuck?** Open Claude Code in the folder and describe the problem in plain words.

## Common problems

| Problem | Fix |
|---|---|
| `harvey: command not found` | Activate the environment first. Windows: `.venv\Scripts\activate`. Mac: `source .venv/bin/activate` |
| `python` not found on Windows | Reinstall Python and tick **"Add Python to PATH"** |
| Claude errors | Run `claude login` again and check your subscription is active |

---

Credit: built on [Harvey](https://github.com/ethanplusai/harvey) by Ethan Rogers, MIT licence.
