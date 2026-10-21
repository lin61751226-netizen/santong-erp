import assert from 'node:assert/strict';
import test from 'node:test';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';

function fixture(api) {
  const items=new Map();
  const elem=()=>({value:'',checked:false,children:[],listeners:{},disabled:false,
    addEventListener(k,fn){this.listeners[k]=fn},replaceChildren(...v){this.children=v},append(v){this.children.push(v)}});
  const get=id=>{if(!items.has(id))items.set(id,elem());return items.get(id)};
  const context={document:{getElementById:get,createElement:elem},window:{addEventListener(){}},confirm:()=>true};
  runInNewContext(readFileSync(new URL('../app/static/photo_classification.js',import.meta.url),'utf8'),context);
  context.window.PhotoTextClassification.configure({actor:{role:'admin'},today:'2026-10-21',api,refresh:async()=>{}});
  return {get,read:()=>get('photoTextForm').listeners.submit({preventDefault(){}})};
}
const plan=count=>({count,legacy_count:count,digest:String(count),summary:[{site_name:'善捷47',work_date:'2026-10-19',count}],photos:[{file_name:'<img>',caption:'47-2026/10/19',legacy:true}]});

test('caption preview, input invalidation, explicit legacy consent and safe text rendering',async()=>{
  const calls=[];const {get,read}=fixture(async(url,o)=>{calls.push(JSON.parse(o.body));return plan(2)});
  get('photoTextFrom').value='2026-10-16';get('photoTextLegacy').checked=true;
  await read();assert.equal(calls[0].allow_legacy,true);assert.equal(calls[0].date_from,'2026-10-16');
  assert.equal(get('photoTextRows').children[0].children[0].textContent,'<img>');
  assert.equal(get('photoTextApply').disabled,false);
  get('photoTextFrom').listeners.input();assert.equal(get('photoTextApply').disabled,true);
});
test('sequential batches re-preview digest and finish at zero',async()=>{
  let left=22;const applied=[];
  const {get,read}=fixture(async(url,o)=>{
    if(url.endsWith('preview'))return plan(left);
    applied.push(JSON.parse(o.body).digest);const saved=Math.min(left,20);left-=saved;
    return {saved,backup_status:'saved',results:[]};
  });
  await read();await get('photoTextApply').listeners.click();
  assert.deepEqual(applied,['22','2']);assert.match(get('photoTextStatus').textContent,/已歸檔 22/);
  assert.equal(get('photoTextApply').disabled,true);
});
test('partial failure stops further moves and backup can be retried without moving files',async()=>{
  let moves=0;const {get,read}=fixture(async(url)=>{
    if(url.endsWith('preview'))return plan(22);
    if(url.endsWith('backup'))return {status:'saved'};
    moves++;return {saved:2,backup_status:'failed',results:[{status:'failed',detail:'未完成'}]};
  });
  await read();await get('photoTextApply').listeners.click();
  assert.equal(moves,1);assert.match(get('photoTextStatus').textContent,/備份未確認/);
  await get('photoTextBackup').listeners.click();assert.match(get('photoTextStatus').textContent,/備份成功/);
  assert.equal(moves,1);
});
