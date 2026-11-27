"""Génère les deux pages de doc (docs/en/harvester-parity.md,
docs/fr/parite-harvester.md) et, avec --html <fichier>, la page autonome du
tableau de parité, depuis data.py (source unique).

  python3 tools/parity/gen.py --docs [--html /chemin/parity.html]
"""
import importlib.util
import json
import sys
from pathlib import Path

HERE = Path(__file__).parent
spec = importlib.util.spec_from_file_location("data", HERE / "data.py")
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)
REPO = HERE.parent.parent

LABEL = {
    "fr": {"ok": "Fait", "part": "Partiel", "todo": "Manquant", "plus": "Console seulement", "na": "Hors périmètre"},
    "en": {"ok": "Done", "part": "Partial", "todo": "Missing", "plus": "Console only", "na": "Out of scope"},
}


def counts(rows):
    c = {k: 0 for k in ("ok", "part", "todo", "plus", "na")}
    for r in rows:
        c[r["s"]] += 1
    return c


def md(lang):
    L = LABEL[lang]
    all_rows = [r for s in d.S for r in s["rows"] if s["key"] != "console"]
    c = counts(all_rows)
    comparable = c["ok"] + c["part"] + c["todo"]
    if lang == "fr":
        out = ["# Parité avec l'interface de Harvester", "",
               f"État au {d.DATE}, console **v{d.AS_OF}**, comparée à l'interface de **Harvester v1.9.1** "
               "(menus relevés dans le code de harvester-ui-extension v1.9.1 et la documentation v1.9).", "",
               f"Sur {comparable} fonctions de l'interface de Harvester : **{c['ok']} faites**, "
               f"**{c['part']} partielles**, **{c['todo']} manquantes** ; {c['na']} hors périmètre. "
               "Une fonction manquante porte la version où elle est prévue.", "",
               "Statuts : Fait, Partiel (ce qui manque est dit), Manquant (version prévue), "
               "Console seulement (ce que Harvester n'a pas), Hors périmètre.", ""]
    else:
        out = ["# Parity with the Harvester UI", "",
               f"Status on {d.DATE}, console **v{d.AS_OF}**, compared with the **Harvester v1.9.1** UI "
               "(menus taken from the harvester-ui-extension v1.9.1 source and the v1.9 documentation).", "",
               f"Of {comparable} functions of the Harvester UI: **{c['ok']} done**, **{c['part']} partial**, "
               f"**{c['todo']} missing**; {c['na']} out of scope. A missing function shows the version it is planned for.", "",
               "Statuses: Done, Partial (what is missing is said), Missing (planned version), "
               "Console only (what Harvester does not have), Out of scope.", ""]
    for s in d.S:
        out.append(f"## {s[lang]}")
        if s["hen"]:
            out.append("")
            out.append(("Menu Harvester : " if lang == "fr" else "Harvester menu: ") + f"*{s['hen']}*")
        out.append("")
        out.append("| " + ("Fonction | Statut | Version | Précision" if lang == "fr" else "Function | Status | Version | Note") + " |")
        out.append("|---|---|---|---|")
        for r in s["rows"]:
            v = r["v"]
            if r["s"] == "todo" and v:
                v = ("prévue " if lang == "fr" else "planned ") + v
            note = r["nfr" if lang == "fr" else "nen"]
            out.append(f"| {r[lang]} | {L[r['s']]} | {v} | {note} |")
        out.append("")
    text = "\n".join(out)
    assert "—" not in text and "→" not in text
    return text


CSS = r"""
:root{--ground:#f4f6f4;--surface:#ffffff;--ink:#1b2320;--muted:#5d6a65;--line:#dbe2de;--accent:#0e7466;
--ok:#2f7d4f;--ok-bg:#e3f2e8;--part:#9a6a12;--part-bg:#f8eed6;--todo:#b3392c;--todo-bg:#f9e2de;--plus:#3a5bb0;--plus-bg:#e3e9f8;--na:#6b7472;--na-bg:#eceeed;
--display:"Archivo",system-ui,sans-serif;--body:"IBM Plex Sans",system-ui,sans-serif;--mono:"IBM Plex Mono",ui-monospace,Menlo,monospace}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--ground:#111614;--surface:#18201d;--ink:#e3ebe7;--muted:#9aa8a2;--line:#2a3531;--accent:#3fbfa9;
--ok:#7bd39a;--ok-bg:#1c3326;--part:#e3b45a;--part-bg:#3a2e14;--todo:#f08a7d;--todo-bg:#3d1f1b;--plus:#93aef0;--plus-bg:#1f2a44;--na:#a7b0ad;--na-bg:#262d2b}}
:root[data-theme="dark"]{color-scheme:dark;--ground:#111614;--surface:#18201d;--ink:#e3ebe7;--muted:#9aa8a2;--line:#2a3531;--accent:#3fbfa9;
--ok:#7bd39a;--ok-bg:#1c3326;--part:#e3b45a;--part-bg:#3a2e14;--todo:#f08a7d;--todo-bg:#3d1f1b;--plus:#93aef0;--plus-bg:#1f2a44;--na:#a7b0ad;--na-bg:#262d2b}
body{background:var(--ground);color:var(--ink);font-family:var(--body);font-size:15px;line-height:1.5}
.wrap{max-width:1180px;margin:0 auto;padding-inline:20px;padding-block:28px 64px}
header.top{display:flex;flex-wrap:wrap;gap:12px 24px;align-items:flex-end;justify-content:space-between;border-bottom:2px solid var(--ink);padding-bottom:14px}
h1{font-family:var(--display);font-weight:800;font-size:clamp(26px,4vw,38px);letter-spacing:-.01em;margin:0;text-wrap:balance}
.sub{color:var(--muted);margin:6px 0 0;max-width:68ch}
.lang{display:flex;gap:4px}
.lang button,.chip{font:600 12px/1 var(--body);letter-spacing:.04em;text-transform:uppercase;border:1px solid var(--line);background:var(--surface);color:var(--ink);padding:7px 10px;border-radius:4px;cursor:pointer}
.lang button[aria-pressed="true"]{background:var(--ink);color:var(--ground);border-color:var(--ink)}
.summary{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:22px 0 10px}
.tile{background:var(--surface);border:1px solid var(--line);border-radius:6px;padding:12px 14px}
.tile b{display:block;font-family:var(--display);font-size:30px;font-weight:800;font-variant-numeric:tabular-nums;line-height:1.1}
.tile span{font-size:12px;letter-spacing:.05em;text-transform:uppercase;color:var(--muted)}
.tile.ok b{color:var(--ok)}.tile.part b{color:var(--part)}.tile.todo b{color:var(--todo)}.tile.plus b{color:var(--plus)}
.bar{display:flex;height:10px;border-radius:5px;overflow:hidden;background:var(--line);margin:4px 0 18px}
.bar i{display:block}.bar .ok{background:var(--ok)}.bar .part{background:var(--part)}.bar .todo{background:var(--todo)}
.tools{display:flex;flex-wrap:wrap;gap:8px;align-items:center;position:sticky;top:env(safe-area-inset-top,0px);background:var(--ground);padding:10px 0;z-index:2;border-bottom:1px solid var(--line)}
.tools input{flex:1 1 220px;min-width:0;font:inherit;padding:8px 10px;border:1px solid var(--line);border-radius:4px;background:var(--surface);color:var(--ink)}
.chip[aria-pressed="false"]{opacity:.45}
.chip .dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;vertical-align:1px}
.layout{display:grid;grid-template-columns:220px 1fr;gap:28px;margin-top:18px}
nav.toc{position:sticky;top:70px;align-self:start;font-size:13.5px}
nav.toc a{display:flex;justify-content:space-between;gap:8px;color:var(--ink);text-decoration:none;padding:5px 8px;border-radius:4px}
nav.toc a:hover,nav.toc a:focus-visible{background:var(--surface);outline:2px solid var(--accent);outline-offset:-2px}
nav.toc small{color:var(--muted);font-family:var(--mono);font-size:11.5px}
section{margin-bottom:30px;scroll-margin-top:70px}
section h2{font-family:var(--display);font-size:21px;font-weight:700;margin:0 0 2px;text-wrap:balance}
.hmenu{font-size:12.5px;color:var(--muted);margin:0 0 10px}.hmenu code{font-family:var(--mono)}
.mini{display:flex;height:4px;border-radius:2px;overflow:hidden;background:var(--line);margin:6px 0 10px;max-width:260px}
.mini i{display:block}.mini .ok{background:var(--ok)}.mini .part{background:var(--part)}.mini .todo{background:var(--todo)}
.tbl{overflow-x:auto;background:var(--surface);border:1px solid var(--line);border-radius:6px}
table{width:100%;min-width:640px;border-collapse:collapse;font-size:14px;table-layout:fixed}
th:nth-child(1){width:42%}th:nth-child(2){width:150px}th:nth-child(3){width:110px}
th{text-align:left;font:600 11.5px/1.2 var(--body);letter-spacing:.06em;text-transform:uppercase;color:var(--muted);padding:9px 12px;border-bottom:1px solid var(--line)}
td{padding:9px 12px;border-bottom:1px solid var(--line);vertical-align:top}
tr:last-child td{border-bottom:0}
td.v{font-family:var(--mono);font-size:12.5px;white-space:nowrap;color:var(--muted)}
td.n{color:var(--muted);font-size:13px}
.pill{display:inline-block;font:600 11.5px/1 var(--body);padding:5px 8px;border-radius:999px;white-space:nowrap}
.pill.ok{color:var(--ok);background:var(--ok-bg)}.pill.part{color:var(--part);background:var(--part-bg)}.pill.todo{color:var(--todo);background:var(--todo-bg)}
.pill.plus{color:var(--plus);background:var(--plus-bg)}.pill.na{color:var(--na);background:var(--na-bg)}
.empty{color:var(--muted);font-style:italic;padding:12px}
footer{margin-top:36px;color:var(--muted);font-size:13px;border-top:1px solid var(--line);padding-top:12px}
@media (max-width:820px){.layout{grid-template-columns:1fr}nav.toc{display:none}}
@media (prefers-reduced-motion:no-preference){.pill,.chip{transition:opacity .15s}}
"""

JS = r"""
const D = JSON.parse(document.getElementById('data').textContent);
const T = {
 fr:{title:"Parité avec l'interface de Harvester",sub:(a,dt)=>`La console harvester-ops v${a}, fonction par fonction, face à l'interface de Harvester v1.9.1 : ce qui est fait, ce qui manque encore et la version où il arrive. Menus relevés dans le code de harvester-ui-extension v1.9.1. État au ${dt}.`,
     search:"Chercher une fonction…",fn:"Fonction",st:"Statut",v:"Version",n:"Précision",planned:"prévue ",menu:"Menu Harvester :",none:"Rien ne correspond à ce filtre.",
     tiles:{ok:"faites",part:"partielles",todo:"manquantes",plus:"console seulement"},
     foot:"Source : harvester-ui-extension v1.9.1 (config, models, edit, dialog) et docs.harvesterhci.io/v1.9. Chaque ligne manquante porte la version où elle est prévue ; la page est remise à jour à chaque release.",
     L:{ok:"Fait",part:"Partiel",todo:"Manquant",plus:"Console seulement",na:"Hors périmètre"}},
 en:{title:"Parity with the Harvester UI",sub:(a,dt)=>`The harvester-ops console v${a}, function by function, against the Harvester v1.9.1 UI: what is done, what is still missing and the version it comes in. Menus taken from the harvester-ui-extension v1.9.1 source. Status on ${dt}.`,
     search:"Find a function…",fn:"Function",st:"Status",v:"Version",n:"Note",planned:"planned ",menu:"Harvester menu:",none:"Nothing matches this filter.",
     tiles:{ok:"done",part:"partial",todo:"missing",plus:"console only"},
     foot:"Source: harvester-ui-extension v1.9.1 (config, models, edit, dialog) and docs.harvesterhci.io/v1.9. Each missing line shows the version it is planned for; the page is updated at every release.",
     L:{ok:"Done",part:"Partial",todo:"Missing",plus:"Console only",na:"Out of scope"}}};
let lang = 'fr', q = '', on = {ok:true,part:true,todo:true,plus:true,na:true};
try { lang = localStorage.getItem('parity-lang') || 'fr'; } catch (e) {}
const esc = s => String(s == null ? '' : s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
function cnt(rows){const c={ok:0,part:0,todo:0,plus:0,na:0};rows.forEach(r=>c[r.s]++);return c;}
function bar(c,cls){const t=c.ok+c.part+c.todo||1;return `<div class="${cls}" role="img" aria-label="${c.ok}/${c.part}/${c.todo}">${['ok','part','todo'].map(k=>`<i class="${k}" style="width:${(c[k]/t*100).toFixed(2)}%"></i>`).join('')}</div>`;}
function render(){
  const t=T[lang]; document.documentElement.lang=lang; document.title=t.title;
  document.getElementById('h1').textContent=t.title;
  document.getElementById('sub').textContent=t.sub(D.as_of,D.date);
  document.getElementById('q').placeholder=t.search;
  document.querySelectorAll('.lang button').forEach(b=>b.setAttribute('aria-pressed',b.dataset.l===lang));
  const all=D.S.filter(s=>s.key!=='console').flatMap(s=>s.rows); const c=cnt(all);
  document.getElementById('summary').innerHTML=['ok','part','todo','plus'].map(k=>`<div class="tile ${k}"><b>${k==='plus'?cnt(D.S.flatMap(s=>s.rows)).plus:c[k]}</b><span>${t.tiles[k]}</span></div>`).join('');
  document.getElementById('bar').innerHTML=bar(c,'bar');
  document.getElementById('chips').innerHTML=['ok','part','todo','plus','na'].map(k=>`<button class="chip" data-s="${k}" aria-pressed="${on[k]}"><span class="dot" style="background:var(--${k})"></span>${t.L[k]}</button>`).join('');
  const words=q.toLowerCase().split(/\s+/).filter(Boolean);
  let toc='', body='';
  D.S.forEach(s=>{
    const rows=s.rows.filter(r=>on[r.s]&&(!words.length||words.every(w=>(r[lang]+' '+r['n'+lang]+' '+r.fr+' '+r.en).toLowerCase().includes(w))));
    const sc=cnt(s.rows);
    toc+=`<a href="#${s.key}"><span>${esc(s[lang])}</span><small>${s.key==='console'?s.rows.length:`${sc.ok}/${sc.ok+sc.part+sc.todo}`}</small></a>`;
    body+=`<section id="${s.key}"><h2>${esc(s[lang])}</h2>${s.hen?`<p class="hmenu">${t.menu} <code>${esc(s.hen)}</code></p>`:''}${s.key!=='console'?bar(sc,'mini'):''}
      <div class="tbl">${rows.length?`<table><thead><tr><th>${t.fn}</th><th>${t.st}</th><th>${t.v}</th><th>${t.n}</th></tr></thead><tbody>${rows.map(r=>`<tr><td>${esc(r[lang])}</td><td><span class="pill ${r.s}">${t.L[r.s]}</span></td><td class="v">${r.s==='todo'&&r.v?t.planned:''}${esc(r.v)}</td><td class="n">${esc(r['n'+lang])}</td></tr>`).join('')}</tbody></table>`:`<p class="empty">${t.none}</p>`}</div></section>`;
  });
  document.getElementById('toc').innerHTML=toc; document.getElementById('body').innerHTML=body;
  document.getElementById('foot').textContent=t.foot;
}
document.addEventListener('click',e=>{
  const b=e.target.closest('.lang button'); if(b){lang=b.dataset.l; try{localStorage.setItem('parity-lang',lang);}catch(err){} render(); return;}
  const c=e.target.closest('.chip'); if(c){on[c.dataset.s]=!on[c.dataset.s]; render();}
});
document.getElementById('q').addEventListener('input',e=>{q=e.target.value; render();});
render();
"""


def html():
    data = {"as_of": d.AS_OF, "date": d.DATE, "S": d.S}
    return f"""<title>Harvester parity</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wght@700;800&family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;600&display=swap">
<style>{CSS}</style>
<div class="wrap">
  <header class="top">
    <div><h1 id="h1">Parité avec l'interface de Harvester</h1><p class="sub" id="sub"></p></div>
    <div class="lang"><button data-l="fr" aria-pressed="true">FR</button><button data-l="en" aria-pressed="false">EN</button></div>
  </header>
  <div class="summary" id="summary"></div>
  <div id="bar"></div>
  <div class="tools"><input id="q" type="search" autocomplete="off"><div id="chips" style="display:flex;flex-wrap:wrap;gap:6px"></div></div>
  <div class="layout"><nav class="toc" id="toc"></nav><main id="body"></main></div>
  <footer id="foot"></footer>
</div>
<script id="data" type="application/json">{json.dumps(data, ensure_ascii=False).replace("</", "<\\/")}</script>
<script>{JS}</script>
"""


if __name__ == "__main__":
    out = Path(sys.argv[sys.argv.index("--html") + 1]) if "--html" in sys.argv else None
    if out:
        out.write_text(html())
    if "--docs" in sys.argv:
        (REPO / "docs/en/harvester-parity.md").write_text(md("en") + "\n")
        (REPO / "docs/fr/parite-harvester.md").write_text(md("fr") + "\n")
    rows = [r for s in d.S for r in s["rows"] if s["key"] != "console"]
    print("ok", counts(rows))
