"""Operator-only readiness view; reuse the private dashboard's HTTP/auth boundary."""

from html import escape


def render(data):
    cards = []
    for item in data["profiles"]:
        state = "Eligible release" if item["ready"] else "Blocked"
        reasons = (
            ", ".join(item["reasons"])
            or "Current evaluation and independent approval are valid."
        )
        slo = item.get("service_level", {})
        rate = slo.get("success_rate")
        cards.append(f"""<article><h2>{escape(str(item["profile"]))}</h2>
        <strong>{state}</strong><p>{escape(reasons)}</p>
        <dl><dt>Outcome sample</dt><dd>{slo.get("successes", 0)} / {slo.get("sample_count", 0)} verified terminal tasks</dd>
        <dt>Success rate</dt><dd>{"Unknown" if rate is None else format(rate, ".1%")}</dd>
        <dt>Service target</dt><dd>{escape(str(slo.get("status", "unknown")))}</dd>
        <dt>Release</dt><dd><code>{escape(str(item.get("release", "Unspecified")))}</code></dd></dl></article>""")
    content = (
        "".join(cards)
        or "<article><h2>No release profiles configured</h2><p>There is no evidence-based release approval. This is not a healthy production certification.</p></article>"
    )
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="10">
    <title>Agent-OS | Release Readiness</title><style>
    :root{{color-scheme:light;--ink:#173831;--paper:#f4f2e9;--mint:#dbebdf}}
    *{{box-sizing:border-box}}body{{margin:0;background:radial-gradient(at 90% 0%,var(--mint),transparent 60%),var(--paper);color:var(--ink);font:18px Georgia,serif}}
    main{{max-width:1120px;margin:auto;padding:40px 24px}}a{{color:inherit}}h1{{font-size:clamp(36px,7vw,70px);font-weight:400;margin-bottom:16px}}h2{{font-weight:400}}
    .eyebrow,dt,code{{font-family:ui-monospace,monospace;font-size:13px}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,300px),1fr));gap:20px}}
    article{{border:1px solid #a4b6a8;padding:24px;background:#ffffff80}}dd{{margin:6px 0 18px;overflow-wrap:anywhere}}p{{line-height:1.5}}.summary{{padding:20px 0;border-block:1px solid #a4b6a8;margin:28px 0}}
    </style></head><body><main><a href="/">Back to delivery operations</a>
    <p class="eyebrow">AGENT-OS / CONTINUOUS ASSURANCE</p><h1>Ready is an evidence claim.</h1>
    <p>Mode: <strong>{escape(data["mode"])}</strong>. Release gates: <strong>{"eligible" if data["ready"] else "not satisfied"}</strong>.</p>
    <p>Readiness checks do not prove sustained business performance. Missing, failed, changed or expired evidence cannot pass.</p>
    <div class="summary">{data["usage_receipts"]} measured usage receipts / {data["sealed_attempts"]} fully reconciled attempts / {data["attempts"]} total attempts.<br>
    {data["unfinished_spans"]} unfinished trace spans. Unknown usage is never zero cost.</div>
    <section class="grid">{content}</section><p class="eyebrow">PRIVATE OPERATOR VIEW / REFRESHES EVERY 10 SECONDS</p></main></body></html>"""
