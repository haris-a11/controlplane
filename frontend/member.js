/* The member-facing app. It speaks the OpenAI chat protocol to ControlPlane and
   renders what a customer is entitled to see — nothing more.
 *
 * The response carries a full `controlplane` block: tier path, grounding score,
 * judge verdict, span scores, cost. None of it is rendered here, deliberately.
 * A customer is not an auditor. What they get is the *effect* of the decision:
 *   pass     → the answer
 *   abstain  → the answer (an honest "your policy does not cover this")
 *   annotate → the answer plus the advisory the router attached
 *   redact   → the answer with personal identifiers removed, and a note saying so
 *   repair   → the corrected answer, silently — the wrong one never reached them
 *   block    → a held-for-review message and a case reference
 *
 * Everything withheld here is shown at /console, which is the point of splitting
 * the two surfaces apart.
 */

const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"]/g,
  c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

const SUGGESTIONS = [
  "When is maternity covered?",
  "What is the room rent cap?",
  "Since cataract has no waiting period, can I claim in month 2?",
  "Are ambulance charges reimbursed?",
  "How long is the waiting period for pre-existing conditions?",
];

$("#suggest").innerHTML = SUGGESTIONS.map((s, i) =>
  `<button data-i="${i}">${esc(s)}</button>`).join("");
$("#suggest").onclick = e => {
  const b = e.target.closest("button");
  if (!b) return;
  $("#q").value = SUGGESTIONS[+b.dataset.i];
  $("#q").focus();
};

/* A deployment normally fixes its use case in config. The demo bar can override
   it, and remembers the choice so a reload does not silently change profile. */
const saved = new URLSearchParams(location.search).get("use_case")
  || localStorage.getItem("cp_use_case");
if (saved) $("#uc").value = saved;
$("#uc").onchange = () => localStorage.setItem("cp_use_case", $("#uc").value);

$("#q").addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
});
$("#q").addEventListener("input", () => {
  $("#q").style.height = "auto";
  $("#q").style.height = Math.min($("#q").scrollHeight, 160) + "px";
});
$("#send").onclick = send;

function add(html, cls = "") {
  const d = document.createElement("div");
  d.className = `turn ${cls}`;
  d.innerHTML = html;
  $("#thread").append(d);
  $("#thread").scrollTop = $("#thread").scrollHeight;
  return d;
}

async function send() {
  const text = $("#q").value.trim();
  if (!text) return;
  $("#welcome").style.display = "none";
  $("#q").value = "";
  $("#q").style.height = "auto";
  $("#send").disabled = true;
  add(`<div class="said">${esc(text)}</div>`, "you");

  const waiting = add(
    `<div class="thinking"><i></i><i></i><i></i> Checking your policy…</div>`, "them");

  try {
    const r = await fetch("/v1/chat/completions", {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "x-controlplane-use-case": $("#uc").value,
        "x-controlplane-user": $("#user").value.trim() || "anon",
      },
      body: JSON.stringify({ messages: [{ role: "user", content: text }] }),
    });
    const body = await r.json();
    if (!r.ok) throw new Error(body.detail || `request failed (${r.status})`);
    waiting.remove();
    render(body.choices[0].message.content, body.controlplane);
  } catch (e) {
    waiting.remove();
    add(`<div class="oops">Sorry — we could not reach member support just now.
         ${esc(e.message)}</div>`);
  } finally {
    $("#send").disabled = false;
    $("#q").focus();
  }
}

/** Split the router's appended advisory off the answer body so it can be shown
    as a callout rather than as more prose. */
function splitAdvice(answer) {
  const [body, ...notes] = String(answer).split("\n\n> ⚠️ ");
  return { body, notes };
}

/** What the member sees, per outcome. The stamp is derived from the real
    decision — it is a description of what happened, not a badge. */
function stampFor(cp) {
  if (cp.action === "block")
    return { cls: "held", text: "Held for review by a member services agent" };
  if (cp.action === "abstain")
    return { cls: "warn", text: "Your policy document does not cover this" };
  if (cp.action === "annotate")
    return { cls: "warn", text: "Released with an advisory — see the note above" };
  if (cp.action === "redact")
    return { cls: "warn", text: "Personal identifiers were removed from this reply" };
  if (cp.action === "repair")
    return { cls: "", text: "Re-checked against your policy document before sending" };
  if (cp.tier1_ran)
    return { cls: "", text: "Checked against your policy document" };
  return { cls: "", text: "Checked" };
}

function render(answer, cp) {
  const { body, notes } = splitAdvice(answer);
  const held = cp.action === "block";
  const stamp = stampFor(cp);

  const advisories = notes.map(n =>
    `<div class="advice"><span class="ico">⚠️</span>
       <span><b>Please note.</b> ${esc(n)}</span></div>`).join("");

  const redactNote = cp.action === "redact" ? `<div class="advice">
      <span class="ico">🔒</span><span><b>Personal details removed.</b>
      This reply mentioned identifiers such as a policy number or email address,
      so they were taken out before it was sent to you.</span></div>` : "";

  const caseRef = held ? `<div class="caseref">Reference
      <code>MHA-${String(cp.decision_id).padStart(6, "0")}</code> — an agent will
      follow up. Quote this reference if you contact us.</div>` : "";

  add(`<div class="said">${esc(body)}${advisories}${redactNote}${caseRef}</div>
       <div class="stamp ${stamp.cls}"><span class="dot"></span>${esc(stamp.text)}</div>`,
      `them${held ? " held" : ""}`);
}

$("#q").focus();
