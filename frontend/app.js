const $ = selector => document.querySelector(selector);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
let documents = [], questions = [], edit = null;

// 只在展示答案时解析 Markdown。原始 HTML 会按文字转义，编辑和提交继续使用原文。
const markdown = window.markdownit({html: false, breaks: true});
markdown.renderer.rules.table_open = () => '<div class="answer-table"><table>';
markdown.renderer.rules.table_close = () => '</table></div>';

function answerHTML(answer, emptyText) {
  return `<div class="markdown-answer">${answer ? markdown.render(answer) : esc(emptyText)}</div>`;
}

async function api(path, method = 'GET', body) {
  const options = {method};
  if (body instanceof FormData) options.body = body;
  else if (body !== undefined) {
    options.body = JSON.stringify(body);
    options.headers = {'Content-Type':'application/json'};
  }
  const response = await fetch('/api' + path, options);
  if (response.status === 204) return;
  const result = await response.json();
  if (!response.ok) {
    const detail = result.detail;
    const error = new Error(Array.isArray(detail) ? '请核对题目、来源位置和输入长度。' : detail || result.error?.message || '请求失败');
    error.status = response.status;
    throw error;
  }
  return result;
}
const button = (text, action, id='') => `<button type="button" class="light" data-action="${action}" data-id="${esc(id)}">${text}</button>`;
function status(text, error=false) {
  const node = $('#editor').open && error ? $('#dialog-error') : $('#status');
  node.hidden = false; node.textContent = text; node.classList.toggle('error', error);
}
function dialog(html) {
  $('#dialog-content').innerHTML = html;
  $('#dialog-error').hidden = true;
  if (!$('#editor').open) $('#editor').showModal();
}
function fields(item={}) {
  return `<fieldset class="fields"><label>题干<textarea name="question" rows="1" required maxlength="10000">${esc(item.question)}</textarea></label>
    <label>参考答案<textarea name="answer" rows="2" maxlength="30000">${esc(item.answer)}</textarea></label>
    <label>知识点（逗号分隔）<textarea name="tags" rows="1">${esc((item.tags || []).join('，'))}</textarea></label>
    <label>来源位置<textarea name="locator" rows="1" maxlength="500" placeholder="章节或题号">${esc(item.locator)}</textarea></label></fieldset>`;
}
function readFields(node) {
  return {question:node.querySelector('[name=question]').value.trim(), answer:node.querySelector('[name=answer]').value.trim(),
    tags:node.querySelector('[name=tags]').value.split(/[,，\n]/).map(x=>x.trim()).filter(Boolean),
    locator:node.querySelector('[name=locator]').value.trim()};
}
async function refresh() {
  [documents, questions] = await Promise.all([api('/documents'),api('/questions?q='+encodeURIComponent($('#filter').elements.q.value))]);
  $('#docs-list').innerHTML = documents.map(d => `<article class="row"><div><h3>${esc(d.filename)}</h3><small>${d.questions_llm_confirmed ? '题目已确认' : '原件已保存'}</small></div>
    <div class="actions"><a target="_blank" rel="noopener" href="/api/documents/${encodeURIComponent(d.id)}/content">查看原文</a>
    ${button('整理草稿','draft',d.id)}${button('删除文档','remove-doc',d.id)}</div></article>`).join('') || '<p>暂无文档。</p>';
  $('#question-list').innerHTML = questions.map(q => {
    const doc = documents.find(d=>d.id===q.document_id);
    return `<article class="row"><div><h3>${esc(q.sort_order)}. ${esc(q.question)}</h3><small>${esc(q.tags.join(' · '))} ${q.index_status==='indexed'?'已索引':'待建索引'}</small>
      <details><summary>答案与来源</summary>${answerHTML(q.answer, '暂无参考答案')}
      ${doc?`<a target="_blank" rel="noopener" href="/api/documents/${encodeURIComponent(doc.id)}/content">${esc(doc.filename)}</a>`:'手动录入'} ${esc(q.locator)}</details></div>
      <div class="actions">${button('编辑','edit',q.question_id)}${button('删除题目','remove-question',q.question_id)}</div></article>`;
  }).join('') || '<p>没有符合条件的已确认题目。</p>';
}
function questionSummary(title, number) {
  return `<summary><span class="draft-number">${number}</span><span class="draft-title">${esc(title || '未填写题干')}</span></summary>`;
}
function questionPreview(items) {
  return `<div class="draft-list">${items.map(q => `<details class="draft-row">
    ${questionSummary(q.question, q.sort_order)}<div class="draft-body"><strong>参考答案</strong>
    ${answerHTML(q.answer, '无参考答案')}<small>${esc(q.tags.join(' · '))} ${esc(q.locator)}</small>
    </div></details>`).join('')}</div>`;
}
function editableDraft(q, number) {
  return `<details class="draft-row" ${q.question ? '' : 'open'}>${questionSummary(q.question, number)}
    <div class="draft-body">${fields(q)}<div class="draft-moves">${button('上移','up')}${button('下移','down')}</div></div></details>`;
}
function draftForm(items, note = '') {
  dialog(`<div class="draft-workspace"><header class="draft-head"><h2>整理题目草稿</h2>
    <p>${esc(edit.filename)}</p></header>${note ? `<p class="draft-note">${esc(note)}</p>` : ''}
    <form id="draft-form"><div class="draft-tools"><strong id="draft-count">共 ${items.length} 道</strong>
      <label>排序 <select id="draft-sort"><option value="">当前顺序</option><option value="locator">按原文位置</option><option value="question">按题干</option></select></label></div>
    <div id="drafts" class="draft-list">${items.map((item,i)=>editableDraft(item,i+1)).join('')}</div>
    <footer class="draft-footer">${button('再加一道','more')}<button>预览草稿</button></footer></form></div>`);
}
function previewDrafts() {
  const items = edit.drafts;
  dialog(`<div class="draft-workspace"><header class="draft-head"><h2>核对题目草稿</h2>
    <p>${esc(edit.filename)} · 共 ${items.length} 道</p></header>
    <div class="review-layout"><nav id="review-list" aria-label="选择预览题目">${items.map((q,i) =>
      `<button type="button" data-action="review-one" data-id="${i}"><span class="draft-number">${i+1}</span><span>${esc(q.question)}</span></button>`).join('')}
    </nav><article id="review-reading" aria-live="polite"></article></div>
    <footer class="draft-footer">${button('返回编辑','back')}<button type="button" data-action="confirm" data-id="${esc(edit.id)}">确认入库</button></footer></div>`);
  showDraft(0);
}
function showDraft(index) {
  const q = edit.drafts[index];
  for (const node of $('#review-list').children) node.setAttribute('aria-current', String(Number(node.dataset.id) === index));
  const panel = $('#review-reading');
  panel.innerHTML = `<small>第 ${index+1} / ${edit.drafts.length} 题</small><h3>${esc(q.question)}</h3>
    ${q.tags.length ? `<div class="review-tags">${q.tags.map(t=>`<span>${esc(t)}</span>`).join('')}</div>` : ''}
    <h4>参考答案</h4>${answerHTML(q.answer, '原文未提供答案')}
    ${q.locator ? `<h4>原文位置</h4><p>${esc(q.locator)}</p>` : ''}`;
  panel.scrollTop = 0;
}
function renumberDrafts() {
  const rows = [...$('#drafts').children];
  rows.forEach((row, i) => { row.querySelector('.draft-number').textContent = i + 1; });
  $('#draft-count').textContent = '共 ' + rows.length + ' 道';
}
document.addEventListener('input', event => {
  const row = event.target.closest('#drafts .draft-row');
  if (row && event.target.name === 'question') {
    row.querySelector('.draft-title').textContent = event.target.value.trim() || '未填写题干';
  }
  if (row) $('#draft-sort').value = '';
});
document.addEventListener('change', event => {
  if (event.target.id !== 'draft-sort' || !event.target.value) return;
  const list = $('#drafts');
  const key = event.target.value;
  const collator = new Intl.Collator('zh-CN', {numeric: true});
  const value = row => row.querySelector(`[name="${key}"]`).value.trim();
  const rows = [...list.children].sort((a, b) => {
    const left = value(a), right = value(b);
    return left && right ? collator.compare(left, right) : Number(!left) - Number(!right);
  });
  list.append(...rows);
  renumberDrafts();
});
document.addEventListener('invalid', event => {
  const row = event.target.closest('#drafts .draft-row');
  if (row) row.open = true;
}, true);
$('#upload').onclick = () => $('#file').click();
$('#file').onchange = async event => {
  const file = event.target.files[0]; if (!file) return;
  event.target.value=''; $('#upload').disabled=true;
  try {
    if (file.size>16*1024*1024) throw new Error('单份 Markdown 原件不能超过 16 MiB');
    const body=new FormData(); body.append('file',file);
    const doc=await api('/documents','POST',body); await refresh();
    status(doc.reused ? '已复用相同原件。' : '原件已保存。');
  } catch(e){status(e.message,true)} finally{$('#upload').disabled=false}
};
$('#rebuild').onclick = async event => {
  const button = event.currentTarget;
  const message = $('#rebuild-status');
  button.disabled = true;
  message.hidden = false;
  message.textContent = '正在重建全部题目的向量索引…';
  try {
    const result = await api('/index/rebuild', 'POST');
    message.textContent = `本次读取 ${result.total} 道题，已更新 ${result.indexed} 道题的索引状态。`;
    await refresh();
  } catch (error) {
    message.textContent = error.message;
  } finally {
    button.disabled = false;
  }
};
$('#add').onclick = () => {edit={}; dialog('<h2>手动添加题目</h2><form id="question-form">'+fields()+'<button>保存题目</button></form>')};
$('#close').onclick=()=>$('#editor').close();
document.addEventListener('click',async event=>{
  const b=event.target.closest('[data-action]'); if(!b || b.disabled)return;
  const {action,id}=b.dataset; b.disabled=true;
  try {
    if (action === 'draft') {
      const doc = documents.find(d => d.id === id);
      edit = doc;
      dialog('<h2>整理草稿</h2><p>正在读取文档题目…</p>');
      try {
        const result = await api('/documents/' + encodeURIComponent(id) + '/prepare', 'POST');
        if (edit !== doc || !$('#editor').open) return;
        if (result.status === 'confirmed') {
          dialog(`<h2>已确认题目</h2><p>${esc(doc.filename)} · ${result.questions.length} 道</p>` +
            questionPreview(result.questions));
        } else {
          draftForm(result.questions.length ? result.questions : [{}]);
        }
      } catch (error) {
        if (edit !== doc || !$('#editor').open) return;
        if (error.status !== 501) throw error;
        draftForm([{}], '自动拆题待接入，可手动整理后确认。');
      }
    }
    if (action === 'more') {
      const list = $('#drafts');
      list.insertAdjacentHTML('beforeend', editableDraft({}, list.children.length + 1));
      renumberDrafts();
      $('#draft-sort').value = '';
      list.lastElementChild.querySelector('textarea').focus();
    }
    if (action === 'up' || action === 'down') {
      const row = b.closest('.draft-row');
      const neighbor = action === 'up' ? row.previousElementSibling : row.nextElementSibling;
      if (neighbor) {
        if (action === 'up') neighbor.before(row);
        else neighbor.after(row);
        renumberDrafts();
        $('#draft-sort').value = '';
        row.querySelector('summary').focus();
      }
    }
    if (action === 'review-one') showDraft(Number(id));
    if(action==='back')draftForm(edit.drafts);
    if(action==='confirm'){
      const result=await api('/documents/'+id+'/confirm','POST',{questions:edit.drafts});
      $('#editor').close(); await refresh(); status('已确认 '+result.question_ids.length+' 道题，题目可在下方查看。');
    }
    if(action==='edit'){
      edit=questions.find(q=>q.question_id===id);
      dialog('<h2>编辑题目</h2><form id="question-form">'+fields(edit)+'<button>保存题目</button></form>');
    }
    if(action==='remove-doc' && confirm('删除此文档、原件和所属题目？')){
      await api('/documents/'+id,'DELETE'); await refresh(); status('文档和所属题目已删除。');
    }
    if(action==='remove-question' && confirm('删除这道题目？')){
      await api('/questions/'+id,'DELETE'); await refresh(); status('题目已删除。');
    }
  }catch(e){status(e.message,true)}finally{b.disabled=false}
});
document.addEventListener('submit',async event=>{
  event.preventDefault(); const form=event.target;
  if(form.dataset.busy)return; form.dataset.busy='1'; const submit=event.submitter;
  if(submit)submit.disabled=true;
  try{
    if(form.id==='filter')await refresh();
    if(form.id==='question-form'){
      const id=edit.question_id;
      await api('/questions'+(id?'/'+id:''),id?'PUT':'POST',readFields(form));
      $('#editor').close(); await refresh(); status('题目已保存。');
    }
    if(form.id==='draft-form'){
      edit.drafts=[...form.querySelectorAll('fieldset')].map(readFields);
      if(edit.drafts.some(d=>!d.question))throw new Error('请填写每道题的题干。');
      previewDrafts();
    }
    if(form.id==='search-form'){
      try{const result=await api('/search','POST',{topic:form.elements.topic.value,limit:Number(form.elements.limit.value)}); $('#search-result').textContent=JSON.stringify(result)}
      catch(e){$('#search-result').textContent=e.message}
    }
  }catch(e){status(e.message,true)}finally{delete form.dataset.busy;if(submit)submit.disabled=false}
});
refresh().catch(e=>status(e.message,true));
