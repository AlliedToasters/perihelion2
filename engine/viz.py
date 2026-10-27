"""Analytics page for one branch (spec §10, user request 2026-10-25): wake counts, station state over time,
inter-station interactions on an octogram, and a wake transcript, rendered as one self-contained HTML file
with inline SVG. The event log is the only input; state over time comes from replaying it (replay.py)."""
from __future__ import annotations

import html
import json
from collections import Counter, defaultdict
from pathlib import Path

from .events import Event
from .log import RunIndex
from .metrics import cost_usd, summarize
from .world import PROGRAM_KINDS, WorldState

TICKS_PER_DAY = 452
WAKE_REASON_GROUPS = {"heartbeat": "heartbeat", "genesis": "heartbeat", "sleep_until": "heartbeat",
                      "arrival": "arrival", "relay": "arrival", "program_done": "program", "alert": "alert",
                      "vote_closed": "alert"}
INTERACTION_KINDS = ("dispatch", "artifact", "transfer", "contract")


def _reason_group(r: str) -> str:
    return WAKE_REASON_GROUPS.get(r.split(":")[0], "other")


def timeline(evs: list[Event]) -> dict:
    """Per-station state after every tick that has events: power allocation by program kind (percent of the
    station's own power), attitude, ledger balance, completed count. Step series: a value holds until the next
    sample."""
    world, samples, cur = WorldState(), defaultdict(list), None

    def take(tick):
        for name, n in world.nodes.items():
            if n["inert"]:
                continue
            alloc = {k: 0 for k in PROGRAM_KINDS}
            for p in n["programs"].values():
                alloc[p["kind"]] += p["pct"]
            smp = {"t": tick, "alloc": alloc, "idle": max(0, 100 - sum(alloc.values())),
                   "attitude": n["attitude"], "balance": world.ledger["balances"].get(name, 0),
                   "completed": len(n["completed"]), "propellant": n["propellant_kg"], "power_mw": n["power_mw"]}
            prev = samples[name][-1] if samples[name] else None
            if prev is None or any(prev[k] != smp[k] for k in smp if k != "t"):
                samples[name].append(smp)   # step series: only changes are kept

    for ev in evs:
        if cur is not None and ev.tick != cur:
            take(cur)
        world.apply(ev)
        cur = ev.tick
    if cur is not None:
        take(cur)
    return dict(samples)


def wakes(evs: list[Event]) -> dict:
    """Every wake with its reason group, plus the orders it produced and the model's output (its rationale)."""
    out, by_wake = [], {}
    for e in evs:
        if e.type == "wake":
            rec = {"id": e.id, "tick": e.tick, "node": e.node, "reasons": e.payload["reasons"],
                   "group": _reason_group(e.payload["reasons"][0]) if e.payload["reasons"] else "other",
                   "orders": [], "rejected": [], "status": None, "output": "", "attempts": 0, "tokens": 0}
            out.append(rec)
            by_wake[e.node] = rec
        elif e.type == "orders" and e.node in by_wake:
            rec = by_wake[e.node]
            rec.update(status=e.payload.get("status"), output=e.payload.get("output") or "",
                       attempts=e.payload.get("attempts", 0),
                       tokens=sum((e.payload.get("usage") or {}).values()))
        elif e.type == "order_accepted" and e.node in by_wake and by_wake[e.node]["tick"] == e.tick:
            by_wake[e.node]["orders"].append(e.payload["order"])
        elif e.type == "order_rejected" and e.node in by_wake and by_wake[e.node]["tick"] == e.tick:
            by_wake[e.node]["rejected"].append({"order": e.payload["order"], "reason": e.payload["reason"]})
    return {"list": out, "per_station": dict(Counter(w["node"] for w in out)),
            "per_group": dict(Counter(w["group"] for w in out))}


def interactions(evs: list[Event]) -> dict:
    """Directed station-to-station counts by kind: dispatches and artifacts transmitted (by destination),
    token transfers (count and amount), contracts offered or accepted (by counterparty). Relay decisions on
    other stations' traffic are counted per relaying station."""
    pairs: dict[tuple, Counter] = defaultdict(Counter)
    amounts: Counter = Counter()
    relay: dict[str, Counter] = defaultdict(Counter)
    for e in evs:
        if e.type == "transmit":
            kind = "artifact" if e.payload.get("kind") == "artifact" else "dispatch"
            pairs[(e.node, e.payload["to"])][kind] += 1
        elif e.type == "order_accepted":
            o = e.payload["order"]
            if o["type"] == "transfer":
                pairs[(e.node, o["to"])]["transfer"] += 1
                amounts[(e.node, o["to"])] += o["amount"]
            elif o["type"] == "offer":
                pairs[(e.node, o["to"])]["contract"] += 1
            elif o["type"] == "accept":
                pairs[(e.node, e.payload["outcome"]["offerer"])]["contract"] += 1
        elif e.type == "forward" and e.payload["action"] != "auto":
            relay[e.node][e.payload["action"]] += 1
        elif e.type == "drop" and e.payload["reason"] == "relay_drop":
            relay[e.node]["drop"] += 1
    edges = [{"from": a, "to": b, **{k: c.get(k, 0) for k in INTERACTION_KINDS}, "amount": amounts.get((a, b), 0)}
             for (a, b), c in sorted(pairs.items())]
    return {"edges": edges, "relay": {k: dict(v) for k, v in relay.items()}}


def page_data(evs: list[Event], price_in: float = 4.0, price_out: float = 20.0) -> dict:
    g = evs[0].payload
    sc, rs = g["scenario"], g["ruleset"]
    summ = summarize(evs)
    usage = Counter()
    for e in evs:
        if e.type in ("orders", "orders_invalid", "refusal", "sample_error"):
            usage.update(e.payload.get("usage") or {})
    end = next((e.payload for e in reversed(evs) if e.type == "end_state"), None)
    return {
        "config": {"scenario": sc["name"], "ruleset": rs["name"], "horizon": sc["horizon_ticks"],
                   "nodes": sc["nodes"], "inert": sc.get("inert", []),
                   "domains": {n: sc["stations"][n]["domain"] for n in sc["nodes"]} if "stations" in sc else {},
                   "neighbors": [[sc["nodes"][i], sc["nodes"][(i + 1) % len(sc["nodes"])]] for i in range(len(sc["nodes"]))],
                   "ticks_per_day": TICKS_PER_DAY, "beacon_ticks": rs.get("beacon_ticks"),
                   "ledger": bool(sc.get("ledger", {}).get("enabled"))},
        "status": summ.get("status"), "health": summ.get("health"), "balance": summ.get("balance"),
        "behavior": summ.get("behavior"), "stations": summ.get("stations"),
        "usage": dict(usage), "cost_usd": round(cost_usd(dict(usage), price_in, price_out), 4),
        "events": len(evs), "last_tick": evs[-1].tick, "end_reason": end["reason"] if end else None,
        "wakes": wakes(evs), "timeline": timeline(evs), "interactions": interactions(evs),
        "program_kinds": list(PROGRAM_KINDS),
    }


def render(data: dict, title: str) -> str:
    return TEMPLATE.replace("__TITLE__", html.escape(title)).replace(
        "__DATA__", json.dumps(data, separators=(",", ":")).replace("</", "<\\/"))


def write_page(run_dir: Path, branch: str = "main", out: Path | None = None,
               price_in: float = 4.0, price_out: float = 20.0) -> Path:
    evs = RunIndex(run_dir).open(branch).events
    data = page_data(evs, price_in, price_out)
    out = out or run_dir / (f"viz.html" if branch == "main" else f"viz-{branch}.html")
    out.write_text(render(data, f"{run_dir.name} {branch}"))
    return out


TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
:root{color-scheme:light;--surface:#fcfcfb;--surface-2:#f0efec;--line:#dcdbd6;--text:#0b0b0b;--text-2:#52514e;--text-3:#8a8984;
--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--s4:#eda100;--s5:#e87ba4;--s6:#008300;--s7:#6250d6;--s8:#e34948;--idle:#c9c8c2;--bad:#d03b3b}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--surface:#1a1a19;--surface-2:#262624;--line:#3a3a37;--text:#f0efec;--text-2:#c3c2b7;--text-3:#8a8984;
--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--s5:#d55181;--s6:#008300;--s7:#9085e9;--s8:#e66767;--idle:#4a4a46}}
:root[data-theme="dark"]{color-scheme:dark;--surface:#1a1a19;--surface-2:#262624;--line:#3a3a37;--text:#f0efec;--text-2:#c3c2b7;--text-3:#8a8984;
--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--s5:#d55181;--s6:#008300;--s7:#9085e9;--s8:#e66767;--idle:#4a4a46}
*{box-sizing:border-box}body{margin:0;padding:16px;background:var(--surface);color:var(--text);font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
h1{font-size:20px;margin:0 0 4px}h2{font-size:16px;margin:28px 0 8px}.sub{color:var(--text-2);margin-bottom:12px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:8px}.tile{background:var(--surface-2);border-radius:8px;padding:10px 12px}
.tile .v{font-size:22px;font-weight:600;font-variant-numeric:tabular-nums}.tile .k{color:var(--text-2);font-size:12px}
svg{display:block;max-width:100%;overflow:visible}text{fill:var(--text-2);font-size:11px}.axis line,.axis path{stroke:var(--line)}.grid line{stroke:var(--line);stroke-dasharray:2 3}
.legend{display:flex;flex-wrap:wrap;gap:6px 14px;margin:6px 0;color:var(--text-2);font-size:12px}.legend span{display:inline-flex;align-items:center;gap:6px}
.sw{width:12px;height:12px;border-radius:3px;display:inline-block}
.multi{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px}.panel{background:var(--surface-2);border-radius:8px;padding:8px}
.panel h3{font-size:13px;margin:0 0 4px;color:var(--text)}.panel .d{color:var(--text-3);font-size:11px;margin-left:6px}
#tip{position:fixed;pointer-events:none;background:var(--text);color:var(--surface);padding:6px 8px;border-radius:6px;font-size:12px;display:none;max-width:320px;z-index:9}
table{border-collapse:collapse;width:100%;font-size:12px}th,td{text-align:left;padding:4px 6px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--text-2)}
td.num{text-align:right;font-variant-numeric:tabular-nums}details summary{cursor:pointer;color:var(--text-2)}pre{white-space:pre-wrap;margin:4px 0 0;font-size:11px;color:var(--text-2)}
.controls{display:flex;gap:10px;flex-wrap:wrap;margin:6px 0 10px;align-items:center}select,button{font:inherit;padding:4px 8px;border-radius:6px;border:1px solid var(--line);background:var(--surface-2);color:var(--text)}
.hit{cursor:crosshair}
</style></head><body>
<div id="tip"></div>
<div class="controls" style="float:right"><button id="theme" title="theme">auto</button></div><h1 id="h1"></h1><div class="sub" id="sub"></div>
<div class="tiles" id="tiles"></div>
<h2>Wakes</h2><div class="sub">One dot per wake. Rows are stations, x is the tick. Bars at the right count wakes per station.</div>
<div class="legend" id="wake-legend"></div><div id="wakes"></div>
<h2>Station state over time</h2><div class="sub">Power allocation by program kind (percent of the station's own power, stacked); the gray remainder is idle. Marks on top: attitude changes (&#9670;) and completed nodes (&#9679;).</div>
<div class="legend" id="alloc-legend"></div><div class="multi" id="alloc"></div>
<div id="balance-section"><h2>Ledger balances</h2><div class="sub">Tokens held per station after each tick with events.</div><div class="legend" id="bal-legend"></div><div id="balance"></div></div>
<h2>Interactions</h2><div class="sub">Stations on the ring in their physical order. A chord from A to B bends to the right of its direction, so the two directions of one pair are two chords; width is the count. Faint lines are the ring's links.</div>
<div class="controls"><label>Kind <select id="kind"></select></label><span class="legend" id="int-legend"></span></div>
<div class="multi"><div class="panel" id="octo"></div><div class="panel" id="matrix"></div></div>
<h2>Wake transcript</h2>
<div class="controls"><label>Station <select id="station"></select></label><label>Show <select id="limit"><option>50</option><option>200</option><option>all</option></select></label><span id="tcount" class="sub"></span></div>
<div id="transcript"></div>
<script id="data" type="application/json">__DATA__</script>
<script>
const D=JSON.parse(document.getElementById('data').textContent);
const C={research:'var(--s1)',mine:'var(--s2)',adopt:'var(--s3)',contribute:'var(--s4)',hail:'var(--s5)',maneuver:'var(--s7)',idle:'var(--idle)'};
const WC={heartbeat:'var(--idle)',arrival:'var(--s1)',program:'var(--s3)',alert:'var(--s2)',other:'var(--s7)'};
const IC={dispatch:'var(--s1)',artifact:'var(--s3)',transfer:'var(--s2)',contract:'var(--s4)'};
const ST=['var(--s1)','var(--s2)','var(--s3)','var(--s4)','var(--s5)','var(--s6)','var(--s7)','var(--s8)'];
const nodes=D.config.nodes, playing=nodes.filter(n=>!D.config.inert.includes(n)), H=D.config.horizon;
const tip=document.getElementById('tip');
function showTip(e,h){tip.innerHTML=h;tip.style.display='block';const x=e.clientX+12,y=e.clientY+12;tip.style.left=Math.min(x,innerWidth-tip.offsetWidth-8)+'px';tip.style.top=Math.min(y,innerHeight-tip.offsetHeight-8)+'px'}
function hideTip(){tip.style.display='none'}
function el(tag,attrs,parent){const n=document.createElementNS('http://www.w3.org/2000/svg',tag);for(const k in attrs)n.setAttribute(k,attrs[k]);if(parent)parent.appendChild(n);return n}
function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function legend(id,map,labels){const g=document.getElementById(id);g.innerHTML=Object.keys(map).map(k=>`<span><i class="sw" style="background:${map[k]}"></i>${labels?labels[k]||k:k}</span>`).join('')}
function fmt(n){return typeof n==='number'?(Math.abs(n)>=1000?n.toLocaleString():(Number.isInteger(n)?n:n.toFixed(2))):n}
function xAxis(svg,x,y,w){const g=el('g',{class:'axis'},svg);el('line',{x1:x(0),x2:x(H),y1:y,y2:y},g);const days=Math.max(1,Math.round(H/D.config.ticks_per_day));const step=days>4?Math.ceil(days/4):1;for(let d=0;d<=days;d+=step){const t=Math.min(H,d*D.config.ticks_per_day);el('line',{x1:x(t),x2:x(t),y1:y,y2:y+4},g);const tx=el('text',{x:x(t),y:y+15,'text-anchor':'middle'},g);tx.textContent=d===0?'tick 0':`day ${d} (t${t})`}}

// theme: auto -> dark -> light; #dark / #light in the URL preselect
(function(){const b=document.getElementById('theme'),modes=['auto','dark','light'];let m=location.hash==='#dark'?'dark':location.hash==='#light'?'light':'auto';
function apply(){if(m==='auto')document.documentElement.removeAttribute('data-theme');else document.documentElement.setAttribute('data-theme',m);b.textContent=m}
b.addEventListener('click',()=>{m=modes[(modes.indexOf(m)+1)%3];apply()});apply()})();
// header
document.getElementById('h1').textContent=document.title;
const b=D.balance||{},hl=D.health||{};
document.getElementById('sub').textContent=`${D.config.scenario} · ${D.config.ruleset} · ${D.events} events · ${D.status||''}${D.end_reason?' ('+D.end_reason+')':''}`;
const tiles=[['wakes',D.wakes.list.length],['wakes / station-day',(D.wakes.list.length/Math.max(1,playing.length)/(H/D.config.ticks_per_day)).toFixed(1)],['dispatches',D.interactions.edges.reduce((a,e)=>a+e.dispatch,0)],['rejection rate',hl.rejection_rate!=null?(hl.rejection_rate*100).toFixed(0)+'%':'–'],['est. cost',D.cost_usd?'$'+D.cost_usd.toFixed(2):'$0'],['winners',(b.winners||[]).join(', ')||'–']];
document.getElementById('tiles').innerHTML=tiles.map(([k,v])=>`<div class="tile"><div class="v">${esc(v)}</div><div class="k">${esc(k)}</div></div>`).join('');

// wakes strip
(function(){legend('wake-legend',WC,{heartbeat:'heartbeat / scheduled',arrival:'arrival',program:'program done',alert:'alert / vote',other:'other'});
const W=Math.min(960,document.body.clientWidth-32),rowH=22,ml=44,mr=120,mt=8,mb=24,ph=rowH*playing.length;
const svg=el('svg',{viewBox:`0 0 ${W} ${ph+mt+mb}`,width:W,height:ph+mt+mb},document.getElementById('wakes'));
const x=t=>ml+(W-ml-mr)*t/H;const maxc=Math.max(1,...playing.map(n=>D.wakes.per_station[n]||0));
playing.forEach((n,i)=>{const y=mt+i*rowH+rowH/2;el('line',{x1:x(0),x2:x(H),y1:y,y2:y,class:'grid'},svg).setAttribute('stroke','var(--line)');const t=el('text',{x:ml-6,y:y+4,'text-anchor':'end'},svg);t.textContent=n;
const c=D.wakes.per_station[n]||0,bw=(W-ml-mr-(W-ml-mr))+ (mr-50)*c/maxc;const r=el('rect',{x:x(H)+12,y:y-6,width:Math.max(0,bw),height:12,rx:2,fill:'var(--s1)',class:'hit'},svg);const lbl=el('text',{x:x(H)+16+Math.max(0,bw),y:y+4},svg);lbl.textContent=c;});
D.wakes.list.forEach(w=>{const i=playing.indexOf(w.node);if(i<0)return;const y=mt+i*rowH+rowH/2;const c=el('circle',{cx:x(w.tick),cy:y,r:3.5,fill:WC[w.group],stroke:'var(--surface)','stroke-width':1,class:'hit'},svg);
c.addEventListener('mousemove',e=>showTip(e,`<b>${w.node}</b> tick ${w.tick}<br>${esc(w.reasons.join(', '))}<br>${w.orders.length} accepted, ${w.rejected.length} rejected${w.status&&w.status!=='ok'?' · '+esc(w.status):''}`));c.addEventListener('mouseleave',hideTip)});
xAxis(svg,x,mt+ph,W)})();

// allocation small multiples
(function(){legend('alloc-legend',C);const host=document.getElementById('alloc');
const panels=playing.map(n=>{const s=D.timeline[n]||[];const p=document.createElement('div');p.className='panel';p.innerHTML=`<h3>${esc(n)}<span class="d">${esc(D.config.domains[n]||'')}${s.length?' · completed '+s[s.length-1].completed:''}</span></h3>`;host.appendChild(p);return p});
playing.forEach((n,pi)=>{const s=D.timeline[n]||[];const p=panels[pi];
const W=p.clientWidth-16||300,ph=90,ml=30,mr=8,mt=10,mb=22;const svg=el('svg',{viewBox:`0 0 ${W} ${ph+mt+mb}`,width:W,height:ph+mt+mb},p);
const x=t=>ml+(W-ml-mr)*t/H,y=v=>mt+ph-ph*v/100;const kinds=D.program_kinds.concat(['idle']);
// stacked step areas
kinds.forEach((k,ki)=>{let d='';const pts=[];s.forEach((smp,i)=>{const t0=smp.t,t1=i+1<s.length?s[i+1].t:H;let lo=0;for(let j=0;j<ki;j++)lo+=k==='idle'?0:(D.program_kinds[j]?smp.alloc[D.program_kinds[j]]||0:0);if(k==='idle')lo=100-smp.idle;const hi=lo+(k==='idle'?smp.idle:(smp.alloc[k]||0));pts.push([t0,t1,lo,hi])});
if(!pts.some(q=>q[3]>q[2]))return;let top='',bot='';pts.forEach(([t0,t1,lo,hi])=>{top+=`${top?'L':'M'}${x(t0)} ${y(hi)}L${x(t1)} ${y(hi)}`});for(let i=pts.length-1;i>=0;i--){const[t0,t1,lo]=pts[i];bot+=`L${x(t1)} ${y(lo)}L${x(t0)} ${y(lo)}`}
const path=el('path',{d:top+bot+'Z',fill:C[k],stroke:'var(--surface)','stroke-width':1,class:'hit'},svg);path.addEventListener('mousemove',e=>{const t=Math.round((e.offsetX-ml)/(W-ml-mr)*H);let cur=s[0];for(const q of s){if(q.t<=t)cur=q;else break}if(!cur)return;showTip(e,`<b>${n}</b> tick ${t}<br>`+kinds.map(kk=>`${kk}: ${kk==='idle'?cur.idle:(cur.alloc[kk]||0)}%`).filter(z=>!z.endsWith(' 0%')).join('<br>')+`<br>attitude ${cur.attitude}${D.config.ledger?' · balance '+fmt(cur.balance):''}`)});path.addEventListener('mouseleave',hideTip)});
// marks: attitude changes, completions
let prevA=null,prevC=0;s.forEach(smp=>{if(prevA!==null&&smp.attitude!==prevA){const m=el('path',{d:`M${x(smp.t)} ${mt-8}l4 4-4 4-4-4Z`,fill:'var(--s7)'},svg);m.addEventListener('mousemove',e=>showTip(e,`${n} tick ${smp.t}: attitude ${esc(smp.attitude)}`));m.addEventListener('mouseleave',hideTip)}
if(smp.completed>prevC){const m=el('circle',{cx:x(smp.t),cy:mt-4,r:3.5,fill:'var(--s3)'},svg);m.addEventListener('mousemove',e=>showTip(e,`${n} tick ${smp.t}: node completed (${smp.completed} total)`));m.addEventListener('mouseleave',hideTip)}prevA=smp.attitude;prevC=smp.completed});
[0,50,100].forEach(v=>{const t=el('text',{x:ml-4,y:y(v)+4,'text-anchor':'end'},svg);t.textContent=v+'%'});xAxis(svg,x,mt+ph,W)})})();

// balances
(function(){const sec=document.getElementById('balance-section');if(!D.config.ledger){sec.style.display='none';return}
const map={};playing.forEach((n,i)=>map[n]=ST[i%8]);legend('bal-legend',map);
const W=Math.min(960,document.body.clientWidth-32),ph=200,ml=56,mr=60,mt=8,mb=24;const svg=el('svg',{viewBox:`0 0 ${W} ${ph+mt+mb}`,width:W,height:ph+mt+mb},document.getElementById('balance'));
let max=1;playing.forEach(n=>(D.timeline[n]||[]).forEach(s=>max=Math.max(max,s.balance)));const x=t=>ml+(W-ml-mr)*t/H,y=v=>mt+ph-ph*v/max;
[0,.5,1].forEach(f=>{el('line',{x1:x(0),x2:x(H),y1:y(f*max),y2:y(f*max),class:'grid',stroke:'var(--line)'},svg);const t=el('text',{x:ml-6,y:y(f*max)+4,'text-anchor':'end'},svg);t.textContent=fmt(Math.round(f*max))});
const labels=[];playing.forEach((n,i)=>{const s=D.timeline[n]||[];if(!s.length)return;let d='';s.forEach((smp,j)=>{d+=`${j?'L':'M'}${x(smp.t)} ${y(smp.balance)}`;if(j+1<s.length)d+=`L${x(s[j+1].t)} ${y(smp.balance)}`;else d+=`L${x(H)} ${y(smp.balance)}`});
el('path',{d,fill:'none',stroke:map[n],'stroke-width':2,'stroke-linejoin':'round'},svg);const last=s[s.length-1];labels.push({n,v:last.balance,y:y(last.balance)})});
labels.sort((a,b)=>a.y-b.y);for(let i=1;i<labels.length;i++)if(labels[i].y-labels[i-1].y<12)labels[i].y=labels[i-1].y+12;
if(labels.length){const n=labels.length-1;labels[n].y=Math.min(labels[n].y,mt+ph-2);for(let i=n-1;i>=0;i--)labels[i].y=Math.min(labels[i].y,labels[i+1].y-12)}
labels.forEach(l=>{const t=el('text',{x:x(H)+6,y:l.y+4},svg);t.textContent=`${l.n} ${fmt(l.v)}`;el('circle',{cx:x(H)+2,cy:l.y,r:2,fill:map[l.n]},svg)});
const hit=el('rect',{x:x(0),y:mt,width:x(H)-x(0),height:ph,fill:'transparent',class:'hit'},svg);const cross=el('line',{y1:mt,y2:mt+ph,stroke:'var(--text-3)',visibility:'hidden'},svg);
hit.addEventListener('mousemove',e=>{const t=Math.round((e.offsetX-ml)/(W-ml-mr)*H);cross.setAttribute('x1',x(t));cross.setAttribute('x2',x(t));cross.setAttribute('visibility','visible');showTip(e,`tick ${t}<br>`+playing.map(n=>{const s=D.timeline[n]||[];let cur=s[0];for(const q of s){if(q.t<=t)cur=q;else break}return cur?`${n}: ${fmt(cur.balance)}`:''}).join('<br>'))});hit.addEventListener('mouseleave',()=>{hideTip();cross.setAttribute('visibility','hidden')});xAxis(svg,x,mt+ph,W)})();

// octogram + matrix
(function(){legend('int-legend',IC);const sel=document.getElementById('kind');['all'].concat(Object.keys(IC)).forEach(k=>{const o=document.createElement('option');o.value=k;o.textContent=k;sel.appendChild(o)});
function draw(){const kind=sel.value;const host=document.getElementById('octo');host.innerHTML='<h3>Octogram</h3>';const S=Math.min(440,host.clientWidth-16||400),R=S/2-50,cx=S/2,cy=S/2;const svg=el('svg',{viewBox:`0 0 ${S} ${S}`,width:S,height:S},host);
const pos={};nodes.forEach((n,i)=>{const a=-Math.PI/2+2*Math.PI*i/nodes.length;pos[n]=[cx+R*Math.cos(a),cy+R*Math.sin(a)]});
D.config.neighbors.forEach(([a,b])=>el('line',{x1:pos[a][0],y1:pos[a][1],x2:pos[b][0],y2:pos[b][1],stroke:'var(--line)','stroke-width':1},svg));
const val=e=>kind==='all'?Object.keys(IC).reduce((s,k)=>s+e[k],0):e[kind];const maxv=Math.max(1,...D.interactions.edges.map(val));
D.interactions.edges.forEach(e=>{const v=val(e);if(!v)return;const [x1,y1]=pos[e.from],[x2,y2]=pos[e.to];const mx=(x1+x2)/2,my=(y1+y2)/2;const dx=x2-x1,dy=y2-y1,len=Math.hypot(dx,dy)||1;const off=Math.min(60,len*0.18);const qx=mx+dy/len*off,qy=my-dx/len*off;
const color=kind==='all'?IC[Object.keys(IC).reduce((b,k)=>e[k]>e[b]?k:b,'dispatch')]:IC[kind];const p=el('path',{d:`M${x1} ${y1}Q${qx} ${qy} ${x2} ${y2}`,fill:'none',stroke:color,'stroke-width':1+9*v/maxv,'stroke-linecap':'round',opacity:.85,class:'hit'},svg);
const tq=0.86,bx=(1-tq)*(1-tq)*x1+2*(1-tq)*tq*qx+tq*tq*x2,by=(1-tq)*(1-tq)*y1+2*(1-tq)*tq*qy+tq*tq*y2;el('circle',{cx:bx,cy:by,r:2.5+2*v/maxv,fill:color,stroke:'var(--surface)','stroke-width':1},svg);
p.addEventListener('mousemove',ev=>showTip(ev,`<b>${e.from} → ${e.to}</b><br>dispatches ${e.dispatch} · artifacts ${e.artifact}<br>transfers ${e.transfer}${e.amount?' ('+fmt(e.amount)+' tokens)':''} · contracts ${e.contract}`));p.addEventListener('mouseleave',hideTip)});
nodes.forEach((n,i)=>{const [x,y]=pos[n];const inert=D.config.inert.includes(n);el('circle',{cx:x,cy:y,r:16,fill:inert?'var(--idle)':'var(--surface-2)',stroke:inert?'none':ST[i%8],'stroke-width':2},svg);const t=el('text',{x,y:y+4,'text-anchor':'middle',style:'fill:var(--text);font-weight:600'},svg);t.textContent=n;
const rl=D.interactions.relay[n];if(rl){const parts=Object.entries(rl).map(([k,v])=>`${k} ${v}`).join(', ');const tt=el('text',{x,y:y+30,'text-anchor':'middle',style:'fill:var(--text-3);font-size:10px'},svg);tt.textContent='relay: '+parts}});
// matrix
const m=document.getElementById('matrix');let h='<h3>From → to</h3><table><tr><th></th>'+nodes.map(n=>`<th>${n}</th>`).join('')+'</tr>';const idx={};D.interactions.edges.forEach(e=>idx[e.from+'>'+e.to]=e);
nodes.forEach(a=>{h+=`<tr><th>${a}</th>`+nodes.map(bn=>{const e=idx[a+'>'+bn];const v=e?val(e):0;const f=v/maxv;return `<td class="num" style="background:${v?`color-mix(in oklab, var(--s1) ${Math.round(15+70*f)}%, var(--surface-2))`:'transparent'}">${v||''}</td>`}).join('')+'</tr>'});m.innerHTML=h+'</table>'}
sel.addEventListener('change',draw);draw()})();

// transcript
(function(){const ss=document.getElementById('station'),ls=document.getElementById('limit');['all'].concat(playing).forEach(n=>{const o=document.createElement('option');o.value=n;o.textContent=n;ss.appendChild(o)});
function sum(o){const p=Object.entries(o).filter(([k])=>k!=='type').map(([k,v])=>`${k}=${typeof v==='string'?JSON.stringify(v.length>60?v.slice(0,57)+'…':v):JSON.stringify(v)}`).join(' ');return `<b>${esc(o.type)}</b> ${esc(p)}`}
function draw(){const st=ss.value,lim=ls.value==='all'?Infinity:+ls.value;const rows=D.wakes.list.filter(w=>st==='all'||w.node===st);document.getElementById('tcount').textContent=`${rows.length} wakes`;
let h='<table><tr><th>tick</th><th>station</th><th>reasons</th><th>orders</th><th>sample</th></tr>';rows.slice(0,lim).forEach(w=>{h+=`<tr><td class="num">${w.tick}</td><td>${w.node}</td><td>${esc(w.reasons.join(', '))}</td><td>${w.orders.map(sum).join('<br>')}${w.rejected.map(r=>`<br><span style="color:var(--bad)">rejected</span> ${sum(r.order)} <i>${esc(r.reason)}</i>`).join('')}</td><td>${w.status&&w.status!=='ok'?`<span style="color:var(--bad)">${esc(w.status)}</span><br>`:''}${w.attempts?w.attempts+' attempt'+(w.attempts>1?'s':''):''}${w.tokens?' · '+fmt(w.tokens)+' tok':''}${w.output?`<details><summary>output</summary><pre>${esc(w.output)}</pre></details>`:''}</td></tr>`});
document.getElementById('transcript').innerHTML=h+'</table>'}ss.addEventListener('change',draw);ls.addEventListener('change',draw);draw()})();
</script></body></html>
"""
