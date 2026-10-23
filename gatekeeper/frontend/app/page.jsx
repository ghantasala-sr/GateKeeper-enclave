"use client";

import { Fragment, useCallback, useEffect, useRef, useState } from "react";

import Inspector from "./inspector";

const API = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

const LEVELS = [
  { key: "public", label: "Public", short: "PUB" },
  { key: "hr_confidential", label: "HR confidential", short: "HR" },
  { key: "executive", label: "Executive", short: "EXEC" },
];
const CLEARANCE = {
  employee: ["public"],
  hr_manager: ["public", "hr_confidential"],
  admin: ["public", "hr_confidential", "executive"],
};
const ROLE_LABEL = { employee: "Employee", hr_manager: "HR manager", admin: "Administrator" };
const IDP_LABEL = { okta_oidc: "Okta · OIDC", entra_saml: "SAML" };

const SUGGESTIONS = [
  { q: "How many PTO days do I get, and can I carry any over?", level: "public" },
  { q: "What are the senior engineer salary bands?", level: "hr_confidential" },
  { q: "What is the Q3 revenue target?", level: "executive" },
];

// The login result the API put in the URL fragment. layout.jsx has already removed it
// from the address bar before the app started; take it (once) from where it parked it.
function takeLoginFragment() {
  if (typeof window === "undefined" || !window.__gkLogin) return {};
  const params = new URLSearchParams(window.__gkLogin);
  delete window.__gkLogin;
  return {
    token: params.get("access_token"),
    expiresIn: Number(params.get("expires_in") || 0),
    error: params.get("error"),
  };
}

function tenantClass(name = "") {
  return name.toLowerCase().startsWith("globex") ? "t-globex" : "t-acme";
}

/* ---------- small pieces ---------- */

function Stamp({ level, dim }) {
  const l = LEVELS.find((x) => x.key === level);
  return (
    <span className={`stamp s-${level}${dim ? " dim" : ""}`} title={l?.label}>
      <span className="stamp-long">{l ? l.label : level}</span>
      <span className="stamp-short" aria-hidden>{l ? l.short : level}</span>
    </span>
  );
}

function ClearanceBar({ role }) {
  const cleared = CLEARANCE[role] || [];
  return (
    <span className="clearance" aria-label={`Cleared for ${cleared.join(", ")}`}>
      {LEVELS.map((l) => (
        <span key={l.key} className={`notch s-${l.key}${cleared.includes(l.key) ? " on" : ""}`} title={l.label}>
          {l.short}
        </span>
      ))}
    </span>
  );
}

// Answers come back as light Markdown. Render bullets, **bold**, and [REF] citations; nothing else.
function Inline({ text }) {
  const parts = text.split(/(\*\*[^*]+\*\*|\[[A-Z]+-[A-Z]+-\d+\])/g);
  return parts.map((p, i) => {
    if (p.startsWith("**") && p.endsWith("**")) return <strong key={i}>{p.slice(2, -2)}</strong>;
    if (/^\[[A-Z]+-[A-Z]+-\d+\]$/.test(p)) return <span key={i} className="cite">{p.slice(1, -1)}</span>;
    return <Fragment key={i}>{p}</Fragment>;
  });
}

function Answer({ text }) {
  const blocks = [];
  for (const line of text.split("\n")) {
    const bullet = line.match(/^\s*[-*]\s+(.*)/);
    const last = blocks[blocks.length - 1];
    if (bullet) {
      if (last?.type === "ul") last.items.push(bullet[1]);
      else blocks.push({ type: "ul", items: [bullet[1]] });
    } else if (line.trim().startsWith("|")) {
      if (/^\s*\|[\s|:-]+\|\s*$/.test(line)) continue; // table divider row
      const cells = line.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
      if (last?.type === "table") last.rows.push(cells);
      else blocks.push({ type: "table", rows: [cells] });
    } else if (line.trim()) {
      blocks.push({ type: "p", text: line });
    }
  }
  return blocks.map((b, i) => {
    if (b.type === "ul") return <ul key={i}>{b.items.map((t, j) => <li key={j}><Inline text={t} /></li>)}</ul>;
    if (b.type === "table")
      return (
        <table key={i}>
          <tbody>{b.rows.map((r, j) => <tr key={j}>{r.map((c, k) => (j === 0 ? <th key={k}><Inline text={c} /></th> : <td key={k}><Inline text={c} /></td>))}</tr>)}</tbody>
        </table>
      );
    return <p key={i}><Inline text={b.text} /></p>;
  });
}

/* ---------- login ---------- */

function Login({ notice }) {
  return (
    <main className="login">
      <section className="login-intro">
        <p className="eyebrow">Gatekeeper · Records desk</p>
        <h1>
          Every answer is <em>cleared at the door.</em>
        </h1>
        <p className="lede">
          Ask about your company's handbook, pay bands, or plans. Before the assistant reads a single page, the
          database removes everything your badge doesn't open. What it never sees, it can't repeat.
        </p>

        <table className="matrix" aria-label="Who can read what">
          <thead>
            <tr>
              <th />
              {LEVELS.map((l) => <th key={l.key}><Stamp level={l.key} /></th>)}
            </tr>
          </thead>
          <tbody>
            {Object.entries(CLEARANCE).map(([role, levels]) => (
              <tr key={role}>
                <th scope="row">{ROLE_LABEL[role]}</th>
                {LEVELS.map((l) => <td key={l.key}>{levels.includes(l.key) ? "●" : "—"}</td>)}
              </tr>
            ))}
          </tbody>
        </table>
        <p className="footnote">Nobody, not even an administrator, can read another company's records.</p>
      </section>

      <section className="login-desk">
        {notice && (
          <div className="revoked" role="alert">
            <span className="revoked-stamp">{notice.title}</span>
            <p>{notice.text}</p>
          </div>
        )}
        <p className="desk-label">Present your company badge</p>
        <a className="idcard t-acme" href={`${API}/auth/okta/login?next=app`} aria-label="Sign in to Acme Corp with Okta">
          <span className="slot" aria-hidden />
          <span className="idcard-company">Acme Corp</span>
          <span className="idcard-meta">Sign in with Okta · OIDC</span>
          <span className="idcard-go">Continue →</span>
        </a>
        <a className="idcard t-globex" href={`${API}/auth/saml/login?next=app`} aria-label="Sign in to Globex with SAML">
          <span className="slot" aria-hidden />
          <span className="idcard-company">Globex</span>
          <span className="idcard-meta">Sign in with SAML</span>
          <span className="idcard-go">Continue →</span>
        </a>
        <p className="desk-note">Your company's identity provider vouches for you. Gatekeeper never sees your password.</p>
      </section>
    </main>
  );
}

/* ---------- desk (signed in) ---------- */

export default function Home() {
  // The access token lives in memory only: not localStorage, not a cookie.
  const [token, setToken] = useState(null);
  const [expiresAt, setExpiresAt] = useState(null);
  const [me, setMe] = useState(null);
  const [notice, setNotice] = useState(null);
  const [messages, setMessages] = useState([]);
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);
  const [selected, setSelected] = useState(null); // index of the answer shown in the inspector
  const [inspectorOpen, setInspectorOpen] = useState(false); // small screens only
  const endRef = useRef(null);
  const inputRef = useRef(null);

  // notice: { title, text } shown on the sign-in screen, e.g. after access is withdrawn.
  const logOut = useCallback((notice) => {
    setToken(null);
    setExpiresAt(null);
    setMe(null);
    setMessages([]);
    setSelected(null);
    setNotice(notice || null);
  }, []);

  useEffect(() => {
    const { token, expiresIn, error } = takeLoginFragment();
    if (error) {
      const reason = error.replace(/^Login refused:\s*/, "");
      setNotice({ title: "Sign-in refused", text: reason.charAt(0).toUpperCase() + reason.slice(1) + "." });
    }
    if (!token) return;
    setToken(token);
    setExpiresAt(Date.now() + expiresIn * 1000);
  }, []);

  // Drop the token when it expires (15 minutes) rather than waiting for a 401.
  useEffect(() => {
    if (!token || !expiresAt) return;
    const timer = setTimeout(
      () => logOut({ title: "Session expired", text: "Sessions last 15 minutes. Sign in again to continue." }),
      expiresAt - Date.now(),
    );
    return () => clearTimeout(timer);
  }, [token, expiresAt, logOut]);

  useEffect(() => {
    if (!token) return;
    fetch(`${API}/me`, { headers: { Authorization: `Bearer ${token}` } })
      .then(async (r) => (r.ok ? setMe(await r.json()) : logOut({ title: "Access ended", text: (await r.json()).detail })))
      .catch(() => logOut({ title: "Unavailable", text: "Can't reach the Gatekeeper API." }));
  }, [token, logOut]);

  // Show each new answer's trace in the inspector as it arrives.
  useEffect(() => {
    const last = messages.length - 1;
    if (last >= 0 && messages[last].trace) setSelected(last);
  }, [messages]);

  useEffect(() => {
    // Braces matter: scrollIntoView can return a Promise, and an effect may only return a cleanup function.
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, busy]);

  async function ask(q) {
    q = q.trim();
    if (!q || busy) return;
    setQuestion("");
    setMessages((m) => [...m, { from: "user", text: q }]);
    setBusy(true);
    try {
      const r = await fetch(`${API}/chat`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
        body: JSON.stringify({ question: q }),
      });
      const body = await r.json();
      if (r.status === 401) {
        // Deactivated in the IdP, demoted, or expired: the API said no.
        logOut({ title: "Access ended", text: `${body.detail}. Your access was withdrawn mid-session.` });
        return;
      }
      if (!r.ok) throw new Error(body.detail || `HTTP ${r.status}`);
      const sources = [];
      for (const c of body.chunks) if (!sources.some((s) => s.ref === c.reference)) sources.push({ ref: c.reference, level: c.classification });
      setMessages((m) => [...m, { from: "desk", text: body.answer, sources, withheld: body.forbidden_retrieved, trace: body.trace, question: q }]);
    } catch (err) {
      setMessages((m) => [...m, { from: "desk", text: `Something went wrong: ${err.message}`, error: true }]);
    } finally {
      setBusy(false);
      inputRef.current?.focus();
    }
  }

  if (!token) return <Login notice={notice} />;

  const shown = selected !== null ? messages[selected] : null;

  return (
    <div className={`desk ${tenantClass(me?.tenant)}`}>
      <header className="desk-head">
        <span className="wordmark">Gatekeeper</span>
        {me && (
          <div className="lanyard" title={me.email}>
            <span className="lanyard-company">{me.tenant}</span>
            <span className="lanyard-role">{ROLE_LABEL[me.role] || me.role}</span>
            <ClearanceBar role={me.role} />
            <span className="lanyard-idp">{IDP_LABEL[me.idp_type] || me.idp_type}</span>
          </div>
        )}
        <button className="inspector-toggle" onClick={() => setInspectorOpen(true)}>Under the hood</button>
        <button className="signout" onClick={() => logOut()}>Sign out</button>
      </header>

      <div className="desk-body">
      <div className="desk-main">
      <main className="ledger" aria-live="polite">
        {messages.length === 0 && (
          <section className="welcome">
            <h2>{me ? <>Good to see you, <em>{me.email.split("@")[0]}</em>.</> : "Opening your file…"}</h2>
            <p>Ask anything about {me?.tenant || "your company"}'s records. Try one of these; some may be above your clearance, and that's the point.</p>
            {me?.last_login_at && (
              <p className="signed-in">Signed in {new Date(me.last_login_at).toLocaleString([], { dateStyle: "medium", timeStyle: "short" })} through {me.idp_type === "entra_saml" ? "SAML" : "OIDC"}.</p>
            )}
            <div className="suggestions">
              {SUGGESTIONS.map((s) => (
                <button key={s.q} className="suggestion" onClick={() => ask(s.q)} disabled={busy}>
                  <Stamp level={s.level} dim={me && !(CLEARANCE[me.role] || []).includes(s.level)} />
                  <span>{s.q}</span>
                </button>
              ))}
            </div>
          </section>
        )}

        {messages.map((m, i) =>
          m.from === "user" ? (
            <div key={i} className="entry ask">
              <span className="entry-label">Q</span>
              <p>{m.text}</p>
            </div>
          ) : (
            <article key={i} className={`entry memo${m.error ? " memo-error" : ""}${selected === i ? " selected" : ""}`}>
              <span className="entry-label">A</span>
              <div className="memo-body" onClick={() => m.trace && setSelected(i)}
                   role={m.trace ? "button" : undefined} tabIndex={m.trace ? 0 : undefined}
                   onKeyDown={(e) => m.trace && (e.key === "Enter" || e.key === " ") && setSelected(i)}
                   aria-label={m.trace ? "Show this answer's trace" : undefined}>
                <Answer text={m.text} />
                {m.sources && (
                  <footer className="memo-foot">
                    <div className="tabs">
                      {m.sources.length ? (
                        m.sources.map((s) => (
                          <span key={s.ref} className={`filetab s-${s.level}`}>
                            <span className="filetab-ref">{s.ref}</span>
                            <span className="filetab-level">{LEVELS.find((l) => l.key === s.level)?.short}</span>
                          </span>
                        ))
                      ) : (
                        <span className="no-files">No files opened</span>
                      )}
                    </div>
                    <span className={`seal${m.withheld ? " breach" : ""}`}>
                      {m.withheld ? `⚠ ${m.withheld} restricted passages reached the model` : "✓ Cleared by the database · 0 restricted passages"}
                    </span>
                  </footer>
                )}
              </div>
            </article>
          ),
        )}

        {busy && (
          <div className="entry memo pending">
            <span className="entry-label">A</span>
            <p className="pulling">Pulling files you're cleared for…</p>
          </div>
        )}
        <div ref={endRef} />
      </main>

      <form className="composer" onSubmit={(e) => { e.preventDefault(); ask(question); }}>
        <input
          ref={inputRef}
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder={`Ask about ${me?.tenant || "your company"}'s records…`}
          aria-label="Question"
          disabled={busy}
          autoFocus
        />
        <button type="submit" disabled={busy || !question.trim()}>Ask</button>
      </form>
      </div>

      <div className={`inspector-wrap${inspectorOpen ? " open" : ""}`}>
        <Inspector entry={shown} number={selected === null ? 0 : messages.slice(0, selected + 1).filter((m) => m.trace).length}
                   onClose={inspectorOpen ? () => setInspectorOpen(false) : null} />
      </div>
      </div>
    </div>
  );
}
