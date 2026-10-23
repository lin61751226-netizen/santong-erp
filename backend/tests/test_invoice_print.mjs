import {readFileSync} from 'node:fs';
import assert from 'node:assert/strict';
import test from 'node:test';
const source=readFileSync(new URL('../app/static/invoice_overprint.js',import.meta.url),'utf8');
const {taipeiDate,positionedFields,backupMessage}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));

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
  assert.match(css,/@page\s*\{\s*size:190mm 105mm;\s*margin:0/);
  assert.match(css,/body:not\(\.print-armed\)/);
  assert.match(source,/element\.textContent=field\.text/);
  assert.doesNotMatch(source,/innerHTML|document\.write/);
  assert.match(source,/window\.addEventListener\('afterprint'/);
  assert.match(source,/sequence!==state\.sequence/);
});
