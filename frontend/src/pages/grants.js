import {store} from '../stores/workspace.js';
import {ledger,field,input,select,badge} from '../components/ledger.js';
import {esc,money,date} from '../utils/format.js';
import {bindForm,request} from '../utils/forms.js';

const riskLabels={high:'高风险',medium:'中风险',low:'低风险'};
const decisionLabels={candidate:'拟入选',funded:'已占款',excluded:'落选'};
const batchStatus={preview:'待确认',confirmed:'已确认'};

function historySection(d){
 const batches=[...d.grant_batches].sort((a,b)=>b.id-a.id);
 if(!batches.length)return `<h3>资助名单批次</h3><p class="hint">尚无批次；选择专项后编制本批名单。</p>`;
 return `<h3>资助名单批次（入选与落选留痕）</h3>`+batches.map(b=>{
   const grant=d.grants.find(g=>g.id===b.grant_id);
   const head=`<article class="batch-card"><header><div><small>#${b.id} · ${esc(grant?.name||('专项 '+b.grant_id))}</small><h4>${batchStatus[b.status]||b.status} · 编制于 ${date(b.created_at)}</h4></div><dl><div><dt>编制时可用</dt><dd>${money(b.available_cents)}</dd></div><div><dt>本批占款</dt><dd>${money(b.funded_cents)}</dd></div><div><dt>确认后可用</dt><dd>${b.balance_cents===null?'—':money(b.balance_cents)}</dd></div></dl></header>`;
   const rows=d.grant_batch_items.filter(it=>it.batch_id===b.id).sort((a,z)=>a.rank_no-z.rank_no).map(it=>{
     const t=d.treatments.find(x=>x.id===it.treatment_id);
     const folio=d.accessions.find(a=>a.id===t?.accession_id);
     return [
       String(it.rank_no),
       `${esc(folio?.code||'')}<strong>${esc(folio?.title||t?.name||'')}</strong>`,
       it.risk_level?badge(riskLabels[it.risk_level]||it.risk_level,it.risk_level):'—',
       money(it.estimated_cents),
       badge(decisionLabels[it.decision]||it.decision,it.decision==='funded'?'good':''),
       it.reason?esc(it.reason):'—'
     ];
   });
   const table=ledger(['序','卷册 / 方案','最新风险','整笔估价','结论','原因'],rows,'本批没有候选草案。');
   const actions=b.status==='preview'
     ? `<form id="confirm-batch-${b.id}" class="inline-form"><input type="hidden" name="batch_id" value="${b.id}">${field('确认备注（可选）',input('note','text','','maxlength="200"'))}<button>一次性确认整批占款</button></form>`
     : `<p class="hint">确认时间 ${date(b.confirmed_at)}${b.note?' · '+esc(b.note):''}</p>`;
   return head+table+actions+`</article>`;
 }).join('');
}

export function render(){
 const d=store.data;
 const grantOptions=d.grants.map(g=>`<option value="${g.id}">${esc(g.name)}（可用 ${money(g.budget_cents-g.reserved_cents-g.spent_cents)}）</option>`).join('');
 return `<h2>专项经费台账</h2>
 <p class="intro">专项到账后按最新复核风险编制资助名单：高风险优先，同风险估价低者优先；每册整笔占款、不拆工序。确认前若发生复测、撤销或他笔先占款，整批名单作废需重新编制；已确认占款保留，落选原因随批次留痕。</p>
 ${d.grants.map(g=>`<section class="grant-strip"><div><small>${esc(g.code)}</small><h3>${esc(g.name)}</h3></div><dl><div><dt>预算</dt><dd>${money(g.budget_cents)}</dd></div><div><dt>占用</dt><dd>${money(g.reserved_cents)}</dd></div><div><dt>已核销</dt><dd>${money(g.spent_cents)}</dd></div><div><dt>可用</dt><dd>${money(g.budget_cents-g.reserved_cents-g.spent_cents)}</dd></div></dl></section>`).join('')}
 <div class="forms-two">
 <form id="batch-preview"><h3>编制资助名单 · 主管</h3><p class="hint">仅纳入已复核、仍在隔离柜且尚未占款的草案；按风险、估价排序并模拟整笔占款。</p>${field('专项经费',select('grant_id',grantOptions))}<button>生成本批名单（草案）</button></form>
 <form id="fund"><h3>单笔批准占款 · 主管</h3>${field('待占款方案',select('id',d.treatments.filter(t=>t.status==='draft').map(t=>`<option value="${t.id}">#${t.id} ${esc(t.name)} / ${money(t.estimated_cents)}</option>`).join('')))}${field('专项',select('grant_id',grantOptions))}<button>原子占用经费</button></form>
 </div>
 ${historySection(d)}
 <div class="forms-two"><form id="settle"><h3>核销施工费用 · 主管</h3>${field('施工中方案',select('id',d.treatments.filter(t=>t.status==='funded').map(t=>`<option value="${t.id}">#${t.id} ${esc(t.name)}</option>`).join('')))}${field('实际支出 / 元',input('yuan','number','2500','min="1" step="0.01"'))}<button>完成核销</button></form></div>
 <details><summary>设立新的专项经费</summary><form id="grant" class="inline-form">${field('编号',input('code','text','GF-2027'))}${field('用途',input('name','text','珍本应急修复'))}${field('预算 / 元',input('yuan','number','8000','min="1" step="0.01"'))}<button>设立专项</button></form></details>
 <h3>占款与核销流水</h3>${ledger(['方案','专项','占用','实际核销','状态'],d.encumbrances.map(e=>['#'+e.treatment_id,esc(d.grants.find(g=>g.id===e.grant_id)?.name),money(e.amount_cents),e.actual_cents===null?'—':money(e.actual_cents),badge(({held:'占用中',released:'已退回',settled:'已核销'})[e.status])]))}`;
}

export function bind(r){
 bindForm('batch-preview',d=>request('/grant-batches',{grant_id:+d.grant_id}),r);
 document.querySelectorAll('form[id^="confirm-batch-"]').forEach(form=>{
   bindForm(form.id,d=>request('/grant-batches/'+d.batch_id+'/confirm',{note:d.note||''}),r);
 });
 bindForm('fund',d=>request('/treatments/'+d.id+'/fund',{grant_id:+d.grant_id}),r);
 bindForm('settle',d=>request('/treatments/'+d.id+'/settle',{actual_cents:Math.round(+d.yuan*100)}),r);
 bindForm('grant',d=>request('/grants',{code:d.code,name:d.name,budget_cents:Math.round(+d.yuan*100)}),r);
}
