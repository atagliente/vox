"""The page, in one constant.

`report.py` already writes VOX-styled HTML this way and it has held up, so
this follows it rather than inventing a second convention. One file, no build
step, nothing fetched from anywhere — the server sends a Content-Security-
Policy that says so, which turns "we should not add a CDN" from a habit into
something the browser enforces.

The shape is claude.ai's: a session list on the left, one centred column of
messages, a textarea at the bottom that grows with what you type, Enter to
send and Shift+Enter for a newline. The colours are VOX's, and they are the
same ones `report._CSS` uses — a page from `/report` and this page should
look like they came from the same program, because they did.
"""

from __future__ import annotations

STYLE = """
:root {
  color-scheme: dark;
  --bg: #16150f;
  --panel: #1c1a14;
  --line: #2b2820;
  --edge: #45402f;
  --text: #cfc7b0;
  --dim: #8b8266;
  --gold: #c9a15a;
  --green: #8da287;
  --blue: #8ea7bb;
  --red: #c47a5d;
  --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--text);
  font-family: var(--mono); font-size: 14px; line-height: 1.55;
  display: flex; height: 100vh; overflow: hidden;
}

/* ------------------------------------------------------------- sidebar */
#side {
  width: 17rem; flex: 0 0 17rem; border-right: 1px solid var(--line);
  display: flex; flex-direction: column; background: #131209;
}
#side.hidden { display: none; }
#side header {
  padding: .9rem 1rem .6rem; border-bottom: 1px solid var(--line);
}
#side h1 { font-size: .95rem; color: var(--gold); margin: 0; letter-spacing: .14em; }
#side .sub { color: var(--dim); font-size: .78rem; margin-top: .2rem;
             overflow-wrap: anywhere; }
#sessions { flex: 1; overflow-y: auto; padding: .5rem; }
#sessions button {
  display: block; width: 100%; text-align: left; background: none; color: var(--text);
  border: 1px solid transparent; border-radius: 4px; padding: .4rem .5rem;
  font: inherit; cursor: pointer; margin-bottom: .15rem;
}
#sessions button:hover { background: var(--panel); border-color: var(--line); }
#sessions .when { color: var(--dim); font-size: .75rem; }
#side footer { padding: .6rem; border-top: 1px solid var(--line); display: flex;
               gap: .4rem; flex-wrap: wrap; }

button.act {
  background: var(--panel); color: var(--text); border: 1px solid var(--edge);
  border-radius: 4px; padding: .3rem .6rem; font: inherit; cursor: pointer;
}
button.act:hover { border-color: var(--gold); color: var(--gold); }

/* ---------------------------------------------------------------- main */
#main { flex: 1; display: flex; flex-direction: column; min-width: 0; }
#top {
  display: flex; align-items: center; gap: .8rem; padding: .55rem 1rem;
  border-bottom: 1px solid var(--line); color: var(--dim); font-size: .8rem;
}
#top .name { color: var(--gold); letter-spacing: .08em; }
#top .flag { color: var(--dim); }
#top .flag.on { color: var(--green); }
#top .spacer { flex: 1; }

#log { flex: 1; overflow-y: auto; padding: 1.5rem 1rem 2rem; }
#log .inner { max-width: 48rem; margin: 0 auto; }

.msg { margin: 0 0 1.4rem; }
.msg .who {
  color: var(--gold); letter-spacing: .1em; font-size: .72rem;
  text-transform: uppercase; margin-bottom: .25rem;
}
.msg .body { white-space: pre-wrap; overflow-wrap: anywhere; }
.msg.user .body {
  background: var(--panel); border: 1px solid var(--line); border-radius: 6px;
  padding: .6rem .8rem;
}
.msg.user .who { color: var(--blue); }
.msg.system .who, .msg.system .body { color: var(--dim); }
.msg.error .who, .msg.error .body { color: var(--red); }
.msg.tool .body {
  background: var(--panel); border: 1px solid var(--line); border-left: 2px solid var(--green);
  border-radius: 4px; padding: .5rem .7rem; color: var(--dim);
}
details.think { margin: 0 0 .5rem; color: var(--dim); }
details.think summary { cursor: pointer; color: var(--dim); font-size: .78rem;
                        letter-spacing: .08em; }
details.think .body { font-style: italic; margin-top: .4rem; }
.cursor::after { content: "\\2588"; color: var(--gold); animation: blink 1s steps(2) infinite; }
@keyframes blink { 50% { opacity: 0; } }

/* --------------------------------------------------------- confirmation */
#confirm { position: fixed; inset: 0; background: rgba(0,0,0,.72);
           display: none; align-items: center; justify-content: center; padding: 2rem; }
#confirm.open { display: flex; }
#confirm .box {
  background: var(--bg); border: 1px solid var(--gold); border-radius: 6px;
  max-width: 52rem; width: 100%; max-height: 80vh; display: flex; flex-direction: column;
}
#confirm h2 { margin: 0; padding: .8rem 1rem; color: var(--gold); font-size: .9rem;
              letter-spacing: .12em; border-bottom: 1px solid var(--line); }
#confirm pre { margin: 0; padding: 1rem; overflow: auto; flex: 1;
               white-space: pre-wrap; overflow-wrap: anywhere; }
#confirm .diff-add { color: var(--green); }
#confirm .diff-del { color: var(--red); }
#confirm .diff-hunk { color: var(--gold); }
#confirm .buttons { display: flex; gap: .6rem; padding: .8rem 1rem;
                    border-top: 1px solid var(--line); justify-content: flex-end; }
#confirm button { padding: .4rem 1rem; }
#confirm .yes { border-color: var(--green); color: var(--green); }
#confirm .no { border-color: var(--red); color: var(--red); }

/* ---------------------------------------------------------------- input */
#compose { border-top: 1px solid var(--line); padding: .8rem 1rem 1rem; }
#compose .inner { max-width: 48rem; margin: 0 auto; display: flex; gap: .6rem;
                  align-items: flex-end; }
#draft {
  flex: 1; background: var(--panel); color: var(--text); font: inherit;
  border: 1px solid var(--edge); border-radius: 6px; padding: .6rem .8rem;
  resize: none; min-height: 2.6rem; max-height: 18rem; overflow-y: auto;
}
#draft:focus { outline: none; border-color: var(--gold); }
#hint { max-width: 48rem; margin: .4rem auto 0; color: var(--dim); font-size: .74rem; }
"""

SCRIPT = r"""
const $ = (id) => document.getElementById(id);
const log = $("log-inner");
let live = null;      // the assistant message being written
let liveBody = null;
let thinking = null;  // its reasoning block
let state = {};

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

const LABEL = {
  user: "you", assistant: "vox", system: "system", error: "error",
  tool: "tool", reasoning: "thinking", web: "sources", peer: "peer",
};

function addMessage(message) {
  if (message.role === "reasoning") return addThinking(message.content);
  const box = el("div", "msg " + message.role);
  box.appendChild(el("div", "who", LABEL[message.role] || message.role));
  box.appendChild(el("div", "body", message.content || ""));
  log.appendChild(box);
  scroll();
  return box;
}

function addThinking(text) {
  const details = el("details", "think");
  details.appendChild(el("summary", null, "thinking"));
  details.appendChild(el("div", "body", text));
  log.appendChild(details);
  scroll();
  return details;
}

function startLive() {
  if (live) return;
  live = el("div", "msg assistant");
  live.appendChild(el("div", "who", "vox"));
  liveBody = el("div", "body cursor", "");
  live.appendChild(liveBody);
  log.appendChild(live);
}

function scroll() {
  const pane = $("log");
  // Only if the reader is already at the bottom: yanking the view while
  // somebody is reading further up is the most irritating thing a chat
  // window does.
  if (pane.scrollHeight - pane.scrollTop - pane.clientHeight < 120) {
    pane.scrollTop = pane.scrollHeight;
  }
}

function draw(s) {
  state = s;
  $("model").textContent = s.model || "no model";
  $("endpoint").textContent = s.endpoint || "";
  $("workspace").textContent = s.workspace || "";
  $("agent").classList.toggle("on", !!s.agent);
  $("web").classList.toggle("on", !!s.web);
  $("mesh").classList.toggle("on", !!s.mesh);
  $("send").textContent = s.generating ? "stop" : "send";
  if (s.messages) {
    log.innerHTML = "";
    live = null; liveBody = null;
    s.messages.forEach(addMessage);
  }
}

async function post(path, body) {
  await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
}

function submit() {
  if (state.generating) { post("/stop"); return; }
  const box = $("draft");
  const text = box.value.trim();
  if (!text) return;
  box.value = "";
  box.style.height = "auto";
  post("/send", { text: text });
}

function diffHtml(text) {
  // The same four rules the confirmation modal uses in the terminal.
  return text.split("\n").map((line) => {
    const safe = line.replace(/[&<>]/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" })[c]);
    if (line.startsWith("+++") || line.startsWith("---")) return "<b>" + safe + "</b>";
    if (line.startsWith("@@")) return "<span class='diff-hunk'>" + safe + "</span>";
    if (line.startsWith("+")) return "<span class='diff-add'>" + safe + "</span>";
    if (line.startsWith("-")) return "<span class='diff-del'>" + safe + "</span>";
    return safe;
  }).join("\n");
}

let pendingConfirm = null;
function askConfirm(frame) {
  pendingConfirm = frame.id;
  $("confirm-title").textContent = "authorize " + frame.name + "?";
  $("confirm-body").innerHTML = diffHtml(frame.description || "");
  $("confirm").classList.add("open");
  $("confirm-no").focus();
}
function answer(allowed) {
  if (!pendingConfirm) return;
  post("/confirm", { id: pendingConfirm, allowed: allowed });
  pendingConfirm = null;
  $("confirm").classList.remove("open");
}

async function loadSessions() {
  const reply = await fetch("/api/sessions");
  const data = await reply.json();
  const list = $("sessions");
  list.innerHTML = "";
  (data.sessions || []).forEach((s) => {
    const button = el("button");
    button.appendChild(el("div", null, s.title || s.name));
    button.appendChild(el("div", "when", (s.updated || "").slice(0, 16).replace("T", " ")));
    button.onclick = () => post("/command", { text: "/session-load " + s.name });
    list.appendChild(button);
  });
}

function connect() {
  const stream = new EventSource("/events");
  stream.onmessage = (event) => {
    const frame = JSON.parse(event.data);
    switch (frame.kind) {
      case "state": draw(frame.state); break;
      case "message": addMessage(frame.message); break;
      case "reset": log.innerHTML = ""; live = null; break;
      case "delta":
        startLive();
        liveBody.textContent += frame.text;
        scroll();
        break;
      case "reasoning":
        if (!thinking) thinking = addThinking("");
        thinking.querySelector(".body").textContent += frame.text;
        scroll();
        break;
      case "tool": {
        const box = addMessage({ role: "tool", content: frame.name + " " + frame.arguments });
        if (frame.result) box.querySelector(".body").textContent += "\n" + frame.result;
        break;
      }
      case "usage": $("usage").textContent = frame.line || ""; break;
      case "confirm": askConfirm(frame); break;
      case "confirmed": if (frame.id === pendingConfirm) answer(frame.allowed); break;
      case "done":
        if (liveBody) liveBody.classList.remove("cursor");
        live = null; liveBody = null; thinking = null;
        loadSessions();
        break;
      case "view":
        // Phase 4 gives these views a page of their own. Until then, saying
        // that nothing happened beats a button that silently does nothing.
        addMessage({ role: "system", content: frame.name.toUpperCase() +
          " HAS NO VIEW IN THE BROWSER YET" });
        break;
      case "closing": stream.close(); break;
    }
  };
  stream.onerror = () => { /* EventSource reconnects on its own. */ };
}

function wire() {
  const box = $("draft");
  box.addEventListener("input", () => {
    box.style.height = "auto";
    box.style.height = Math.min(box.scrollHeight, 288) + "px";
    post("/draft", { text: box.value });
  });
  box.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      submit();
    }
  });
  $("send").onclick = submit;
  $("new").onclick = () => post("/command", { text: "/new" });
  $("keys").onclick = () => post("/command", { text: "/help" });
  $("toggle-side").onclick = () => $("side").classList.toggle("hidden");
  $("confirm-yes").onclick = () => answer(true);
  $("confirm-no").onclick = () => answer(false);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && pendingConfirm) answer(false);
  });
  loadSessions();
  connect();
  box.focus();
}

document.addEventListener("DOMContentLoaded", wire);
"""

BODY = """
<aside id="side">
  <header>
    <h1>VOX</h1>
    <div class="sub" id="workspace"></div>
  </header>
  <div id="sessions"></div>
  <footer>
    <button class="act" id="new">new</button>
    <button class="act" id="keys">help</button>
  </footer>
</aside>

<div id="main">
  <div id="top">
    <button class="act" id="toggle-side">&#9776;</button>
    <span class="name" id="model">…</span>
    <span id="endpoint"></span>
    <span class="spacer"></span>
    <span class="flag" id="agent">agent</span>
    <span class="flag" id="web">web</span>
    <span class="flag" id="mesh">mesh</span>
    <span id="usage"></span>
  </div>

  <div id="log"><div class="inner" id="log-inner"></div></div>

  <div id="compose">
    <div class="inner">
      <textarea id="draft" rows="1" placeholder="Ask something, or type / for a command"></textarea>
      <button class="act" id="send">send</button>
    </div>
    <div id="hint">enter sends &middot; shift+enter is a newline &middot; / for the commands</div>
  </div>
</div>

<div id="confirm">
  <div class="box">
    <h2 id="confirm-title">authorize?</h2>
    <pre id="confirm-body"></pre>
    <div class="buttons">
      <button class="act no" id="confirm-no">deny (esc)</button>
      <button class="act yes" id="confirm-yes">authorize</button>
    </div>
  </div>
</div>
"""

PAGE = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>VOX</title>
<style>{STYLE}</style>
</head><body>
{BODY}
<script>{SCRIPT}</script>
</body></html>
"""
