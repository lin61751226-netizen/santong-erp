import {readFileSync} from 'node:fs';
import assert from 'node:assert/strict';
import test from 'node:test';
const source=readFileSync(new URL('../app/static/invoice_overprint.js',import.meta.url),'utf8');
const {taipeiDate,positionedFields,backupMessage,paperGeometry,applySampleLayout,
       printAllowed,normalizeLayout,moveFieldWithinPaper,esc}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));

const FIELD_ORDER=['buyer','tax_id','address','year','month','day','description','quantity',
  'unit_price','amount','subtotal','tax','total','uppercase','note'];

function layout(overrides={}) {
  const fields=Object.fromEntries(FIELD_ORDER.map(key=>[key,{x:27,y:22,width:50,height: key==='note'?5:0}]));
  return {offset_x:1,offset_y:-2,row_pitch:6,tax_id_spacing:6,paper_width:195,paper_height:103,
          feed_mode:'portrait_cw',font_size:11,fields,...overrides};
}
const draft={buyer:'<script>x</script>',tax_id:'12345678',address:'桃園市某路1號',
  invoice_date:'2026-10-23',note:'',
  items:[{description:'a',quantity:'1',unit_price:'1'},{description:'b',quantity:'2',unit_price:'1'}]};
const calc={roc_year:115,subtotal:3,tax:0,total:3,
  uppercase:'零億零仟零佰零拾零萬零仟零佰零拾參元',
  uppercase_digits:[...'零零零零零零零零參'],amounts:[1,2]};

test('Taipei date crosses UTC date correctly',()=>{
  assert.equal(taipeiDate(new Date('2026-10-22T17:00:00Z')),'2026-10-23');
});

test('all positions receive global offset, detail rows receive row pitch; no unit column',()=>{
  const result=positionedFields(layout(),draft,calc);
  const keys=new Set(result.map(x=>x.key));
  assert.ok(keys.has('address') && !keys.has('unit'));
  assert.equal(result.find(x=>x.key==='buyer').x,28);
  assert.equal(result.find(x=>x.key==='address').x,28);
  const descriptions=result.filter(x=>x.key==='description');
  assert.equal(descriptions[0].y,20);
  assert.equal(descriptions[1].y,26);
  assert.equal(result.find(x=>x.key==='total').numeric,true);
  // eight tax-id chars at 6mm spacing, first at x+offset
  const tax=result.filter(x=>x.key==='tax_id').map(x=>x.x);
  assert.deepEqual(tax,[28,34,40,46,52,58,64,70]);
  // note carries its multi-line height
  const note=result.find(x=>x.key==='note');
  assert.equal(note,undefined); // empty note is not rendered
  assert.equal(result.find(x=>x.key==='uppercase').text,'零零零零零零零零參');
});

test('note text is carried with height when present',()=>{
  const result=positionedFields(layout(),{...draft,note:'測試備註'},calc);
  const note=result.find(x=>x.key==='note');
  assert.equal(note.text,'測試備註');
  assert.equal(note.height,5);
});

test('HTML escaping is applied at render time',()=>{
  assert.equal(esc('<script>x</script>'),'&lt;script&gt;x&lt;/script&gt;');
  assert.equal(esc('a&b"c'),'a&amp;b&quot;c');
});

test('backup failure never says cloud saved',()=>{
  assert.match(backupMessage({status:'saved'}),/成功/);
  assert.match(backupMessage({status:'skipped_regression'}),/尚未成功/);
  assert.match(backupMessage({status:'failed',message:'boom'}),/雲端備份失敗/);
});

test('print CSS uses portrait 103x195 by default with CSS-driven rotation',()=>{
  const css=readFileSync(new URL('../app/static/invoice_overprint.css',import.meta.url),'utf8');
  assert.match(css,/@page\s*\{\s*size:\s*103mm 195mm;\s*margin:\s*0/);
  assert.match(css,/\.feed-portrait\.feed-cw\s*\{[^}]*rotate\(90deg\) translateY\(-100%\)/);
  assert.match(css,/\.feed-portrait\.feed-ccw\s*\{[^}]*rotate\(-90deg\) translateX\(-100%\)/);
  assert.match(css,/body:not\(\.print-armed\)/);
  assert.match(source,/window\.addEventListener\('afterprint',\s*restorePrint\)/);
  assert.match(source,/if \(state\.printing\) return/);
});

test('paper geometry maps feed modes to physical page and rejects bad input',()=>{
  const cw=paperGeometry({paper_width:195,paper_height:103,feed_mode:'portrait_cw'});
  assert.equal(cw.pageRule,'@page { size:103mm 195mm; margin:0; }');
  assert.equal(cw.pageWidth,103); assert.equal(cw.pageHeight,195);
  assert.equal(cw.portrait,true);
  const ccw=paperGeometry({paper_width:195,paper_height:103,feed_mode:'portrait_ccw'});
  assert.equal(ccw.pageRule,'@page { size:103mm 195mm; margin:0; }');
  const land=paperGeometry({paper_width:195,paper_height:103,feed_mode:'landscape'});
  assert.equal(land.pageRule,'@page { size:195mm 103mm; margin:0; }');
  assert.equal(land.portrait,false);
  for(const value of [NaN,Infinity,0,79,298,301]){
    assert.throws(()=>paperGeometry({paper_width:value,paper_height:103,feed_mode:'portrait_cw'}));
  }
});

test('sample layout resets all fields, paper and feed but keeps font/offset',()=>{
  const original=layout({font_size:12,offset_x:1,offset_y:-2});
  const sample=layout({feed_mode:'portrait_cw',tax_id_spacing:6,row_pitch:6.1});
  sample.fields.buyer={x:35,y:15.5,width:70,height:0};
  const result=applySampleLayout(original,sample);
  assert.equal(result.paper_width,195);
  assert.equal(result.feed_mode,'portrait_cw');
  assert.equal(result.fields.buyer.y,15.5);
  assert.equal(result.font_size,12);
  assert.equal(result.offset_x,1);
  assert.equal(result.offset_y,-2);
  // field set is exactly the real form (no unit, has address)
  assert.deepEqual(Object.keys(result.fields).sort(),FIELD_ORDER.slice().sort());
});

test('legacy layouts are normalized read-only (unit hidden, address added, feed set)',()=>{
  const sample=layout();
  globalThis.window=globalThis.window||{};
  const legacy={paper_width:190,paper_height:105,font_size:11,offset_x:0,offset_y:0,
    fields:Object.fromEntries(FIELD_ORDER.filter(k=>k!=='address').map(k=>[k,{x:1,y:2,width:3,height:0}]))};
  legacy.fields.unit={x:1,y:2,width:3,height:0};
  const snapshot=JSON.stringify(legacy);
  // window.__SAMPLE_LAYOUT__ is consulted by normalizeLayout

  globalThis.window.__SAMPLE_LAYOUT__=sample;
  const result=normalizeLayout(legacy);
  assert.equal(result.feed_mode,'portrait_cw');
  assert.ok(result.fields.address);
  assert.ok(!result.fields.unit);
  assert.equal(result.fields.buyer.x,1); // stored values preserved
  assert.equal(JSON.stringify(legacy),snapshot); // never mutated
});

test('photo drag assets keep the original photo out of print',()=>{
  const html=readFileSync(new URL('../app/templates/invoice_overprint.html',import.meta.url),'utf8');
  const css=readFileSync(new URL('../app/static/invoice_overprint.css',import.meta.url),'utf8');
  assert.match(html,/href="\/static\/invoice_overprint\.css\?v=photo-amount-slots-20261023"/);
  assert.match(html,/src="\/static\/invoice_overprint\.js\?v=photo-amount-slots-20261023"/);
  assert.match(html,/src="\/api\/invoice-print\/reference-image"/);
  assert.match(html,/id="showCoordinates" checked/);
  assert.match(html,/id="showPhoto" checked/);
  assert.match(source,/function renderCoordinates\(paper\)/);
  assert.match(source,/Number\(field\.x\) \+ offsetX/);
  assert.match(css,/\.invoice-reference-photo \{ display: none !important; \}/);
  assert.match(source,/sheet\.setPointerCapture\(event\.pointerId\)/);
  assert.match(css,/grid-template-columns: repeat\(9, minmax\(0, 1fr\)\)/);
  assert.match(source,/result\.uppercase_digits/);
  assert.match(source,/setUppercaseReference/);
});

test('per-field type size and drag stays inside paper',()=>{
  const local=layout();
  local.fields.buyer.font_size=19;
  assert.equal(positionedFields(local,draft,calc)[0].font_size,19);
  assert.deepEqual(moveFieldWithinPaper({x:35,y:15,width:70,height:0},-40,200,{width:195,height:103}),{x:0,y:101.5});
  assert.deepEqual(moveFieldWithinPaper({x:35,y:15,width:70,height:0},10.26,2.75,{width:195,height:103}),{x:45.3,y:17.8});
});

test('printAllowed blocks formal print unless paper and validation pass; test print always allowed',()=>{
  assert.equal(printAllowed(true,true,false,false),true);
  assert.equal(printAllowed(true,false,false,false),true);
  assert.equal(printAllowed(false,true,false,false),true);
  assert.equal(printAllowed(false,false,false,false),false);
  assert.equal(printAllowed(false,true,true,false),false);
  assert.equal(printAllowed(false,true,false,true),false);
});

test('print dialog exit paths restore controls and guard duplicate clicks',()=>{
  assert.match(source,/window\.addEventListener\('afterprint', onAfter\)/);
  assert.match(source,/window\.addEventListener\('focus', onFocus\)/);
  assert.match(source,/printMedia\.addEventListener\('change', onChange\)/);
  assert.match(source,/if \(state\.printing\) return/);
  assert.match(source,/clearTimeout\(timer\)/);
  assert.match(source,/state\.printing = true/);
});
