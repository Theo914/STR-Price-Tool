"""Build a compact, self-contained portfolio dashboard from saved quote reports."""
from __future__ import annotations

import html
import json
from pathlib import Path, PureWindowsPath
from typing import Any
from urllib.parse import urlparse


MAX_DASHBOARD_LISTINGS = 10
SUPPORTED_CHANNELS = ("booking", "airbnb")


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ValueError(f"could not read {label}: {error}") from error
    except json.JSONDecodeError as error:
        raise ValueError(f"{label} is not valid JSON: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain a JSON object")
    return value


def _safe_url(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return value.strip()


def _quote_path(
    value: object,
    *,
    manifest_path: Path,
    quotes_dir: Path | None,
) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = Path(value)
    candidates = [raw]
    if not raw.is_absolute():
        candidates.append(manifest_path.parent / raw)
    if quotes_dir is not None:
        candidates.append(quotes_dir / raw.name)
        windows_name = PureWindowsPath(value).name
        if windows_name != raw.name:
            candidates.append(quotes_dir / windows_name)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _money_value(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return round(float(value), 2)


def _price_view(price: object, *, fallback_total: object = None) -> dict[str, Any]:
    source = price if isinstance(price, dict) else {}
    outcome = str(source.get("outcome") or "unknown")
    line_items = source.get("line_items")
    if source.get("breakdown_status") != "exact" or not isinstance(line_items, list):
        line_items = []
    safe_lines = []
    for item in line_items:
        if not isinstance(item, dict):
            continue
        amount = _money_value(item.get("amount"))
        label = item.get("label")
        if amount is None or not isinstance(label, str) or not label.strip():
            continue
        safe_lines.append({
            "label": label.strip(),
            "amount": amount,
            "category": str(item.get("category") or "component"),
            "included": item.get("included_in_displayed_total") is True,
        })
    return {
        "outcome": outcome,
        "guest_total": _money_value(source.get("headline_total"))
        if source else _money_value(fallback_total),
        "component_total": _money_value(source.get("comparable_total")),
        "breakdown_status": source.get("breakdown_status"),
        "breakdown_note": source.get("breakdown_note"),
        "line_items": safe_lines,
        "refundable": source.get("refundable") if isinstance(source.get("refundable"), bool) else None,
        "cancellation_type": source.get("cancellation_type")
        if isinstance(source.get("cancellation_type"), str) else None,
        "notes": source.get("notes") if isinstance(source.get("notes"), str) else None,
        "rate_options": [
            {
                "total": _money_value(item.get("total")),
                "cancellation": str(item.get("cancellation") or ""),
                "selected": item.get("selected") is True,
            }
            for item in (source.get("rate_options") or [])
            if isinstance(item, dict) and _money_value(item.get("total")) is not None
        ][:8] if isinstance(source.get("rate_options"), list) else [],
    }


def _scenario_view(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    scenarios = []
    for item in value:
        if not isinstance(item, dict) or item.get("status") != "available":
            continue
        percent = _money_value(item.get("discount_percent"))
        adjusted = _money_value(item.get("adjusted_guest_total"))
        difference = _money_value(item.get("adjusted_candidate_minus_subject"))
        difference_percent = _money_value(item.get("adjusted_difference_percent"))
        if percent is None or adjusted is None:
            continue
        scenarios.append({
            "discount_percent": percent,
            "adjusted_guest_total": adjusted,
            "difference": difference,
            "difference_percent": difference_percent,
            "additional_discount": _money_value(item.get("additional_discount_amount")),
            "tax_reduction": _money_value(item.get("percentage_tax_reduction")),
            "note": str(item.get("note") or "Hypothetical only; not a bookable quote."),
        })
    return scenarios


def _review_candidates(value: object) -> list[dict[str, Any]]:
    """Return a small, safe list of candidate links for manual review."""
    if not isinstance(value, list):
        return []
    candidates = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        url = _safe_url(item.get("url"))
        if url is None or url in seen:
            continue
        seen.add(url)
        title = item.get("title")
        identity_status = item.get("identity_status")
        candidates.append({
            "url": url,
            "title": title.strip() if isinstance(title, str) and title.strip() else None,
            "score": _money_value(item.get("score")),
            "identity_status": (
                identity_status.strip()
                if isinstance(identity_status, str) and identity_status.strip()
                else None
            ),
        })
        if len(candidates) >= 5:
            break
    return candidates


def load_dashboard_data(
    manifest_path: Path,
    *,
    quotes_dir: Path | None = None,
) -> dict[str, Any]:
    """Load a saved portfolio and its quote reports into a small UI contract."""
    manifest_path = manifest_path.resolve()
    manifest = _read_json(manifest_path, "portfolio results")
    listings = manifest.get("listings")
    results = manifest.get("results")
    if not isinstance(listings, list) or not listings:
        raise ValueError("portfolio results require a non-empty listings array")
    if len(listings) > MAX_DASHBOARD_LISTINGS:
        raise ValueError(
            f"dashboard contains {len(listings)} listings; maximum is "
            f"{MAX_DASHBOARD_LISTINGS}"
        )
    if not isinstance(results, list):
        raise ValueError("portfolio results require a results array")

    results_by_listing: dict[str, list[dict[str, Any]]] = {}
    for result in results:
        if not isinstance(result, dict):
            continue
        listing_id = result.get("listing_id")
        if isinstance(listing_id, str):
            results_by_listing.setdefault(listing_id, []).append(result)

    dashboard_listings = []
    for listing in listings:
        if not isinstance(listing, dict):
            raise ValueError("portfolio listing is not an object")
        listing_id = str(listing.get("listing_id") or "").strip()
        if not listing_id:
            raise ValueError("portfolio listing is missing listing_id")
        listing_results = results_by_listing.get(listing_id, [])
        channel_views = []
        subject_view = None
        common = {
            "currency": str(listing.get("currency") or "").upper(),
            "checkin": listing.get("checkin"),
            "checkout": listing.get("checkout"),
            "nights": None,
            "guests": listing.get("guests"),
        }
        for channel in SUPPORTED_CHANNELS:
            result = next(
                (item for item in listing_results if item.get("channel") == channel),
                None,
            )
            if result is None:
                continue
            quote = None
            quote_path = _quote_path(
                result.get("quote_output"),
                manifest_path=manifest_path,
                quotes_dir=quotes_dir,
            )
            if quote_path is not None:
                quote = _read_json(quote_path, f"{channel} quote report")
            quote = quote or {}
            candidate = quote.get("candidate") if isinstance(quote.get("candidate"), dict) else {}
            candidate_url = _safe_url(
                candidate.get("url") or result.get("candidate_url")
            )
            price = _price_view(
                quote.get("price"),
                fallback_total=result.get("ota_guest_total"),
            )
            comparison = quote.get("comparison")
            comparison = comparison if isinstance(comparison, dict) else {}
            channel_views.append({
                "channel": channel,
                "status": str(result.get("status") or "UNKNOWN"),
                "url": candidate_url,
                "price": price,
                "difference": _money_value(
                    comparison.get("candidate_minus_subject")
                    if comparison else result.get("difference")
                ),
                "difference_percent": _money_value(
                    comparison.get("candidate_difference_percent")
                    if comparison else result.get("difference_percent")
                ),
                "verdict": comparison.get("verdict") or result.get("verdict"),
                "terms_note": comparison.get("refundability_note"),
                "discount_scenarios": _scenario_view(quote.get("discount_scenarios")),
                "error": result.get("error") if isinstance(result.get("error"), str) else None,
                "review_candidates": _review_candidates(result.get("review_candidates")),
            })
            if subject_view is None and quote:
                subject = quote.get("subject") if isinstance(quote.get("subject"), dict) else {}
                subject_view = {
                    "url": _safe_url(subject.get("url") or listing.get("subject")),
                    "price": _price_view(
                        quote.get("subject_price"),
                        fallback_total=result.get("travelstaytion_guest_total"),
                    ),
                }
            for key in ("currency", "checkin", "checkout", "nights", "guests"):
                if result.get(key) is not None:
                    common[key] = result.get(key)

        if subject_view is None:
            first = listing_results[0] if listing_results else {}
            subject_view = {
                "url": _safe_url(listing.get("subject")),
                "price": _price_view(
                    None,
                    fallback_total=first.get("travelstaytion_guest_total"),
                ),
            }
        dashboard_listings.append({
            "listing_id": listing_id,
            "name": str(listing.get("name") or listing_id),
            "subject": subject_view,
            "channels": channel_views,
            **common,
        })

    return {
        "schema_version": 1,
        "host": str(manifest.get("host") or "Parity portfolio"),
        "generated_at": manifest.get("generated_at"),
        "listings": dashboard_listings,
    }


def _embedded_json(value: object) -> str:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )


def _render_dashboard_legacy(data: dict[str, Any]) -> str:
    """Render one portable HTML file with no remote scripts or assets."""
    title = html.escape(str(data.get("host") or "Parity portfolio"), quote=True)
    payload = _embedded_json(data)
    return f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} · Skopia</title>
<style>
:root{{--ink:#17213a;--muted:#69728a;--line:#e2e8e4;--paper:#f4f7f5;--white:#fff;--ts:#173f35;--ts-soft:#eaf3ef;--airbnb:#ff385c;--airbnb-soft:#fff0f3;--booking:#003b95;--booking-soft:#eef5ff;--good:#087a55;--good-bg:#e8f7f0;--bad:#bd2c3b;--bad-bg:#fff0f1;--warn:#9a5a00}}
*{{box-sizing:border-box}}body{{margin:0;background:linear-gradient(150deg,#eaf3ef,#fafafa 55%,#edf2f8);color:var(--ink);font:13px/1.38 Inter,Segoe UI,Arial,sans-serif}}button,select{{font:inherit}}a{{color:inherit}}.shell{{width:min(100%,540px);min-height:100vh;margin:0 auto;padding:12px}}.top{{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}}.brand{{font-weight:850;letter-spacing:-.035em;font-size:18px}}.brand span{{color:var(--ts);font-size:11px;font-weight:700;letter-spacing:.02em}}.live{{display:flex;gap:7px;align-items:center;color:var(--muted);font-size:11px}}.dot{{width:7px;height:7px;border-radius:50%;background:#24a875;box-shadow:0 0 0 3px #dff5ec}}.toolbar{{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px}}.tool{{border:1px solid var(--line);border-radius:999px;padding:5px 8px;background:#fff;color:var(--muted);font-size:10px;font-weight:800;text-decoration:none;cursor:pointer}}.tool.primary{{background:var(--ts);border-color:var(--ts);color:#fff}}.mode{{display:grid;grid-template-columns:1fr 1fr;background:#e6ece9;border-radius:13px;padding:3px;margin-bottom:8px}}.mode button{{border:0;border-radius:10px;padding:7px 10px;background:transparent;color:var(--muted);font-weight:800;cursor:pointer}}.mode button.active{{background:#fff;color:var(--ts);box-shadow:0 3px 10px rgba(23,63,53,.10)}}.picker{{background:rgba(255,255,255,.94);border:1px solid var(--line);border-radius:14px;padding:9px 11px;margin-bottom:8px;box-shadow:0 6px 18px rgba(23,63,53,.055)}}.picker label{{display:block;color:var(--muted);font-size:9px;font-weight:850;letter-spacing:.11em;text-transform:uppercase;margin-bottom:4px}}.select-wrap{{position:relative}}.select-wrap:after{{content:'⌄';position:absolute;right:2px;top:50%;transform:translateY(-58%);pointer-events:none;color:var(--muted);font-size:16px}}select{{width:100%;border:0;background:transparent;color:var(--ink);font-weight:800;outline:0;appearance:none;padding-right:24px}}.meta{{display:flex;gap:5px;flex-wrap:wrap;margin:7px 1px 8px}}.pill{{background:#fff;border:1px solid var(--line);border-radius:999px;padding:4px 8px;color:var(--muted);font-size:10px}}.card{{position:relative;overflow:hidden;background:var(--white);border:1px solid var(--line);border-radius:15px;padding:11px 12px;margin-bottom:7px;box-shadow:0 6px 18px rgba(23,63,53,.05)}}.card:before{{content:'';position:absolute;left:0;top:0;bottom:0;width:4px;background:#aeb5c8}}.card.primary:before{{background:var(--ts)}}.card.airbnb-card:before{{background:var(--airbnb)}}.card.booking-card:before{{background:var(--booking)}}.row{{display:flex;justify-content:space-between;gap:10px;align-items:flex-start}}.row-actions{{display:flex;gap:5px;align-items:center}}.source{{font-weight:850;text-transform:capitalize}}.source.ts{{color:var(--ts)}}.source.airbnb{{color:var(--airbnb)}}.source.booking{{color:var(--booking)}}.eyebrow{{font-size:9px;letter-spacing:.11em;text-transform:uppercase;color:var(--muted);font-weight:850;margin-bottom:3px}}.price{{font-weight:880;font-size:23px;letter-spacing:-.04em;line-height:1.05;margin:5px 0 2px}}.sub{{font-size:10px;color:var(--muted)}}.open,.copy{{font-size:10px;text-decoration:none;border:1px solid var(--line);border-radius:999px;padding:4px 7px;color:var(--muted);background:#fff;white-space:nowrap;cursor:pointer}}.difference{{display:inline-flex;border-radius:999px;padding:4px 7px;font-size:10px;font-weight:800;margin-top:6px}}.difference.good{{color:var(--good);background:var(--good-bg)}}.difference.bad{{color:var(--bad);background:var(--bad-bg)}}.difference.neutral{{color:var(--muted);background:var(--paper)}}details{{border-top:1px solid var(--line);margin-top:8px;padding-top:7px}}summary{{cursor:pointer;font-size:11px;font-weight:800;color:var(--muted)}}.breakdown{{margin-top:6px}}.component{{display:flex;justify-content:space-between;gap:10px;padding:3px 0;font-size:11px}}.component span:first-child{{color:var(--muted)}}.component.total{{border-top:1px dashed var(--line);margin-top:3px;padding-top:6px;font-weight:850}}.whatif-buttons{{display:flex;gap:5px;flex-wrap:wrap;margin:7px 0}}.scenario{{border:1px solid var(--line);background:#fff;color:var(--muted);border-radius:999px;padding:4px 8px;font-size:10px;cursor:pointer}}.scenario.active{{background:var(--ink);border-color:var(--ink);color:#fff}}.scenario-result{{background:var(--paper);border-radius:10px;padding:8px}}.scenario-price{{font-size:17px;font-weight:850}}.note{{font-size:10px;color:var(--muted);margin-top:5px}}.warning{{margin-top:6px;color:var(--warn);font-size:10px}}.status{{font-weight:850;font-size:10px}}.status.PRICED{{color:var(--good)}}.status.IDENTITY_UNVERIFIED,.status.REVIEW_REQUIRED{{color:var(--warn)}}.empty{{padding:11px 7px;text-align:left;color:var(--muted)}}.review-list{{display:grid;gap:5px;margin-top:7px}}.review-row{{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:7px;background:var(--paper);border-radius:9px}}.review-name{{font-size:10px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}.portfolio-list{{display:grid;gap:7px}}.portfolio-row{{width:100%;border:1px solid var(--line);border-radius:14px;background:#fff;padding:10px 11px;text-align:left;color:var(--ink);cursor:pointer;box-shadow:0 5px 16px rgba(23,63,53,.045)}}.portfolio-row:hover{{border-color:#a8bdb5}}.portfolio-title{{display:flex;justify-content:space-between;gap:10px;font-weight:850}}.portfolio-prices{{display:flex;gap:6px;flex-wrap:wrap;margin-top:6px}}.mini{{font-size:10px;border-radius:999px;padding:3px 7px;background:var(--paper);color:var(--muted)}}.mini.ts{{background:var(--ts-soft);color:var(--ts)}}.mini.airbnb{{background:var(--airbnb-soft);color:#b81e3a}}.mini.booking{{background:var(--booking-soft);color:var(--booking)}}footer{{text-align:center;color:#8b92a5;font-size:9px;padding:7px 0}}@media(max-width:420px){{.shell{{padding:8px}}.price{{font-size:21px}}}}
.review-copy{{min-width:0}}.review-status{{margin-top:2px;font-size:8px;color:#8b92a5;text-transform:uppercase;letter-spacing:.05em}}
</style>
</head>
<body>
<main class="shell">
  <div class="top"><div class="brand">Skopia <span>· rate watch</span></div><div class="live"><i class="dot"></i>Check complete</div></div>
  <nav class="toolbar"><button class="tool primary" id="back" type="button">← New check</button><a class="tool download" href="/downloads/results.csv">Export CSV</a><a class="tool download" href="/downloads/results.json">Export JSON</a></nav>
  <div class="mode" role="tablist"><button id="singleMode" class="active" type="button">Single check</button><button id="portfolioMode" type="button">Up to 10</button></div>
  <section class="picker" id="picker"><label for="listing">Choose listing</label><div class="select-wrap"><select id="listing"></select></div></section>
  <div id="app"></div>
  <footer>Guest-visible totals · Hypothetical discounts are never presented as bookable prices</footer>
</main>
<script>
const DATA={payload};
const $=(tag,cls,text)=>{{const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n}};
const money=(value,currency)=>value==null?'—':new Intl.NumberFormat('en-GB',{{style:'currency',currency,currencyDisplay:'code'}}).format(value);
const pct=value=>value==null?'—':`${{Math.abs(value).toFixed(2)}}%`;
const link=(url,label)=>{{if(!url)return null;const a=$('a','open',label+' ↗');a.href=url;a.target='_blank';a.rel='noopener noreferrer';return a}};
const copyButton=url=>{{if(!url)return null;const b=$('button','copy','Copy');b.type='button';b.onclick=async()=>{{try{{await navigator.clipboard.writeText(url);b.textContent='Copied'}}catch(_e){{b.textContent='Copy failed'}}setTimeout(()=>b.textContent='Copy',1300)}};return b}};
const linkActions=url=>{{const box=$('div','row-actions');const a=link(url,'Open'),b=copyButton(url);if(a)box.append(a);if(b)box.append(b);return box}};
function breakdown(price,currency){{
  if(!price||price.breakdown_status!=='exact'||!price.line_items.length)return null;
  const d=$('details');d.append($('summary','',`Price breakdown · ${{price.line_items.length}} components`));
  const box=$('div','breakdown');
  price.line_items.forEach(item=>{{const r=$('div','component');r.append($('span','',item.label),$('span','',money(item.amount,currency)));box.append(r)}});
  const total=$('div','component total');total.append($('span','','Guest total'),$('span','',money(price.guest_total,currency)));box.append(total);d.append(box);return d;
}}
function differenceBadge(diff,percent,currency){{
  const cls=diff>0?'good':diff<0?'bad':'neutral';const direction=diff>0?'higher':diff<0?'lower':'equal';
  return $('div',`difference ${{cls}}`,diff===0?'Same guest total':`${{money(Math.abs(diff),currency)}} ${{direction}} · ${{pct(percent)}}`);
}}
function discountPanel(channel,current){{
  if(!channel.discount_scenarios.length)return null;
  const d=$('details','whatif');d.append($('summary','',channel.channel==='booking'?'Genius what-if':'10% what-if'));
  const buttons=$('div','whatif-buttons');const result=$('div','scenario-result');
  const options=[{{discount_percent:0,adjusted_guest_total:channel.price.guest_total,difference:channel.difference,difference_percent:channel.difference_percent,note:'Captured guest total; no hypothetical discount applied.'}},...channel.discount_scenarios];
  const show=(option,button)=>{{buttons.querySelectorAll('button').forEach(b=>b.classList.remove('active'));button.classList.add('active');result.replaceChildren();result.append($('div','scenario-price',money(option.adjusted_guest_total,current.currency)));if(option.difference!=null)result.append(differenceBadge(option.difference,option.difference_percent,current.currency));result.append($('div','note',option.note));}};
  options.forEach((option,index)=>{{const b=$('button','scenario',index===0?'Actual':`-${{option.discount_percent}}%`);b.type='button';b.onclick=()=>show(option,b);buttons.append(b);if(index===0)queueMicrotask(()=>show(option,b))}});
  d.append(buttons,result);return d;
}}
function priceCard(channel,current){{
  const card=$('section',`card ${{channel.channel}}-card`);const head=$('div','row');const left=$('div');left.append($('div',`source ${{channel.channel}}`,channel.channel==='booking'?'Booking.com':'Airbnb'));left.append($('div',`status ${{channel.status}}`,channel.status.replaceAll('_',' ')));head.append(left);if(channel.url)head.append(linkActions(channel.url));card.append(head);
  if(channel.status==='PRICED'&&channel.price.guest_total!=null){{card.append($('div','price',money(channel.price.guest_total,current.currency)));card.append($('div','sub','Guest total'));card.append(differenceBadge(channel.difference,channel.difference_percent,current.currency));const audit=channel.price.component_total;if(audit!=null&&audit!==channel.price.guest_total)card.append($('div','note',`Captured components excluding taxes: ${{money(audit,current.currency)}}`));const bd=breakdown(channel.price,current.currency);if(bd)card.append(bd);const others=(channel.price.rate_options||[]).filter(o=>!o.selected);if(others.length)card.append($('div','note','Other rates on this site: '+others.map(o=>`${{money(o.total,current.currency)}} ${{o.cancellation.toLowerCase()}}`).join(' · ')));const dp=discountPanel(channel,current);if(dp)card.append(dp);if(channel.terms_note)card.append($('div','warning',channel.terms_note));}}
  else{{card.append($('div','empty',channel.error||channel.price.notes||'No usable guest price was captured.'))}}
  const alternatives=(channel.review_candidates||[]).filter(item=>item.url!==channel.url);if(alternatives.length){{const d=$('details');d.append($('summary','',`Other possible links · ${{alternatives.length}}`));const list=$('div','review-list');alternatives.forEach((item,index)=>{{const row=$('div','review-row'),copy=$('div','review-copy');copy.append($('div','review-name',item.title||`Possible ${{channel.channel==='booking'?'Booking.com':'Airbnb'}} match ${{index+1}}`));if(item.identity_status)copy.append($('div','review-status',item.identity_status.replaceAll('_',' ')));row.append(copy,linkActions(item.url));list.append(row)}});d.append(list);card.append(d)}}return card;
}}
function renderSingle(index){{
  window.current=DATA.listings[index];const current=window.current;const app=document.getElementById('app');app.replaceChildren();
  const meta=$('div','meta');[[`${{current.checkin||'—'}} → ${{current.checkout||'—'}}`],[`${{current.nights||'—'}} nights`],[`${{current.guests||'—'}} guests`],[current.currency]].forEach(x=>meta.append($('span','pill',x[0])));app.append(meta);
  const subject=$('section','card primary');const h=$('div','row');const l=$('div');l.append($('div','eyebrow','TravelStaytion'));l.append($('div','source ts','Your guest price'));h.append(l);if(current.subject.url)h.append(linkActions(current.subject.url));subject.append(h,$('div','price',money(current.subject.price.guest_total,current.currency)),$('div','sub','Guest total'));const bd=breakdown(current.subject.price,current.currency);if(bd)subject.append(bd);app.append(subject);
  current.channels.forEach(channel=>app.append(priceCard(channel,current)));if(!current.channels.length)app.append($('div','empty','No Airbnb or Booking comparison is available.'));
}}
function renderPortfolio(){{const app=document.getElementById('app');app.replaceChildren();const list=$('div','portfolio-list');DATA.listings.forEach((item,index)=>{{const row=$('button','portfolio-row');row.type='button';const title=$('div','portfolio-title');title.append($('span','',item.name),$('span','',`${{item.nights||'—'}} nights`));const prices=$('div','portfolio-prices');prices.append($('span','mini ts',`TS ${{money(item.subject.price.guest_total,item.currency)}}`));item.channels.forEach(channel=>prices.append($('span',`mini ${{channel.channel}}`,`${{channel.channel==='booking'?'Booking':'Airbnb'}} ${{money(channel.price.guest_total,item.currency)}}`)));row.append(title,prices);row.onclick=()=>{{select.value=index;setMode('single');renderSingle(index)}};list.append(row)}});app.append(list)}}
const select=document.getElementById('listing');const single=document.getElementById('singleMode');const portfolio=document.getElementById('portfolioMode');const picker=document.getElementById('picker');document.getElementById('back').onclick=()=>{{if(location.protocol.startsWith('http'))location.href='/';else history.back()}};if(!location.protocol.startsWith('http'))document.querySelectorAll('.download').forEach(node=>node.hidden=true);DATA.listings.forEach((item,index)=>{{const option=$('option','',`${{item.name}} · TS ${{item.listing_id}}`);option.value=index;select.append(option)}});function setMode(mode){{const isSingle=mode==='single';single.classList.toggle('active',isSingle);portfolio.classList.toggle('active',!isSingle);picker.hidden=!isSingle}}single.onclick=()=>{{setMode('single');renderSingle(Number(select.value))}};portfolio.onclick=()=>{{setMode('portfolio');renderPortfolio()}};select.onchange=()=>renderSingle(Number(select.value));setMode('single');renderSingle(0);
</script>
</body>
</html>'''


def render_dashboard(data: dict[str, Any]) -> str:
    """Render the compact result with portfolio controls hidden from single mode."""
    rendered = _render_dashboard_legacy(data)
    single_mode_css = ".mode,.picker{display:none!important}"
    return rendered.replace("</style>", f"{single_mode_css}</style>", 1)


def write_dashboard(
    manifest_path: Path,
    output_path: Path,
    *,
    quotes_dir: Path | None = None,
) -> dict[str, Any]:
    data = load_dashboard_data(manifest_path, quotes_dir=quotes_dir)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_dashboard(data), encoding="utf-8")
    return data
