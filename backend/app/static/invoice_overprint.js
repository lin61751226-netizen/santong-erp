const LABELS = {buyer:'買受人',tax_id:'統一編號',year:'民國年',month:'月',day:'日',description:'品名',quantity:'數量',unit:'單位',unit_price:'單價',amount:'明細金額',subtotal:'未稅小計',tax:'稅額',total:'數字總計',uppercase:'國字總計',note:'備註'};
const NUMERIC = new Set(['quantity','unit_price','amount','subtotal','tax','total']);

export function paperGeometry(layout) {
  const width=layout.paper_width ?? 190, height=layout.paper_height ?? 105;
  if(!Number.isFinite(width)||!Number.isFinite(height)||width<80||width>297||height<80||height>297)throw new Error('紙張寬高須為80至297mm。');
  return {width,height,pageRule:`@page { size:${width}mm ${height}mm; margin:0; }`};
}

export function applySampleLayout(layout, sample) {
  const next=structuredClone(layout);
  next.paper_width=sample.paper_width;next.paper_height=sample.paper_height;
  for(const key of ['buyer','tax_id'])next.fields[key]=structuredClone(sample.fields[key]);
  return next;
}

export function taipeiDate(now = new Date()) {
  const parts = new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Taipei',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(now);
  const p = Object.fromEntries(parts.map(x=>[x.type,x.value]));
  return `${p.year}-${p.month}-${p.day}`;
}

export function positionedFields(layout, draft, calculated) {
  const values = {buyer:draft.buyer,tax_id:draft.tax_id,year:calculated.roc_year,month:Number(draft.invoice_date.slice(5,7)),day:Number(draft.invoice_date.slice(8,10)),subtotal:calculated.subtotal,tax:calculated.tax,total:calculated.total,uppercase:calculated.uppercase,note:draft.note};
  const result = [];
  function add(key, value, row = 0) {
    const p = layout.fields[key];
    result.push({key,text:String(value ?? ''),x:p.x+layout.offset_x,y:p.y+layout.offset_y+row*layout.row_pitch,width:p.width,numeric:NUMERIC.has(key)});
  }
  for (const [key,value] of Object.entries(values)) add(key,value);
  draft.items.forEach((row,index)=>{
    for (const key of ['description','quantity','unit','unit_price']) add(key,row[key],index);
    add('amount',calculated.amounts[index],index);
  });
  return result;
}

export function backupMessage(result) {
  return result?.status === 'saved' ? '雲端資料庫備份成功。' : '雲端備份尚未成功，請勿重新部署；請按「重試雲端備份」。';
}

if (typeof document !== 'undefined') {
  const $ = id=>document.getElementById(id);
  const state = {layout:null, layoutRevision:0, draftKey:null, draftRevision:0, ready:false, sequence:0, draftDirty:false, layoutDirty:false, draftOffset:0};
  let timer;
  function message(text, error=false) { $('status').textContent=text; $('status').classList.toggle('error',error); }
  async function api(path, options={}) {
    const response = await fetch('/api/invoice-print/'+path,{...options,credentials:'same-origin',headers:{'Content-Type':'application/json',...options.headers}});
    if (response.status===401) throw new Error('登入已過期，請返回管理後台使用原帳密登入；未保存內容請先保留。');
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail==='string'?data.detail:'輸入格式有誤，請檢查日期、數量、單價及欄位範圍。');
    return data;
  }
  function readDraft() {
    return {buyer:$('buyer').value.trim(),tax_id:$('taxId').value.trim(),invoice_date:$('invoiceDate').value,tax:Number($('tax').value),note:$('note').value,
      items:[...$('items').rows].map(tr=>Object.fromEntries(['description','quantity','unit','unit_price'].map(key=>[key,tr.querySelector(`[data-value="${key}"]`).value])))};
  }
  function readLayout() {
    const content = structuredClone(state.layout);
    for (const key of ['paper_width','paper_height','offset_x','offset_y','font_size','tax_id_spacing','row_pitch']) content[key]=Number($(key).value);
    content.font=$('font').value;
    for (const input of $('fieldPositions').querySelectorAll('input')) content.fields[input.dataset.field][input.dataset.axis]=Number(input.value);
    return content;
  }
  function invalidate() {
    state.sequence++; state.ready=false; $('aligned').checked=false; $('overprint').disabled=true; $('testPrint').disabled=true;
    $('validation').textContent='重新檢查中…';
    document.body.classList.remove('print-armed','test-print');
  }
  function schedule() { invalidate(); clearTimeout(timer); timer=setTimeout(()=>refresh(),220); }
  function addRow(row={description:'',quantity:'1',unit:'台',unit_price:'0'}) {
    if ($('items').rows.length>=5) return;
    const tr=document.createElement('tr');
    for (const key of ['description','quantity','unit','unit_price']) {
      const td=tr.insertCell(); const input=document.createElement('input'); input.dataset.value=key; input.value=row[key]; input.setAttribute('aria-label',`${LABELS[key]}第${$('items').rows.length+1}行`);
      input.required=key!=='unit';
      if (key==='quantity'||key==='unit_price') { input.type='number'; input.min=key==='quantity'?'0.001':'0'; input.step=key==='quantity'?'0.001':'0.01'; input.max=key==='quantity'?'1000000':'999999999'; }
      else input.maxLength=key==='unit'?4:40;
      td.append(input);
    }
    const amount=tr.insertCell(); amount.dataset.amount=''; amount.textContent='—';
    const remove=document.createElement('button');remove.type='button'; remove.textContent='移除';remove.onclick=()=>{if($('items').rows.length===1)return;tr.remove();state.draftDirty=true;schedule();};
    tr.insertCell().append(remove); $('items').append(tr); $('addItem').disabled=$('items').rows.length>=5;
  }
  function fillDraft(content) {
    for (const [id,key] of [['buyer','buyer'],['taxId','tax_id'],['invoiceDate','invoice_date'],['tax','tax'],['note','note']]) $(id).value=content[key];
    $('items').replaceChildren(); content.items.forEach(addRow); state.draftDirty=false; invalidate(); refresh();
  }
  function fillLayout(content) {
    state.layout=structuredClone(content);
    const paper=paperGeometry(content);state.layout.paper_width=paper.width;state.layout.paper_height=paper.height;
    for (const key of ['paper_width','paper_height','offset_x','offset_y','font','font_size','tax_id_spacing','row_pitch']) $(key).value=state.layout[key];
    $('fieldPositions').replaceChildren();
    for (const [key,p] of Object.entries(content.fields)) {
      const tr=document.createElement('tr');tr.insertCell().textContent=LABELS[key];
      for (const axis of ['x','y','width']) {
        const input=document.createElement('input');input.type='number';input.step='0.1';input.min=axis==='width'?'0.1':'0';input.max='297';input.required=true;input.value=p[axis];input.dataset.field=key;input.dataset.axis=axis;input.setAttribute('aria-label',`${LABELS[key]} ${axis} mm`);tr.insertCell().append(input);
      }
      $('fieldPositions').append(tr);
    }
    state.layoutDirty=false; $('layoutVersion').textContent=`共用印模版本 ${state.layoutRevision}（0為未保存初始值）`;
  }
  async function refresh() {
    if (!state.layout) return false;
    const sequence=++state.sequence; state.ready=false; $('overprint').disabled=true;$('testPrint').disabled=true;
    if (!$('draftForm').checkValidity() || !$('layoutForm').checkValidity()) {
      $('validation').textContent='請完成必填內容；數量、日期及校正值必須在範圍內。'; $('printFields').replaceChildren();return false;
    }
    const draft=readDraft(), layout=readLayout();
    try {
      const paper=paperGeometry(layout);
      document.documentElement.style.setProperty('--paper-width',`${paper.width}mm`);
      document.documentElement.style.setProperty('--paper-height',`${paper.height}mm`);
      $('paperStyle').textContent=paper.pageRule;
      $('paperSummary').textContent=`${paper.width} × ${paper.height} mm${paper.width>=paper.height?' 橫式':''}`;
      $('sheet').setAttribute('aria-label',`${paper.width}乘${paper.height}公厘套印預覽`);
      $('paperHint').textContent=`版面${paper.width}×${paper.height}mm。Brother短邊進紙${Math.min(paper.width,paper.height)}×${Math.max(paper.width,paper.height)}mm，方向${paper.width>=paper.height?'橫向':'直向'}；紙張含裝訂邊，不再加邊。`;
      const calculated=await api('calculate',{method:'POST',body:JSON.stringify(draft)});
      if(sequence!==state.sequence)return false;
      $('totals').textContent=`未稅 ${calculated.subtotal.toLocaleString()} ＋ 稅額 ${calculated.tax.toLocaleString()} ＝ 總計 ${calculated.total.toLocaleString()} 元 ／ ${calculated.uppercase}`;
      [...$('items').rows].forEach((row,i)=>row.querySelector('[data-amount]').textContent=calculated.amounts[i].toLocaleString());
      $('addItem').disabled=draft.items.length>=5;
      $('printFields').replaceChildren();
      $('sheet').style.fontFamily=`"${layout.font}", serif`; $('sheet').style.fontSize=`${layout.font_size}pt`;
      const invalid=[], boxes=[];const sheet=$('sheet').getBoundingClientRect(); const scale=sheet.width/paper.width;
      for (const field of positionedFields(layout,draft,calculated)) {
        const element=document.createElement('div');element.className='print-field'+(field.numeric?' numeric':'');element.textContent=field.text;
        element.style.left=`${field.x}mm`;element.style.top=`${field.y}mm`;element.style.width=`${field.width}mm`;element.title=LABELS[field.key];
        if(field.key==='tax_id')element.style.letterSpacing=`${layout.tax_id_spacing}mm`;
        $('printFields').append(element);
        const rect=element.getBoundingClientRect();
        if(field.text)boxes.push({key:field.key,element,rect});
        if(field.text && (field.x<0||field.y<0||field.x+field.width>paper.width+.01||field.y+rect.height/scale>paper.height+.01||element.scrollWidth>element.clientWidth+1)) {
          invalid.push(LABELS[field.key]);element.classList.add('bad');
        }
      }
      for(let i=0;i<boxes.length;i++)for(let j=i+1;j<boxes.length;j++) {
        const a=boxes[i], b=boxes[j];
        if(Math.min(a.rect.right,b.rect.right)-Math.max(a.rect.left,b.rect.left)>.5 && Math.min(a.rect.bottom,b.rect.bottom)-Math.max(a.rect.top,b.rect.top)>.5) {
          invalid.push(LABELS[a.key],LABELS[b.key]);a.element.classList.add('bad');b.element.classList.add('bad');
        }
      }
      state.ready=invalid.length===0;
      $('validation').textContent=state.ready?'欄位位於紙張內；仍需白紙試印比對實際格位。':`超出範圍或欄位重疊：${[...new Set(invalid)].join('、')}。請調整座標、寬度、字體或內容。`;
      $('validation').classList.toggle('error',!state.ready);
      $('testPrint').disabled=!state.ready; $('overprint').disabled=!(state.ready&&$('aligned').checked);
      return state.ready;
    } catch(error) { if(sequence===state.sequence){$('validation').textContent=error.message;message(error.message,true);} return false; }
  }
  function mayDiscard() {
    if(state.savingDraft){message('草稿正在保存，請等保存結果後再切換。');return false;}
    return !state.draftDirty || window.confirm('目前草稿有未保存的修改。確定放棄並載入其他草稿？');
  }
  function newDraft() {
    if(!mayDiscard())return;
    state.draftKey=crypto.randomUUID();state.draftRevision=0;
    fillDraft({buyer:'',tax_id:'',invoice_date:taipeiDate(),tax:0,note:'',items:[{description:'堆高機作業',quantity:'1',unit:'台',unit_price:'0'}]});
    message('新草稿尚未保存。請填寫內容，再按「保存草稿」。');
  }
  async function loadLayout() {
    if(state.savingLayout){message('印模正在保存，請等保存結果後再載入。');return;}
    if(state.layoutDirty&&!window.confirm('放棄目前未保存的校正，重新載入共用印模？'))return;
    const result=await api('template'); state.layoutRevision=result.revision;fillLayout(result.content);invalidate();await refresh();
  }
  async function listDrafts(offset=0) {
    const result=await api(`drafts?offset=${offset}`);state.draftOffset=offset; $('draftList').replaceChildren();
    for(const row of result.items) {
      const button=document.createElement('button');button.type='button';button.textContent=`${row.content.invoice_date} ／ ${row.content.buyer||'未填抬頭'} ／ 版本${row.revision}`;
      button.onclick=()=>{if(!mayDiscard())return;state.draftKey=row.record_key;state.draftRevision=row.revision;fillDraft(row.content);message(`已開啟草稿版本${row.revision}；校正沿用目前共用印模。`);};$('draftList').append(button);
    }
    if(!result.items.length)$('draftList').textContent='此批沒有已存草稿。';
    $('moreDrafts').hidden=!result.has_more;
  }
  async function action(button, operation, lock=null) {
    if(lock&&state[lock])return;
    if(lock)state[lock]=true;
    button.disabled=true;try {await operation();} catch(error){message(error.message,true);} finally{button.disabled=false;if(lock)state[lock]=false;}
  }
  $('contentTab').onclick=()=>{ $('contentPanel').hidden=false;$('layoutPanel').hidden=true;$('contentTab').setAttribute('aria-pressed','true');$('layoutTab').setAttribute('aria-pressed','false'); };
  $('layoutTab').onclick=()=>{ $('contentPanel').hidden=true;$('layoutPanel').hidden=false;$('contentTab').setAttribute('aria-pressed','false');$('layoutTab').setAttribute('aria-pressed','true'); };
  $('draftForm').onsubmit=event=>event.preventDefault();$('layoutForm').onsubmit=event=>event.preventDefault();
  $('draftForm').addEventListener('input',()=>{state.draftDirty=true;schedule();});
  $('layoutForm').addEventListener('input',()=>{state.layoutDirty=true;schedule();});
  $('addItem').onclick=()=>{addRow();state.draftDirty=true;schedule();};
  $('aligned').onchange=()=>{$('overprint').disabled=!(state.ready&&$('aligned').checked);};
  $('saveLayout').onclick=()=>action($('saveLayout'),async()=>{
    if(!$('layoutForm').reportValidity())return;
    const content=readLayout(); const result=await api('template',{method:'PUT',body:JSON.stringify({expected_revision:state.layoutRevision,content})});
    state.layoutRevision=result.revision;state.layoutDirty=JSON.stringify(readLayout())!==JSON.stringify(content);state.layout=content;
    $('layoutVersion').textContent=`共用印模版本 ${result.revision}`;message(`印模已保存至資料庫，版本${result.revision}。${backupMessage(result.backup)}${state.layoutDirty?'保存期間另有校正，請再保存。':''}`);
  },'savingLayout');
  $('saveDraft').onclick=()=>action($('saveDraft'),async()=>{
    if(!$('draftForm').reportValidity())return;
    const fingerprint=JSON.stringify(readDraft());const result=await api('drafts',{method:'POST',body:JSON.stringify({record_key:state.draftKey,expected_revision:state.draftRevision,content:readDraft()})});
    state.draftRevision=result.revision;state.draftDirty=JSON.stringify(readDraft())!==fingerprint;
    message(`草稿版本${result.revision}已保存至資料庫。${backupMessage(result.backup)}${state.draftDirty?'保存期間另有修改，請再保存。':''}`);
  },'savingDraft');
  $('newDraft').onclick=newDraft;
  $('sampleLayout').onclick=()=>action($('sampleLayout'),async()=>{
    if(state.savingLayout)throw new Error('印模正在保存，請稍候。');
    if(!$('layoutForm').reportValidity())return;
    const sample=await api('sample-layout');
    if(state.savingLayout)throw new Error('印模正在保存，請稍候。');
    if(!window.confirm('只套用195×103mm紙張與買受人／統編位置；其他欄位、字型、位移與金額不變。套用後仍須按保存。'))return;
    fillLayout(applySampleLayout(readLayout(),sample));state.layoutDirty=true;schedule();
    message('已套用照片估算位置：買受人在上、統編在下；尚未保存，仍需白紙試印。');
  });
  $('reloadLayout').onclick=()=>action($('reloadLayout'),loadLayout);
  $('reloadDrafts').onclick=()=>action($('reloadDrafts'),()=>listDrafts());
  $('moreDrafts').onclick=()=>action($('moreDrafts'),()=>listDrafts(state.draftOffset+50));
  $('backup').onclick=()=>action($('backup'),async()=>message(backupMessage(await api('backup',{method:'POST'}))));
  $('tax5').onclick=()=>action($('tax5'),async()=>{
    const draft=readDraft(), fingerprint=JSON.stringify(draft);const result=await api('calculate',{method:'POST',body:JSON.stringify(draft)});
    if(JSON.stringify(readDraft())!==fingerprint)throw new Error('內容已變更，請重新試算。');
    $('tax').value=Math.floor((result.subtotal*5+50)/100);state.draftDirty=true;schedule();
  });
  async function print(test) {
    if(!test&&!$('aligned').checked)return;
    if(!await refresh()||(!test&&!$('aligned').checked))return;
    if(!test&&(!readDraft().buyer||!readDraft().tax_id)){message('正式套印前請填寫買受人與8位統編。',true);return;}
    document.body.classList.toggle('test-print',test);document.body.classList.add('print-armed');window.print();
  }
  $('testPrint').onclick=()=>print(true);$('overprint').onclick=()=>print(false);
  window.addEventListener('afterprint',()=>document.body.classList.remove('print-armed','test-print'));
  window.addEventListener('beforeunload',event=>{if(state.draftDirty||state.layoutDirty||state.savingDraft||state.savingLayout){event.preventDefault();event.returnValue='';}});
  (async()=>{
    try { await loadLayout();newDraft();$('saveLayout').disabled=false;$('saveDraft').disabled=false;$('sampleLayout').disabled=false; }
    catch(error){message(error.message,true);}
  })();
}
