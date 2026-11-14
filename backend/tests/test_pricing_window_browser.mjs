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
    await page.setContent(html.replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi,'').replace(/<link\b[^>]*>/gi,''));
    await page.addStyleTag({content:css});
    await page.addScriptTag({content:`const state={workspaceViews:{}};const managementReports={sourcesLoaded:true};const escapeHtml=v=>String(v).replaceAll('&','&amp;').replaceAll('<','&lt;');const escapeAttr=escapeHtml;${navigation}`});
    await page.locator('#loginOverlay').evaluate(x=>x.hidden=true);
    await page.addStyleTag({content:'#loginOverlay{display:none!important}'});
    for(const width of [1440,390]) {
      await page.setViewportSize({width,height:950});
      await page.evaluate(()=>setWorkspace('pricing',false));
      assert.equal(await page.locator('#pricingWindowNav button').count(),7);
      for(const index of [2,3,4]) {
        await page.locator(`[data-pricing-view="${index}"]`).click();
        assert.equal(await page.locator('[data-workspace="pricing"]:visible').count(),1);
        assert.equal(await page.locator(`[data-pricing-view="${index}"]`).getAttribute('aria-pressed'),'true');
      }
      const bounds=await page.locator('[data-workspace="pricing"]:visible').boundingBox();
      assert.ok(bounds.width>300);
      await page.screenshot({path:`outputs/pricing-window/${width}.png`,fullPage:false});
    }
    await page.evaluate(()=>setWorkspace('today',false));
    assert.equal(await page.locator('#pricingWindowNav').isVisible(),false);
  } finally {await browser.close();}
});
