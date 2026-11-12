import assert from 'node:assert/strict';
import {existsSync} from 'node:fs';
import {test} from 'node:test';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const root = path.dirname(fileURLToPath(import.meta.url));
const script = path.join(root, '..', 'app', 'static', 'pricing_print.js');
const chromePath = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
let chromium;
let PDFDocument;
try { ({chromium} = await import('playwright')); ({PDFDocument} = await import('pdf-lib')); }
catch { /* Browser QA is optional in CI. */ }

const days = (year, month) => Array.from(
  {length: new Date(year, month, 0).getDate()},
  (_, index) => `${year}-${String(month).padStart(2, '0')}-${String(index + 1).padStart(2, '0')}`,
);

test('saved legacy pricing renders one A4 page with monthly totals',
  {skip: !chromium || !PDFDocument || !existsSync(chromePath)}, async () => {
    const browser = await chromium.launch({headless: true, executablePath: chromePath});
    try {
      const page = await browser.newPage();
      await page.setContent('<p>test</p>');
      await page.evaluate(() => { window.escapeHtml = value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;'); });
      await page.addScriptTag({path: script});
      const html = await page.evaluate(input => pricingPrintHtml('legacy', input.data, input.sheet), {
        data: {year: 2026, month: 11, tax_rate: 0.05, document_name: '正式計價.xlsm'},
        sheet: {label: '53', worksheet_name: '11511-53', totals: {untaxed: 6400, tax: 320, taxed: 6720}, rows: days(2026, 11).map((day, index) => ({
          day, is_holiday: false, normal_hours: index === 0 ? 8 : 0,
          normal_amount: index === 0 ? 6000 : 0, overtime_hours: 0, overtime_amount: 0,
          support_hours: 0, support_amount: 0, total: index === 0 ? 6000 : 0,
          effective_total: index === 0 ? 6400 : 0,
          manual_total_amount: index === 0 ? 6400 : null,
          amount_adjustment_reason: index === 0 ? '依簽單核定' : '',
          external_customer: '', fuel_cost: 0, payer: '',
        }))},
      });
      assert.match(html, /115 年 11 月 53 計價表/);
      assert.match(html, /6,000/);
      assert.match(html, /6,400/);
      assert.match(html, /6,720/);
      assert.match(html, /依簽單核定/);
      assert.match(html, /@page\{size:A4 portrait/);
      await page.setContent(html);
      await page.emulateMedia({media: 'print'});
      const layout = await page.evaluate(() => Object.fromEntries(['.page', 'table', '.footnote', '.signatures', '.stamp'].map(selector => {
        const rect = document.querySelector(selector).getBoundingClientRect();
        return [selector, {top: rect.top, bottom: rect.bottom, height: rect.height}];
      })));
      const pdf = await page.pdf({printBackground: true, preferCSSPageSize: true});
      const document = await PDFDocument.load(pdf);
      assert.equal(document.getPageCount(), 1, JSON.stringify(layout));
    } finally { await browser.close(); }
  });

test('worksite pricing prints saved per-unit hours and blocks unsaved edits',
  {skip: !chromium || !existsSync(chromePath)}, async () => {
    const browser = await chromium.launch({headless: true, executablePath: chromePath});
    try {
      const page = await browser.newPage();
      await page.setContent('<p id="worksitePricingStatus"></p>');
      await page.evaluate(() => {
        window.escapeHtml = value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
        window.worksitePricing = {vehicle: '2.5噸（2節桿）', edits: new Map(), data: {
          site_name: '國泰慈文', year: 2026, month: 11, sheets: [{vehicle_type: '2.5噸（2節桿）', rows: [{
            day: '2026-11-07', units: 2, normal_hours_detail: '2,3', overtime_hours_detail: '',
            normal_total_hours: 5, overtime_total_hours: 0, normal_amount: 6000,
            overtime_amount: 0, total_amount: 6000, external_customer: '', manual_total_amount: null,
          }, ...Array.from({length: 29}, (_, index) => ({
            day: `2026-11-${String(index + 1).padStart(2, '0')}`, units: 0, normal_hours: 0,
            normal_hours_detail: '', overtime_hours_detail: '', normal_total_hours: 0,
            overtime_total_hours: 0, normal_amount: 0, overtime_amount: 0,
            total_amount: 0, external_customer: '', manual_total_amount: null,
          }))]}],
        }};
        window.worksitePricingDirty = () => worksitePricing.edits.size > 0;
      });
      await page.addScriptTag({path: script});
      const html = await page.evaluate(() => pricingPrintHtml('worksite', worksitePricing.data, worksitePricing.data.sheets[0]));
      assert.match(html, /2,3/);
      assert.match(html, /6,000/);
      await page.setContent(html);
      const document = await PDFDocument.load(await page.pdf({printBackground: true, preferCSSPageSize: true}));
      assert.equal(document.getPageCount(), 1);
      await page.setContent('<p id="worksitePricingStatus"></p>');
      await page.evaluate(() => { worksitePricing.edits.set('2026-11-07', {normal_hours_detail: '2,4'}); openPricingPrint('worksite'); });
      assert.match(await page.locator('#worksitePricingStatus').innerText(), /未保存/);
    } finally { await browser.close(); }
  });
