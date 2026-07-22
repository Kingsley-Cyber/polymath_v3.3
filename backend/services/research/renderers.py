"""Research artifact renderers."""

from __future__ import annotations

import json
from datetime import datetime
from html import escape
from typing import Any

from models.research import ResearchJob


def _line(value: Any) -> str:
    return str(value or "").replace("\n", " ").strip()


def _html(value: Any) -> str:
    return escape(_line(value))


def render_markdown_report(
    *,
    job: ResearchJob,
    subquestions: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    graph_traces: list[dict[str, Any]],
    caveats: list[str],
) -> str:
    lines: list[str] = [
        f"# Research Report: {_line(job.question)}",
        "",
        f"- Job: `{job.job_id}`",
        f"- Mode: `{job.mode}`",
        f"- Corpora: {', '.join(job.corpus_ids) if job.corpus_ids else 'all accessible/requested'}",
        f"- Generated: {datetime.utcnow().isoformat()}Z",
        "",
        "## Executive answer",
        "",
    ]
    if evidence:
        first_refs = ", ".join(f"[{item['citation_id']}]" for item in evidence[:5])
        lines.append(
            "The corpus contains directly retrieved evidence relevant to the question. "
            f"Start with {first_refs}; the evidence table below preserves source scope."
        )
    else:
        lines.append(
            "No retrieval evidence was returned for this run. Treat this as an empty-evidence artifact, not a grounded answer."
        )
    lines.extend(["", "## Subquestions", ""])
    for item in subquestions:
        lines.append(f"- `{item['id']}` {item['question']} ({item['purpose']})")
    lines.extend(["", "## Findings", ""])
    if evidence:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in evidence:
            grouped.setdefault(str(item.get("subquestion_id") or "sq"), []).append(item)
        for subq in subquestions:
            rows = grouped.get(subq["id"], [])[:4]
            if not rows:
                continue
            refs = ", ".join(f"[{row['citation_id']}]" for row in rows)
            lines.append(f"- {subq['question']} Evidence: {refs}.")
    else:
        lines.append("- No evidence-backed findings could be rendered.")
    lines.extend(["", "## Graph traversal", ""])
    if graph_traces:
        for trace in graph_traces[:12]:
            lines.append(
                "- "
                f"`{trace.get('subquestion_id')}` corpus `{trace.get('corpus_id')}`: "
                f"{trace.get('seed_count', 0)} seeds, "
                f"{trace.get('node_count', 0)} nodes, "
                f"{trace.get('edge_count', 0)} edges, "
                f"status `{trace.get('status')}`."
            )
    else:
        lines.append("- Graph traversal did not run or returned no graph packets.")
    lines.extend(["", "## Evidence table", ""])
    if evidence:
        lines.append("| Citation | Corpus | Doc | Chunk | Evidence |")
        lines.append("|---|---|---|---|---|")
        for item in evidence:
            lines.append(
                "| "
                f"[{item['citation_id']}] | "
                f"`{_line(item.get('corpus_id'))}` | "
                f"`{_line(item.get('doc_id'))}` | "
                f"`{_line(item.get('chunk_id'))}` | "
                f"{_line(item.get('quote'))[:500]} |"
            )
    else:
        lines.append("No evidence rows.")
    lines.extend(["", "## Caveats", ""])
    if caveats:
        for caveat in caveats:
            lines.append(f"- {caveat}")
    else:
        lines.append("- This first worker pass uses deterministic retrieval/graph evidence and does not yet run an LLM synthesis rewrite.")
    lines.extend(["", "## Artifact metadata", ""])
    lines.append("- Schema: `polymath.research_report.v1`")
    lines.append("- Citation scope: corpus/doc/chunk IDs")
    return "\n".join(lines) + "\n"


def render_json_report(
    *,
    job: ResearchJob,
    subquestions: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    graph_traces: list[dict[str, Any]],
    caveats: list[str],
) -> str:
    return json.dumps(
        {
            "schema": "polymath.research_report.v1",
            "job": job.model_dump(mode="json"),
            "subquestions": subquestions,
            "evidence_ledger": evidence,
            "graph_trace": graph_traces,
            "caveats": caveats,
        },
        indent=2,
        sort_keys=True,
    )


def render_html_report(
    *,
    job: ResearchJob,
    subquestions: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    graph_traces: list[dict[str, Any]],
    caveats: list[str],
) -> str:
    corpus_label = (
        ", ".join(_html(corpus_id) for corpus_id in job.corpus_ids)
        if job.corpus_ids
        else "all accessible/requested"
    )
    subquestion_rows = "\n".join(
        "<li><code>{id}</code> {question} <span>{purpose}</span></li>".format(
            id=_html(item.get("id")),
            question=_html(item.get("question")),
            purpose=_html(item.get("purpose")),
        )
        for item in subquestions
    )
    if evidence:
        evidence_rows = "\n".join(
            "<tr>"
            f"<td>[{_html(item.get('citation_id'))}]</td>"
            f"<td>{_html(item.get('corpus_id'))}</td>"
            f"<td>{_html(item.get('doc_id'))}</td>"
            f"<td>{_html(item.get('chunk_id'))}</td>"
            f"<td>{_html(item.get('quote'))[:600]}</td>"
            "</tr>"
            for item in evidence
        )
        executive = (
            "The corpus contains directly retrieved evidence relevant to the "
            "question. Use the citation ledger below to audit each claim."
        )
    else:
        evidence_rows = (
            "<tr><td colspan=\"5\">No retrieval evidence was returned.</td></tr>"
        )
        executive = (
            "No retrieval evidence was returned for this run. Treat this as an "
            "empty-evidence artifact, not a grounded answer."
        )
    if graph_traces:
        graph_rows = "\n".join(
            "<li><code>{subq}</code> corpus <code>{corpus}</code>: "
            "{seeds} seeds, {nodes} nodes, {edges} edges, status <code>{status}</code>.</li>".format(
                subq=_html(trace.get("subquestion_id")),
                corpus=_html(trace.get("corpus_id")),
                seeds=_html(trace.get("seed_count", 0)),
                nodes=_html(trace.get("node_count", 0)),
                edges=_html(trace.get("edge_count", 0)),
                status=_html(trace.get("status")),
            )
            for trace in graph_traces[:12]
        )
    else:
        graph_rows = "<li>Graph traversal did not run or returned no graph packets.</li>"
    caveat_rows = "\n".join(f"<li>{_html(caveat)}</li>" for caveat in caveats) or (
        "<li>This first worker pass uses deterministic retrieval/graph evidence "
        "and does not yet run an LLM synthesis rewrite.</li>"
    )
    generated_at = f"{datetime.utcnow().isoformat()}Z"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Research Report</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #10131a;
      --panel: #171c26;
      --ink: #edf2ff;
      --muted: #98a2b3;
      --line: #2a3344;
      --accent: #7dd3fc;
    }}
    body {{
      margin: 0;
      background: radial-gradient(circle at top left, #1e2b3d, var(--bg) 48rem);
      color: var(--ink);
      font: 15px/1.55 ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    main {{
      max-width: 1120px;
      margin: 0 auto;
      padding: 48px 24px 72px;
    }}
    section {{
      margin-top: 24px;
      border: 1px solid var(--line);
      border-radius: 20px;
      background: color-mix(in srgb, var(--panel) 92%, transparent);
      padding: 22px;
    }}
    h1, h2 {{
      line-height: 1.12;
      margin: 0;
    }}
    h1 {{
      font-size: clamp(2rem, 5vw, 4rem);
      letter-spacing: -0.05em;
    }}
    h2 {{
      color: var(--accent);
      font-size: 0.8rem;
      letter-spacing: 0.18em;
      text-transform: uppercase;
    }}
    .meta, code, span {{
      color: var(--muted);
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      overflow-wrap: anywhere;
    }}
    th, td {{
      border-top: 1px solid var(--line);
      padding: 10px;
      text-align: left;
      vertical-align: top;
    }}
    th {{
      color: var(--accent);
      font-size: 0.75rem;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }}
    li + li {{
      margin-top: 8px;
    }}
  </style>
</head>
<body>
  <main>
    <p class="meta">Schema polymath.research_report.v1 / Generated {escape(generated_at)}</p>
    <h1>{_html(job.question)}</h1>
    <p class="meta">Job <code>{_html(job.job_id)}</code> / Mode <code>{_html(job.mode)}</code> / Corpora {corpus_label}</p>
    <section>
      <h2>Executive answer</h2>
      <p>{escape(executive)}</p>
    </section>
    <section>
      <h2>Subquestions</h2>
      <ul>{subquestion_rows}</ul>
    </section>
    <section>
      <h2>Graph traversal</h2>
      <ul>{graph_rows}</ul>
    </section>
    <section>
      <h2>Evidence table</h2>
      <table>
        <thead>
          <tr><th>Citation</th><th>Corpus</th><th>Doc</th><th>Chunk</th><th>Evidence</th></tr>
        </thead>
        <tbody>{evidence_rows}</tbody>
      </table>
    </section>
    <section>
      <h2>Caveats</h2>
      <ul>{caveat_rows}</ul>
    </section>
  </main>
</body>
</html>
"""
