# Zamp AI Matchbook: walkthrough

Notes on how Matchbook works and why it is built this way. Read this before recording the video or
doing the live demo; the interview questions will mostly be "why did you do it that way".

## The idea

An AP clerk does three different kinds of work on every invoice: reading a messy document, applying
company rules, and explaining the result to someone else. Matchbook gives each kind of work to
whatever is good at it.

- **Reading** goes to an AI model. Layouts vary, scans are crooked, and no template survives the
  next vendor.
- **Deciding** goes to plain rules. Whether to pay is a matter of policy, and policy has to give the
  same answer every time and be traceable afterwards.
- **Explaining** goes back to the AI, which is given the decision and the findings and asked to
  word them. It cannot change them.

So the model can misread an invoice, but it can never approve one. That is the sentence to say to a
finance buyer.

## The process

Invoices arrive the way they do in real AP: by email. The app has its own mailbox. Every ~20 seconds
it takes in unread email (`mail.InboxWatcher`), and each PDF or photo attached becomes its own run
(`intake.EmailIntake`). An upload in the browser starts exactly the same run. Runs go through a
small pool of workers (`worker.RunQueue`), so a burst of email doesn't fire twenty model calls at once.

Each run is nine stages, always in this order. The run view shows each one as it happens, with a tag
saying whether AI or rules did the work.

| # | Stage | Done by | What happens | Code |
|---|---|---|---|---|
| 1 | Receive the file | System | Checks the file opens, counts pages, and works out whether it is a digital PDF or a scan with no text layer | `pipeline._receive` |
| 2 | Read the invoice | AI | Sends the file to the model (Claude or Gemini) and gets back vendor, numbers, dates, amounts, bank details, plus what it was unsure of | `ai.AiInvoiceReader` |
| 3 | Check it is complete and adds up | Rules | Required fields present; line items, tax and total reconcile; date is sane | `rules.validate_invoice` |
| 4 | Identify the vendor | Rules | Matches the vendor by tax ID, then by name; compares bank details with the vendor record | `rules.identify_vendor` |
| 5 | Look for duplicates | Rules | Same invoice number after normalising; same invoice already waiting for review; same amount on the same date | `rules.find_duplicates` |
| 6 | Find the purchase order | Rules | Uses the PO number if one is printed; otherwise scores the vendor's open POs and suggests the best | `rules.match_purchase_order` |
| 7 | Compare amounts with the PO | Rules | Brings the invoice to a before-tax figure and compares it with what is left on the PO | `rules.compare_amounts` |
| 8 | Decide and explain | Rules, then AI | Any reject wins, otherwise any review wins, otherwise approve. Then the write-up and any message | `rules.decide`, `ai.AiExplainer` |
| 9 | Tell whoever needs to know | System | Works out the address from the rules' "notify" and sends, drafts or holds the email | `outbox.Outbox` |

Each check produces a *finding*: a pass, a note, a review or a reject, with a sentence a person can
read. The decision is the worst finding. Nothing else can approve an invoice.

Two behaviours cut across the stages:

- **Hard stops end the run early.** Once an invoice is a duplicate there is no point matching it to
  a PO, so the remaining stages are marked "not run". A clerk would stop there too.
- **Review issues are collected, not stopped on.** If an invoice has a changed bank account and
  also goes over its PO, the reviewer sees both at once instead of fixing one and being sent back.

## Design choices and what was rejected

**Compare against what is left on the PO, not the PO total.** The obvious check is "invoice amount
is at most PO amount". It passes every instalment of a split PO, including the one that takes the
total over. Matchbook keeps a ledger of recorded invoices and compares with the remaining balance.

**A guessed PO is never approved automatically.** When an invoice has no PO number, Matchbook scores
the vendor's open POs (60% how well the amount fits the balance, 40% how much the wording overlaps)
and suggests the winner if it scores at least 75% with a clear lead. Even a 93% match goes to a
person, with one click to confirm. Guessing wrong silently bills the wrong budget; asking costs ten
seconds.

**The arithmetic is the confidence check.** Models report their own confidence badly. Instead of
trusting a self-reported score, Matchbook checks that line items, tax and total reconcile. If the
numbers add up, they were almost certainly read correctly; if they do not, a person looks. The
model's "unsure about" list is used as well, but as a second signal.

**Invoice and PO numbers are compared after normalising.** `KOS/26-27/0874`, `kos-26-27-874` and
`KOS 26 27 0874` all reduce to the same key (`domain.reference_key`). Exact matching misses re-sent
invoices, which is the commonest way a duplicate gets paid.

**The rules decide who is emailed; the outbox only finds the address.** A vendor message answers the
email the invoice came in, in the same thread. A PO-owner message goes to the owner of the matched
PO. Replies go out automatically (`MATCHBOOK_MAIL_SEND=auto`), and every one is shown on its run with
its status, so nothing is sent that isn't visible. `manual` makes each one wait for a Send button.

**Nothing goes back towards the sender when the bank details change.** For a changed bank account
Matchbook sends nothing, and the run says why: the reviewer phones the number in the vendor master. If
the invoice is fraudulent, replying to it reaches the fraudster. The outbox enforces this a second
time, so even a vendor draft that slipped through is held.

**No mail loops.** Mail from the app's own address and delivery failures are ignored. Automated
senders (no-reply addresses, auto-responders) have their invoices processed, but are never answered.
Every email Matchbook sends carries `Auto-Submitted`, which tells other systems not to auto-reply. An
email with no invoice gets one polite request for it, but a reply in an existing thread ("thanks!")
gets nothing.

**The file itself goes to the model.** Claude and Gemini both accept PDFs directly and read them as
text and image together, so a clean PDF and a scan take the same path. There is no OCR step to maintain and
no branch that only scans exercise.

**Structured output, not free text.** The reader is given a JSON schema and the API guarantees the
reply matches it. The current Claude models reject forced tool calls and the `temperature` setting,
so structured output is the supported way to get a fixed shape back.

**The provider is a setting, not a dependency.** Claude and Gemini sit behind the same small
interface (`ai.JsonModel`): instructions, an optional file, a prompt and a schema in, JSON out. Both
use the same schema and the same instructions. `MATCHBOOK_AI_PROVIDER` and `MATCHBOOK_MODEL` in `.env`
choose which one runs. Since only the reading changes between providers and the rules don't, the
decisions differ only if the two models read an invoice differently, and the "See everything that
was read" panel shows exactly that.

**Everything from the invoice is treated as data.** The reader's instructions say to ignore any
instructions inside the document, and even if a document did trick the model, the model has no way
to approve anything.

**The interface polls instead of streaming.** The pipeline saves its progress after each stage and
the page asks for it a few times a second. It is less clever than a live stream and has nothing to
break behind a proxy during a demo. The page reveals at most one finished stage per tick so that a
rule taking 2 ms is still seen; the timings shown are the real ones.

**One set of SQL, two databases.** On a laptop it is a SQLite file; deployed it is Postgres on
Supabase, set by `DATABASE_URL`. All SQL is in `store.py` and written once, portably (`db.py`
converts placeholders and column types). The test suite passes on both. Invoice files go to a private
Supabase Storage bucket; the browser sees them through the app or a one-hour signed link.

**Every step is safe to repeat.** If the app restarts mid-run, unfinished runs are started again
when it comes back (`main.lifespan`). Nothing is recorded until the decision; a resumed run ignores
what it wrote itself. A unique index on (vendor, invoice number) means two copies of an invoice
processed at the same moment can't both be recorded: the second is turned into a duplicate. An email
is taken in once (by its Message-ID), and one whose intake failed is retried rather than lost.

**A busy model fails over instead of failing.** Free AI tiers return "503 high demand" often. The
reader tries the main model with few retries, and on an overload hands the document to a backup
model (`MATCHBOOK_FALLBACK_MODEL`). The run says which model read it. A bad key or a refusal is not
retried on the backup, because it would fail the same way. If both are busy, the run goes to a
person with "Run it again".

## The four edge cases

Each one is a case where a simpler process gets the wrong answer.

**1. Changed bank details (Northwind).** Vendor approved, PO found, amounts exact, tax right. A
process that only matches invoice to PO approves it. Matchbook holds it because the account on the
invoice differs from the vendor record, and says how to verify. This is the one to lead with: every
check is green except the one that matters.

**2. Third invoice on a split PO (Brightpath).** The PO is ₹9,00,000. Two invoices totalling
₹6,60,000 are already recorded. This one is ₹3,00,000: fine on its own, ₹60,000 over in total,
because two unapproved change requests were added to the final milestone. Matchbook flags the overage
and drafts a note to the PO owner. Approving it as a reviewer then shows up on the Purchase orders
page as a changed balance.

**3. Scanned bill with no PO number (Sahyadri).** No text layer, one bundled line, tax included in
the price, and only "as per your order dt. 21-10-2026" for a reference. Matchbook reads it as an
image, works the tax back out (₹1,01,480 is ₹86,000 before 18% GST), scores the vendor's two open
POs, suggests the right one and shows the scores. It still asks a person to confirm.

**4. Re-sent invoice with a retyped number (Kaveri reminder).** `KOS-26-27-874` is
`KOS/26-27/0874`, paid in October. Exact matching misses it. Matchbook rejects it, names the original,
and drafts a reply to the vendor. Running the happy-path invoice a second time shows the same rule
from another angle.

Smaller cases that are handled but are not demo pieces: missing invoice number (rejected, back to
the vendor), numbers that do not add up, line items printed with their tax included, unknown vendor,
a small overage inside tolerance (approved with a note), the AI service being down (backup model,
then a person, nothing guessed), and a second copy of an invoice that is already waiting for review.

Email-specific cases:

- **An email with several attachments.** Each invoice becomes its own run. A purchase order copy or a
  delivery note attached alongside is recognised as "not an invoice" and set aside without writing
  to anyone. Files that aren't PDFs or images are skipped and noted.
- **An email with no invoice.** One polite reply asking for it, unless it is a reply in a thread or
  comes from a robot.
- **The same email seen twice.** Processed once.
- **An invoice from a no-reply address.** Processed normally, but any message is held, because nobody
  would read it.

## Assumptions

The brief says to make assumptions and note them.

- Purchase orders are raised before tax, so invoices are compared before tax.
- Tolerance is 2% of the PO value, applied to the amount over the remaining balance. It is one
  setting (`MATCHBOOK_TOLERANCE_PCT`).
- An invoice from a vendor who is not on the approved list is never paid; it goes to a person, who
  sends the vendor to onboarding.
- There is no goods-received data, so this is a two-way match (invoice against PO), not three-way.
- Approval here means "recorded and queued for payment". Matchbook does not move money.
- The PO owners in the sample data aren't real, so their notes go to one procurement inbox
  (`MATCHBOOK_PROCUREMENT_EMAIL`), addressed to the owner by name.
- The sample data is Indian (rupees, GST, GSTIN). Nothing in the rules depends on that except the
  number formatting.

## Rehearsing and running the demo

Approving an invoice changes the data, so the same sample behaves differently the second time.
Before every run-through:

1. Open **Purchase orders**, scroll to **Rehearsal controls**, and choose **Restore purchase orders
   and recorded invoices**. This also closes any runs still waiting for a person. Run history is
   kept unless you clear it.
2. If the app is on Render's free plan, open it a few minutes early so it is awake.

A run order that tells a story. Send each from your own email account to the app's address, with
the Inbox page open on screen ("Check now" saves the 20-second wait):

1. Kaveri, chairs and desks. The clean case: approved, the PO balance drops to zero, nobody emailed.
2. Northwind. Everything green except the bank account, and the last stage says why nobody is emailed.
3. Brightpath. Over the PO: the note lands in the procurement inbox, addressed to the PO owner.
   Approve it as a reviewer with a note.
4. Sahyadri. The scan; show the PO scores.
5. Kaveri reminder. Rejected as a duplicate, and the reply lands in your inbox in the same thread.
   Show it.
6. An email with no attachment. The polite request for the invoice arrives.
7. The Runs page (the "From" column), then the Purchase orders page to show the balances that changed.

Have a sixth thing ready: upload a file of your own, or a photo of any invoice. An interviewer may
well ask for something that is not in the inbox.

## The five-minute video

Say it in your own words; these are only the beats. Three runs fit comfortably in five minutes.

- 20 seconds: the problem and the one idea (AI reads, rules decide, AI explains).
- 60 seconds: email the clean invoice in, and watch it arrive and run. Point at the AI and Rules tags.
- 75 seconds: the bank-change invoice. Let the checks go green, stop on the one that is not, and show
  that nobody was emailed and why.
- 60 seconds: the duplicate reminder, and the reply arriving in your inbox in the same thread.
- 45 seconds: the Runs page and the Purchase orders page.
- 30 seconds: what it does not do yet, and what you would build next.

## What it does not do yet

Being clear about these is worth more than hiding them.

- **No three-way match.** There is no goods-received data, so Matchbook cannot tell whether what was
  billed was delivered.
- **Line items are not matched one by one** against PO lines; only totals and wording are compared.
- **The mailbox is polled, not pushed.** Checking every 20 seconds over IMAP is simple and works with
  any mailbox; a production version would use Gmail push notifications or an inbound-mail webhook.
- **A sleeping free host doesn't read its mail.** On Render's free plan the app sleeps after 15 idle
  minutes; email waits in Gmail until it wakes. An uptime monitor or a paid instance fixes it.
- **No sign-in or roles.** Anyone with the link can approve or send.
- **Replies aren't read.** When a vendor answers our email, it lands in the mailbox but isn't matched
  to the original run.
- **The PO scoring is simple.** Word overlap and amount fit are easy to explain but would need
  tuning on real data.
- **Accuracy is unmeasured.** The sensible next step is a labelled set of real invoices and a
  report of how often each field is read correctly.

## Questions to be ready for

- *Why not let the model decide?* Decisions must be repeatable and auditable; a model's are neither.
  It also means a prompt injection in a PDF cannot approve a payment.
- *What happens when the model misreads a number?* The arithmetic check usually catches it. If it
  does not, the amount will not fit the PO and a person sees it. A misread fails towards review, not
  towards payment.
- *How would this handle 500 invoices a month?* That is about 25 a working day. The rules take
  milliseconds; the model call is the cost and the wait. Runs already go through a worker pool, so
  raise `MATCHBOOK_WORKERS` on a paid AI tier. People only see the ones that need them.
- *What if two copies of an invoice arrive at once?* The database refuses to record the same vendor
  and invoice number twice, so the second becomes a duplicate even if both passed the check together.
- *What if the server restarts mid-run?* Unfinished runs start again when it comes back, and every
  step is safe to repeat.
- *Why not reply to the vendor about the bank change?* Because the email might be from a fraudster.
  The phone number on file is the only safe channel.
- *What would you change for a real customer?* Their tolerance rules, their vendor master and PO
  system in place of the seed file, goods-received data for a three-way match, and sign-in.
- *How do you know it works?* 82 automated tests cover the happy path, the four edge cases, the
  email cases and the failure cases (restarts, races, outages, storage down). They use known readings
  of each sample and a fake mail server, so nothing calls a model or sends real email. They pass on
  SQLite and on Postgres.
