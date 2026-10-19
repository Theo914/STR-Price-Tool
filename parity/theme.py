"""Midnight theme: one dark stylesheet appended to every Skopia page.

It loads after each page's own rules, so it only has to restate colours.
The site accents stay recognisable (Airbnb red, Booking blue, TravelStaytion
teal) and red/green keep meaning more/less expensive.
"""

MIDNIGHT_CSS = """
:root{--ink:#e8eefc;--muted:#8d9bb8;--line:#24314f;--paper:#111a2e;--white:#16213a;
--ts:#4fd1c5;--ts-soft:rgba(79,209,197,.14);--airbnb:#ff5a76;--airbnb-soft:rgba(255,90,118,.14);
--booking:#7aa2ff;--booking-soft:rgba(122,162,255,.14);--good:#4ade80;--good-bg:rgba(74,222,128,.13);
--bad:#f87171;--bad-bg:rgba(248,113,113,.14);--warn:#fbbf24;color-scheme:dark}
html{background:#0b1220!important;min-height:100%}
body{min-height:100vh;background:linear-gradient(160deg,#0b1220,#111a2e 60%) fixed!important;color:var(--ink)!important}
.brand{color:#fff}.brand span{color:var(--ts)!important}
.card,.panel,.progress,.picker,.portfolio-row{background:#16213a!important;border-color:var(--line)!important;box-shadow:0 10px 28px rgba(0,0,0,.35)!important}
.card:before{background:#3a4a6e}.card.primary:before{background:var(--ts)}
.card.airbnb-card:before{background:var(--airbnb)}.card.booking-card:before{background:var(--booking)}
.tool,.open,.copy,.pill,.scenario,.clear{background:#111a2e!important;border-color:var(--line)!important;color:var(--muted)!important}
.tool.primary,.run{background:var(--ts)!important;border-color:var(--ts)!important;color:#0b1220!important}
.scenario.active{background:var(--ts)!important;border-color:var(--ts)!important;color:#0b1220!important}
input,select,textarea{background:#0f1729!important;border-color:var(--line)!important;color:var(--ink)!important}
input::placeholder,textarea::placeholder{color:#5d6b88}
input:focus,select:focus,textarea:focus{border-color:var(--ts)!important;box-shadow:0 0 0 3px rgba(79,209,197,.18)!important}
option{background:#16213a;color:var(--ink)}
.mode{background:#0f1729!important}.mode button.active,.mode .active{background:#16213a!important;color:var(--ts)!important}
.channel.ts{background:var(--ts-soft)!important;color:var(--ts)!important}
.channel.airbnb{background:var(--airbnb-soft)!important;color:var(--airbnb)!important}
.channel.booking{background:var(--booking-soft)!important;color:var(--booking)!important}
.mini{background:#111a2e!important;color:var(--muted)!important}.mini.ts{background:var(--ts-soft)!important;color:var(--ts)!important}
.mini.airbnb{background:var(--airbnb-soft)!important;color:var(--airbnb)!important}.mini.booking{background:var(--booking-soft)!important;color:var(--booking)!important}
.scenario-result,.review-row{background:#111a2e!important}
.difference.neutral{background:#111a2e!important;color:var(--muted)!important}
.step{border-top-color:#24314f!important;color:#5d6b88!important}
.step.active{border-top-color:var(--ts)!important;color:var(--ts)!important}.step.done{border-top-color:var(--good)!important;color:var(--good)!important}
.bar{background:#24314f!important}
pre{color:#9fb0cf!important}
.dot{background:var(--good)!important;box-shadow:0 0 0 3px rgba(74,222,128,.18)!important}
.secure{color:var(--good)!important}
footer,.review-status{color:#5d6b88!important}
a{color:inherit}
"""
