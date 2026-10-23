import {readFileSync} from 'node:fs';
import assert from 'node:assert/strict';
import test from 'node:test';
const source=readFileSync(new URL('../app/static/invoice_overprint.js',import.meta.url),'utf8');
const {taipeiDate,positionedFields,backupMessage,paperGeometry,applySampleLayout}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));

test('Taipei date crosses UTC date correctly',()=>{
  assert.equal(taipeiDate(new Date('2026-10-22T17:00:00Z')),'2026-10-23');
});
test('all positions receive global offset, details receive row pitch',()=>{
  const keys=['buyer','tax_id','year','month','day','subtotal','tax','total','uppercase','note','description','quantity','unit','unit_price','amount'];
  const layout={offset_x:1,offset_y:-2,row_pitch:6,fields:Object.fromEntries(keys.map(key=>[key,{x:27,y:22,width:50}]))};
  const draft={buyer:'<script>x</script>',tax_id:'12345678',invoice_date:'2026-10-23',note:'',items:[{description:'a',quantity:'1',unit:'台',unit_price:'1'},{description:'b',quantity:'2',unit:'台',unit_price:'1'}]};
  const result=positionedFields(layout,draft,{roc_year:115,subtotal:3,tax:0,total:3,uppercase:'參元整',amounts:[1,2]});
  assert.ok(result.every(x=>x.x===28));assert.equal(result.filter(x=>x.key==='description')[1].y,26);
  assert.equal(result.find(x=>x.key==='buyer').text,'<script>x</script>');
  assert.equal(result.find(x=>x.key==='total').numeric,true);
});
test('backup failure never says cloud saved',()=>{
  assert.match(backupMessage({status:'saved'}),/成功/);
  assert.match(backupMessage({status:'skipped_regression'}),/尚未成功/);
});
test('print is content-only at exact physical size and guarded',()=>{
  const css=readFileSync(new URL('../app/static/invoice_overprint.css',import.meta.url),'utf8');
  assert.match(css,/@page\s*\{\s*size:195mm 103mm;\s*margin:0/);
  assert.match(css,/body:not\(\.print-armed\)/);
  assert.match(source,/element\.textContent=field\.text/);
  assert.doesNotMatch(source,/innerHTML|document\.write/);
  assert.match(source,/window\.addEventListener\('afterprint'/);
  assert.match(source,/sequence!==state\.sequence/);
});

test('paper dimensions drive CSS page size with safe legacy fallback',()=>{
  assert.deepEqual(paperGeometry({}),{width:190,height:105,pageRule:'@page { size:190mm 105mm; margin:0; }'});
  assert.deepEqual(paperGeometry({paper_width:195,paper_height:103}),{width:195,height:103,pageRule:'@page { size:195mm 103mm; margin:0; }'});
  for(const value of [NaN,Infinity,0,79,298,'195; color:red'])assert.throws(()=>paperGeometry({paper_width:value,paper_height:103}));
  assert.match(source,/sheet\.width\/paper\.width/);
  assert.match(source,/field\.y\+rect\.height\/scale>paper\.height/);
});

test('sample changes only paper and buyer/tax coordinates, not other calibration',()=>{
  const original={paper_width:190,paper_height:105,font_size:12,offset_x:1,offset_y:-2,fields:{buyer:{x:27,y:22,width:70},tax_id:{x:30,y:12,width:65},note:{x:12,y:94,width:165}}};
  const sample={paper_width:195,paper_height:103,fields:{buyer:{x:35,y:13.5,width:70},tax_id:{x:35,y:20,width:65}}};
  const result=applySampleLayout(original,sample);
  assert.equal(result.paper_height,103);assert.equal(result.fields.buyer.y,13.5);
  assert.equal(result.font_size,12);assert.equal(result.offset_x,1);
  assert.deepEqual(result.fields.note,original.fields.note);
  assert.equal(original.fields.buyer.y,22);
});

test('updated layout loads versioned CSS and module rather than cached old assets',()=>{
  const html=readFileSync(new URL('../app/templates/invoice_overprint.html',import.meta.url),'utf8');
  assert.match(html,/href="\/static\/invoice_overprint\.css\?v=195x103-20261023"/);
  assert.match(html,/src="\/static\/invoice_overprint\.js\?v=195x103-20261023"/);
});
