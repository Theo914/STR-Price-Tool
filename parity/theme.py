"""Midnight theme: one dark stylesheet appended to every Skopia page.

It loads after each page's own rules, so it only restates what changes.
Deliberately plain: a flat background, thin borders, small radii, no glows,
gradients or pill-shaped everything; one quiet accent and tabular figures so
prices line up.  Site colours stay as small markers, and red/green keep
meaning dearer/cheaper.
"""

MIDNIGHT_CSS = """
:root{--ink:#e6e8eb;--muted:#8b929c;--line:#262c35;--paper:#12161c;--white:#161b22;
--ts:#e6e8eb;--ts-soft:transparent;--airbnb:#e8677a;--airbnb-soft:transparent;
--booking:#7f9cd8;--booking-soft:transparent;--good:#6fbf8a;--good-bg:transparent;
--bad:#e07a6f;--bad-bg:transparent;--warn:#d6a85c;color-scheme:dark}
html{background:#0e1116!important;min-height:100%}
body{min-height:100vh;background:#0e1116!important;color:var(--ink)!important;
font-family:-apple-system,"Segoe UI",Roboto,Arial,sans-serif!important;font-variant-numeric:tabular-nums}
.brand{font-family:Georgia,"Times New Roman",serif;font-weight:600!important;letter-spacing:0!important;color:var(--ink)}
.brand span{color:var(--muted)!important;font-family:-apple-system,"Segoe UI",Roboto,Arial,sans-serif;font-weight:400!important}
.card,.panel,.progress,.picker,.portfolio-row{background:#161b22!important;border:1px solid var(--line)!important;border-radius:6px!important;box-shadow:none!important}
.card:before{width:2px!important;background:#3a414c}.card.primary:before{background:var(--ink)}
.card.airbnb-card:before{background:var(--airbnb)}.card.booking-card:before{background:var(--booking)}
.eyebrow,label{text-transform:none!important;letter-spacing:0!important;font-size:11px!important;font-weight:600!important;color:var(--muted)!important}
.source{text-transform:none!important}
.price{font-weight:600!important;letter-spacing:-.01em!important}
.status{font-weight:600!important;letter-spacing:.02em}
.tool,.open,.copy,.scenario,.clear{background:transparent!important;border:1px solid var(--line)!important;border-radius:4px!important;color:var(--muted)!important;font-weight:500!important}
.tool:hover,.open:hover,.copy:hover,.clear:hover{color:var(--ink)!important;border-color:#3a414c!important}
.tool.primary,.run{background:#e6e8eb!important;border:1px solid #e6e8eb!important;border-radius:4px!important;color:#0e1116!important;font-weight:600!important}
.scenario.active{background:#e6e8eb!important;border-color:#e6e8eb!important;color:#0e1116!important}
.pill{background:transparent!important;border:0!important;border-radius:0!important;padding:0 10px 0 0!important;color:var(--muted)!important}
.pill+.pill{border-left:1px solid var(--line)!important;padding-left:10px!important}
.difference{background:transparent!important;padding:0!important;border-radius:0!important;font-weight:600!important}
input,select,textarea{background:#0e1116!important;border:1px solid var(--line)!important;border-radius:4px!important;color:var(--ink)!important}
input::placeholder,textarea::placeholder{color:#5c636d}
input:focus,select:focus,textarea:focus{border-color:#8b929c!important;box-shadow:none!important}
option{background:#161b22;color:var(--ink)}
.mode{background:#0e1116!important;border:1px solid var(--line);border-radius:6px!important}
.mode button{border-radius:4px!important}.mode button.active,.mode .active{background:#161b22!important;color:var(--ink)!important;box-shadow:none!important}
.channel{background:transparent!important;border:1px solid var(--line);border-radius:4px!important;font-weight:500!important}
.channel.ts{color:var(--ink)!important}.channel.airbnb{color:var(--airbnb)!important}.channel.booking{color:var(--booking)!important}
.mini{background:transparent!important;border:1px solid var(--line);border-radius:4px!important;color:var(--muted)!important}
.mini.airbnb{color:var(--airbnb)!important}.mini.booking{color:var(--booking)!important}.mini.ts{color:var(--ink)!important}
.scenario-result,.review-row{background:#12161c!important;border-radius:4px!important}
.step{border-top:2px solid #262c35!important;color:#5c636d!important;text-transform:none!important;letter-spacing:0!important}
.step.active{border-top-color:var(--ink)!important;color:var(--ink)!important}.step.done{border-top-color:var(--good)!important;color:var(--good)!important}
.bar{background:#262c35!important}.bar i{background:#8b929c!important}
pre{color:#9aa1ab!important;background:#0e1116;border:1px solid var(--line);border-radius:4px;padding:6px}
.dot{background:var(--good)!important;box-shadow:none!important}
.secure,.live{color:var(--muted)!important}
.canonical{color:var(--muted)!important}
footer,.review-status,.promise{color:#5c636d!important}
a{color:inherit}
"""
