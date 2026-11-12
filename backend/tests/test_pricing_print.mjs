import assert from 'node:assert/strict';
import {existsSync, writeFileSync} from 'node:fs';
import {test} from 'node:test';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const root = path.dirname(fileURLToPath(import.meta.url));
const script = path.join(root, '..', 'app', 'static', 'pricing_print.js');
const chromeCandidates = [
  process.env.CHROME_PATH,
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  '/usr/bin/google-chrome',
  '/usr/bin/google-chrome-stable',
  '/usr/bin/chromium',
  '/usr/bin/chromium-browser',
].filter(Boolean);
const chromePath = chromeCandidates.find(candidate => existsSync(candidate));
let chromium;
let PDFDocument;
try { ({chromium} = await import('playwright')); ({PDFDocument} = await import('pdf-lib')); }
catch { /* Browser QA is optional in CI. */ }

const launchOptions = chromePath ? {headless: true, executablePath: chromePath} : {headless: true};
if (process.platform !== 'win32') launchOptions.args = ['--no-sandbox', '--disable-dev-shm-usage'];

function billingDate(iso) {
  const [year, month, date] = iso.split('-').map(Number);
  const weekday = new Date(Date.UTC(year, month - 1, date)).getUTCDay();
  return `${month}月${date}日(週${'日一二三四五六'[weekday]})`;
}

function rowCells(html, marker) {
  const start = html.indexOf(marker);
  assert.notEqual(start, -1, marker);
  const row = html.slice(html.lastIndexOf('<tr', start), html.indexOf('</tr>', start) + 5);
  const cells = [...row.matchAll(/<td class="([^"]*)">([^<]*)<\/td>/g)].map(match => ({
    className: match[1], text: match[2],
  }));
  return {row, cells};
}

const oct53Source = [
  [1, 16, 12000, 0, 0], [2, 16, 12000, 0, 0], [3, 24, 18000, 0, 0], [4, 0, 0, 4, 4000],
  [5, 24, 18000, 0, 0], [6, 24, 18000, 1, 1000], [7, 24, 18000, 4, 4000], [8, 24, 18000, 1, 1000],
  [9, 16, 12000, 0, 0], [10, 16, 12000, 0, 0], [11, 0, 0, 16, 16000], [12, 20, 15200, 2, 2000],
  [13, 20, 15200, 0, 0], [14, 24, 18000, 4, 4000], [15, 24, 18000, 1, 1000], [16, 20, 15200, 1, 1000],
  [17, 16, 12000, 0, 0], [18, 0, 0, 4, 4000], [19, 16, 12000, 1, 1000], [20, 16, 12000, 0, 0],
  [21, 16, 12000, 0, 0], [22, 20, 15200, 0, 0], [23, 8, 6000, 0, 0], [24, 6, 4800, 0, 0],
  [25, 0, 0, 0, 0], [26, 16, 12000, 0, 0], [27, 20, 15200, 2, 2000], [28, 20, 15200, 1, 1000],
  [29, 20, 15200, 2, 2000], [30, 22, 16800, 4, 4000], [31, 12, 9200, 0, 0],
];
// 後端 is_holiday：週日，以及 115 年 10 月已登錄的國定假日（9、10、11、24、25、26 日）。
const octHolidayDays = new Set([4, 9, 10, 11, 18, 24, 25, 26]);

function oct53Sheet() {
  const rows = oct53Source.map(([day, normalHours, normalAmount, overtimeHours, overtimeAmount]) => {
    const total = normalAmount + overtimeAmount;
    const approved = day === 23;
    return {
      day: `2026-10-${String(day).padStart(2, '0')}`,
      is_holiday: octHolidayDays.has(day),
      normal_hours: normalHours, normal_amount: normalAmount,
      overtime_hours: overtimeHours, overtime_amount: overtimeAmount,
      support_hours: 0, support_amount: 0, total,
      effective_total: approved ? 6400 : total,
      manual_total_amount: approved ? 6400 : null,
      amount_adjustment_reason: approved ? '依當日簽單核定' : '',
      adjustment_stale: false,
    };
  });
  return {
    data: {year: 2026, month: 10, tax_rate: 0.05, document_name: '三通工程行115年推高機計價1107.xlsm'},
    sheet: {
      label: '53', worksheet_name: '11510-53',
      totals: {untaxed: 425600, tax: 21280, taxed: 446880},
      rows,
    },
  };
}

async function withBrowser(run) {
  const browser = await chromium.launch(launchOptions);
  try {
    const page = await browser.newPage();
    await page.setContent('<p id="costGridStatus"></p><p id="worksitePricingStatus"></p>');
    await page.evaluate(() => {
      window.escapeHtml = value => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
    });
    await page.addScriptTag({path: script});
    await run(page);
  } finally { await browser.close(); }
}

test('October 53 legacy billing sheet matches the company Excel layout on one A4 page',
  {skip: !chromium || !PDFDocument || !chromePath}, async () => {
    const {data, sheet} = oct53Sheet();
    assert.equal(sheet.rows.filter(row => row.day.endsWith('-03') || row.day.endsWith('-17') || row.day.endsWith('-31'))
      .every(row => row.is_holiday === false), true);
    assert.equal(sheet.rows.find(row => row.day.endsWith('-09')).is_holiday, true);
    assert.equal(sheet.rows.find(row => row.day.endsWith('-26')).is_holiday, true);
    assert.equal(sheet.rows.reduce((sum, row) => sum + row.normal_hours, 0), 500);
    assert.equal(sheet.rows.reduce((sum, row) => sum + row.normal_amount, 0), 377200);
    assert.equal(sheet.rows.reduce((sum, row) => sum + row.overtime_hours, 0), 48);
    assert.equal(sheet.rows.reduce((sum, row) => sum + row.overtime_amount, 0), 48000);
    await withBrowser(async page => {
      const html = await page.evaluate(input => pricingPrintHtml('legacy', input.data, input.sheet), {data, sheet});
      assert.match(html, /53工地 × 三通工程行 115年10月 請款計價表/);
      assert.match(html, /請款日期：115年10月/);
      assert.match(html, /承辦人：____________/);
      assert.match(html, /審核：____________/);
      assert.match(html, /總計（未稅）/);
      assert.match(html, /含稅請款金額 NT\$/);
      assert.match(html, /@page\s*\{[^}]*size:\s*A4 portrait/);
      assert.match(html, /#1f4e79/i);
      assert.match(html, /#2e75b6/i);
      assert.match(html, /#d6e4f0/i);
      assert.match(html, /#ebf5fb/i);
      assert.match(html, /#fff0f0/i);
      assert.match(html, /#c00000/i);
      assert.match(html, /#fff2cc/i);
      assert.match(html, /#fce4d6/i);
      assert.match(html, /print-color-adjust:\s*exact/);
      assert.doesNotMatch(html, /11510-53/);
      assert.doesNotMatch(html, /期間：/);
      assert.doesNotMatch(html, /已保存月表/);
      assert.doesNotMatch(html, /來源：/);
      assert.doesNotMatch(html, /列印日期/);
      assert.doesNotMatch(html, /核定未稅/);
      assert.doesNotMatch(html, /營業稅/);
      assert.doesNotMatch(html, /製表/);
      assert.doesNotMatch(html, /支援/);
      assert.doesNotMatch(html, /377,200/);
      assert.doesNotMatch(html, /425,200/);
      assert.doesNotMatch(html, /21,280/);
      assert.doesNotMatch(html, /\.print\s*\(/);
      assert.doesNotMatch(html, /\u31d1/);
      assert.equal(html.match(/id="pricingPrintButton"/g).length, 1);
      const header = html.slice(html.indexOf('<thead'), html.indexOf('</thead>'));
      assert.match(header, /日期/);
      assert.ok(header.indexOf('正常<br>時數') < header.indexOf('正常<br>金額'));
      assert.ok(header.indexOf('正常<br>金額') < header.indexOf('加班<br>時數'));
      assert.ok(header.indexOf('加班<br>時數') < header.indexOf('加班<br>金額'));
      assert.ok(header.indexOf('加班<br>金額') < header.indexOf('備註'));
      assert.doesNotMatch(header, /支援/);

      const texts = marker => rowCells(html, marker).cells.map(cell => cell.text);
      assert.deepEqual(texts(billingDate('2026-10-01')), ['10月1日(週四)', '16', '12,000', '0', '', '']);
      assert.deepEqual(texts(billingDate('2026-10-04')), ['10月4日(週日)', '0', '', '4', '4,000', '']);
      assert.deepEqual(texts(billingDate('2026-10-23')), [
        '10月23日(週五)', '8', '6,400', '0', '', '核定 6,000→6,400（依當日簽單核定）',
      ]);
      assert.deepEqual(texts(billingDate('2026-10-25')), ['10月25日(週日)', '0', '', '0', '', '']);
      assert.deepEqual(texts('>小計<'), ['小計', '500', '377,600', '48', '48,000', '']);
      assert.match(html, /<td class="boxed untaxed">425,600<\/td>/);
      assert.match(html, /<td class="boxed taxed">446,880<\/td>/);
      assert.equal((html.match(/(?<!\d)6,400(?!\d)/g) || []).length, 2);
      assert.equal((html.match(/(?<!\d)6,000(?!\d)/g) || []).length, 1);
      assert.match(rowCells(html, billingDate('2026-10-01')).cells[0].className, /^date$/);
      assert.match(rowCells(html, billingDate('2026-10-01')).cells[2].className, /^money$/);

      for (const day of [3, 4, 9, 10, 11, 17, 18, 24, 25, 26, 31]) {
        const found = rowCells(html, billingDate(`2026-10-${String(day).padStart(2, '0')}`));
        assert.match(found.row, /^<tr class="off">/, billingDate(`2026-10-${String(day).padStart(2, '0')}`));
      }
      for (const day of [1, 2, 5, 6, 8, 12, 16, 19, 23, 27, 30]) {
        const found = rowCells(html, billingDate(`2026-10-${String(day).padStart(2, '0')}`));
        assert.match(found.row, /^<tr><td class="date">/, billingDate(`2026-10-${String(day).padStart(2, '0')}`));
      }
      assert.match(html, /10月5日\(週一\)/);
      assert.match(html, /10月12日\(週一\)/);
      assert.match(html, /10月19日\(週一\)/);
      assert.match(html, /10月26日\(週一\)/);

      await page.setContent(html);
      await page.emulateMedia({media: 'print'});
      const pdf = await page.pdf({printBackground: true, preferCSSPageSize: true});
      writeFileSync('/tmp/oct53-billing-print.pdf', pdf);
      const document = await PDFDocument.load(pdf);
      const layout = await page.evaluate(() => {
        const sign = document.querySelector('.billing-sign').getBoundingClientRect();
        return {signBottom: sign.bottom, bodyHeight: document.body.scrollHeight};
      });
      assert.equal(document.getPageCount(), 1, JSON.stringify(layout));
    });
  });

test('legacy billing keeps backend totals, support money, and print safeguards',
  {skip: !chromium || !chromePath}, async () => {
    await withBrowser(async page => {
      const html = await page.evaluate(() => pricingPrintHtml('legacy',
        {year: 2026, month: 10, tax_rate: 0.05},
        {label: '53', totals: {untaxed: 16200, tax: 810, taxed: 17010}, rows: [
          {day: '2026-10-01', is_holiday: false, normal_hours: 8, normal_amount: 6000,
            overtime_hours: 1, overtime_amount: 1000, support_hours: 0, support_amount: 0, total: 7000,
            effective_total: 7400, manual_total_amount: 7400, amount_adjustment_reason: '無法拆分', adjustment_stale: false},
          {day: '2026-10-02', is_holiday: false, normal_hours: 0, normal_amount: 0,
            overtime_hours: 1, overtime_amount: 1000, support_hours: 0, support_amount: 0, total: 1000,
            effective_total: 1200, manual_total_amount: 1200, amount_adjustment_reason: '加班簽單', adjustment_stale: false},
          {day: '2026-10-03', is_holiday: false, normal_hours: 0, normal_amount: 0,
            overtime_hours: 0, overtime_amount: 0, support_hours: 2, support_amount: 1600, total: 1600,
            effective_total: 1600, manual_total_amount: null, amount_adjustment_reason: '', adjustment_stale: false},
          {day: '2026-10-04', is_holiday: true, normal_hours: 8, normal_amount: 6000,
            overtime_hours: 0, overtime_amount: 0, support_hours: 0, support_amount: 0, total: 6000,
            effective_total: 6000, manual_total_amount: 6400, amount_adjustment_reason: '舊簽單', adjustment_stale: true},
        ]}));
      const header = html.slice(html.indexOf('<thead'), html.indexOf('</thead>'));
      assert.ok(header.indexOf('加班<br>金額') < header.indexOf('支援<br>時數'));
      assert.ok(header.indexOf('支援<br>金額') < header.indexOf('備註'));
      const texts = marker => rowCells(html, marker).cells.map(cell => cell.text);
      assert.deepEqual(texts('10月1日(週四)'), ['10月1日(週四)', '8', '6,400', '1', '1,000', '0', '', '核定 7,000→7,400（無法拆分）']);
      assert.deepEqual(texts('10月2日(週五)'), ['10月2日(週五)', '0', '', '1', '1,200', '0', '', '核定 1,000→1,200（加班簽單）']);
      assert.deepEqual(texts('10月3日(週六)'), ['10月3日(週六)', '0', '', '0', '', '2', '1,600', '']);
      assert.match(rowCells(html, '10月3日(週六)').row, /^<tr class="off">/);
      assert.deepEqual(texts('10月4日(週日)'), ['10月4日(週日)', '8', '6,000', '0', '', '0', '', '舊核定 6,400 待重核，暫採公式']);
      assert.deepEqual(texts('>小計<'), ['小計', '16', '12,400', '2', '2,200', '2', '1,600', '']);
      assert.match(html, /<td class="boxed untaxed">16,200<\/td>/);
      assert.match(html, /<td class="boxed taxed">17,010<\/td>/);

      const backendHtml = await page.evaluate(() => pricingPrintHtml('legacy',
        {year: 2026, month: 11, tax_rate: 0.05},
        {label: '53', totals: {untaxed: 425600, taxed: 446880}, rows: [
          {day: '2026-11-01', is_holiday: false, normal_hours: 8, normal_amount: 6000,
            overtime_hours: 0, overtime_amount: 0, support_hours: 0, support_amount: 0, total: 6000,
            effective_total: 6000, manual_total_amount: null, adjustment_stale: false},
        ]}));
      assert.match(backendHtml, /<td class="boxed untaxed">425,600<\/td>/);
      assert.match(backendHtml, /<td class="boxed taxed">446,880<\/td>/);
      assert.match(backendHtml, />小計</);
      assert.match(backendHtml, />6,000</);

      await page.evaluate(() => {
        window.costGrid = {label: '53', data: {year: 2026, month: 10, tax_rate: 0.05, sheets: [
          {label: '53', totals: {untaxed: 0, taxed: 0}, rows: []},
        ]}};
        window.costGridDirty = () => false;
        window.printed = false;
        window.opened = 0;
        window.open = () => {
          window.opened += 1;
          return {
            document: {
              open() {}, write() {}, close() {},
              getElementById() { return {addEventListener(_type, fn) { window.printClick = fn; }}; },
            },
            print() { window.printed = true; },
          };
        };
      });
      await page.evaluate(() => openPricingPrint('legacy'));
      assert.equal(await page.evaluate(() => window.opened), 1);
      assert.equal(await page.evaluate(() => window.printed), false);
      await page.evaluate(() => window.printClick());
      assert.equal(await page.evaluate(() => window.printed), true);
      assert.match(await page.locator('#costGridStatus').innerText(), /未送印/);
      await page.evaluate(() => { window.costGridDirty = () => true; window.opened = 0; openPricingPrint('legacy'); });
      assert.match(await page.locator('#costGridStatus').innerText(), /未保存/);
      assert.equal(await page.evaluate(() => window.opened), 0);
    });
  });

test('a 28-day legacy billing month stays on one A4 page',
  {skip: !chromium || !PDFDocument || !chromePath}, async () => {
    await withBrowser(async page => {
      const html = await page.evaluate(() => pricingPrintHtml('legacy',
        {year: 2026, month: 2, tax_rate: 0.05},
        {label: '45', totals: {untaxed: 0, tax: 0, taxed: 0}, rows: Array.from({length: 28}, (_, index) => ({
          day: `2026-02-${String(index + 1).padStart(2, '0')}`, is_holiday: false,
          normal_hours: 0, normal_amount: 0, overtime_hours: 0, overtime_amount: 0,
          support_hours: 0, support_amount: 0, total: 0, effective_total: 0,
          manual_total_amount: null, adjustment_stale: false,
        }))}));
      assert.match(html, /45工地 × 三通工程行 115年2月 請款計價表/);
      assert.match(html, /請款日期：115年2月/);
      assert.match(rowCells(html, '2月1日(週日)').row, /^<tr class="off">/);
      assert.match(rowCells(html, '2月2日(週一)').row, /^<tr><td class="date">/);
      assert.match(rowCells(html, '2月7日(週六)').row, /^<tr class="off">/);
      assert.match(html, /<td class="boxed untaxed">0<\/td>/);
      assert.match(html, /<td class="boxed taxed">0<\/td>/);
      await page.setContent(html);
      await page.emulateMedia({media: 'print'});
      const document = await PDFDocument.load(await page.pdf({printBackground: true, preferCSSPageSize: true}));
      assert.equal(document.getPageCount(), 1);
    });
  });

test('worksite pricing prints saved per-unit hours and blocks unsaved edits',
  {skip: !chromium || !PDFDocument || !chromePath}, async () => {
    await withBrowser(async page => {
      await page.evaluate(() => {
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
      const html = await page.evaluate(() => pricingPrintHtml('worksite', worksitePricing.data, worksitePricing.data.sheets[0]));
      assert.match(html, /三通工程行 115 年 11 月 國泰慈文 計價表/);
      assert.match(html, /期間：2026 年 11 月/);
      assert.match(html, /來源：國泰慈文/);
      assert.match(html, /已保存月表/);
      assert.match(html, /逐台明細/);
      assert.match(html, /實際未稅/);
      assert.match(html, /2,3/);
      assert.match(html, /6,000/);
      assert.match(html, /製表：/);
      assert.match(html, /核對：/);
      assert.match(html, /客戶確認：/);
      assert.match(html, /@page\{size:A4 portrait/);
      assert.doesNotMatch(html, /請款計價表/);
      assert.doesNotMatch(html, /請款日期：/);
      assert.doesNotMatch(html, /含稅請款金額/);
      await page.setContent(html);
      const document = await PDFDocument.load(await page.pdf({printBackground: true, preferCSSPageSize: true}));
      assert.equal(document.getPageCount(), 1);
      await page.setContent('<p id="worksitePricingStatus"></p>');
      await page.evaluate(() => {
        window.worksitePricing = {vehicle: '2.5噸（2節桿）', edits: new Map(), data: {
          site_name: '國泰慈文', year: 2026, month: 11, sheets: [{vehicle_type: '2.5噸（2節桿）', rows: []}],
        }};
        window.worksitePricingDirty = () => worksitePricing.edits.size > 0;
        worksitePricing.edits.set('2026-11-07', {normal_hours_detail: '2,4'});
        openPricingPrint('worksite');
      });
      assert.match(await page.locator('#worksitePricingStatus').innerText(), /未保存/);
    });
  });
