async function fetchSnapshot() {
  const res = await fetch("/api/snapshot", { cache: "no-store" });
  if (!res.ok) throw new Error(`snapshot ${res.status}`);
  return res.json();
}

function chip(status) {
  const s = status || "unknown";
  return `<span class="chip ${s}">${s}</span>`;
}

function renderRoster(snap) {
  const el = document.getElementById("roster");
  const rows = snap.instances || [];
  if (!rows.length) {
    el.innerHTML = `<p class="muted">No instances yet. Claim hub / spawn to populate sessions.json.</p>`;
    return;
  }
  const claim = snap.claim || {};
  const body = rows
    .map((i) => {
      const isHub = i.role === "orchestrator" || i.intercomName === "orchestrator";
      let badge = "";
      if (isHub) {
        badge = claim.bound
          ? `<span class="badge on">claimed · ${claim.surface || ""}</span>`
          : `<span class="badge">unclaimed</span>`;
      }
      return `<tr>
        <td><strong>${i.intercomName || "?"}</strong>${badge}<div class="muted">${i.role || ""} · ${i.lifecycle || ""}</div></td>
        <td>${chip(i.status)}</td>
        <td>${i.taskId || "—"}</td>
        <td class="muted">${i.cmuxSurface || "—"}</td>
      </tr>`;
    })
    .join("");
  el.innerHTML = `<table>
    <thead><tr><th>Instance</th><th>Status</th><th>taskId</th><th>Surface</th></tr></thead>
    <tbody>${body}</tbody>
  </table>`;
}

function renderBus(snap) {
  const el = document.getElementById("bus");
  const inbox = snap.inbox || {};
  const names = Object.keys(inbox);
  if (!names.length) {
    el.innerHTML = `<p class="muted">No inbox folders yet.</p>`;
    return;
  }
  el.innerHTML = names
    .map((name) => {
      const stages = inbox[name];
      const summary = ["pending", "processing", "done"]
        .map((s) => `${s}: ${(stages[s] && stages[s].count) || 0}`)
        .join(" · ");
      const details = ["pending", "processing", "done"]
        .map((s) => {
          const msgs = (stages[s] && stages[s].messages) || [];
          if (!msgs.length) return "";
          const lis = msgs
            .map(
              (m) =>
                `<li><code>${m.type}</code> ${m.from || "?"}→${m.to || "?"} · ${m.taskId || "—"}</li>`
            )
            .join("");
          return `<div><strong>${s}</strong><ul>${lis}</ul></div>`;
        })
        .join("");
      return `<details open>
        <summary><strong>${name}</strong> <span class="muted">${summary}</span></summary>
        ${details || `<p class="muted">Empty</p>`}
      </details>`;
    })
    .join("");
}

function renderTimeline(snap) {
  const el = document.getElementById("timeline");
  const tl = snap.timeline || {};
  const events = tl.events || [];
  if (!events.length) {
    el.innerHTML = `<p class="muted">${tl.emptyCopy || "No events yet."}</p>`;
    return;
  }
  el.innerHTML = events
    .slice()
    .reverse()
    .map((e) => {
      const bits = [e.ts, e.type, e.instance || "", e.taskId || ""].filter(Boolean);
      return `<div class="event">${bits.join(" · ")}</div>`;
    })
    .join("");
}

function renderMeta(snap) {
  const el = document.getElementById("meta");
  const claim = snap.claim?.bound ? "hub claimed" : "hub unclaimed";
  el.textContent = `${snap.agencyRoot} · ${claim} · lastSnapshotAt ${snap.lastSnapshotAt}`;
}

async function tick() {
  const err = document.getElementById("error");
  try {
    const snap = await fetchSnapshot();
    err.hidden = true;
    renderMeta(snap);
    renderRoster(snap);
    renderBus(snap);
    renderTimeline(snap);
  } catch (e) {
    err.hidden = false;
    err.textContent = `Failed to load snapshot: ${e.message}. Retrying…`;
  }
}

tick();
setInterval(tick, 2000);

try {
  const es = new EventSource("/api/events/stream");
  es.onmessage = () => {
    tick();
  };
} catch (_) {
  /* poll-only fallback */
}
