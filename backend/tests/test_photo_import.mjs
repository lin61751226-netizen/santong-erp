import assert from 'node:assert/strict';
import test from 'node:test';
import {createRequire} from 'node:module';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
const require = createRequire(import.meta.url);
const {validateFiles, retryable} = require('../app/static/photo_import.js');
test('batch limits and supported photo types', () => {
  assert.match(validateFiles([]), /選擇/);
  assert.equal(validateFiles([{name:'相簿.PNG', size:1024}]), '');
  assert.match(validateFiles(Array(101).fill({name:'a.jpg', size:1})), /100/);
  assert.match(validateFiles([{name:'a.jpg', size:10485761}]), /10 MB/);
  assert.match(validateFiles([{name:'a.jpg', size:0}]), /空白/);
  assert.match(validateFiles([{name:'a.heic', size:1024}]), /轉換/);
});
test('retry only pending, failed, or backup-unconfirmed items', () => {
  ['pending', 'failed', 'backup-warning'].forEach(status => assert.equal(retryable({status}), true));
  ['imported', 'duplicate'].forEach(status => assert.equal(retryable({status}), false));
});

function fixture() {
  const elements = new Map();
  function element() { return {value:'', hidden:false, disabled:false, dataset:{}, children:[], listeners:{},
    addEventListener(name, callback) { this.listeners[name] = callback; },
    replaceChildren(...children) { this.children = children; },
    add(child) { this.children.push(child); }, append(child) { this.children.push(child); }}; }
  const document = {getElementById(id) { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); },
    createElement: element};
  const context = {document, window:{addEventListener(){}}, confirm:() => true,
    Option: class { constructor(text, value) { this.text = text; this.value = value; } },
    FormData: class { constructor() { this.entries = []; } append(...args) { this.entries.push(args); } }};
  runInNewContext(readFileSync(new URL('../app/static/photo_import.js', import.meta.url), 'utf8'), context);
  return {ui: context.window.PhotoAlbumImport, get: id => document.getElementById(id)};
}

test('partial batch retries only failed items and freezes construction metadata until reset', async () => {
  const {ui, get} = fixture();
  const calls = [], outcomes = [{status:'imported', backup_status:'saved'}, new Error('暫時中斷'),
    {status:'duplicate', backup_status:'failed'}, {status:'duplicate', backup_status:'saved'}];
  ui.configure({sites:[{id:47, name:'善捷47'}], actor:{role:'admin'}, today:'2026-10-15',
    refresh:async () => {}, api:async (url, options) => {
      calls.push({url, entries:options.body.entries});
      const outcome = outcomes.shift(); if (outcome instanceof Error) throw outcome; return outcome;
    }});
  get('photoImportSite').value = '47'; get('photoImportDate').value = '2026-09-01';
  get('photoImportGroup').value = '47&76工地照片';
  get('photoImportFiles').listeners.change({target:{files:[{name:'a.jpg', size:100}, {name:'b.jpg', size:200}]}});
  const submit = () => get('photoImportForm').listeners.submit({preventDefault(){}});
  await submit();
  assert.equal(calls.length, 2);
  assert.equal(get('photoImportSite').disabled, true);
  assert.match(get('photoImportResults').children[1].textContent, /暫時中斷/);
  await submit();
  assert.equal(calls.length, 3);
  assert.equal(calls[2].entries.find(([key]) => key === 'file')[1].name, 'b.jpg');
  assert.match(get('photoImportResults').children[1].textContent, /備份尚未確認/);
  await submit();
  assert.equal(calls.length, 4);
  assert.equal(get('photoImportStart').disabled, true);
  get('photoImportReset').listeners.click();
  assert.equal(get('photoImportSite').disabled, false);
  assert.equal(get('photoImportResults').children.length, 0);
});

test('authorization failure stops the batch without touching later photos', async () => {
  const {ui, get} = fixture(); let calls = 0;
  ui.configure({sites:[{id:76, name:'金駿76'}], actor:{role:'admin'}, today:'2026-10-15',
    refresh:async () => {}, api:async () => { calls++; throw new Error('Google Drive 授權失效'); }});
  get('photoImportSite').value = '76'; get('photoImportDate').value = '2026-09-02';
  get('photoImportFiles').listeners.change({target:{files:[{name:'a.png', size:100}, {name:'b.png', size:100}]}});
  await get('photoImportForm').listeners.submit({preventDefault(){}});
  assert.equal(calls, 1);
  assert.match(get('photoImportResults').children[1].textContent, /等待匯入/);
});
