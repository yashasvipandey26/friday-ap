# Zamp AI Matchbook

Invoice processing for an accounts payable team, from email to decision. Built for PS-1 of the
AI Solutions Analyst case study.

A vendor emails an invoice (a PDF, a scan or a photo) to the AP mailbox. Matchbook picks it up, reads
it, checks it against approved vendors, purchase orders and earlier invoices, and comes back with
one of three outcomes: approved, needs a person, or rejected. Every check is shown as it runs, the
reason is written in plain English, and whoever needs to know is emailed: the vendor gets a reply in
their own thread, the PO owner gets a note. Invoices can also be uploaded in the browser.

The one idea behind the design: **the AI reads and explains; fixed rules decide.** The model turns a
messy document into structured fields and writes the summary at the end. The decision in between
comes from ordinary code, so the same invoice always gets the same answer and every answer can be
traced to a rule. The rules also decide who is emailed, and never write back when the bank details
changed, because if the invoice is a fraud, a reply reaches the fraudster.

`WALKTHROUGH.md` explains the process, the design choices and the edge cases in detail.

## Run it on your machine

You need Python 3.11 or newer and an API key for either Google (Gemini) or Anthropic (Claude).

```bash
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env          # then paste your key after GOOGLE_API_KEY= or ANTHROPIC_API_KEY=
uvicorn app.main:create_app --factory --reload --env-file .env
```

Open <http://localhost:8000>. The header shows "AI reader ready" when the key has been picked up;
hover over it to see which model is reading and where runs are stored.

With only the AI key set, everything works except email: runs are kept in a local SQLite file, files
in a local folder, and messages wait as drafts on each run. The next two sections add the mailbox and
cloud storage. Each is optional and independent.

## Receive invoices by email (Gmail, free, about 10 minutes)

1. Create a new Gmail account just for the app, for example `matchbook.ap.yourname@gmail.com`.
2. In that account, turn on 2-Step Verification (Google Account > Security).
3. Open <https://myaccount.google.com/apppasswords>, create an app password called "Matchbook" and
   copy the 16 characters.
4. In `.env`:
   ```
   MATCHBOOK_MAIL_ADDRESS=matchbook.ap.yourname@gmail.com
   MATCHBOOK_MAIL_APP_PASSWORD=abcd efgh ijkl mnop
   MATCHBOOK_PROCUREMENT_EMAIL=you@yourmail.com     # receives the notes meant for PO owners
   ```
5. Restart. The header says "Inbox connected" and the Inbox page shows the address.

To try it, email one of the files in `samples/` to the app's address **from a different account**
(the app ignores mail from its own address). Within about 20 seconds ("Check now" skips the wait) it
appears on the Inbox page, the run starts, and any reply lands in the account you sent from, in the
same thread.

Leave the app's mailbox to the app: it takes in unread email, so an email you open in Gmail first is
skipped.

## Keep runs and files in the cloud (Supabase, free)

On a free Render instance the disk is wiped on every restart, which would empty the run history.
Supabase gives you a Postgres database and file storage in one free project.

1. Create a project at <https://supabase.com>. Pick the region closest to where the app runs.
2. **Connect** (top of the dashboard) > **Session pooler** > copy the URI, replace
   `[YOUR-PASSWORD]` with the database password, and set it as `DATABASE_URL`. Use the session
   pooler, not the direct connection: the direct one is IPv6-only and Render can't reach it.
3. **Project Settings > API Keys**: copy the project URL into `SUPABASE_URL` and a secret key (or the
   legacy `service_role` key) into `SUPABASE_SERVICE_ROLE_KEY`. It is a server-side key; never put it
   in front-end code.
4. Restart. The tables and a private `invoices` storage bucket are created on first start.

A free Supabase project pauses after a week with no activity; opening the app wakes it.

## What the five sample invoices should do

The sample inbox holds five vendor emails. Run them on freshly restored data (by email, upload or the
sample list) and you should see:

| Sample | Outcome | Reason | Who is emailed |
|---|---|---|---|
| Kaveri, chairs and desks | Approved | Matches PO-2026-0412 exactly; recorded against it | Nobody |
| Northwind, October freight | Needs a person | Everything matches, but the bank account is not the one on file | Nobody, on purpose |
| Brightpath, final milestone | Needs a person | Third invoice on PO-2026-0388; ₹60,000 over what is left | The PO owner |
| Sahyadri, scanned bill | Needs a person | No PO number; PO-2026-0440 is suggested, not assumed | Nobody |
| Kaveri, payment reminder | Rejected | Duplicate of KOS/26-27/0874, with the number retyped | The vendor |

Approving an invoice changes the data, so order matters: run the first sample twice and the second
run is rejected as a duplicate. "Restore purchase orders and recorded invoices" at the bottom of the
Purchase orders page puts everything back.

If a sample gives a different outcome with the real model, open "See everything that was read" on
the Read stage. That shows exactly what the model extracted, which is where to look first.

## Tests

```bash
python -m pip install -r requirements-dev.txt
pytest
```

The tests run the real pipeline, rules, mail intake and outbox against a throwaway database. The AI
reader and the mail server are replaced by stubs, so the tests are fast, free and repeatable. Set
`MATCHBOOK_TEST_DATABASE_URL` to an empty Postgres database to run the same tests against Postgres.

## Put it online

Render is the quickest route.

1. Push this folder to a GitHub repository.
2. In Render: New, then Blueprint, and pick the repository; it reads `render.yaml` and asks for each
   setting. (New, then Web Service, also works: build command `pip install -r requirements.txt`,
   start command `uvicorn app.main:create_app --factory --host 0.0.0.0 --port $PORT`.)
3. Fill in at least `GOOGLE_API_KEY` (or `ANTHROPIC_API_KEY`), and the mail and Supabase settings
   from above. Leave the rest empty.

Things to know about Render's free plan:

- The service sleeps after 15 minutes without web traffic, and **a sleeping app doesn't check its
  mailbox**. Emails wait in Gmail and are processed once it wakes, but for a demo that is too slow. A
  free uptime monitor (UptimeRobot or cron-job.org) requesting `/api/health` every 10 minutes keeps it
  awake. Waking takes about a minute, so open the link a few minutes before any demo anyway.
- Without Supabase, the disk is wiped whenever the service sleeps or restarts.

The link is public, so anyone who has it can spend your API credit. The app refuses more than 300
runs a day (`MATCHBOOK_MAX_RUNS_PER_DAY`). A `Dockerfile` is included for any other host.

## How the code is laid out

```
app/
  domain.py     what an invoice, a finding and a decision look like (read this first)
  rules.py      every check and the final decision; pure functions, no AI, no database
  pipeline.py   runs the stages in order and records each one as it happens
  ai.py         the two AI calls (read the document, write up the decision), on Claude or Gemini,
                with a backup model for when the main one is busy
  mail.py       reading the mailbox (IMAP) and sending (SMTP)
  intake.py     an incoming email becomes one run per invoice attached
  outbox.py     who is emailed, and when a message is held instead
  worker.py     the pool of workers that process runs
  store.py      all SQL: vendors, purchase orders, ledger, runs, emails, outbox
  db.py         the same SQL on SQLite (local) or Postgres (Supabase)
  files.py      invoice files in a local folder or Supabase Storage
  main.py       the web endpoints, and starting the inbox watcher
  config.py     settings read from the environment
data/seed.json          the starting vendors, purchase orders (with owners) and recorded invoices
samples/                the five sample invoices and the sample inbox list
scripts/make_samples.py regenerates the samples
static/                 the interface: one HTML file, one stylesheet, one script, no build step
tests/                  scenario, rule, email, robustness and API tests
```

## Settings

All optional except one API key. See `.env.example`.

| Variable | Default | What it does |
|---|---|---|
| `MATCHBOOK_AI_PROVIDER` | whichever has a key | `anthropic` or `gemini` |
| `GOOGLE_API_KEY` | none | Reads invoices with Gemini (`GEMINI_API_KEY` also works) |
| `ANTHROPIC_API_KEY` | none | Reads invoices with Claude |
| `MATCHBOOK_MODEL` | `gemini-3.6-flash` or `claude-sonnet-5-5` | Model used for reading and writing |
| `MATCHBOOK_FALLBACK_MODEL` | `gemini-3.5-flash-lite` for Gemini | Tried when the main model is busy; `none` turns it off |
| `MATCHBOOK_READ_EFFORT` | `medium` | How hard the model thinks while reading: `low`, `medium`, `high` |
| `MATCHBOOK_MAIL_ADDRESS` | none | The mailbox invoices are sent to |
| `MATCHBOOK_MAIL_APP_PASSWORD` | none | Its app password |
| `MATCHBOOK_PROCUREMENT_EMAIL` | none | Receives notes meant for PO owners |
| `MATCHBOOK_MAIL_SEND` | `auto` | `auto` sends replies at once; `manual` waits for a person to press Send |
| `MATCHBOOK_MAIL_POLL_SECONDS` | `20` | How often the mailbox is checked |
| `DATABASE_URL` | none (SQLite) | Postgres connection string, e.g. Supabase's session pooler |
| `SUPABASE_URL` | none (local folder) | Supabase project URL, for file storage |
| `SUPABASE_SERVICE_ROLE_KEY` | none | Supabase secret key, for file storage |
| `MATCHBOOK_TOLERANCE_PCT` | `2.0` | How far over a PO's balance an invoice may go, as % of the PO |
| `MATCHBOOK_COMPANY_NAME` | `Meridian Retail` | Signs the emails |
| `MATCHBOOK_WORKERS` | `2` | Invoices checked at the same time |
| `MATCHBOOK_DATA_DIR` | `./var` | Where the local database and files live |
| `MATCHBOOK_MAX_RUNS_PER_DAY` | `300` | Daily cap on runs |
