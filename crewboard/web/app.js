/* crewboard page.
   Left: live sessions and agents (from /hub.json), then the task list (from /progress.json).
   Center: the picked task (overview + its pages) or the picked session.
   Right: Now, the task or page the file's `focus` points at.
   Every save re-reads the latest file, bumps rev by exactly 1 and posts it; a 409 means someone wrote
   first, so it re-reads and tries again. */
'use strict';
const I={
  check:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>',
  cal:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="3.5" y="5" width="17" height="15.5" rx="2.5"/><path d="M3.5 10h17M8 3v4M16 3v4"/></svg>',
  subs:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M10 6h10M10 12h10M10 18h10M4 6l1 1 2-2M4 12l1 1 2-2M4 18l1 1 2-2"/></svg>',
  pop:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 4h6v6M20 4l-8.5 8.5M18 14v4.5a1.5 1.5 0 0 1-1.5 1.5h-11A1.5 1.5 0 0 1 4 18.5v-11A1.5 1.5 0 0 1 5.5 6H10"/></svg>',
  back:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M15 6l-6 6 6 6"/></svg>',
  book:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 5a2 2 0 0 1 2-2h13v16H6a2 2 0 0 0-2 2z"/><path d="M4 21V5"/></svg>',
  down:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M6 9l6 6 6-6"/></svg>'};
const STATUS=[['open','To do'],['doing','In progress'],['waiting','Waiting on you'],['done','Done']];
const GROUPS=[['doing','In progress'],['waiting','Waiting on you'],['open','To do'],['done','Done']];
const MON=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const pad=n=>String(n).padStart(2,'0');
const todayISO=()=>{const d=new Date();return d.getFullYear()+'-'+pad(d.getMonth()+1)+'-'+pad(d.getDate())};
const fmt=iso=>{if(!iso)return'';const[y,m,d]=iso.slice(0,10).split('-');return MON[+m-1]+' '+(+d)};
const days=(a,b)=>Math.round((Date.parse(b)-Date.parse(a))/864e5);
const notesOf=x=>Array.isArray(x.notes)?x.notes:(x.notes?[{date:'',text:String(x.notes)}]:[]);
const SINGLE=new URLSearchParams(location.search).get('id');
const store={get(k,d){try{const v=localStorage.getItem(k);return v===null?d:JSON.parse(v)}catch(e){return d}},
  set(k,v){try{localStorage.setItem(k,JSON.stringify(v))}catch(e){}}};

let doc={rev:0,items:[]},last='',selId=store.get('hub.sel',null),pageSel={},shut=store.get('hub.shut',{done:true,earlier:true}),view=store.get('hub.view','now');
function remember(){store.set('hub.view',view);store.set('hub.sel',selId);store.set('hub.shut',shut)}
function toast(t){const e=document.getElementById('toast');e.textContent=t;e.classList.add('show');clearTimeout(toast.t);toast.t=setTimeout(()=>e.classList.remove('show'),1600)}
function find(items,id){for(const it of items||[]){if(it.id===id)return it;const k=find(it.items,id);if(k)return k}return null}
function newId(){return 'i'+Date.now().toString(36)+Math.random().toString(36).slice(2,5)}
function setStatus(it,st){if(it.status===st)return;it.status=st;it.done=st==='done'?todayISO():null}
const newestFirst=ps=>(ps||[]).map((p,i)=>[p,i]).sort((a,b)=>((b[0].batch||0)-(a[0].batch||0))||(a[1]-b[1])).map(x=>x[0]);
const page=(tid,pid)=>(find(doc.items,tid)?.pages||[]).find(p=>p.id===pid);
const pending=pg=>pg&&pg.status!=='done'&&(pg.items||[]).some(q=>(pg.choices||[]).length&&!q.answer);
function modebar(){const it=selId&&find(doc.items,selId),s=live.sel&&live.by[live.sel];const label=s?s.title:it?it.title:'';
  return`<div class="modebar"><button class="${view==='now'?'on':''}" data-mode="now"><i class="live"></i>Now</button>${label?`<button class="${view==='task'?'on':''}" data-mode="task">${esc(label)}</button>`:''}</div>`}

/* ------------------------------------------------------------ the task list file */
async function load(){const t=await (await fetch('progress.json?'+Date.now(),{cache:'no-store'})).text();return[t,JSON.parse(t)]}
async function mutate(fn,msg){
  for(let tries=0;tries<3;tries++){
    const[,cur]=await load();fn(cur);cur.rev=(cur.rev||0)+1;cur.updated=new Date().toISOString().slice(0,16);
    const r=await fetch('progress',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(cur)});
    if(r.status===409)continue;
    if(!r.ok){toast('Could not save');return}
    await poll(true);if(msg)toast(msg);return}
  toast('Could not save, try again')}

function when(it){
  const t=todayISO();
  if(it.status==='done')return`<span>Done ${fmt(it.done)}</span>`;
  if(it.due){const n=days(t,it.due);
    if(n<0)return`<span class="late">${-n}d late</span>`;
    if(n===0)return`<span class="soon">Today</span>`;
    if(n===1)return`<span class="soon">Tomorrow</span>`;
    return`<span>${fmt(it.due)}</span>`}
  return''}
const checkBtn=it=>`<button class="check ${esc(it.status||'open')}" data-toggle="${esc(it.id)}" title="${it.status==='done'?'Mark not done':'Mark done'}">${it.status==='done'?I.check:''}</button>`;

/* ------------------------------------------------------------ left: the list */
function renderList(){
  const all=doc.items||[],its=all.filter(i=>!i.ref),refs=all.filter(i=>i.ref);let h='';
  for(const[s,label]of GROUPS){
    const g=its.filter(i=>(i.status||'open')===s).sort((a,b)=>s==='done'?(b.done||'').localeCompare(a.done||''):((a.due||'9999').localeCompare(b.due||'9999')||(a.added||'').localeCompare(b.added||'')));
    if(!g.length)continue;
    h+=`<button class="gh${shut[s]?' shut':''}" data-group="${s}">${I.down}${label}</button>`;
    if(!shut[s])h+=g.map(it=>`<div class="rrow${it.id===selId&&!live.sel?' sel':''}${it.status==='done'?' isdone':''}" data-sel="${esc(it.id)}">${checkBtn(it)}<span class="rt">${esc(it.title)}</span><span class="rd">${(it.pages||[]).some(pending)?'<i class="pdot" title="Has questions for you"></i>':''}${when(it)}</span></div>`).join('');
  }
  if(refs.length){
    h+=`<button class="gh${shut.lib?' shut':''}" data-group="lib">${I.down}Library</button>`;
    if(!shut.lib){const shelves=[...new Set(refs.map(r=>r.shelf||r.area||'Other'))];
      for(const a of shelves){h+=`<div class="shelf">${esc(a)}</div>`+refs.filter(r=>(r.shelf||r.area||'Other')===a).map(it=>`<div class="rrow lib${it.id===selId&&!live.sel?' sel':''}" data-sel="${esc(it.id)}"><span class="bk">${I.book}</span><span class="rt">${esc(it.title)}</span></div>`).join('')}}
  }
  document.getElementById('list').innerHTML=h}

/* ------------------------------------------------------------ center: a task */
function subRow(k){
  const st=k.status||'open',note=notesOf(k).map(n=>`<span class="sn">${esc(n.text)}</span>`).join('');
  return`<li class="sub${st==='done'?' isdone':''}">${k.id?checkBtn(k):`<span class="check ${esc(st)}">${st==='done'?I.check:''}</span>`}<span class="st"><span class="stt">${esc(k.title)}</span>${note}</span>${k.done?`<span class="dd">${fmt(k.done)}</span>`:''}</li>`}
function overview(it){
  const st=it.status||'open';let h='';
  if(it.what)h+=`<p class="what">${esc(it.what)}</p>`;
  if(!it.ref)h+=`<div class="fields"><div class="seg">${STATUS.map(([v,l])=>`<button class="${st===v?'on':''}" data-status="${esc(it.id)}" data-v="${v}">${l}</button>`).join('')}</div>`
    +`<label class="field">Due <input type="date" value="${esc(it.due||'')}" data-due="${esc(it.id)}"></label>`
    +(it.due?`<button class="clear" data-cleardue="${esc(it.id)}">Clear</button>`:'')+`</div>`;
  const subs=it.items||[];
  h+=`<div class="sect">Subtasks</div>${subs.length?`<ul class="subs">${subs.map(subRow).join('')}</ul>`:''}`
    +`<div class="add"><input placeholder="Add a subtask" data-addsub="${esc(it.id)}"></div>`;
  const notes=notesOf(it);
  h+=`<div class="sect">Notes</div>${notes.length?`<ul class="notes">${notes.slice().reverse().map(n=>`<li><time>${fmt(n.date)}</time>${esc(n.text)}</li>`).join('')}</ul>`:''}`
    +`<div class="add"><input placeholder="Add a note" data-addnote="${esc(it.id)}"></div>`;
  return h}
/* Page kinds: "raw" shows trusted HTML written by your own agents; anything else is an ask page:
   a list of lines, each answered with one of the page's choices, plus comments. */
function pageBody(it,pg){
  if(pg.kind==='raw')return`<div class="raw">${pg.html||''}</div>`;
  const ch=pg.choices||[],qs=pg.items||[],k=it.id+'|'+pg.id,ans=qs.filter(q=>q.answer).length;
  return (pg.text?`<p class="what">${esc(pg.text)}</p>`:'')
   +qs.map(q=>`<div class="q"><div class="line">${esc(q.line)}</div>${q.sub?`<div class="qs">${esc(q.sub)}</div>`:''}`
     +(ch.length?`<div class="seg">${ch.map(c=>`<button class="${q.answer===c?'on':''}" data-ans="${esc(k+'|'+q.id)}" data-v="${esc(c)}">${esc(c)}</button>`).join('')}</div>`:'')+`</div>`).join('')
   +`<div class="add"><input placeholder="Add a comment" data-pgcomment="${esc(k)}"></div>`
   +((pg.comments||[]).length?`<ul class="cmts">${pg.comments.slice().reverse().map(c=>`<li><time>${fmt(c.date)}</time>${esc(c.text)}</li>`).join('')}</ul>`:'')
   +`<div class="pgfoot"><span>${ch.length&&qs.length?`${ans} of ${qs.length} answered`:''}</span><button class="btn" data-pgdone="${esc(k)}">${pg.status==='done'?'Reopen':'Mark done'}</button></div>`}
function renderTask(m){
  const it=selId&&find(doc.items,selId);
  if(!it){m.innerHTML=`<div class="mhead">${SINGLE?'':modebar()}</div><div class="empty">Pick a task or a session on the left.</div>`;return}
  document.title=it.title;
  const pages=newestFirst(it.pages),cur=pageSel[it.id]||'overview',pg=pages.find(x=>x.id===cur);
  const subs=it.items||[],sd=subs.filter(k=>k.status==='done').length,w=when(it).replace(/<[^>]+>/g,'');
  m.innerHTML=`<div class="mhead">`+(SINGLE?`<a class="back" href="./">${I.back}All tasks</a>`:modebar())
   +`<div class="thead"><h2>${esc(it.title)}</h2>${SINGLE?'':`<a class="pop" href="?id=${encodeURIComponent(it.id)}" target="_blank" rel="noopener" title="Open in a new tab">${I.pop}</a>`}</div>`
   +`<div class="tmeta">${it.area?`<span class="area">${esc(it.area)}</span>`:''}${subs.length?`<span>${I.subs}${sd}/${subs.length}</span>`:''}${w?`<span>${I.cal}${esc(w)}</span>`:''}${it.added?`<span>Added ${fmt(it.added)}</span>`:''}</div>`
   +`<div class="ptabs"><button class="${!pg?'on':''}" data-ptab="${esc(it.id)}" data-pg="overview">Overview</button>`
   +pages.map(x=>`<button class="${pg&&pg.id===x.id?'on':''}" data-ptab="${esc(it.id)}" data-pg="${esc(x.id)}">${esc(x.title)}${pending(x)?'<i class="pdot"></i>':''}</button>`).join('')+`</div></div>`
   +`<div class="mbody">`+(pg?pageBody(it,pg):overview(it))+`</div>`}

/* ------------------------------------------------------------ right: what you are talking about now */
let nowPage=null,focusKey='';
function renderNow(){
  const el=document.getElementById('now'),f=doc.focus||{},it=f.task&&find(doc.items,f.task);
  const fk=(f.task||'')+'|'+(f.page||'');if(fk!==focusKey){focusKey=fk;nowPage=null}
  const pgid=nowPage||f.page,pg=it&&pgid&&page(f.task,pgid);
  if(!it){el.innerHTML=modebar()+`<div class="nowempty">Nothing in focus.</div>`;return}
  const tabs=(it.pages||[]).length>1?`<div class="ptabs">${newestFirst(it.pages).map(x=>`<button class="${pg&&pg.id===x.id?'on':''}" data-nowpg="${esc(x.id)}">${esc(x.title)}${pending(x)?'<i class="pdot"></i>':''}</button>`).join('')}</div>`:'';
  el.innerHTML=modebar()+`<button class="nowtask" data-sel="${esc(it.id)}">${esc(it.title)}</button>`
   +tabs+(pg?`<h3>${esc(pg.title)}</h3>${pageBody(it,pg)}`:`<h3>${esc(f.title||it.title)}</h3>${f.text?`<p class="what">${esc(f.text)}</p>`:''}`)}

/* ------------------------------------------------------------ live sessions and agents */
const STATE={working:'Working',waiting:'Waiting on you',idle:'Idle',done:'Finished',stopped:'Stopped'};
const RANK={working:0,waiting:1,stopped:2,idle:3,done:4};
const SRC={claude:'Claude Code',codex:'Codex',error:'Live feed'};
const HOT=3*3600e3,FRESH=30*60e3;
const live={items:[],by:{},kids:{},tops:[],text:'',ok:true,loaded:false,sel:store.get('hub.live',null),tab:{},open:new Set(),more:new Set()};
function clock(ms){if(!ms)return'';const d=new Date(ms),t=d.toLocaleTimeString('en-US',{hour:'numeric',minute:'2-digit'});
  return new Date().toDateString()===d.toDateString()?t:MON[d.getMonth()]+' '+d.getDate()+', '+t}
function ago(ms){if(!ms)return'';const s=Math.max(0,Date.now()-ms)/1000;
  if(s<45)return'now';if(s<3600)return Math.round(s/60)+'m';if(s<86400){const h=Math.floor(s/3600),m=Math.round((s%3600)/60);return h+'h'+(h<10&&m?' '+m+'m':'')}
  return Math.round(s/86400)+'d'}
const byRank=(a,b)=>(RANK[a.state]??5)-(RANK[b.state]??5)||(b.last||0)-(a.last||0);
function index(){live.by={};live.kids={};const tops=[];live.items.forEach(i=>live.by[i.id]=i);
  live.items.forEach(i=>{if(i.parent&&live.by[i.parent])(live.kids[i.parent]=live.kids[i.parent]||[]).push(i);else tops.push(i)});
  live.tops=tops.sort(byRank)}
const activeNow=i=>i.state==='working'||i.state==='waiting';
const busy=i=>activeNow(i)||(live.kids[i.id]||[]).some(busy);
const holds=i=>i.id===live.sel||(live.kids[i.id]||[]).some(holds);
function srow(it,top){
  const line=it.state==='working'&&it.now?it.now.text:'';
  return`<button class="hubrow${live.sel===it.id?' sel':''}${!activeNow(it)&&Date.now()-(it.last||0)>FRESH?' quiet':''}" data-live="${esc(it.id)}" title="${esc(it.title)}">`
   +`<span class="sd"><i class="sdot ${esc(it.state)}"></i></span><span class="tx"><span class="rt">${esc(it.title)}</span>${line?`<span class="rn">${esc(line)}</span>`:''}</span>`
   +`<span class="rd">${top&&it.src==='codex'?'<span class="src">Codex</span>':''}<span title="${esc(clock(it.last))}">${ago(it.last)}</span></span></button>`}
function branch(it,top){
  let h=srow(it,top);const ks=(live.kids[it.id]||[]).slice().sort(byRank);if(!ks.length)return h;
  const show=k=>busy(k)||Date.now()-(k.last||0)<FRESH||holds(k);
  const vis=live.open.has(it.id)?ks:ks.filter(show),hid=ks.length-vis.length;
  if(!vis.length&&!hid)return h;
  h+=`<div class="hubkids">`+vis.map(k=>branch(k,false)).join('');
  if(hid>0)h+=`<button class="hubmore" data-hubopen="${esc(it.id)}">Show finished agents</button>`;
  else if(live.open.has(it.id)&&ks.some(k=>!show(k)))h+=`<button class="hubmore" data-hubopen="${esc(it.id)}">Hide finished agents</button>`;
  return h+`</div>`}
function renderLive(){
  const hot=live.tops.filter(i=>busy(i)||Date.now()-(i.last||0)<HOT||holds(i)),cold=live.tops.filter(i=>!hot.includes(i));
  let h=`<button class="gh${shut.active?' shut':''}" data-group="active">${I.down}Active</button>`;
  if(!shut.active){
    h+=hot.map(t=>branch(t,true)).join('');
    if(!hot.length)h+=`<div class="hubmore" style="cursor:default">${!live.loaded?'':live.ok?'Nothing running':'Live feed not answering'}</div>`}
  if(cold.length){h+=`<button class="gh${shut.earlier?' shut':''}" data-group="earlier">${I.down}Earlier today</button>`;
    if(!shut.earlier)h+=cold.map(t=>branch(t,true)).join('')}
  document.getElementById('live').innerHTML=h}
function textBlock(label,m,cls,key){if(!m||!m.text)return'';
  const long=m.text.length>480||m.text.split('\n').length>8,o=live.more.has(key);
  return`<div class="sect"><span>${label}</span><time title="${esc(clock(m.ts))}">${clock(m.ts)}</time></div>`
   +`<div class="msg ${cls}${long&&!o?' clamp':''}">${esc(m.text)}</div>${long?`<button class="link" data-hubmore="${esc(key)}">${o?'Less':'More'}</button>`:''}`}
function renderSession(m,it){
  const par=it.parent&&live.by[it.parent],ks=(live.kids[it.id]||[]).slice().sort(byRank);
  const act=it.activity||[],files=it.files||[],cur=live.tab[it.id]||'overview';
  const tabs=[['overview','Overview'],...(act.length?[['steps','Steps']]:[]),...(files.length?[['files','Files']]:[])];
  const t=tabs.some(x=>x[0]===cur)?cur:'overview';
  let h=`<div class="hs">${modebar()}<div class="thead"><h2>${esc(it.title)}</h2></div><div class="tmeta">`
   +`<span class="st"><i class="sdot ${esc(it.state)}"></i>${STATE[it.state]||esc(it.state)}</span>`
   +`<span>${SRC[it.src]||''}${it.project?' / '+esc(it.project):''}</span>`
   +(par?`<span>Agent of <button data-live="${esc(par.id)}">${esc(par.title)}</button></span>`:'')
   +(it.last?`<span title="${esc(clock(it.last))}">Active ${ago(it.last)==='now'?'just now':ago(it.last)+' ago'}</span>`:'')
   +(it.started?`<span>Started ${esc(clock(it.started))}</span>`:'')
   +(it.model?`<span class="mono">${esc(it.model)}</span>`:'')+`</div>`
   +`<div class="ptabs">${tabs.map(([k,l])=>`<button class="${t===k?'on':''}" data-hubtab="${esc(it.id)}" data-v="${k}">${l}</button>`).join('')}</div>`;
  if(t==='steps'){
    h+=`<ul class="tl">${act.map(a=>`<li><time title="${esc(clock(a.ts))}">${ago(a.ts)}</time><span>${esc(a.text)}</span></li>`).join('')}</ul>`}
  else if(t==='files'){
    h+=`<ul class="tl">${files.map(f=>{const p=f.p,i=Math.max(p.lastIndexOf('\\'),p.lastIndexOf('/')),b=p.slice(i+1),d=p.slice(0,i+1);
      return`<li title="${esc(p)}"><time>${ago(f.ts)}</time><span class="fp"><span class="fb">${esc(b)}</span><span class="fd">${esc(d)}</span></span></li>`}).join('')}</ul>`}
  else{
    if(it.summary)h+=`<p class="what">${esc(it.summary)}</p>`;
    if(it.now&&it.now.text)h+=`<div class="nowline"><i class="sdot ${esc(it.state)}"></i><div class="nt">${esc(it.now.text)}</div><time>${ago(it.now.ts)}</time></div>`;
    h+=it.kind==='agent'?textBlock('Task',it.task,'you',it.id+':t')+textBlock('Your message',it.you,'you',it.id+':y'):textBlock('You said',it.you,'you',it.id+':y');
    h+=textBlock(it.state==='working'?'Latest reply':'Last reply',it.reply,'',it.id+':r');
    const td=it.todos||[];
    if(td.length)h+=`<div class="sect"><span>Its to-do list</span></div><ul class="plan subs">`
      +td.map(x=>`<li class="sub ${esc(x.s)}"><i class="ck"></i><span class="st"><span class="stt">${esc(x.t)}</span></span></li>`).join('')+`</ul>`;
    if(ks.length){const vis=ks.filter(k=>busy(k)||Date.now()-(k.last||0)<HOT),list=vis.length?vis:ks.slice(0,6);
      h+=`<div class="sect"><span>Agents</span></div><div class="ags">`
       +list.map(k=>`<button class="ag" data-live="${esc(k.id)}"><i class="sdot ${esc(k.state)}"></i><span class="tx"><span class="kt">${esc(k.title)}</span><span class="kn">${esc(k.state==='working'&&k.now?k.now.text:STATE[k.state])}</span></span><time>${ago(k.last)}</time></button>`).join('')+`</div>`}
  }
  m.innerHTML=h+`</div>`;document.title=it.title}

/* ------------------------------------------------------------ render and poll */
function renderMain(){
  const m=document.getElementById('main'),s=live.sel&&live.by[live.sel];
  if(live.sel&&!s&&live.loaded){live.sel=null;store.set('hub.live',null)}
  document.body.classList.toggle('live-on',!!s);
  if(s)return renderSession(m,s);
  renderTask(m)}
function render(){
  if(SINGLE){document.body.classList.add('single');selId=SINGLE;renderTask(document.getElementById('main'));return}
  if(!selId||!find(doc.items,selId))selId=(doc.focus&&doc.focus.task)||((doc.items||[]).find(i=>i.status==='doing')||{}).id||null;
  document.body.classList.toggle('view-now',view==='now');document.body.classList.toggle('view-task',view!=='now');
  renderLive();renderList();renderMain();renderNow()}
const typing=()=>{const a=document.activeElement;return a&&(a.tagName==='INPUT'||a.tagName==='SELECT'||a.tagName==='TEXTAREA')};
async function poll(force){
  if(!force&&typing())return;
  try{const[t,d]=await load();if(force||t!==last){last=t;doc=d;render()}}catch(e){}}
async function pollLive(){
  if(SINGLE)return;
  try{const r=await fetch('hub.json',{cache:'no-store'});live.ok=r.ok;const t=await r.text();if(t!==live.text){live.text=t;live.items=JSON.parse(t).items||[];index()}}catch(e){live.ok=false}
  live.loaded=true;renderLive();if(live.sel&&!typing()){renderMain()}}
function pickSession(id){live.sel=id;store.set('hub.live',id);view='task';remember();render();
  if(!matchMedia('(min-width:1181px)').matches)document.getElementById('main').scrollIntoView({behavior:'smooth'})}

document.addEventListener('click',e=>{
  if(e.target.closest('a'))return;
  const t=e.target.closest('button,[data-sel]');if(!t)return;
  const d=t.dataset;
  if(d.live){pickSession(d.live);return}
  if(d.hubopen){live.open.has(d.hubopen)?live.open.delete(d.hubopen):live.open.add(d.hubopen);renderLive();return}
  if(d.hubtab){live.tab[d.hubtab]=d.v;renderMain();return}
  if(d.hubmore){live.more.has(d.hubmore)?live.more.delete(d.hubmore):live.more.add(d.hubmore);renderMain();return}
  if(d.toggle){e.stopPropagation();const id=d.toggle;mutate(c=>{const it=find(c.items,id);setStatus(it,it.status==='done'?'open':'done')});return}
  if(d.nowpg){nowPage=d.nowpg;renderNow();return}
  if(d.mode){view=d.mode;remember();render();return}
  if(d.sel){const changed=selId!==d.sel||live.sel;selId=d.sel;live.sel=null;store.set('hub.live',null);view='task';remember();render();
    if(changed)document.getElementById('main').scrollTop=0;if(!matchMedia('(min-width:1181px)').matches)document.getElementById('main').scrollIntoView({behavior:'smooth'});return}
  if(d.group){shut[d.group]=!shut[d.group];remember();renderLive();renderList();return}
  if(d.ptab){pageSel[d.ptab]=d.pg;renderMain();document.getElementById('main').scrollTop=0;return}
  if(d.status){mutate(c=>setStatus(find(c.items,d.status),d.v));return}
  if(d.cleardue){const id=d.cleardue;mutate(c=>{const it=find(c.items,id);(it.notes=notesOf(it)).push({date:todayISO(),text:'Due date removed (was '+fmt(it.due)+').'});it.due=null},'Due date cleared');return}
  if(d.ans){const[tid,pid,qid]=d.ans.split('|'),v=d.v;mutate(c=>{const q=find(c.items,tid).pages.find(x=>x.id===pid).items.find(x=>x.id===qid);q.answer=q.answer===v?null:v;q.answered=todayISO()});return}
  if(d.pgdone){const[tid,pid]=d.pgdone.split('|');mutate(c=>{const pg=find(c.items,tid).pages.find(x=>x.id===pid);pg.status=pg.status==='done'?'open':'done';pg.done=pg.status==='done'?todayISO():null});return}
  if(t.id==='newbtn'){const r=document.getElementById('newrow');r.classList.toggle('show');if(r.classList.contains('show'))document.getElementById('newtitle').focus();return}
  if(t.id==='railbtn'||t.id==='railopen'){const off=t.id==='railbtn';document.body.classList.toggle('rail-off',off);store.set('hub.railOff',off);return}
});
document.addEventListener('change',e=>{const t=e.target;
  if(t.dataset.due){const id=t.dataset.due,v=t.value||null;mutate(c=>{const it=find(c.items,id);
    if(it.due&&v&&it.due!==v)(it.notes=notesOf(it)).push({date:todayISO(),text:'Due moved from '+fmt(it.due)+' to '+fmt(v)+'.'});
    it.due=v},v?'Due '+fmt(v):'Due date cleared')}});
document.addEventListener('keydown',e=>{const t=e.target;
  if(e.key==='Escape'&&t.id==='newtitle'){t.value='';document.getElementById('newrow').classList.remove('show');t.blur();return}
  if(e.key==='Escape'&&live.sel&&!typing()){live.sel=null;store.set('hub.live',null);render();return}
  if(e.key!=='Enter'||!t.value||!t.value.trim())return;const v=t.value.trim();
  if(t.id==='newtitle'){const id=newId();t.value='';document.getElementById('newrow').classList.remove('show');
    mutate(c=>{c.items.push({id,title:v,status:'open',added:todayISO(),due:null,done:null})},'Task added').then(()=>{selId=id;live.sel=null;view='task';remember();render()});return}
  if(t.dataset.pgcomment){const[tid,pid]=t.dataset.pgcomment.split('|');t.value='';mutate(c=>{const pg=find(c.items,tid).pages.find(x=>x.id===pid);(pg.comments=pg.comments||[]).push({date:todayISO(),text:v})},'Comment added');return}
  if(t.dataset.addnote){const id=t.dataset.addnote;t.value='';mutate(c=>{const it=find(c.items,id);(it.notes=notesOf(it)).push({date:todayISO(),text:v})},'Note added');return}
  if(t.dataset.addsub){const id=t.dataset.addsub;t.value='';mutate(c=>{const it=find(c.items,id);(it.items=it.items||[]).push({id:newId(),title:v,status:'open',added:todayISO(),due:null,done:null})},'Subtask added')}
});
if(store.get('hub.railOff',false))document.body.classList.add('rail-off');
{const q=new URLSearchParams(location.search),th=q.get('theme');if(th==='dark'||th==='light')document.documentElement.dataset.theme=th;
  const h=decodeURIComponent(location.hash.slice(1));if(h){if(/^[cax]:/.test(h)){live.sel=h;view='task'}else{selId=h;live.sel=null;view='task'}}}
poll(true);pollLive();setInterval(poll,3000);setInterval(pollLive,2500);
