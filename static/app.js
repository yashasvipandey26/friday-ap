// Matchbook front end. No framework and no build step: one module that draws four views into <main>.
// Everything that comes from an invoice is written with textContent (via el), never as HTML.

const main = document.getElementById("view");
const state = { liveRunId: null, cameFrom: "inbox", runFilter: "all", health: null };

// Every navigation draws into a fresh page element of its own. Only the newest one is put on screen:
// clicking quickly through the tabs shows a loader and then the last tab, never the ones in between.
// Anything still running for an older page (a slow fetch, a refresh timer) checks this and stops.
const nav = { page: null };
const isCurrent = (page) => page === nav.page;

// ---------------------------------------------------------------- small helpers

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(props)) {
    if (value == null || value === false) continue;
    if (name === "class") node.className = value;
    else if (name.startsWith("on")) node.addEventListener(name.slice(2), value);
    else node.setAttribute(name, value === true ? "" : value);
  }
  for (const child of children.flat(Infinity)) {
    if (child == null || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

async function api(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let message = `The server answered with an error (${response.status}).`;
    try { message = (await response.json()).detail || message; } catch { /* keep the generic message */ }
    throw new Error(message);
  }
  return response.json();
}

const postJson = (path, body) =>
  api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

function money(amount, currency = "INR") {
  if (amount == null) return "";
  const digits = Number.isInteger(amount) ? 0 : 2;
  return new Intl.NumberFormat("en-IN", { style: "currency", currency: currency || "INR", maximumFractionDigits: digits, minimumFractionDigits: digits }).format(amount);
}

function day(iso) {
  if (!iso) return "";
  const [year, month, date] = iso.slice(0, 10).split("-").map(Number);
  if (!year || !month || !date) return iso;
  return new Date(year, month - 1, date).toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" });
}

const moment = (iso) => iso ? new Date(iso).toLocaleString("en-IN", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "";
const seconds = (ms) => ms == null ? "" : ms < 1 ? "under 1 ms" : ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`;
const percent = (fraction) => `${Math.round(fraction * 100)}%`;

function toast(message) {
  const box = document.getElementById("toast");
  box.textContent = message;
  box.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { box.hidden = true; }, 6000);
}

const OUTCOMES = {
  approved: { tone: "pass", label: "Approved", stamp: "Approved" },
  needs_review: { tone: "review", label: "Needs a person", stamp: "Needs a person" },
  rejected: { tone: "reject", label: "Rejected", stamp: "Rejected" },
};

const isActive = (run) => run.status === "running" || run.status === "queued";

function outcomeOf(run) {
  if (run.status === "queued") return { tone: "running", label: "Waiting to start" };
  if (run.status === "running") return { tone: "running", label: "Checking" };
  if (run.status === "failed") return { tone: "reject", label: "Stopped" };
  if (run.resolution === "approved") return { tone: "pass", label: "Approved by a reviewer" };
  if (run.resolution === "rejected") return { tone: "reject", label: "Rejected by a reviewer" };
  if (run.resolution === "closed") return { tone: "pending", label: "Closed, not decided" };
  return OUTCOMES[run.outcome] || { tone: "pending", label: "" };
}

// Redraws a page every few seconds while it is open, so email arriving in the background shows up.
function refreshEvery(view, ms, redraw) {
  const timer = setInterval(() => {
    if (isCurrent(view)) redraw().catch(() => {});
    else clearInterval(timer);
  }, ms);
  return () => clearInterval(timer);
}

// ---------------------------------------------------------------- routing

const routes = { inbox: showInbox, runs: showRuns, orders: showOrders };

function route() {
  const name = location.hash.replace(/^#/, "") || "inbox";
  const runId = name.match(/^run-(\d+)$/)?.[1];
  const section = runId ? state.cameFrom : (routes[name] ? name : "inbox");
  for (const link of document.querySelectorAll("[data-nav]")) {
    if (link.dataset.nav === section) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  const view = el("div", { class: "page-frame" });
  nav.page = view;
  main.replaceChildren(el("div", { class: "page-loading", role: "status" }, el("span", { class: "spinner" }), "Loading…"));
  window.scrollTo(0, 0);
  const draw = runId ? showRun(Number(runId), view) : routes[section](view);
  draw.then(
    () => { if (isCurrent(view)) main.replaceChildren(view); },
    (error) => { if (isCurrent(view)) main.replaceChildren(el("p", { class: "empty" }, error.message)); },
  );
}

window.addEventListener("hashchange", route);

// ---------------------------------------------------------------- inbox: start a run

async function showInbox(view) {
  state.cameFrom = "inbox";
  const samples = await api("/api/samples");

  const picker = el("input", { type: "file", id: "invoice-file", accept: ".pdf,.png,.jpg,.jpeg,.webp,.heic,.heif", class: "visually-hidden",
    onchange: (event) => event.target.files[0] && startRun({ file: event.target.files[0] }) });
  const dropzone = el("div", { class: "dropzone" },
    el("p", { class: "dropzone-title" }, "Drop a PDF or a photo of an invoice here"),
    el("label", { class: "button", for: "invoice-file" }, "Choose a file"),
    picker,
    el("p", { class: "fine-print" }, "A PDF or a photo (JPEG, PNG, WebP or iPhone HEIC), up to 8 MB. Scans are fine."));
  for (const type of ["dragenter", "dragover"]) {
    dropzone.addEventListener(type, (event) => { event.preventDefault(); dropzone.classList.add("is-over"); });
  }
  for (const type of ["dragleave", "drop"]) {
    dropzone.addEventListener(type, (event) => { event.preventDefault(); dropzone.classList.remove("is-over"); });
  }
  dropzone.addEventListener("drop", (event) => {
    const file = event.dataTransfer.files[0];
    if (file) startRun({ file });
  });

  const mails = samples.map((sample) =>
    el("li", { class: "mail" },
      el("button", { class: "mail-open", type: "button", onclick: () => startRun({ sampleId: sample.id }) },
        el("span", { class: "mail-sender" }, sample.sender),
        el("span", { class: "mail-subject" }, sample.subject),
        el("span", { class: "mail-preview" }, sample.preview)),
      el("a", { class: "mail-attachment", href: `/api/samples/${sample.id}/file`, target: "_blank", rel: "noopener",
        title: `Open ${sample.file} in a new tab` }, "View PDF")));

  const mailbox = el("section", { class: "mailbox", "aria-live": "polite" });
  view.replaceChildren(
    el("header", { class: "page-head" },
      el("h1", {}, "Inbox"),
      el("p", {}, "Invoices arrive by email or upload, and are checked against vendors, purchase orders and earlier invoices.")),
    el("div", { class: "intake" },
      el("section", {},
        el("h2", { class: "section-title" }, "Email"),
        mailbox,
        el("h2", { class: "section-title" }, "Upload"),
        dropzone),
      el("section", { class: "inbox" },
        el("h2", { class: "section-title" }, "Sample invoices"),
        el("ul", { class: "mail-list" }, mails),
        el("p", { class: "fine-print" }, "Demo emails with invoices attached. They go through the same checks as real ones."))));

  const drawMailbox = async () => mailbox.replaceChildren(...mailboxNodes(await api("/api/mailbox")));
  await drawMailbox().catch((error) => mailbox.replaceChildren(el("p", { class: "mailbox-error" }, error.message)));
  refreshEvery(view, 5000, drawMailbox);
}

const EMAIL_STATUS = {
  received: { tone: "running", label: "Taking it in" },
  processed: { tone: "pass", label: "Invoice sent for checking" },
  no_invoice: { tone: "review", label: "No invoice attached" },
  ignored: { tone: "pending", label: "Ignored" },
  failed: { tone: "reject", label: "Couldn't take it in" },
};

function mailboxNodes(box) {
  if (!box.enabled) {
    return [el("p", { class: "fine-print mailbox-off" }, "Email intake is off. Set MATCHBOOK_MAIL_ADDRESS and " +
      "MATCHBOOK_MAIL_APP_PASSWORD on the server to have invoices emailed straight in.")];
  }
  const check = el("button", { class: "button quiet small", type: "button" }, "Check now");
  check.addEventListener("click", async () => {
    try { await postJson("/api/mailbox/check", {}); check.textContent = "Checking…"; } catch (error) { toast(error.message); }
  });
  const health = box.last_error
    ? el("p", { class: "mailbox-error" }, box.last_error)
    : el("p", { class: "fine-print" }, box.last_checked
      ? `Checked ${moment(box.last_checked)}, every ${box.poll_seconds} s. ` +
        (box.send_mode === "auto" ? "Replies go out automatically." : "Replies wait for a person to press Send.")
      : "Connecting to the mailbox…");
  const rows = box.emails.map((email) => {
    const status = EMAIL_STATUS[email.status] || { tone: "pending", label: email.status };
    const sent = email.messages.filter((message) => message.status === "sent").length;
    return el("li", { class: "mail-row" },
      el("span", { class: "mail-sender" }, email.from_name || email.from_address),
      el("span", { class: "mail-subject" }, email.subject || "(no subject)"),
      el("span", { class: `outcome tone-${status.tone}` }, status.label),
      el("span", { class: "mail-runs" },
        email.runs.map((run) => el("a", { href: `#run-${run.id}` }, `Run #${run.id}`)),
        sent ? el("span", { class: "fine-print" }, `${sent} repl${sent === 1 ? "y" : "ies"} sent`) : null),
      email.note && el("span", { class: "fine-print mail-note" }, email.note));
  });
  return [
    el("div", { class: "mailbox-head" },
      el("p", {}, "Send invoices to ", el("strong", { class: "address" }, box.address)),
      check),
    health,
    rows.length ? el("ul", { class: "mail-rows" }, rows) : el("p", { class: "fine-print" }, "No email yet. Send one with an invoice attached."),
  ];
}

async function startRun({ file, sampleId }) {
  const body = new FormData();
  if (file) body.append("file", file);
  else body.append("sample_id", sampleId);
  try {
    const { id } = await api("/api/runs", { method: "POST", body });
    state.liveRunId = id;
    location.hash = `run-${id}`;
  } catch (error) {
    toast(error.message);
  }
}

// ---------------------------------------------------------------- run view: every stage, as it happens

const WORKING = {
  receive: "Opening the file",
  read: "Reading the document",
  validate: "Checking the fields and the arithmetic",
  vendor: "Looking the vendor up",
  duplicate: "Searching earlier invoices",
  match: "Looking for the purchase order",
  amounts: "Comparing with what is left on the PO",
  decide: "Deciding",
  notify: "Sending emails",
};
const ACTORS = { ai: "AI", rules: "Rules" };
const MARKS = { pass: "✓", note: "✓", review: "!", reject: "×", skipped: "–" };

async function showRun(id, view) {
  const live = state.liveRunId === id;
  state.liveRunId = null;
  const run = await api(`/api/runs/${id}`);

  const status = el("p", { class: "run-status", "aria-live": "polite" });
  const stages = el("ol", { class: "stages" });
  const verdict = el("div", { class: "verdict" });
  const back = state.cameFrom === "runs" ? { href: "#runs", text: "Back to runs" } : { href: "#inbox", text: "Back to the inbox" };
  const isPdf = run.media_type === "application/pdf";
  const storedLabel = state.health?.storage === "supabase" ? "Open the copy kept in cloud storage" : "Open the original in a new tab";

  view.replaceChildren(
    el("div", { class: "run" },
      el("header", { class: "run-head" },
        el("a", { class: "back", href: back.href }, back.text),
        el("h1", {}, run.filename),
        el("p", { class: "fine-print" }, `Run ${run.id}, received ${moment(run.created_at)}`),
        sourceNode(run),
        status),
      el("div", { class: "run-body" },
        el("aside", { class: "document" },
          isPdf
            ? el("iframe", { src: `${run.file_url}#toolbar=0&navpanes=0&view=FitH`, title: "The invoice as received" })
            : el("img", { src: run.file_url, alt: "The invoice as received" }),
          el("a", { class: "fine-print", href: run.stored_url, target: "_blank", rel: "noopener" }, storedLabel)),
        el("section", { class: "working", "aria-label": "The checks" }, stages, verdict))));

  const frame = { status, stages, verdict };
  if (live || isActive(run)) watchRun(id, frame, view);
  else paintRun(run, frame, { revealed: run.stages.length, live: false });
}

function sourceNode(run) {
  if (run.email) {
    const who = run.email.from_name ? `${run.email.from_name} <${run.email.from_address}>` : run.email.from_address;
    return el("p", { class: "source" }, el("span", { class: "actor" }, "Email"), " From ", el("strong", {}, who),
      run.email.subject ? ` · “${run.email.subject}”` : "");
  }
  const label = run.source === "sample" ? "Sample" : "Upload";
  return el("p", { class: "source" }, el("span", { class: "actor" }, label),
    run.source === "sample" ? " From the sample inbox" : " Uploaded in the browser");
}

function watchRun(id, frame, view) {
  // The page is allowed to finish at most one stage per tick, so a rule that takes 2 ms is still
  // seen happening. The timings printed beside each stage are the real ones from the server.
  let revealed = 0;
  let latest = null;

  const tick = async () => {
    if (!isCurrent(view)) return;  // the user has moved to another page
    try {
      if (!latest || isActive(latest)) latest = await api(`/api/runs/${id}`);
    } catch (error) {
      frame.status.textContent = `Lost contact with the server: ${error.message}`;
      return;
    }
    const next = latest.stages[revealed];
    if (next && (next.state === "done" || next.state === "skipped")) revealed += 1;
    const finished = !isActive(latest) && revealed >= latest.stages.length;
    paintRun(latest, frame, { revealed, live: true, finished });
    if (!finished) setTimeout(tick, 430);
  };
  tick();
}

function paintRun(run, frame, { revealed, live, finished = true }) {
  run.stages.forEach((stage, index) => {
    const display = index < revealed ? "final" : index === revealed && stage.state !== "skipped" ? "running" : "pending";
    const signature = `${display}:${stage.state}`;
    const existing = frame.stages.children[index];
    if (existing?.dataset.signature === signature) return;
    const node = stageNode(stage, index, display);
    node.dataset.signature = signature;
    if (existing) existing.replaceWith(node);
    else frame.stages.append(node);
  });

  const outcome = outcomeOf(run);
  frame.status.className = `run-status tone-${finished ? outcome.tone : "running"}`;
  frame.status.textContent = finished ? outcome.label : "Checking";

  if (!finished || frame.verdict.childElementCount) return;
  if (run.status === "failed" || !run.decision) {
    frame.verdict.append(el("section", { class: "decision tone-reject" },
      el("h2", {}, "This run stopped unexpectedly"),
      el("p", {}, "Nothing was decided and nothing was recorded. Run the invoice again; if it stops again, process it by hand."),
      retryNode(run)));
    return;
  }
  frame.verdict.append(decisionCard(run, frame));
  if (live) frame.verdict.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function stageNode(stage, index, display) {
  let tone = display;
  if (display === "final") {
    if (stage.state === "skipped") tone = "skipped";
    else if (stage.key === "decide") tone = OUTCOMES[stage.details.outcome]?.tone || "pass";
    else tone = stage.result || "pass";
  }
  const mark = display === "running" ? el("span", { class: "spinner" }) : (MARKS[tone] || String(index + 1));
  const body = [];
  if (display === "running") body.push(el("p", { class: "stage-note is-working" }, WORKING[stage.key] || "Working"));
  if (display === "final") {
    if (!stage.findings.length || stage.key === "decide") body.push(el("p", { class: "stage-note" }, stage.summary));
    if (stage.findings.length) body.push(el("ul", { class: "findings" }, stage.findings.map(findingNode)));
    if (stage.key === "read" && stage.details.invoice) body.push(readingNode(stage.details.invoice));
    if (stage.key === "match" && stage.details.candidates?.length) body.push(candidatesNode(stage.details.candidates));
  }
  return el("li", { class: `stage tone-${tone}` },
    el("span", { class: "stage-mark", "aria-hidden": "true" }, mark),
    el("div", { class: "stage-body" },
      el("div", { class: "stage-head" },
        el("h3", {}, stage.title),
        ACTORS[stage.actor] && el("span", { class: `actor actor-${stage.actor}` }, ACTORS[stage.actor]),
        display === "final" && stage.duration_ms != null && el("span", { class: "stage-time" }, seconds(stage.duration_ms))),
      body));
}

function findingNode(finding) {
  return el("li", { class: `finding tone-${finding.status}` },
    el("span", { class: "finding-mark", "aria-hidden": "true" }, MARKS[finding.status]),
    el("p", {}, el("strong", {}, `${finding.title}.`), " ", finding.message));
}

function readingNode(invoice) {
  const currency = invoice.currency || "INR";
  const taxWords = { separate: "Listed separately", embedded: "Included in the prices", none: "None charged", unclear: "Unclear" };
  const rows = [
    ["Vendor", invoice.vendor_name],
    ["Vendor tax ID", invoice.vendor_tax_id],
    ["Invoice number", invoice.invoice_number],
    ["Invoice date", day(invoice.invoice_date)],
    ["PO number", invoice.po_reference || (invoice.po_hint ? `Not printed. The invoice says: “${invoice.po_hint}”` : "Not printed")],
    ["Before tax", invoice.subtotal != null ? money(invoice.subtotal, currency) : "Not shown"],
    ["Tax", invoice.tax_amount != null ? money(invoice.tax_amount, currency) : taxWords[invoice.tax_treatment]],
    ["Total", money(invoice.total, currency)],
    ["Already paid", invoice.amount_paid != null ? money(invoice.amount_paid, currency) : null],
    ["Pay to", invoice.bank_account_number
      ? `Account ending ${invoice.bank_account_number.slice(-4)}${invoice.bank_ifsc ? `, ${invoice.bank_ifsc}` : ""}` : "No bank details printed"],
    ["Copy quality", { clear: "Clear", degraded: "Readable scan", poor: "Poor" }[invoice.document_quality]],
    ["Unsure about", invoice.uncertain_fields.length ? invoice.uncertain_fields.join(", ") : "Nothing"],
    ["Reader's note", invoice.notes],
  ].filter(([, value]) => value);

  return el("details", { class: "more" },
    el("summary", {}, "See everything that was read"),
    el("dl", { class: "reading" }, rows.map(([label, value]) => [el("dt", {}, label), el("dd", {}, value)])),
    invoice.line_items.length > 0 && el("table", { class: "ledger compact" },
      el("thead", {}, el("tr", {}, el("th", {}, "Line item"), el("th", { class: "num" }, "Qty"), el("th", { class: "num" }, "Rate"), el("th", { class: "num" }, "Amount"))),
      el("tbody", {}, invoice.line_items.map((item) => el("tr", {},
        el("td", {}, item.description),
        el("td", { class: "num" }, item.quantity ?? ""),
        el("td", { class: "num" }, item.unit_price != null ? money(item.unit_price, currency) : ""),
        el("td", { class: "num" }, item.amount != null ? money(item.amount, currency) : ""))))));
}

function candidatesNode(candidates) {
  return el("details", { class: "more", open: true },
    el("summary", {}, "How each open purchase order scored"),
    el("table", { class: "ledger compact" },
      el("thead", {}, el("tr", {}, el("th", {}, "Purchase order"), el("th", { class: "num" }, "Left on PO"),
        el("th", { class: "num" }, "Amount fit"), el("th", { class: "num" }, "Wording fit"), el("th", { class: "num" }, "Score"))),
      el("tbody", {}, candidates.map((candidate) => el("tr", {},
        el("td", {}, el("strong", {}, candidate.po_number), el("br"), candidate.description),
        el("td", { class: "num" }, money(candidate.remaining)),
        el("td", { class: "num" }, percent(candidate.amount_fit)),
        el("td", { class: "num" }, percent(candidate.wording_fit)),
        el("td", { class: "num" }, el("strong", {}, percent(candidate.score))))))),
    el("p", { class: "fine-print" }, "Score is 60% amount fit and 40% wording fit. A suggestion needs at least 75% and a clear lead."));
}

function decisionCard(run, frame) {
  const { decision, explanation } = run;
  const outcome = OUTCOMES[decision.outcome];
  const others = decision.issues.slice(1);
  const showWriteUp = explanation && explanation.summary !== decision.reason;

  return el("section", { class: `decision tone-${outcome.tone}` },
    el("span", { class: "badge" }, outcome.label),
    el("h2", {}, decision.headline),
    el("p", { class: "decision-reason" }, decision.reason),
    el("h3", {}, "What happens next"),
    el("p", {}, decision.next_step),
    others.length > 0 && [
      el("h3", {}, "Also flagged"),
      el("ul", { class: "plain-list" }, others.map((issue) => el("li", {}, el("strong", {}, issue.title), ". ", issue.message))),
    ],
    showWriteUp && [
      el("h3", {}, "In plain English"),
      el("p", {}, explanation.summary),
      el("p", { class: "fine-print" }, explanation.written_by === "ai"
        ? "Written by AI from the checks above. The decision itself came from the rules."
        : "Written from a template because the AI writer was not available."),
    ],
    run.messages?.length ? run.messages.map(messageNode) : (explanation?.message && draftNode(explanation.message)),
    retryNode(run),
    resolutionNode(run, frame));
}

const RETRYABLE = new Set(["READ_FAILED", "STEP_FAILED"]);

function retryNode(run) {
  if (run.resolution) return null;
  if (run.status !== "failed" && !RETRYABLE.has(run.decision?.primary_code)) return null;
  const again = el("button", { class: "button", type: "button" }, "Run it again");
  again.addEventListener("click", async () => {
    try {
      const { id } = await api(`/api/runs/${run.id}/retry`, { method: "POST" });
      state.liveRunId = id;
      location.hash = `run-${id}`;
    } catch (error) {
      toast(error.message);
    }
  });
  return el("div", { class: "review" },
    el("p", { class: "fine-print" }, "The document couldn't be checked this time, usually because the AI service was busy. " +
      "Running it again starts a fresh run on the same file."),
    el("div", { class: "review-actions" }, again));
}

const MESSAGE_STATUS = {
  sent: { tone: "pass", label: "Sent" },
  draft: { tone: "running", label: "Draft" },
  held: { tone: "pending", label: "Not sent" },
  failed: { tone: "reject", label: "Failed" },
};

function messageNode(message) {
  const status = MESSAGE_STATUS[message.status] || { tone: "pending", label: message.status };
  const heading = message.kind === "vendor" ? "Email to the vendor" : "Note to the PO owner";
  const container = el("div", { class: `draft message-${message.status}` });
  const draw = (current) => {
    const state_ = MESSAGE_STATUS[current.status] || status;
    const copy = el("button", { class: "button quiet small", type: "button" }, "Copy");
    copy.addEventListener("click", async () => {
      await navigator.clipboard.writeText(`To: ${current.to_address || ""}\nSubject: ${current.subject}\n\n${current.body}`);
      copy.textContent = "Copied";
    });
    const canSend = (current.status === "draft" || current.status === "failed") && current.to_address && state.health?.mail?.send_mode !== "off";
    const send = canSend && el("button", { class: "button small", type: "button" }, current.status === "failed" ? "Try again" : "Send");
    if (send) {
      send.addEventListener("click", async () => {
        send.disabled = true;
        try { draw(await postJson(`/api/messages/${current.id}/send`, {})); } catch (error) { toast(error.message); send.disabled = false; }
      });
    }
    container.className = `draft message-${current.status}`;
    container.replaceChildren(
      el("div", { class: "draft-head" },
        el("h3", {}, heading, " ", el("span", { class: `outcome tone-${state_.tone}` },
          current.status === "sent" ? `Sent ${moment(current.sent_at)}` : state_.label)),
        el("div", { class: "review-actions" }, send, copy)),
      current.to_address && el("p", { class: "fine-print" }, `To ${current.to_name ? `${current.to_name} <${current.to_address}>` : current.to_address}`,
        current.in_reply_to ? ", as a reply in the same thread" : ""),
      current.reason && current.status !== "sent" && el("p", { class: "message-reason" }, current.reason),
      el("p", { class: "draft-subject" }, current.subject),
      el("p", { class: "draft-body" }, current.body));
  };
  draw(message);
  return container;
}

function draftNode(message) {
  const copy = el("button", { class: "button quiet", type: "button" }, "Copy");
  copy.addEventListener("click", async () => {
    await navigator.clipboard.writeText(`Subject: ${message.subject}\n\n${message.body}`);
    copy.textContent = "Copied";
  });
  return el("div", { class: "draft" },
    el("div", { class: "draft-head" },
      el("h3", {}, message.to === "vendor" ? "Draft email to the vendor" : "Draft note to the PO owner"), copy),
    el("p", { class: "draft-subject" }, message.subject),
    el("p", { class: "draft-body" }, message.body),
    el("p", { class: "fine-print" }, "A draft for a person to check and send."));
}

function resolutionNode(run, frame) {
  if (run.resolution) {
    const what = { approved: "Approved by a reviewer", rejected: "Rejected by a reviewer", closed: "Closed without a decision" }[run.resolution];
    return el("p", { class: "resolution" }, `${what} on ${moment(run.resolved_at)}.`,
      run.resolution_note ? ` Note: ${run.resolution_note}` : "");
  }
  if (run.outcome !== "needs_review") return null;

  // The same conditions the server enforces, so approval isn't offered when it would be refused.
  const approvalBlocked = !run.vendor_id
    ? "Approval is off because this vendor isn't on the approved list. Onboard them, then resubmit the invoice."
    : (run.total == null || !run.invoice_number || !run.invoice_date)
      ? "Approval is off because key details couldn't be read. Key this invoice in by hand."
      : null;
  const note = el("input", { type: "text", id: "review-note", placeholder: "What did you check? (optional)", maxlength: "300" });
  const resolve = async (approve) => {
    try {
      const updated = await postJson(`/api/runs/${run.id}/resolve`, { approve, note: note.value });
      frame.verdict.replaceChildren();
      paintRun(updated, frame, { revealed: updated.stages.length, live: false });
    } catch (error) {
      toast(error.message);
    }
  };
  return el("div", { class: "review" },
    el("h3", {}, "Your call"),
    el("label", { class: "fine-print", for: "review-note" }, "The process stops here until a person decides. Your decision and note are kept with the run."),
    note,
    el("div", { class: "review-actions" },
      el("button", { class: "button", type: "button", disabled: Boolean(approvalBlocked), onclick: () => resolve(true) },
        "Approve for payment"),
      el("button", { class: "button quiet", type: "button", onclick: () => resolve(false) }, "Reject invoice")),
    approvalBlocked && el("p", { class: "fine-print" }, approvalBlocked));
}

// ---------------------------------------------------------------- runs: the dashboard

const FILTERS = [
  { key: "all", label: "All", test: () => true },
  { key: "approved", label: "Approved", test: (run) => outcomeOf(run).tone === "pass" },
  { key: "waiting", label: "Needs a person", test: (run) => run.outcome === "needs_review" && !run.resolution },
  { key: "rejected", label: "Rejected", test: (run) => outcomeOf(run).tone === "reject" },
];

async function showRuns(view) {
  state.cameFrom = "runs";
  const { stats, runs } = await api("/api/runs");
  let signature = JSON.stringify(runs.map((run) => [run.id, run.status, run.outcome, run.resolution]));
  const stop = refreshEvery(view, 5000, async () => {  // new email or finished runs appear without a reload
    const latest = await api("/api/runs");
    const next = JSON.stringify(latest.runs.map((run) => [run.id, run.status, run.outcome, run.resolution]));
    if (next !== signature) { signature = next; stop(); await showRuns(view); }
  });

  if (!runs.length) {
    view.replaceChildren(el("div", { class: "page" },
      el("h1", {}, "Runs"),
      el("div", { class: "empty" },
        el("p", {}, "No invoices have been processed yet. Every run will be listed here with its outcome, its reason and the full trail of checks."),
        el("a", { class: "button", href: "#inbox" }, "Process an invoice"))));
    return;
  }

  const share = stats.processed ? percent(stats.approved_untouched / stats.processed) : "0%";
  const figures = [
    [stats.processed, "invoices processed"],
    [`${stats.approved_untouched} (${share})`, "approved automatically"],
    [stats.waiting_for_person, "waiting for a person"],
    [stats.rejected, "rejected"],
    [money(stats.approved_value), "approved for payment"],
    [stats.median_seconds != null ? `${stats.median_seconds} s` : "–", "median time per invoice"],
    [stats.by_email, "arrived by email"],
  ];
  const rows = el("tbody");
  const filters = el("div", { class: "filters", role: "group", "aria-label": "Filter runs" });

  const draw = () => {
    const active = FILTERS.find((filter) => filter.key === state.runFilter) || FILTERS[0];
    filters.replaceChildren(...FILTERS.map((filter) => el("button", {
      type: "button", "aria-pressed": String(filter.key === active.key),
      onclick: () => { state.runFilter = filter.key; draw(); },
    }, `${filter.label} (${runs.filter(filter.test).length})`)));
    const visible = runs.filter(active.test);
    rows.replaceChildren(...(visible.length ? visible.map(runRow) : [el("tr", {}, el("td", { colspan: "10", class: "none" }, "No runs match this filter."))]));
  };
  draw();

  view.replaceChildren(el("div", { class: "page" },
    el("h1", {}, "Runs"),
    el("dl", { class: "figures" }, figures.map(([value, label]) => el("div", {}, el("dt", {}, label), el("dd", {}, value)))),
    filters,
    el("div", { class: "table-scroll" },
      el("table", { class: "ledger" },
        el("thead", {}, el("tr", {}, ["Run", "Received", "From", "Vendor", "Invoice", "Amount", "PO", "Outcome", "Why", "Took"]
          .map((heading, index) => el("th", { class: index === 5 || index === 9 ? "num" : null }, heading)))),
        rows))));
}

function runRow(run) {
  const outcome = outcomeOf(run);
  return el("tr", { class: "clickable", onclick: () => { location.hash = `run-${run.id}`; } },
    el("td", {}, el("a", { href: `#run-${run.id}` }, `#${run.id}`)),
    el("td", { class: "nowrap" }, moment(run.created_at)),
    el("td", { class: "source-cell" }, run.source === "email" ? `Email · ${run.email_from || ""}` : run.source === "sample" ? "Sample" : "Upload"),
    el("td", {}, run.vendor_name || run.filename),
    el("td", { class: "nowrap" }, run.invoice_number || ""),
    el("td", { class: "num" }, money(run.total, run.currency)),
    el("td", { class: "nowrap" }, run.po_number || ""),
    el("td", {}, el("span", { class: `outcome tone-${outcome.tone}` }, outcome.label)),
    el("td", { class: "why" }, run.headline || ""),
    el("td", { class: "num" }, seconds(run.duration_ms)));
}

// ---------------------------------------------------------------- purchase orders: what the rules check against

async function showOrders(view) {
  state.cameFrom = "orders";
  const data = await api("/api/procurement");

  const orderRows = data.purchase_orders.map((order) => {
    const used = Math.min(100, Math.round((order.billed / order.amount) * 100));
    return el("tr", {},
      el("td", { class: "nowrap" }, el("strong", {}, order.po_number)),
      el("td", {}, order.vendor_name),
      el("td", {}, order.description, order.owner_name ? el("span", { class: "fine-print block" }, `Raised by ${order.owner_name}`) : null),
      el("td", { class: "num" }, money(order.amount, order.currency)),
      el("td", {},
        el("span", { class: "meter", role: "img", "aria-label": `${used}% billed` }, el("span", { style: `width:${used}%` })),
        el("span", { class: "fine-print" }, `${money(order.billed, order.currency)} on ${order.invoice_count} invoice${order.invoice_count === 1 ? "" : "s"}`)),
      el("td", { class: "num" }, money(order.remaining, order.currency)),
      el("td", {}, order.status === "open" ? "Open" : "Closed"));
  });

  const reset = async (scope, question) => {
    if (!confirm(question)) return;
    try {
      await postJson("/api/demo/reset", { scope });
      await showOrders(view);
      toast(scope === "runs" ? "Run history cleared." : "Purchase orders and recorded invoices are back to their starting state.");
    } catch (error) {
      toast(error.message);
    }
  };

  view.replaceChildren(el("div", { class: "page" },
    el("h1", {}, "Purchase orders and vendors"),
    el("p", { class: "lede" }, `The procurement data every invoice is checked against. Tolerance: up to ${data.tolerance_pct}% of a PO over its remaining balance.`),

    el("h2", {}, "Purchase orders"),
    el("div", { class: "table-scroll" }, el("table", { class: "ledger" },
      el("thead", {}, el("tr", {}, el("th", {}, "PO"), el("th", {}, "Vendor"), el("th", {}, "For"), el("th", { class: "num" }, "Value before tax"),
        el("th", {}, "Billed so far"), el("th", { class: "num" }, "Left"), el("th", {}, "Status"))),
      el("tbody", {}, orderRows))),

    el("h2", {}, "Approved vendors"),
    el("div", { class: "table-scroll" }, el("table", { class: "ledger" },
      el("thead", {}, el("tr", {}, el("th", {}, "Vendor"), el("th", {}, "Tax ID"), el("th", {}, "Bank account on file"), el("th", {}, "Phone on file"))),
      el("tbody", {}, data.vendors.map((vendor) => el("tr", {},
        el("td", {}, vendor.name), el("td", {}, vendor.tax_id), el("td", {}, vendor.bank), el("td", {}, vendor.phone)))))),

    el("h2", {}, "Invoices already recorded"),
    el("div", { class: "table-scroll" }, el("table", { class: "ledger" },
      el("thead", {}, el("tr", {}, el("th", {}, "Recorded"), el("th", {}, "Vendor"), el("th", {}, "Invoice"), el("th", {}, "PO"),
        el("th", { class: "num" }, "Before tax"), el("th", { class: "num" }, "Total"), el("th", {}, "Recorded by"))),
      el("tbody", {}, data.ledger.map((entry) => el("tr", {},
        el("td", {}, day(entry.posted_at)), el("td", {}, entry.vendor_name), el("td", {}, entry.invoice_number),
        el("td", {}, entry.po_number || ""), el("td", { class: "num" }, money(entry.pre_tax)), el("td", { class: "num" }, money(entry.total)),
        el("td", {}, { history: "Before Matchbook", matchbook: "Matchbook, automatically", reviewer: "A reviewer" }[entry.recorded_by])))))),

    el("section", { class: "rehearsal" },
      el("h2", {}, "Rehearsal controls"),
      el("p", { class: "fine-print" }, "Approving an invoice changes the data above, so the same invoice behaves differently the second time: it becomes a duplicate. Restoring puts the data back for another run-through and closes any runs still waiting for a person, so they don't block the same invoices."),
      el("div", { class: "review-actions" },
        el("button", { class: "button quiet", type: "button",
          onclick: () => reset("procurement", "Put purchase orders and recorded invoices back to their starting state? Run history is kept, and runs still waiting for a person are closed.") },
          "Restore purchase orders and recorded invoices"),
        el("button", { class: "button quiet", type: "button",
          onclick: () => reset("runs", "Delete all run history?") }, "Clear run history")))));
}

// ---------------------------------------------------------------- start

api("/api/health").then((health) => {
  state.health = health;
  const status = document.getElementById("ai-status");
  status.textContent = health.ai_ready ? "AI reader ready" : "AI reader is not set up";
  status.title = health.ai_ready
    ? `Reading with ${health.model}${health.fallback_model ? `, backed up by ${health.fallback_model}` : ""}. ` +
      `Runs kept in ${health.database === "postgres" ? "Postgres" : "SQLite"}, files in ${health.storage === "supabase" ? "Supabase Storage" : "a local folder"}.`
    : `Set ${health.key_name} on the server and restart.`;
  status.classList.add(health.ai_ready ? "is-ready" : "is-missing");
  const mail = document.getElementById("mail-status");
  mail.textContent = health.mail.enabled ? "Inbox connected" : "Inbox off";
  mail.title = health.mail.enabled ? `Watching ${health.mail.address}` : "Set MATCHBOOK_MAIL_ADDRESS and MATCHBOOK_MAIL_APP_PASSWORD to receive invoices by email.";
  mail.classList.add(health.mail.enabled && !health.mail.last_error ? "is-ready" : "is-missing");
}).catch(() => {}).finally(route);
