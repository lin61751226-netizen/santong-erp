import assert from 'node:assert/strict';
import {existsSync,readFileSync} from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {test} from 'node:test';

const base=path.join(path.dirname(fileURLToPath(import.meta.url)),'..','app','static');
const chrome='C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
let chromium;
try { ({chromium}=await import('playwright')); } catch {}
const fieldIds=['signSlipId','signSlipNo','signSlipDate','signSlipCustomer','signSlipSiteCode','signSlipLocation',
  'signSlipV25','signSlipV30','signSlipV45','signSlipTruck','signSlipNormal','signSlipOvertime','signSlipTotal',
  'signSlipStart','signSlipEnd','signSlipAmount','signSlipDrivers','signSlipSignature','signSlipContent','signSlipSource','signSlipNotes'];
const html=`<!doctype html><meta charset="utf-8"><form id="signSlipForm">${fieldIds.map(id=>`<input id="${id}">`).join('')}</form>
  <div id="signSlipVehicleHoursSection" hidden></div>
  ${['signSlip','costGrid'].map(id=>`<p id="${id}DraftStatus"></p><p id="${id}SaveStatus"></p>
    <button id="${id}DraftRestore" hidden></button><button id="${id}DraftDiscard" hidden></button>`).join('')}
  <select id="costGridDocument"><option value="7">Pricing</option></select><input id="costGridMonth" value="2026-11">
  <p id="costGridStatus"></p><div id="costGridTabs"></div><div id="costGridSheet"></div>
  <button id="costGridPreview"></button><input id="costGridConfirm" type="checkbox"><button id="costGridSave"></button>
  <div id="costGridReview" hidden></div><div id="costGridSlipAmountReason" hidden><input id="costGridSlipReasonInput"></div>
  <div id="costGridAdjustment" hidden><h3 id="costGridAdjustmentTitle"></h3><p id="costGridAdjustmentFormula"></p>
    <input id="costGridAdjustmentAmount"><input id="costGridAdjustmentReason"><button id="costGridAdjustmentSave"></button>
    <div id="costGridAdjustmentReview" hidden></div></div>
  <script>
    window.signSlipCache=[]; window.details=[];
    window.signSlipVehicleDetails=()=>window.details;
    window.resetSignSlipForm=()=>document.getElementById('signSlipForm').reset();
    window.renderSignSlipVehicleHours=data=>{window.details=data;};
    window.openNamedPanel=()=>{}; window.openPricingGridShortcut=()=>{};
    window.formatTW=v=>v; window.escapeHtml=v=>String(v); window.escapeAttr=window.escapeHtml;
    window.api=async(url,options={})=>{const r=await fetch(url,{...options,headers:{'Content-Type':'application/json'}});
      const data=await r.json(); if(!r.ok)throw new Error(data.detail);return data;};
  </script><script src="/cost_grid.js"></script><script src="/editing_recovery.js"></script>`;

async function fixture(run) {
  const browser=await chromium.launch({headless:true,executablePath:chrome});
  const drafts={}; let actor='A',sha='a'.repeat(64),writes=0,businessWrites=0;
  try {
    const page=await browser.newPage();
    page.on('pageerror',error=>process.stderr.write(`recovery browser: ${error.message}\n`));
    page.on('dialog',dialog=>dialog.accept());
    await page.route('http://draft.test/**',async route=>{
      const request=route.request(),url=new URL(request.url());
      let result={},status=200;
      if(url.pathname==='/') return route.fulfill({body:html,contentType:'text/html'});
      if(url.pathname.endsWith('.js')) return route.fulfill({body:readFileSync(path.join(base,url.pathname.slice(1)),'utf8'),contentType:'application/javascript; charset=utf-8'});
      if(url.pathname.startsWith('/api/editing-drafts/')) {
        const kind=url.pathname.split('/').pop(),key=`${actor}:${kind}`;
        result=drafts[key] || {revision:0,active:false,payload:{}};
        if(request.method()==='PUT') {
          const data=request.postDataJSON();
          if(data.expected_revision!==result.revision) {status=409;result={detail:'另一分頁已更新草稿'};}
          else {result={revision:result.revision+1,active:data.active!==false,payload:data.payload || {},saved_at:'2026-11-13T10:00:00',backup_status:'pending'};drafts[key]=result;writes++;}
        }
        if(kind==='cloud') result={backup_status:'failed'};
      } else if(url.pathname==='/api/cost-hour-imports/grid') {
        if(request.method()!=='GET') businessWrites++;
        result={document_id:7,document_name:'Pricing',year:2026,month:11,sha256:sha,tax_rate:0,
          sheets:[{label:'53',worksheet_name:'11511-53',rates:{},rows:[{day:'2026-11-01',normal_hours:0,overtime_hours:0,support_hours:0,total:0,adjustment_revision:0}]}]};
      }
      await route.fulfill({json:result,status});
    });
    await page.goto('http://draft.test/');
    await page.evaluate(()=>DraftRecovery.configure({employee_code:'A'}));
    await run({page,drafts,get writes(){return writes;},get businessWrites(){return businessWrites;},
      actor:value=>{actor=value;},sha:value=>{sha=value;}});
  } finally {await browser.close();}
}

test('sign slip autosaves, reload offers explicit recovery, and another account cannot see it',
  {skip:!chromium || !existsSync(chrome)},async()=>fixture(async context=>{
    const {page}=context;
    await page.locator('#signSlipNo').fill('DRAFT-ONLY');
    await page.locator('#signSlipContent').fill('work not posted');
    await page.waitForFunction(()=>document.getElementById('signSlipDraftStatus').textContent.includes('草稿已存系統'),null,{timeout:5000}).catch(async error=>{throw new Error(`${error.message}: ${await page.locator('#signSlipDraftStatus').innerText()}`);});
    assert.equal(context.writes,1);
    await page.reload(); await page.evaluate(()=>DraftRecovery.configure({employee_code:'A'}));
    assert.equal(await page.locator('#signSlipNo').inputValue(),'');
    assert.equal(await page.locator('#signSlipDraftRestore').isVisible(),true);
    await page.evaluate(()=>DraftRecovery.restore('sign-slip'));
    assert.equal(await page.locator('#signSlipNo').inputValue(),'DRAFT-ONLY');
    await page.evaluate(()=>DraftRecovery.pause());
    assert.equal(await page.locator('#signSlipNo').inputValue(),'');
    await page.evaluate(()=>DraftRecovery.configure({employee_code:'A'}));
    assert.equal(await page.locator('#signSlipDraftRestore').isVisible(),true);
    await page.evaluate(()=>DraftRecovery.restore('sign-slip'));
    assert.equal(await page.locator('#signSlipNo').inputValue(),'DRAFT-ONLY');
    context.actor('B'); await page.evaluate(()=>DraftRecovery.configure({employee_code:'B'}));
    assert.equal(await page.locator('#signSlipNo').inputValue(),'');
    assert.equal(await page.locator('#signSlipDraftRestore').isVisible(),false);
    assert.equal(context.businessWrites,0);
  }));

test('grid recovery checks SHA, never restores approval, and backup failure stays honest',
  {skip:!chromium || !existsSync(chrome)},async()=>fixture(async context=>{
    const {page}=context;
    await page.evaluate(()=>loadCostGrid());
    await page.locator('[data-field="normal_hours"]').fill('8');
    await page.waitForFunction(()=>document.getElementById('costGridDraftStatus').textContent.includes('草稿已存系統'));
    await page.reload(); await page.evaluate(()=>DraftRecovery.configure({employee_code:'A'}));
    await page.evaluate(()=>DraftRecovery.restore('cost-grid'));
    assert.equal(await page.locator('[data-field="normal_hours"]').inputValue(),'8');
    assert.equal(await page.locator('#costGridSave').isDisabled(),true);
    assert.equal(await page.locator('#costGridReview').isVisible(),false);
    await page.evaluate(()=>DraftRecovery.backup('cost-grid'));
    assert.match(await page.locator('#costGridDraftStatus').innerText(),/雲端備份未完成/);
    context.sha('b'.repeat(64));
    await page.reload(); await page.evaluate(()=>DraftRecovery.configure({employee_code:'A'}));
    await page.evaluate(()=>DraftRecovery.restore('cost-grid'));
    assert.match(await page.locator('#costGridDraftStatus').innerText(),/來源版本已變/);
    assert.equal(await page.locator('[data-field="normal_hours"]').inputValue(),'0');
    assert.equal(context.businessWrites,0);
  }));

test('a stale tab cannot overwrite another draft and discarding preserves history on server',
  {skip:!chromium || !existsSync(chrome)},async()=>fixture(async context=>{
    context.drafts['A:sign-slip']={revision:1,active:true,payload:{fields:{signSlipNo:'OTHER'}}};
    await context.page.locator('#signSlipNo').fill('STALE');
    await context.page.waitForFunction(()=>document.getElementById('signSlipDraftStatus').textContent.includes('另一分頁'));
    assert.equal(context.writes,0);
    assert.equal(context.drafts['A:sign-slip'].payload.fields.signSlipNo,'OTHER');
    assert.equal(await context.page.locator('#signSlipNo').inputValue(),'STALE');
  }));
