"use client";

import { useState } from "react";

// What each layer of the backend is for, in the order a request meets them.
export const LAYERS = {
  auth: { label: "Auth", blurb: "Gatekeeper's own RS256 token: signature, issuer, audience, expiry." },
  database: { label: "DB check", blurb: "User looked up on every request: still active, same role. This is what makes firing instant." },
  rls: { label: "RLS", blurb: "Postgres sets tenant + role for this transaction; policies hide every other row." },
  model: { label: "Model", blurb: "Claude sees the question and tool results only, never who you are." },
  tool: { label: "Tool", blurb: "search_documents runs as you. The model can only choose the query text." },
  audit: { label: "Audit", blurb: "Insert-only log: question and every chunk ID handed to the model." },
  check: { label: "Check", blurb: "Independent re-check of every chunk in Python, separate from RLS." },
};

function Value({ v }) {
  if (v === null || v === undefined) return <span className="v-null">—</span>;
  if (typeof v === "boolean") return <span className={v ? "v-yes" : "v-no"}>{v ? "yes" : "no"}</span>;
  if (typeof v !== "object") return <span>{String(v)}</span>;
  if (Array.isArray(v)) {
    if (!v.length) return <span className="v-null">none</span>;
    if (v.every((x) => x && typeof x === "object" && "ref" in x)) {
      return (
        <span className="mini-tabs">
          {v.map((x, i) => (
            <span key={i} className={`mini-tab s-${x.classification}`} title={`distance ${x.distance}`}>{x.ref}</span>
          ))}
        </span>
      );
    }
    return <ul className="v-list">{v.map((x, i) => <li key={i}><Value v={x} /></li>)}</ul>;
  }
  return (
    <dl className="v-obj">
      {Object.entries(v).map(([k, x]) => (
        <div key={k}><dt>{k.replaceAll("_", " ")}</dt><dd><Value v={x} /></dd></div>
      ))}
    </dl>
  );
}

function Step({ step, total, defaultOpen }) {
  const [open, setOpen] = useState(defaultOpen);
  const left = total ? (step.start_ms / total) * 100 : 0;
  const width = total ? Math.max((step.ms / total) * 100, 0.8) : 0;
  return (
    <li className={`step l-${step.layer}${step.ok ? "" : " failed"}`}>
      <button className="step-head" onClick={() => setOpen(!open)} aria-expanded={open}>
        <span className="layer-chip">{LAYERS[step.layer]?.label || step.layer}</span>
        <span className="step-name">{step.name}</span>
        <span className="step-ms">{step.ms < 1 ? "<1" : Math.round(step.ms)} ms</span>
        <span className="step-bar" aria-hidden><span style={{ left: `${left}%`, width: `${width}%` }} /></span>
      </button>
      {open && (
        <div className="step-details">
          {!step.ok && <p className="step-verdict">✕ Blocked here</p>}
          <Value v={step.details} />
        </div>
      )}
    </li>
  );
}

function Guards({ trace }) {
  const by = (layer) => trace.steps.filter((s) => s.layer === layer);
  const rls = by("rls")[0];
  const tools = by("tool");
  const check = by("check")[0];
  const guards = [
    { name: "Company IdP", ok: true, note: "signed you in" },
    { name: "Token + live check", ok: by("auth").every((s) => s.ok) && by("database").every((s) => s.ok), note: "valid & still active" },
    { name: "Postgres RLS", ok: rls?.ok && rls.details.other_tenants_documents_visible === 0, note: "own company, own clearance" },
    { name: "Tool sandbox", ok: true, note: `${tools.filter((t) => !t.ok).length} bad call(s) refused` },
    { name: "Audit", ok: by("audit").every((s) => s.ok), note: "recorded" },
  ];
  const leaked = check?.details?.forbidden_retrieved ?? 0;
  return (
    <>
      <ol className="guards">
        {guards.map((g, i) => (
          <li key={g.name} className={g.ok ? "ok" : "bad"}>
            <span className="guard-n">{i + 1}</span>
            <span className="guard-name">{g.name}</span>
            <span className="guard-note">{g.note}</span>
          </li>
        ))}
      </ol>
      <p className={`verdict${leaked ? " breach" : ""}`}>
        {leaked ? `⚠ ${leaked} restricted passages reached the model` : "✓ 0 restricted passages reached the model"}
      </p>
    </>
  );
}

export default function Inspector({ entry, number, onClose }) {
  const trace = entry?.trace;
  return (
    <aside className="inspector" aria-label="Under the hood">
      <div className="inspector-head">
        <div>
          <p className="eyebrow">Under the hood</p>
          <h3>{trace ? `Question ${number}` : "How a question travels"}</h3>
        </div>
        {trace && <span className="total">{Math.round(trace.total_ms)} ms</span>}
        {onClose && <button className="inspector-close" onClick={onClose} aria-label="Close">✕</button>}
      </div>

      {!trace ? (
        <>
          <p className="inspector-intro">
            Ask something and this panel shows the backend's real trace: each check, every database search the
            model asked for, and what row-level security let through.
          </p>
          <ol className="pipeline">
            {Object.entries(LAYERS).map(([key, l]) => (
              <li key={key} className={`l-${key}`}>
                <span className="layer-chip">{l.label}</span>
                <span>{l.blurb}</span>
              </li>
            ))}
          </ol>
        </>
      ) : (
        <>
          {entry.question && <p className="inspector-q">“{entry.question}”</p>}
          <Guards trace={trace} />
          <h4 className="timeline-title">Timeline</h4>
          <ol className="timeline">
            {trace.steps.map((s, i) => (
              <Step key={`${number}-${i}`} step={s} total={trace.total_ms}
                    defaultOpen={s.layer === "rls" || !s.ok || (s.layer === "tool" && i === trace.steps.findIndex((x) => x.layer === "tool"))} />
            ))}
          </ol>
          <p className="inspector-foot">Click a step to see its details. Times are measured on the server.</p>
        </>
      )}
    </aside>
  );
}
