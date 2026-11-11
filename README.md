# Friday

Invoice processing for an accounts payable team, from PDF to decision. Built for PS-1 of the
AI Solutions Analyst case study.

An invoice goes in (a PDF, a scan or a photo). Friday reads it, checks it against approved vendors,
purchase orders and earlier invoices, and comes back with one of three outcomes: approved, needs a
person, or rejected. Every check is shown as it runs, and the reason is written in plain English.

The one idea behind the design: **the AI reads and explains; fixed rules decide.** Claude turns a
messy document into structured fields and writes the summary at the end. The decision in between
comes from ordinary code, so the same invoice always gets the same answer and every answer can be
traced to a rule.

`WALKTHROUGH.md` explains the process, the design choices and the edge cases in detail.

## Run it on your machine

You need Python 3.11 or newer and an Anthropic API key.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then paste your key after ANTHROPIC_API_KEY=
uvicorn app.main:create_app --factory --reload --env-file .env
```

Open <http://localhost:8000>. The header shows "AI reader ready" when the key has been picked up.

## What the five sample invoices should do

The inbox holds five vendor emails. Run them on freshly restored data and you should see:

| Sample | Outcome | Reason |
|---|---|---|
| Kaveri, chairs and desks | Approved | Matches PO-2026-0412 exactly; recorded against it |
| Northwind, October freight | Needs a person | Everything matches, but the bank account is not the one on file |
| Brightpath, final milestone | Needs a person | Third invoice on PO-2026-0388; ₹60,000 over what is left |
| Sahyadri, scanned bill | Needs a person | No PO number; PO-2026-0440 is suggested, not assumed |
| Kaveri, payment reminder | Rejected | Duplicate of KOS/26-27/0874, with the number retyped |

Approving an invoice changes the data, so order matters: run the first sample twice and the second
run is rejected as a duplicate. "Restore purchase orders and recorded invoices" at the bottom of the
Purchase orders page puts everything back.

If a sample gives a different outcome with the real model, open "See everything that was read" on
the Read stage. That shows exactly what the model extracted, which is where to look first.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

The tests run the real pipeline and rules against a throwaway database. The AI reader is replaced by
a stub that returns a known reading of each sample, so the tests are fast, free and repeatable.

## Put it online

Render is the quickest route.

1. Push this folder to a GitHub repository.
2. In Render: New, then Web Service, and pick the repository. (New, then Blueprint, also works and
   reads `render.yaml`.)
3. Build command `pip install -r requirements.txt`, start command
   `uvicorn app.main:create_app --factory --host 0.0.0.0 --port $PORT`.
4. Add the environment variable `ANTHROPIC_API_KEY`.

Two things to know about Render's free plan:

- The service sleeps after 15 minutes without traffic and takes about a minute to wake. Open the
  link a few minutes before any demo.
- Its disk is wiped whenever the service sleeps or restarts, which empties the run history. A free
  uptime monitor that requests `/api/health` every 10 minutes keeps it awake. For the week your
  submission is being reviewed, a paid instance avoids both problems.

The link is public, so anyone who has it can spend your API credit. The app refuses more than 300
runs a day (`FRIDAY_MAX_RUNS_PER_DAY`); set a spend limit in the Anthropic console as well.

A `Dockerfile` is included for any other host.

## How the code is laid out

```
app/
  domain.py     what an invoice, a finding and a decision look like (read this first)
  rules.py      every check and the final decision; pure functions, no AI, no database
  pipeline.py   runs the stages in order and records each one as it happens
  ai.py         the two Claude calls: read the document, write up the decision
  store.py      SQLite: vendors, purchase orders, recorded invoices, run history
  main.py       the web endpoints
  config.py     settings read from the environment
data/seed.json          the starting vendors, purchase orders and recorded invoices
samples/                the five sample invoices and the inbox list
scripts/make_samples.py regenerates the samples
static/                 the interface: one HTML file, one stylesheet, one script, no build step
tests/                  scenario tests, rule tests, API tests
```

## Settings

All optional except the key. See `.env.example`.

| Variable | Default | What it does |
|---|---|---|
| `ANTHROPIC_API_KEY` | none | Needed to read invoices |
| `FRIDAY_MODEL` | `claude-sonnet-5-5` | Model used for reading and writing |
| `FRIDAY_READ_EFFORT` | `medium` | How hard the model thinks while reading: `low`, `medium`, `high` |
| `FRIDAY_TOLERANCE_PCT` | `2.0` | How far over a PO's balance an invoice may go, as % of the PO |
| `FRIDAY_COMPANY_NAME` | `Meridian Retail` | Signs the draft messages |
| `FRIDAY_DATA_DIR` | `./var` | Where the database and uploaded files live |
| `FRIDAY_MAX_RUNS_PER_DAY` | `300` | Daily cap on runs |
