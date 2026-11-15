import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync, mkdirSync} from 'node:fs';
import {chromium} from 'playwright';

test('pricing window switches original panels at desktop and mobile widths', async () => {
  const html=readFileSync('backend/app/templates/index.html','utf8');
  const css=readFileSync('backend/app/static/app.css','utf8');
  const navigation=html.slice(html.indexOf('    const legacyDocumentTasks'),html.indexOf('    function api('));
  const browser=await chromium.launch({headless:true,executablePath:'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'});
  mkdirSync('outputs/pricing-window',{recursive:true});
  try {
    const page=await browser.newPage();
    await page.setContent(html.replaceAll('{{ app_name }}','三通工程自動化管理系統（本機測試）').replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi,'').replace(/<link\b[^>]*>/gi,''));
    await page.addStyleTag({content:css});
    await page.addScriptTag({content:`const state={workspaceViews:{}};const managementReports={sourcesLoaded:true};const escapeHtml=v=>String(v).replaceAll('&','&amp;').replaceAll('<','&lt;');const escapeAttr=escapeHtml;function showExcelMode(mode){window.testExcelMode=mode};${navigation}`});
    await page.locator('#loginOverlay').evaluate(x=>x.hidden=true);
    await page.addStyleTag({content:'#loginOverlay{display:none!important}'});
    for(const width of [1440,1100,768,390]) {
      await page.setViewportSize({width,height:950});
      await page.evaluate(()=>setWorkspace('pricing',false));
      assert.equal(await page.locator('#pricingWindowNav button').count(),3);
      for(const index of [0,1,2]) {
        await page.locator(`[data-pricing-view="${index}"]`).click();
        assert.equal(await page.locator('[data-workspace="pricing"]:visible').count(),1);
        assert.equal(await page.locator(`[data-pricing-view="${index}"]`).getAttribute('aria-pressed'),'true');
      }
      const bounds=await page.locator('[data-workspace="pricing"]:visible').boundingBox();
      assert.ok(bounds.width>300);
      assert.ok(bounds.x + bounds.width <= width + 1, 'panel must stay within viewport');
      if(width===1440) {
        const nav=await page.locator('#pricingWindowNav').boundingBox();
        assert.ok(nav.x+nav.width<=bounds.x,'sidebar must not overlap content');
        assert.equal(await page.locator('#workspaceTools').isVisible(),false);
      }
      await page.screenshot({path:`outputs/pricing-window/${width}.png`,fullPage:false});
    }
    await page.evaluate(()=>setWorkspace('today',false));
    assert.equal(await page.locator('#pricingWindowNav').isVisible(),false);
    for(const workspace of ['today','people','dispatch','fleet','journals','finance','files']) {
      await page.evaluate(value=>setWorkspace(value,false),workspace);
      assert.equal(await page.locator('[data-workspace]:visible').count(),1);
      assert.ok(await page.locator('#workWindowNav button').count()>0);
      await page.locator('#workWindowNav button').last().click();
      assert.equal(await page.locator('[data-workspace]:visible').count(),1);
      assert.equal(await page.locator('#workWindowTitle').textContent(),await page.locator('[data-workspace]:visible h2').first().textContent());
      await page.screenshot({path:`outputs/pricing-window/${workspace}-390.png`,fullPage:false});
    }
    await page.setViewportSize({width:1440,height:950});
    await page.evaluate(()=>setWorkspace('finance',false));
    await page.locator('#workWindowNav button').first().click();
    assert.equal(await page.evaluate(()=>window.testExcelMode),'finance');
    assert.equal(await page.locator('[data-workspace]:visible').getAttribute('data-panel'),'excel-editor');
    await page.evaluate(()=>setWorkspace('pricing',false));
    await page.locator('[data-pricing-view="2"]').click();
    await page.locator('#workWindowFocus').click();
    assert.equal(await page.locator('.hero').isVisible(),false);
    assert.equal(await page.locator('#workWindowFocus').getAttribute('aria-pressed'),'true');
    await page.screenshot({path:'outputs/pricing-window/focus-1440.png'});
    await page.locator('#workWindowFocus').click();
    assert.equal(await page.locator('.hero').isVisible(),true);
  } finally {await browser.close();}
});
