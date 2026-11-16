// Executes the page's inline script against a stub DOM to prove it actually runs.
//
// `node --check` only parses. A hoisted function reaching a `let` that has not been
// initialised yet parses perfectly and throws at run time, which kills the rest of the
// script -- the page then loads with no working controls while the server looks fine.
// That is exactly how the UI language work shipped broken.
//
//   node map/page_stub.mjs <script.js>      [EMBED=1] [NAVLANG=ru-RU] [BASEMAP=<json>]
import fs from 'node:fs';
import vm from 'node:vm';
const target = process.argv[2];
if (!target) { console.error('usage: node page_stub.mjs <script.js>'); process.exit(2); }
const js = fs.readFileSync(target, 'utf8');

const mkEl = (id='') => {
  const el = {
    id, style:{}, dataset:{}, classList:{add(){},remove(){},toggle(){},contains(){return false}},
    children:[], attributes:{},
    textContent:'', innerHTML:'', value:'', placeholder:'', title:'', checked:false, disabled:false,
    appendChild(c){this.children.push(c); return c},
    removeChild(){}, remove(){}, insertBefore(c){this.children.push(c); return c},
    addEventListener(){}, removeEventListener(){}, click(){},
    setAttribute(k,v){this.attributes[k]=v}, getAttribute(k){return this.attributes[k] ?? null},
    querySelector(){return mkEl()}, querySelectorAll(){return []},
    getBoundingClientRect(){return {top:0,left:0,width:100,height:100,right:100,bottom:100}},
    get firstChild(){return this.children[0] ?? null},
    get parentNode(){return null},
  };
  return el;
};
const store = {};
const els = {};
const document = {
  documentElement: mkEl('html'), body: mkEl('body'),
  getElementById:(id)=>(els[id] ||= mkEl(id)),
  querySelector:()=>mkEl(), querySelectorAll:()=>[],
  createElement:(t)=>mkEl(t), createTextNode:()=>mkEl(),
  addEventListener(){},
};
const sandbox = {
  console, setTimeout, clearTimeout, setInterval:()=>0, clearInterval(){},
  performance,
  requestAnimationFrame:()=>0, cancelAnimationFrame(){},
  fetch: async () => ({ ok:true, status:200, json: async()=>({}), text: async()=>'',
                        headers:{get:()=>null} }),
  document, navigator:{ languages:[process.env.NAVLANG||'en-US'], language:process.env.NAVLANG||'en-US', geolocation:{watchPosition(){},clearWatch(){}} },
  localStorage:{ getItem:k=>store[k]??null, setItem:(k,v)=>store[k]=String(v), removeItem:k=>{delete store[k]} },
  location:{ href:'https://map.rns.moscow/', origin:'https://map.rns.moscow' },
  Date, Math, JSON, Object, Array, String, Number, Boolean, RegExp, Map, Set, Error, parseInt, parseFloat, isNaN,
  encodeURIComponent, decodeURIComponent, URL, Promise,
  maplibregl:{
    Map: class { constructor(){ } on(){} once(){} addControl(){} getContainer(){return mkEl()}
      setStyle(){} getStyle(){return {layers:[]}} isStyleLoaded(){return false}
      addSource(){} addLayer(){} getSource(){} getLayer(){} setLayoutProperty(){}
      getLayoutProperty(){return 'visible'} setPaintProperty(){} project(){return {x:0,y:0}}
      unproject(){return {lng:0,lat:0}} getZoom(){return 9} jumpTo(){} flyTo(){} easeTo(){}
      resize(){} fitBounds(){} getBounds(){return {getWest:()=>0,getEast:()=>0,getNorth:()=>0,getSouth:()=>0}}
      touchZoomRotate = {disableRotation(){}};
      getCanvas(){return mkEl()} },
    Marker: class { constructor(){} setLngLat(){return this} setPopup(){return this}
      addTo(){return this} remove(){} setOffset(){} getPopup(){return {isOpen:()=>false,remove(){},on(){}}}
      togglePopup(){} getElement(){return mkEl()} },
    Popup: class { constructor(){} setHTML(){return this} isOpen(){return false} remove(){} on(){} },
    addProtocol(){}, setRTLTextPlugin:()=>Promise.resolve(),
    NavigationControl: class {}, AttributionControl: class {},
  },
  pmtiles:{ Protocol: class { constructor(){ this.tile=()=>{} } } },
};
// BASEMAP='{"provider":...}' selects a basemap provider. A raster page never loads
// pmtiles.js, so the global is withheld to catch any code path that still reaches for it.
if (process.env.BASEMAP) {
  sandbox.__BASEMAP__ = JSON.parse(process.env.BASEMAP);
  if (sandbox.__BASEMAP__.raster) delete sandbox.pmtiles;
}
// COMMUNITY=1 switches on the community overlay block (overlay_enabled in the config).
if (process.env.COMMUNITY) sandbox.__COMMUNITY__ = 1;
sandbox.window = sandbox;
sandbox.self = sandbox;
sandbox.globalThis = sandbox;
sandbox.window.addEventListener = () => {};
sandbox.addEventListener = () => {};
sandbox.top = process.env.EMBED ? {} : sandbox;   // EMBED=1 simulates the iframe

vm.createContext(sandbox);
try {
  vm.runInContext(js, sandbox, { filename: 'page.js' });
  console.log('SCRIPT RAN TO COMPLETION with no thrown error');
  const lb = els['lang-toggle'];
  console.log('  lang-toggle display :', JSON.stringify(lb && lb.style.display), '(expect "block" standalone)');
  console.log('  lang-toggle text    :', JSON.stringify(lb && lb.textContent));
  const hb = els['heatmap-toggle'];
  console.log('  overlay button text :', JSON.stringify(hb && hb.textContent));
} catch (e) {
  console.log('THREW: ' + e.constructor.name + ': ' + e.message);
  process.exitCode = 1;
  const line = (e.stack.match(/page\.js:(\d+)/) || [])[1];
  if (line) {
    console.log('at line ' + line + ':');
    const src = js.split('\n');
    for (let i = Math.max(0, line-4); i < Math.min(src.length, +line+2); i++)
      console.log(`  ${i+1===+line?'>>':'  '} ${i+1}  ${src[i]}`);
  }
}
