"""Add the habitat-model layer controls to docs/index.html (idempotent).

Run once: python habitat/tools/patch_dashboard.py
"""
from pathlib import Path

P = Path(__file__).resolve().parents[2] / "docs" / "index.html"
MARK = "<!-- habitat-layers -->"

CSS = """
  /* habitat-layers */
  .hab-sub{font-size:10px;color:var(--text-dim);margin:8px 0 4px;text-transform:uppercase;letter-spacing:.06em}
  .hab-legend{display:flex;flex-wrap:wrap;gap:3px 10px;margin-top:6px}
  .hab-legend-item{display:flex;align-items:center;gap:5px;font-size:10.5px;color:var(--text-dim)}
  .hab-legend-swatch{width:14px;height:9px;border-radius:2px;flex-shrink:0}
  .hab-legend-line{width:16px;height:0;border-top-width:2px;border-top-style:solid;flex-shrink:0}
  .hab-legend-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
  .hab-note{font-size:10px;color:var(--text-dim);margin-top:5px;line-height:1.35}
  .hab-check{display:flex;align-items:center;gap:7px;font-size:11.5px;color:var(--text);margin:4px 0;cursor:pointer}
  .hab-check input{accent-color:var(--accent)}
  .hab-check .hab-status{font-size:10px;color:var(--text-dim)}
  .hab-feat-legend{margin:0 0 6px 21px}
  .hab-popup b{display:block;margin-bottom:2px}
"""

HTML = """
    <div class="control-group" id="habGroup" style="display:none">""" + MARK + """
      <div class="control-label">Elk Habitat Model <span id="habSeason"></span></div>
      <div id="habModelWrap">
        <div class="pill-group" style="margin-bottom:6px">
          <input type="radio" name="hab" id="habOff" value="off" checked><label for="habOff">Off</label>
          <input type="radio" name="hab" id="habHunt" value="hunt"><label for="habHunt">Hunt</label>
          <input type="radio" name="hab" id="habHab" value="habitat"><label for="habHab">Habitat</label>
        </div>
        <div id="habModelOpts" style="display:none">
          <div class="opacity-row"><input type="range" id="habOpacity" min="10" max="100" value="85" step="1"><span class="opacity-val" id="habOpacityVal">85%</span></div>
          <div class="hab-legend" id="habModelLegend"></div>
        </div>
      </div>
      <div id="habInputWrap">
        <div class="hab-sub">Background layer</div>
        <select id="habInput"><option value="">None</option></select>
        <div id="habInputOpts" style="display:none;margin-top:6px">
          <div class="opacity-row"><input type="range" id="habInputOpacity" min="10" max="100" value="70" step="1"><span class="opacity-val" id="habInputOpacityVal">70%</span></div>
          <div class="hab-legend" id="habInputLegend"></div>
        </div>
      </div>
      <div id="habFeatWrap">
        <div class="hab-sub">Features</div>
        <div id="habFeatures"></div>
      </div>
      <div class="hab-note" id="habNote"></div>
    </div>"""

JS = """
// ---- ELK HABITAT MODEL LAYERS ----
// Built from data/habitat_layers/meta.json (written by habitat/export_layers.py).
// Anything not exported simply doesn't appear.
const HAB_BASE='data/habitat_layers/';
const EMPTY_TILE='data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=';
let habMeta=null,habModelLayer=null,habInputLayer=null;
const habVec={};   // id -> {layer, loading}
const rgbaCss=c=>`rgba(${c[0]},${c[1]},${c[2]},${(c[3]/255).toFixed(2)})`;
const habEsc=s=>String(s||'').replace(/[&<>"]/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[ch]));

function habRasterLegend(el,layer){
  el.innerHTML=(layer.group==='model'?layer.legend.slice().reverse():layer.legend).map(c=>
    `<span class="hab-legend-item"><span class="hab-legend-swatch" style="background:${rgbaCss(c.rgba)}"></span>${habEsc(c.label)}</span>`).join('');
}
function habTileLayer(layer,pane,opacity){
  return L.tileLayer(layer.url,{pane,opacity,minZoom:(layer.minZoom||8)-2,maxNativeZoom:layer.maxNativeZoom||12,
    maxZoom:18,bounds:habMeta.bounds?L.latLngBounds(habMeta.bounds):undefined,errorTileUrl:EMPTY_TILE,
    attribution:'Elk habitat model'});
}

async function initHabitat(){
  try{
    const r=await fetch(HAB_BASE+'meta.json',{cache:'no-cache'});
    if(!r.ok)return;
    habMeta=await r.json();
  }catch(e){return}
  const L_=habMeta.layers||[];
  const model=L_.filter(l=>l.group==='model'),inputs=L_.filter(l=>l.group==='input'),vecs=L_.filter(l=>l.type==='vector');
  if(!L_.length)return;
  document.getElementById('habGroup').style.display='';
  document.getElementById('habSeason').textContent={late:'(2nd/3rd rifle)',early:'(archery/1st rifle)'}[habMeta.season]||'';
  if(!model.length)document.getElementById('habModelWrap').style.display='none';
  if(!inputs.length)document.getElementById('habInputWrap').style.display='none';
  else{
    const sel=document.getElementById('habInput');
    inputs.forEach(l=>{const o=document.createElement('option');o.value=l.id;o.textContent=l.label;sel.appendChild(o)});
  }
  if(!vecs.length)document.getElementById('habFeatWrap').style.display='none';
  else document.getElementById('habFeatures').innerHTML=vecs.map(l=>{
    const leg=Object.values(l.styles).map(s=>{
      const sw=l.kind==='point'?`<span class="hab-legend-dot" style="background:${s.color}"></span>`
        :l.kind==='poly'||s.fill?`<span class="hab-legend-swatch" style="background:${s.color};opacity:${Math.max(s.fill||0,.5)}"></span>`
        :`<span class="hab-legend-line" style="border-top-color:${s.color};border-top-style:${s.dash?'dashed':'solid'}"></span>`;
      return `<span class="hab-legend-item">${sw}${habEsc(s.label)}</span>`}).join('');
    return `<label class="hab-check"><input type="checkbox" data-hab-vec="${l.id}">${habEsc(l.label)} <span class="hab-status" id="habStat-${l.id}"></span></label>
      <div class="hab-legend hab-feat-legend" id="habLeg-${l.id}" style="display:none">${leg}</div>`}).join('');
  const units=Object.values(habMeta.units||{}).flat().sort((a,b)=>a-b).join(', ');
  const snow=habMeta.snow_scenario&&habMeta.snow_scenario!=='normal'?` · ${habMeta.snow_scenario} snow scenario`:'';
  document.getElementById('habNote').textContent=`Model layers ranked within each unit group (units ${units}). Updated ${habMeta.generated}${snow}.`;
  document.querySelectorAll('input[name="hab"]').forEach(r=>r.addEventListener('change',()=>setHabModel(r.value)));
  document.getElementById('habOpacity').addEventListener('input',()=>{const v=+habOpacity.value;habOpacityVal.textContent=v+'%';if(habModelLayer)habModelLayer.setOpacity(v/100)});
  document.getElementById('habInput').addEventListener('change',e=>setHabInput(e.target.value));
  document.getElementById('habInputOpacity').addEventListener('input',()=>{const v=+habInputOpacity.value;habInputOpacityVal.textContent=v+'%';if(habInputLayer)habInputLayer.setOpacity(v/100)});
  document.querySelectorAll('[data-hab-vec]').forEach(cb=>cb.addEventListener('change',()=>setHabVector(cb.dataset.habVec,cb.checked)));
}

function setHabModel(mode){
  if(habModelLayer){map.removeLayer(habModelLayer);habModelLayer=null}
  const opts=document.getElementById('habModelOpts');
  if(mode==='off'){opts.style.display='none';return}
  const id=mode==='hunt'?habMeta.season:habMeta.season+'_habitat';
  const layer=habMeta.layers.find(l=>l.id===id);
  if(!layer){opts.style.display='none';return}
  habModelLayer=habTileLayer(layer,'habPane',+document.getElementById('habOpacity').value/100).addTo(map);
  habRasterLegend(document.getElementById('habModelLegend'),layer);
  opts.style.display='';
}

function setHabInput(id){
  if(habInputLayer){map.removeLayer(habInputLayer);habInputLayer=null}
  const opts=document.getElementById('habInputOpts');
  const layer=habMeta.layers.find(l=>l.id===id);
  if(!layer){opts.style.display='none';return}
  habInputLayer=habTileLayer(layer,'habInputPane',+document.getElementById('habInputOpacity').value/100).addTo(map);
  habRasterLegend(document.getElementById('habInputLegend'),layer);
  opts.style.display='';
}

async function setHabVector(id,on){
  const meta=habMeta.layers.find(l=>l.id===id),stat=document.getElementById('habStat-'+id);
  document.getElementById('habLeg-'+id).style.display=on?'':'none';
  const st=habVec[id]||(habVec[id]={layer:null,loading:false});
  if(!on){if(st.layer)map.removeLayer(st.layer);return}
  if(st.layer){st.layer.addTo(map);return}
  if(st.loading)return;
  st.loading=true;stat.textContent='loading…';
  try{
    const gj=await (await fetch(meta.url)).json();
    const renderer=L.canvas({pane:'habFeatPane',padding:0.5});
    const style=f=>{const s=meta.styles[f.properties.c]||{};return{renderer,color:s.color,weight:s.weight||1.5,
      dashArray:s.dash||null,opacity:.95,fill:!!s.fill,fillColor:s.color,fillOpacity:s.fill||0}};
    st.layer=L.geoJSON(gj,{pane:'habFeatPane',style,
      pointToLayer:(f,ll)=>{const s=meta.styles[f.properties.c]||{};return L.circleMarker(ll,{renderer,pane:'habFeatPane',
        radius:s.radius||4,color:'#10202a',weight:1,fillColor:s.color,fillOpacity:.95})},
      onEachFeature:(f,l)=>{const p=f.properties,s=meta.styles[p.c]||{};
        l.bindPopup(`<div class="hab-popup"><b>${habEsc(p.n||s.label||'')}</b>${habEsc(p.d||'')}${p.n&&s.label?'<br>'+habEsc(s.label):''}</div>`)}});
    stat.textContent='';
    if(document.querySelector(`[data-hab-vec="${id}"]`).checked)st.layer.addTo(map);
  }catch(e){stat.textContent='failed to load'}
  st.loading=false;
}
"""

PANES = """
  // Habitat layers: background input tiles above the SMA layer, model tiles above
  // the GMU fills (clicks pass through to units), features on top (clickable).
  map.createPane('habInputPane');map.getPane('habInputPane').style.zIndex=440;map.getPane('habInputPane').style.pointerEvents='none';
  map.createPane('habPane');map.getPane('habPane').style.zIndex=450;map.getPane('habPane').style.pointerEvents='none';
  map.createPane('habFeatPane');map.getPane('habFeatPane').style.zIndex=460;"""


def main():
    s = P.read_text(encoding="utf-8")
    if MARK in s:
        print("already patched")
        return
    def ins(after, text):
        nonlocal s
        assert after in s, f"anchor not found: {after[:60]}"
        s = s.replace(after, after + text, 1)
    ins("  .sma-legend.visible{display:block}", CSS)
    ins('''        <div class="opacity-row"><input type="range" id="smaOpacity" min="10" max="100" value="50" step="1"><span class="opacity-val" id="smaOpacityVal">50%</span></div>
      </div>
    </div>''', HTML)
    s = s.replace("// ---- MAP ----\nfunction initMap(){", JS + "\n// ---- MAP ----\nfunction initMap(){", 1)
    ins("  map.getPane('smaPane').style.pointerEvents='none';", PANES)
    ins("  document.getElementById('smaOpacity').addEventListener('input',updateSMAOpacity);", "\n  initHabitat();")
    assert "function initHabitat" in s
    P.write_text(s, encoding="utf-8")
    print("patched", P)


if __name__ == "__main__":
    main()
