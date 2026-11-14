import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {chromium} from 'playwright';

test('isolated ledger creates, modifies and reloads without production requests',async()=>{
  const template=readFileSync('backend/app/templates/index.html','utf8');
  const script=readFileSync('backend/app/static/document_data.js','utf8');
  const fields=[['entry_date','date'],['entry_type','select'],['category','text'],['summary','text'],['vendor_name','text'],['income_amount','number'],['expense_amount','number'],['project_name','text']].map(([key,type])=>({key,type,label:key}));
  let rows=[],writes=0;
  const browser=await chromium.launch({headless:true,executablePath:'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'});
  try {
    const page=await browser.newPage();
    const setup=`window.escapeHtml=v=>String(v??'').replaceAll('&','&amp;').replaceAll('<','&lt;');window.escapeAttr=escapeHtml;window.todayString=()=> '2026-11-14';window.formatTW=v=>v;window.api=async(url,o={})=>{const r=await fetch(url,{...o,headers:{'Content-Type':'application/json'}});return r.json()};document.querySelector('#loginOverlay').remove();document.querySelector('#dataDocument').innerHTML='<option value="999">TEST ONLY</option>';document.querySelector('#dataKind').value='finance';document.querySelector('#dataMonth').value='2026-11';`;
    await page.route('**/*',async route=>{
      const url=new URL(route.request().url());
      assert.equal(url.hostname,'localhost','No production or external network is permitted');
      if(url.pathname==='/') return route.fulfill({contentType:'text/html',body:template.replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi,'').replace(/<link\b[^>]*>/gi,'')+`<script>${setup}</script><script>${script}</script><script>loadDocumentData(false,0)</script>`});
      if(url.pathname.endsWith('/save')) {
        const p=JSON.parse(route.request().postData()); writes++;
        if(p.record_id) rows=rows.map(r=>r.id===p.record_id?{...r,revision:r.revision+1,values:p.values}:r);
        else rows.push({id:1,revision:1,values:p.values});
        return route.fulfill({json:{record_id:1,backup:{status:'saved'}}});
      }
      return route.fulfill({json:{kind:'finance',document:{name:'TEST ONLY'},fields,rows,sheets:[],history:[],exports:[],offset:0,total:rows.length,dataset_total:rows.length}});
    });
    await page.goto('http://localhost/');
    await page.locator('#dataEditorSection').evaluate(x=>{x.hidden=false});
    await page.getByText('已存 0 筆',{exact:false}).waitFor();
    await page.evaluate(()=>editDocumentData());
    await page.locator('#edit-category').fill('測試非正式');
    await page.locator('#edit-summary').fill('TEST ONLY - not accounting');
    await page.locator('#edit-expense_amount').fill('123');
    await page.locator('#dataEditForm button[type="submit"]').click();
    await page.getByText('資料庫已保存 #1',{exact:false}).waitFor();
    await page.reload();
    await page.locator('#dataEditorSection').evaluate(x=>{x.hidden=false});
    await page.getByText('已存 1 筆',{exact:false}).waitFor();
    assert.equal(rows[0].values.expense_amount,123);
    await page.evaluate(()=>editDocumentData(1));
    await page.locator('#edit-expense_amount').fill('456');
    await page.locator('#dataEditForm button[type="submit"]').click();
    await page.getByText('資料庫已保存 #1',{exact:false}).waitFor();
    await page.reload();
    await page.locator('#dataEditorSection').evaluate(x=>{x.hidden=false});
    await page.getByText('已存 1 筆',{exact:false}).waitFor();
    assert.equal(rows.length,1); assert.equal(rows[0].values.expense_amount,456); assert.equal(writes,2);
  } finally {await browser.close();}
});
