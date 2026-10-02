"""Add a dark/light theme toggle to docs/index.html (idempotent).

Run once: python habitat/tools/patch_theme.py
"""
from pathlib import Path

P = Path(__file__).resolve().parents[2] / "docs" / "index.html"
MARK = "/* theme-toggle */"

# Applied before the stylesheet renders, so a saved light theme doesn't flash dark.
HEAD = """<script>try{if(localStorage.getItem('elk-theme')==='light')document.documentElement.setAttribute('data-theme','light')}catch(e){}</script>
"""

CSS = """
  """ + MARK + """
  :root[data-theme="light"]{
    --bg:#eceeed;--surface:#fbfaf8;--surface2:#f2f0eb;--border:#d5d2ca;
    --text:#1f2428;--text-dim:#5a626a;--accent:#9c6210;--accent-dim:rgba(156,98,16,.12);
    --green:#3d8a5a;--red:#b04545;--blue:#3f6fae;--purple:#7a5fae;
  }
  :root[data-theme="light"] .leaflet-control-attribution{background:rgba(251,250,248,.88)!important}
  :root[data-theme="light"] #panelBackdrop{background:rgba(0,0,0,.25)}
  .panel-header{display:flex;align-items:flex-start;justify-content:space-between;gap:10px}
  .theme-toggle{flex-shrink:0;width:32px;height:32px;border-radius:7px;border:1px solid var(--border);background:var(--bg);
    color:var(--text-dim);cursor:pointer;display:flex;align-items:center;justify-content:center;transition:color .2s,border-color .2s}
  .theme-toggle:hover{color:var(--accent);border-color:var(--accent)}
  .theme-toggle svg{width:16px;height:16px}
  .theme-toggle .ico-sun{display:none}
  :root[data-theme="light"] .theme-toggle .ico-sun{display:block}
  :root[data-theme="light"] .theme-toggle .ico-moon{display:none}
"""

BUTTON = """<button class="theme-toggle" id="themeToggle" type="button" title="Switch light / dark" aria-label="Switch light / dark theme">
    <svg class="ico-moon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>
    <svg class="ico-sun" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>
  </button>"""

CB_OLD = "const CB={responsive:true"
CB_FN = """// Chart colors follow the theme (see setTheme)
function themeColors(){const s=getComputedStyle(document.documentElement),v=n=>s.getPropertyValue(n).trim();
  const light=document.documentElement.getAttribute('data-theme')==='light';
  return{surface:v('--surface'),border:v('--border'),text:v('--text'),dim:v('--text-dim'),
    grid:light?'rgba(120,115,105,.18)':'rgba(42,47,53,.4)'}}
function mkCB(){const T=themeColors();return {responsive:true"""

JS = """
// ---- THEME (dark / light) ----
function currentTheme(){return document.documentElement.getAttribute('data-theme')==='light'?'light':'dark'}
function setTheme(t,fromUser){
  if(t==='light')document.documentElement.setAttribute('data-theme','light');
  else document.documentElement.removeAttribute('data-theme');
  try{localStorage.setItem('elk-theme',t)}catch(e){}
  CB=mkCB();
  CC[0]=t==='light'?'#cfccc4':'#1e2124';   // "No data" units: soft gray on light, near-black on dark
  // Match the basemap to the theme unless the user is on satellite.
  const sat=document.getElementById('bmSat');
  if(map&&!(sat&&sat.checked)){
    const id=t==='light'?'bmLight':'bmDark',r=document.getElementById(id);
    if(r&&!r.checked){r.checked=true;setBM(t)}
  }
  if(fromUser&&map)updateMap();
  [popChart,harvestChart].forEach(ch=>{if(ch)recolorChart(ch)});   // recolor open charts
}
function recolorChart(ch){
  const T=themeColors(),o=ch.options,tt=o.plugins&&o.plugins.tooltip;
  if(tt){tt.backgroundColor=T.surface;tt.borderColor=T.border;tt.titleColor=T.text;tt.bodyColor=T.text}
  if(o.plugins&&o.plugins.legend&&o.plugins.legend.labels)o.plugins.legend.labels.color=T.dim;
  ['x','y'].forEach(k=>{const a=o.scales&&o.scales[k];if(!a)return;
    if(a.ticks)a.ticks.color=T.dim;if(a.grid)a.grid.color=T.grid;if(a.border)a.border.color=T.border});
  ch.update('none');
}
"""


def main():
    s = P.read_text(encoding="utf-8")
    if MARK in s:
        print("already patched")
        return

    def rep(old, new, count=1):
        nonlocal s
        assert old in s, f"anchor not found: {old[:70]}"
        s = s.replace(old, new, count)

    rep("<style>", HEAD + "<style>")
    rep("  *{margin:0;padding:0;box-sizing:border-box}", CSS + "  *{margin:0;padding:0;box-sizing:border-box}")
    rep('<div class="panel-header"><h1>Colorado Elk Dashboard</h1><div class="subtitle">Harvest & Population · CPW Data</div></div>',
        '<div class="panel-header"><div><h1>Colorado Elk Dashboard</h1><div class="subtitle">Harvest & Population · CPW Data</div></div>'
        + BUTTON + '</div>')

    # Chart config: constant -> function of the theme
    a = s.index(CB_OLD)
    b = s.index("};", a) + 2
    cb = s[a:b]
    cb = (cb.replace("'#181c20'", "T.surface").replace("'#2a2f35'", "T.border")
            .replace("'#e8e6e3'", "T.text").replace("'#8a9199'", "T.dim")
            .replace("'rgba(42,47,53,.4)'", "T.grid"))
    cb = cb.replace(CB_OLD, CB_FN, 1)[:-1] + "}\nlet CB=mkCB();"
    s = s[:a] + cb + s[b:]
    rep("labels:{color:'#8a9199',font:{family:'DM Sans',size:9.5}",
        "labels:{color:themeColors().dim,font:{family:'DM Sans',size:9.5}")

    rep("// ---- CHARTS ----", JS + "\n// ---- CHARTS ----")
    rep("DATA=d;GEOJSON=g;buildIdx();initMap();initCtrl();updateAll();",
        "DATA=d;GEOJSON=g;buildIdx();initMap();if(currentTheme()==='light')setTheme('light',false);initCtrl();updateAll();")
    rep("function initCtrl(){",
        "function initCtrl(){\n  document.getElementById('themeToggle').addEventListener('click',()=>setTheme(currentTheme()==='light'?'dark':'light',true));")
    P.write_text(s, encoding="utf-8")
    print("patched", P)


if __name__ == "__main__":
    main()
