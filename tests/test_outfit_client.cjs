'use strict';
// Behavioral checks using a small DOM double; browser rendering is a separate check.
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const path = require('node:path');
class Element {
  constructor() {
    this.children = []; this.hidden = false; this.disabled = false; this.value = '';
    this.dataset = {}; this.attributes = {}; this.style = {}; this.events = {};
    const classes = new Set();
    this.classList = {add:c=>classes.add(c), remove:c=>classes.delete(c),
      toggle:(c,on)=>on ? classes.add(c) : classes.delete(c), contains:c=>classes.has(c)};
  }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  setAttribute(key, value) { this.attributes[key] = value; }
  getAttribute(key) { return this.attributes[key] ?? null; }
  removeAttribute(key) { delete this.attributes[key]; }
  addEventListener(key, value) { this.events[key] = value; }
  showModal() { this.open = true; }
  close() { this.open = false; this.events.close?.(); }
  animate() {}
}
const elements = new Map(), get = id => {
  if (!elements.has(id)) elements.set(id, new Element());
  return elements.get(id);
};
const views = ['before','after'].map(view=>Object.assign(new Element(), {dataset:{view}}));
const sandbox = {
  document: {getElementById:get, querySelector:s=>s.includes('fitme-api') ? {content:''} : get('shell'),
    querySelectorAll:s=>s === '[data-view]' ? views : [], createElement:()=>new Element(),
    createTextNode:text=>({textContent:text})},
  location:{hash:'',pathname:'/',search:''}, history:{replaceState(){}},
  crypto:require('node:crypto').webcrypto, URLSearchParams, URL, Uint8Array,
  Image:Element, window:{matchMedia:()=>({matches:false})},
  setInterval(){}, clearTimeout(){}, setTimeout(){}, console,
};
vm.createContext(sandbox);
const appScript = fs.existsSync(path.join(__dirname,'../static/app.js')) ? '../static/app.js' : '../app.js';
vm.runInContext(fs.readFileSync(path.join(__dirname,appScript),'utf8')
  .split('// First-party activity')[0].replace('\ninit();','\n'), sandbox);
const run = code => vm.runInContext(code, sandbox);
(async () => {
  run(`state.config={generation_enabled:true, remaining:5, classification_enabled:true};
    state.person='person'; state.garments=[
      {id:'shirt', image:'shirt-photo', category:'Футболка'},
      {id:'jeans', image:'jeans-photo', category:'Джинсы'},
      {id:'hat', image:'hat-photo', category:'Кепка'}]; refreshUploads();`);
  assert.equal(get('garment-list').children.length, 4); // three things and +
  assert.equal(get('generate').disabled, false);
  run(`openItem('jeans')`);
  assert.equal(get('item-category').value, 'Джинсы');
  get('item-category').value = 'Шорты'; await get('item-category').onchange();
  assert.equal(run('state.garments[1].category'), 'Шорты');

  // A late classifier response cannot overwrite a user's manual correction.
  let finishRecognition;
  sandbox.recognitionResponse = new Promise(resolve=>finishRecognition=resolve);
  run(`api=async()=>recognitionResponse; var recognizing=recognize(state.garments[1]);`);
  get('item-category').value = 'Брюки'; await get('item-category').onchange();
  finishRecognition({category:'Джинсы'}); await run('recognizing');
  assert.equal(run('state.garments[1].category'), 'Брюки');
  await get('item-remove').onclick();
  assert.equal(run('state.garments.length'), 2);

  // Exactly one job request carries all current products, even on a double tap.
  const requests = [];
  sandbox.recordJob = body=>requests.push(body);
  run(`api=async(path, options)=>{recordJob(JSON.parse(options.body)); return {
    id:'saved', status:'queued', created:Date.now()/1000, garments:[{category:'Футболка',index:0},{category:'Кепка',index:1}]};};
    session=async()=>({}); pollJob=async()=>{};`);
  await Promise.all([get('generate').onclick(), get('generate').onclick()]);
  assert.equal(requests.length, 1);
  assert.equal(requests[0].garments.length, 2);
  assert.equal(requests[0].garments[1].category, 'Кепка');

  run(`state.busy=false; state.jobs=[{id:'old',status:'success',created:1,garments:[
    {category:'Свитер',index:0},{category:'Джинсы',index:1}]}]; renderRecent();`);
  const card = get('recent-list').children[0];
  assert.equal(card.children[1].children[1].children.length, 2);
  card.children[1].children[0].onclick();
  views[0].onclick();
  assert.equal(get('main-photo').getAttribute('src'), null); // src property is used by the DOM double
  assert.equal(get('main-photo').src, '/api/jobs/old/person');
  views[1].onclick();
  assert.equal(get('main-photo').src, '/api/jobs/old/result');

  // Editing a historical outfit restores its own photos, not the unrelated draft.
  sandbox.fetch = async url=>({ok:true,blob:async()=>url});
  run(`prepareFile=async blob=>blob; state.person='unrelated draft';`);
  await run('backToPhotos(false)');
  assert.equal(run('state.person'), '/api/jobs/old/person');
  assert.equal(run('state.garments[0].category'), 'Свитер');
  assert.equal(run('state.garments[1].image'), '/api/jobs/old/garments/1');
  assert.equal(get('garment-list').children.length, 3);
  console.log('Outfit client: tiles, labels, stale recognition, deletion, single submission, history, before/after, restoration passed');
})().catch(error=>{console.error(error); process.exitCode=1;});
