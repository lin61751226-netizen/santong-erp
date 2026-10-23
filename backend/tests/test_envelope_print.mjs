import {readFileSync} from 'node:fs';
import assert from 'node:assert/strict';
import test from 'node:test';
const source=readFileSync(new URL('../app/static/envelope_overprint.js',import.meta.url),'utf8');
const {positionedElements,applySenderLayout,backupMessage,verticalFontSize}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));

const layout={
  paper_width:100,paper_height:200,font_size:12.5,name_font_size:20,small_font_size:10,
  postcode_spacing:6.5,postcode_gap:3,offset_x:0,offset_y:0,
  fields:{
    receiver_postcode:{x:52,y:9.5,width:42,height:7},
    receiver_company:{x:84,y:34,width:12,height:118},
    receiver_address:{x:58,y:34,width:24,height:118},
    receiver_name:{x:30,y:40,width:24,height:112},
    sender_postcode:{x:8,y:178,width:30,height:7},
    sender_address:{x:18,y:32,width:10,height:140},
    sender_name:{x:6,y:32,width:10,height:140},
    sender_phone:{x:6,y:32,width:10,height:140},
  },
};
const draft={
  receiver_postcode:'10603',receiver_company:'齊裕營造股份有限公司',
  receiver_address:'臺北市大安區金山南路2段55號',receiver_name:'王大明收',
  sender_postcode:'33059',sender_address:'桃園市桃園區永安路385巷6號8樓之3',
  sender_name:'三通工程行 林金生緘  電話：0978-909-078',sender_phone:'',
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
  assert.equal(address.vertical,true);
  assert.equal(address.oneColumn,true);
  assert.equal(address.text,'桃園市桃園區永安路385巷6號8樓之3');
  assert.equal(address.x,18);
  assert.equal(address.height,140);
  assert.equal(name.oneColumn,true);
  assert.equal(name.vertical,true);
  assert.equal(name.x,6);
  assert.match(name.text,/三通工程行 林金生緘 {2}電話：0978-909-078/);
  assert.equal(name.fontSize,10);
  assert.equal(verticalFontSize(name.text,30,10),5);
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
  assert.equal(result.fields.sender_name.y,32);
  assert.equal(result.fields.sender_address.x,18);
  assert.equal(result.fields.sender_phone.y,32);
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
  assert.match(css,/\.sheet-field\.vertical\.one-column/);
  assert.match(css,/text-orientation:\s*upright/);
  assert.match(source,/oneColumn: true/);
  assert.match(css,/body:not\(\.print-armed\)/);
});

test('HTML pre-fills the confirmed Santong sender and ships versioned assets',()=>{
  const html=readFileSync(new URL('../app/templates/envelope_overprint.html',import.meta.url),'utf8');
  assert.match(html,/value="33059"/);
  assert.match(html,/value="三通工程行 林金生"/);
  assert.match(html,/value="0978-909-078"/);
  assert.match(html,/value="桃園市桃園區永安路385巷6號8樓之3"/);
  assert.match(html,/href="\/static\/envelope_overprint\.css\?v=sender-columns-20261023"/);
  assert.match(html,/src="\/static\/envelope_overprint\.js\?v=sender-columns-20261023"/);
  assert.match(html,/套用寄件地址／姓名電話各一行（尚未保存）/);
});

test('backup messages stay honest',()=>{
  assert.match(backupMessage({status:'saved'}),/成功/);
  assert.match(backupMessage({status:'failed',message:'x'}),/雲端備份失敗/);
});

test('test mode draws stamp guide and postcode boxes',()=>{
  assert.match(source,/guide-stamp/);
  assert.match(source,/postcodeBoxesHtml/);
});
