const $ = selector => document.querySelector(selector);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
let documents = [], questions = [], edit = null;

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
    throw new Error(Array.isArray(detail) ? '请核对题目、来源位置和输入长度。' : detail || result.error?.message || '请求失败');
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
  return `<fieldset class="fields"><label>题干<textarea name="question" required maxlength="10000">${esc(item.question)}</textarea></label>
    <label>参考答案<textarea name="answer" maxlength="30000">${esc(item.answer)}</textarea></label>
    <label>知识点（逗号分隔）<input name="tags" value="${esc((item.tags || []).join('，'))}"></label>
    <label>来源位置<input name="locator" maxlength="500" value="${esc(item.locator)}" placeholder="章节或题号"></label></fieldset>`;
}
function readFields(node) {
  return {question:node.querySelector('[name=question]').value.trim(), answer:node.querySelector('[name=answer]').value.trim(),
    tags:node.querySelector('[name=tags]').value.split(/[,，]/).map(x=>x.trim()).filter(Boolean),
    locator:node.querySelector('[name=locator]').value.trim()};
}
async function refresh() {
  [documents, questions] = await Promise.all([api('/documents'),api('/questions?q='+encodeURIComponent($('#filter').elements.q.value))]);
  $('#docs-list').innerHTML = documents.map(d => `<article class="row"><div><h3>${esc(d.filename)}</h3><small>原件已保存，自动拆题待接入</small></div>
    <div class="actions"><a target="_blank" rel="noopener" href="/api/documents/${encodeURIComponent(d.id)}/content">查看原文</a>
    ${button('整理草稿','draft',d.id)}${button('删除文档','remove-doc',d.id)}</div></article>`).join('') || '<p>暂无文档。</p>';
  $('#question-list').innerHTML = questions.map(q => {
    const doc = documents.find(d=>d.id===q.document_id);
    return `<article class="row"><div><h3>${esc(q.question)}</h3><small>${esc(q.tags.join(' · '))} ${q.index_status==='indexed'?'已索引':'待建索引'}</small>
      <details><summary>答案与来源</summary><p class="prose">${esc(q.answer || '暂无参考答案')}</p>
      ${doc?`<a target="_blank" rel="noopener" href="/api/documents/${encodeURIComponent(doc.id)}/content">${esc(doc.filename)}</a>`:'手动录入'} ${esc(q.locator)}</details></div>
      <div class="actions">${button('编辑','edit',q.question_id)}${button('删除题目','remove-question',q.question_id)}</div></article>`;
  }).join('') || '<p>没有符合条件的已确认题目。</p>';
}
function draftForm(items) {
  dialog(`<h2>整理题目草稿</h2><p>${esc(edit.filename)} · 请按原文填写，核对后再确认。</p><form id="draft-form">
    <div id="drafts" style="width:100%">${items.map(fields).join('')}</div>${button('再加一道','more')}<button>预览草稿</button></form>`);
}
$('#upload').onclick = () => $('#file').click();
$('#file').onchange = async event => {
  const file = event.target.files[0]; if (!file) return;
  event.target.value=''; $('#upload').disabled=true;
  try {
    if (file.size>16*1024*1024) throw new Error('单份 Markdown 原件不能超过 16 MiB');
    const body=new FormData(); body.append('file',file);
    const doc=await api('/documents','POST',body); await refresh();
    status(doc.reused?'已复用相同原件，自动拆题待接入。':'原件已保存，自动拆题待接入。');
  } catch(e){status(e.message,true)} finally{$('#upload').disabled=false}
};
$('#add').onclick = () => {edit={}; dialog('<h2>手动添加题目</h2><form id="question-form">'+fields()+'<button>保存题目</button></form>')};
$('#close').onclick=()=>$('#editor').close();
document.addEventListener('click',async event=>{
  const b=event.target.closest('[data-action]'); if(!b || b.disabled)return;
  const {action,id}=b.dataset; b.disabled=true;
  try {
    if(action==='draft'){edit=documents.find(d=>d.id===id); draftForm([{}])}
    if(action==='more')$('#drafts').insertAdjacentHTML('beforeend',fields());
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
      dialog('<h2>核对题目草稿</h2>'+edit.drafts.map((d,i)=>`<article><h3>${i+1}. ${esc(d.question)}</h3><p class="prose">${esc(d.answer || '无参考答案')}</p><small>${esc(d.tags.join(' · '))} ${esc(d.locator)}</small></article>`).join('')+
        button('返回编辑','back')+button('确认入库','confirm',edit.id));
    }
    if(form.id==='search-form'){
      try{const result=await api('/search','POST',{topic:form.elements.topic.value,limit:Number(form.elements.limit.value)}); $('#search-result').textContent=JSON.stringify(result)}
      catch(e){$('#search-result').textContent=e.message}
    }
  }catch(e){status(e.message,true)}finally{delete form.dataset.busy;if(submit)submit.disabled=false}
});
refresh().catch(e=>status(e.message,true));
