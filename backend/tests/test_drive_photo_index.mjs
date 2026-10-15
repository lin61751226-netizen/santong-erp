import assert from 'node:assert/strict';
import test from 'node:test';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';

function fixture() {
  const items = new Map();
  const elem = () => ({value:'',children:[],listeners:{},disabled:false,
    addEventListener(k,fn){this.listeners[k]=fn}, replaceChildren(...v){this.children=v},
    append(v){this.children.push(v)},add(v){this.children.push(v)}});
  const get=id=>{if(!items.has(id))items.set(id,elem());return items.get(id)};
  const context={document:{getElementById:get,createElement:elem},window:{addEventListener(){}},
    confirm:()=>true,Option:class{constructor(text,value){this.text=text;this.value=value}}};
  runInNewContext(readFileSync(new URL('../app/static/drive_photo_index.js',import.meta.url),'utf8'),context);
  return {get,ui:context.window.DrivePhotoIndex};
}
test('preview is separate from apply and edits invalidate the reviewed digest',async()=>{
  const {get,ui}=fixture();const calls=[];
  const result={digest:'a'.repeat(64),site_name:'善捷47',counts:{new:2,duplicate:1,skipped:3,conflict:0},dates:[{work_date:'2026-09-07',count:2}],exception_count:3,exceptions:[{name:'<img>',reason:'日期不完整'}]};
  ui.configure({sites:[{id:4,name:'善捷47'}],actor:{role:'admin'},refresh:async()=>{},api:async(url,options)=>{calls.push({url,data:JSON.parse(options.body)});return url.endsWith('preview')?result:{added:2,backup_status:'saved'}}});
  get('drivePhotoFolder').value='https://drive.google.com/drive/folders/example-folder';
  get('drivePhotoSite').value='4';get('drivePhotoCode').value='47';
  const preview=()=>get('drivePhotoIndexForm').listeners.submit({preventDefault(){}});
  await preview();assert.equal(calls.length,1);assert.equal(get('drivePhotoApply').disabled,false);
  get('drivePhotoCode').listeners.input();assert.equal(get('drivePhotoApply').disabled,true);
  await get('drivePhotoApply').listeners.click();assert.equal(calls.length,1);
  await preview();await get('drivePhotoApply').listeners.click();
  assert.equal(calls[2].data.digest,'a'.repeat(64));assert.match(get('drivePhotoFeedback').textContent,/已新增 2/);
  assert.equal(get('drivePhotoApply').disabled,true);
});
