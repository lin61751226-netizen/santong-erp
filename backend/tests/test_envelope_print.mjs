import {readFileSync} from 'node:fs';
import assert from 'node:assert/strict';
import test from 'node:test';
const source=readFileSync(new URL('../app/static/envelope_overprint.js',import.meta.url),'utf8');
const {positionedElements,applySenderLayout,backupMessage,verticalFontSize,verticalColumnHtml,verticalAdvanceEm,GLYPH_EM}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));

const layout={
  paper_width:100,paper_height:200,font_size:12.5,name_font_size:20,small_font_size:10,
  postcode_spacing:6.5,postcode_gap:3,offset_x:0,offset_y:0,
  fields:{
    receiver_postcode:{x:52,y:9.5,width:42,height:7},
    receiver_company:{x:84,y:34,width:12,height:118},
    receiver_address:{x:58,y:34,width:24,height:118},
    receiver_name:{x:30,y:40,width:24,height:112},
    sender_postcode:{x:8,y:178,width:30,height:7},
    sender_address:{x:18,y:25,width:10,height:150},
    sender_name:{x:6,y:25,width:10,height:150},
    sender_phone:{x:6,y:25,width:10,height:150},
  },
};
const draft={
  receiver_postcode:'10603',receiver_company:'齊裕營造股份有限公司',
  receiver_address:'臺北市大安區金山南路2段55號',receiver_name:'王大明收',
  sender_postcode:'33059',sender_address:'桃園市桃園區永安路385巷6號8樓之3',
  sender_name:'三通工程行 林金生緘 電話：0978-909-078',sender_phone:'',
};
const lines=draft;

test('receiver postcode is five chars with 3+2 gap',()=>{
  const out=positionedElements(layout,draft,lines);
  const chars=out.filter(x=>x.key==='receiver_postcode');
  assert.equal(chars.length,5);
  assert.deepEqual(chars.map(c=>c.text),['1','0','6','0','3']);
  // spacing 6.5, gap 3 after the third box
  const xs=chars.map(c=>c.x);
  assert.ok(Math.abs(xs[0]-52)<1e-9);
  assert.ok(Math.abs(xs[1]-58.5)<1e-9);
  assert.ok(Math.abs(xs[2]-65)<1e-9);
  assert.ok(Math.abs(xs[3]-(71.5+3))<1e-9);
});

test('address and name are vertical fields with correct font sizes',()=>{
  const out=positionedElements(layout,draft,lines);
  const name=out.find(x=>x.key==='receiver_name');
  assert.equal(name.vertical,true);
  assert.equal(name.fontSize,20);
  assert.equal(name.width,24);
  assert.equal(name.height,112);
  const addr=out.find(x=>x.key==='receiver_address');
  assert.equal(addr.vertical,true);
  assert.equal(addr.fontSize,12.5);
  const sender=out.find(x=>x.key==='sender_name');
  assert.equal(sender.fontSize,10);
});

test('sender address and name+phone are one vertical column each',()=>{
  const out=positionedElements(layout,draft,lines);
  const address=out.find(x=>x.key==='sender_address');
  const name=out.find(x=>x.key==='sender_name');
  assert.equal(out.find(x=>x.key==='sender_phone'),undefined);
  assert.equal(address.vertical,false);
  assert.equal(address.oneColumn,true);
  assert.equal(address.sidewaysPhone,false);
  assert.equal(address.text,'桃園市桃園區永安路385巷6號8樓之3');
  assert.equal(address.x,18);
  assert.equal(address.y,25);
  assert.equal(address.height,150);
  assert.equal(name.oneColumn,true);
  assert.equal(name.sidewaysPhone,true);
  assert.equal(name.vertical,false);
  assert.equal(name.x,6);
  assert.equal(name.y,25);
  assert.equal(name.height,150);
  assert.equal(name.text,'三通工程行 林金生緘 電話：0978-909-078');
  assert.equal(name.fontSize,10);
  assert.equal(verticalFontSize(name.text,150,10,{sidewaysPhone:true}),10);
  assert.equal(verticalFontSize(address.text,50,10),6.4);
  const addressHtml=verticalColumnHtml(address.text);
  assert.equal((addressHtml.match(/class="glyph"/g)||[]).length,19);
  assert.match(addressHtml,/<span class="glyph">路<\/span><span class="glyph">3<\/span><span class="glyph">8<\/span><span class="glyph">5<\/span><span class="glyph">巷<\/span><span class="glyph">6<\/span><span class="glyph">號<\/span><span class="glyph">8<\/span><span class="glyph">樓<\/span><span class="glyph">之<\/span><span class="glyph">3<\/span>$/);
  assert.doesNotMatch(addressHtml,/phone-run|writing-mode|sideways/);
  const nameHtml=verticalColumnHtml(name.text,{sidewaysPhone:true});
  assert.match(nameHtml,/<span class="glyph">：<\/span><span class="phone-run"[^>]*>[\s\S]*0978-909-078<\/span><\/span>$/);
  assert.equal((nameHtml.match(/class="glyph"/g)||[]).length,14);
  assert.equal(verticalAdvanceEm(address.text),19*GLYPH_EM);
  assert.ok(verticalAdvanceEm(address.text)*0.3528*10<address.height);
  const senderCode=out.filter(x=>x.key==='sender_postcode').map(c=>c.text);
  assert.deepEqual(senderCode,['3','3','0','5','9']);
  assert.equal(out.find(x=>x.key==='sender_postcode').y,178);
});

test('sender-only layout update retains recipient positioning',()=>{
  const older=structuredClone(layout);
  older.fields.sender_name.y=120;
  older.fields.sender_phone.y=146;
  older.fields.sender_address.x=8;
  const result=applySenderLayout(older,layout);
  assert.equal(result.fields.sender_name.y,25);
  assert.equal(result.fields.sender_name.height,150);
  assert.equal(result.fields.sender_address.x,18);
  assert.equal(result.fields.sender_phone.y,25);
  assert.deepEqual(result.fields.receiver_address,older.fields.receiver_address);
  assert.equal(older.fields.sender_name.y,120);
});

test('global offsets apply to every element',()=>{
  const shifted=positionedElements({...layout,offset_x:1,offset_y:-1},draft,lines);
  const first=shifted.find(x=>x.key==='receiver_postcode');
  assert.ok(Math.abs(first.x-53)<1e-9);
  assert.ok(Math.abs(first.y-8.5)<1e-9);
});

test('CSS page is 100x200 portrait with vertical writing',()=>{
  const css=readFileSync(new URL('../app/static/envelope_overprint.css',import.meta.url),'utf8');
  assert.match(css,/@page\s*\{\s*size:\s*100mm 200mm;\s*margin:\s*0/);
  assert.match(css,/writing-mode:\s*vertical-rl/);
  assert.match(css,/\.sheet-field\.stack-column \.glyph/);
  assert.match(css,/height:\s*1\.15em/);
  assert.match(css,/flex:\s*0 0 1\.15em/);
  assert.match(css,/rotate\(-90deg\)/);
  assert.doesNotMatch(css,/text-combine-upright:\s*all/);
  assert.match(css,/text-orientation:\s*upright/);
  assert.match(css,/@media print[\s\S]*width:\s*var\(--page-width\)/);
  assert.doesNotMatch(css,/@media print[\s\S]*stack-column/);
  const printCss=css.slice(css.lastIndexOf('@media print'));
  assert.match(printCss,/overflow:\s*hidden !important/);
  assert.match(printCss,/page-break-after:\s*avoid/);
  assert.match(printCss,/\.form-inline,/);
  assert.match(printCss,/\.button-row,/);
  assert.match(printCss,/height:\s*var\(--page-height\) !important/);
  assert.match(source,/\/api\/envelope-print\/pdf/);
  assert.match(source,/oneColumn: true/);
  assert.match(source,/verticalColumnHtml\(el\.text/);
  assert.match(css,/body:not\(\.print-armed\)/);
});

test('HTML pre-fills the confirmed Santong sender and ships versioned assets',()=>{
  const html=readFileSync(new URL('../app/templates/envelope_overprint.html',import.meta.url),'utf8');
  assert.match(html,/value="33059"/);
  assert.match(html,/value="三通工程行 林金生"/);
  assert.match(html,/value="0978-909-078"/);
  assert.match(html,/value="桃園市桃園區永安路385巷6號8樓之3"/);
  assert.match(html,/href="\/static\/envelope_overprint\.css\?v=envelope-print-pdf-20261023"/);
  assert.match(html,/src="\/static\/envelope_overprint\.js\?v=envelope-print-pdf-20261023"/);
  assert.match(html,/id="downloadPdfBtn"/);
  assert.match(html,/id="downloadCalibrationPdfBtn"/);
  assert.match(html,/下載信封 PDF（單頁 100×200mm，不含按鈕）/);
  assert.match(html,/套用寄件兩欄完整直書（尚未保存）/);
});

test('backup messages stay honest',()=>{
  assert.match(backupMessage({status:'saved'}),/成功/);
  assert.match(backupMessage({status:'failed',message:'x'}),/雲端備份失敗/);
});

test('test mode draws stamp guide and postcode boxes',()=>{
  assert.match(source,/guide-stamp/);
  assert.match(source,/postcodeBoxesHtml/);
});
