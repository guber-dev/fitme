'use strict';
const $ = id => document.getElementById(id);
let archiveOffset=0, archiveRevision=0;
let ownerKey = new URLSearchParams(location.hash.slice(1)).get('admin');
if (location.hash) history.replaceState(null, '', location.pathname);
function notice(text='') { $('notice').textContent=text; $('notice').hidden=!text; }
function loggedIn(value) { $('login-panel').hidden=value; $('owner-panel').hidden=!value; if(!value){$('invite-url').value='';$('new-link').hidden=true;$('invitations').replaceChildren();$('users').replaceChildren();$('stats').replaceChildren();$('engagement').replaceChildren();$('generations').replaceChildren();$('photo-dialog').close();$('full-photo').removeAttribute('src');$('photo-download').removeAttribute('href');archiveRevision++;} }
async function api(path, body) {
  let response;
  try { response=await fetch('/api/admin/'+path,{method:body===undefined?'GET':'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-Fitme-Request':'1'},body:body===undefined?undefined:JSON.stringify(body)}); }
  catch { throw new Error('Нет соединения. Обновите список перед повторным созданием ссылки.'); }
  const result=await response.json();
  if(!response.ok) {if(response.status===401)loggedIn(false);throw new Error(result.error||'Не удалось выполнить запрос.');}
  return result;
}
async function refresh(){
  const [info, dashboard]=await Promise.all([api('invitations'),api('dashboard')]);loggedIn(true);renderDashboard(dashboard);
  $('balance').textContent=`${info.remaining_total} / ${info.total_limit}`;
  $('create-button').disabled=info.remaining_total===0;
  $('invitations').replaceChildren();
  for(const invite of info.invitations){
    const card=document.createElement('article');card.className='invitation';
    const title=document.createElement('strong');title.textContent=invite.label;
    const description=document.createElement('p');
    const expired=Date.now()>=invite.expires*1000;
    description.textContent=`${invite.revoked?'Отозвана':expired?'Срок истёк':'Активна'} · ${invite.used} из ${info.per_invite} попыток · до ${new Date(invite.expires*1000).toLocaleDateString('ru-RU')}`;
    card.append(title,description);
    if(!invite.revoked&&!expired){
      const revoke=document.createElement('button');revoke.className='secondary';revoke.textContent='Отозвать';
      revoke.onclick=async()=>{if(!confirm(`Отозвать приглашение «${invite.label}»? Новые примерки по нему будут недоступны.`))return;revoke.disabled=true;try{await api(`invitations/${invite.id}/revoke`,{});notice('Приглашение отозвано. Уже запущенная примерка может завершиться.');await refresh();}catch(e){notice(e.message);revoke.disabled=false;}};
      card.append(revoke);
    }
    $('invitations').append(card);
  }
  if(!info.invitations.length)$('invitations').textContent='Здесь появятся созданные ссылки. Старые коды друзей продолжают работать.';
  await refreshArchive();
}
async function login(key){await api('login',{key});await refresh();}
$('login-form').onsubmit=async e=>{e.preventDefault();$('login-button').disabled=true;notice();try{await login($('owner-key').value.trim());$('owner-key').value='';}catch(e){notice(e.message);}finally{$('login-button').disabled=false;}};
$('create-form').onsubmit=async e=>{
  e.preventDefault();$('create-button').disabled=true;notice();
  try{const invite=await api('invitations',{label:$('label').value.trim()});$('invite-url').value=`${location.origin}/#invite=${encodeURIComponent(invite.token)}`;$('new-link').hidden=false;$('label').value='';await refresh();}
  catch(e){notice(e.message);}finally{$('create-button').disabled=false;}
};
$('copy-button').onclick=async()=>{try{await navigator.clipboard.writeText($('invite-url').value);notice('Ссылка скопирована. Отправьте её другу.');}catch{$('invite-url').focus();$('invite-url').select();notice('Выделил ссылку — скопируйте её вручную.');}};
$('refresh').onclick=()=>refresh().catch(e=>notice(e.message));
$('logout').onclick=async()=>{try{await api('logout',{});loggedIn(false);notice('Вы вышли.');}catch(e){notice(e.message);}};


function element(tag, text, className) { const el=document.createElement(tag); if(text!==undefined)el.textContent=text;if(className)el.className=className;return el; }
function date(value) { return value?new Date(value*1000).toLocaleString('ru-RU'):'—'; }
function duration(value) { return value===null||value===undefined?'—':`${value.toFixed(1)} с`; }
function activeTime(value) { return value===null||value===undefined?'—':`${(value/60).toFixed(1)} мин`; }
function ratio(a,b) { return b?`${Math.round(100*a/b)}% (${a}/${b})`:'—'; }
function renderDashboard(info) {
  const e=info.engagement;
  $('engagement').replaceChildren();
  for(const [label,value] of [
    ['Среднее активное время',activeTime(e.average_active_seconds)],
    ['Начали примерять',ratio(e.activated_users,e.invited_users)],
    ['Повторили примерку',ratio(e.repeat_users,e.activated_users)],
    ['Успешные генерации',e.success_rate===null?'—':`${Math.round(e.success_rate*100)}%`],
    ['Нравится среди оценённых',ratio(e.helpful,e.rated)]
  ]) { const card=element('div',undefined,'stat');card.append(element('span',label),element('strong',value));$('engagement').append(card); }
  $('engagement-note').textContent=`Время измерено у ${e.measured_users} из ${e.invited_users} приглашённых · ${e.visits} визитов. «Начали» — среди вошедших по приглашению; «Повторили» — 2 и более генераций среди начавших. Успешность — среди сохранённых завершённых работ; оценки добровольные.`;
  $('stats').replaceChildren();
  for(const [key,label] of [['users','Пользователей'],['generations','Генераций'],['successful','Успешных'],['errors','Ошибок'],['pending','В процессе']]) {
    const card=element('div',undefined,'stat');card.append(element('span',label),element('strong',String(info.totals[key])));$('stats').append(card);
  }
  const selected=$('user-filter').value;
  $('user-filter').replaceChildren(new Option('Все пользователи',''));$('users').replaceChildren();
  for(const user of info.users) {
    $('user-filter').append(new Option(user.label,user.user_id));
    const row=element('tr'), cell=element('td'), button=element('button',user.label,'user-link');button.type='button';
    button.onclick=()=>{$('user-filter').value=user.user_id;archiveOffset=0;refreshArchive().catch(e=>notice(e.message));$('archive-panel').scrollIntoView({behavior:'smooth'});};
    cell.append(button);row.append(cell);
    for(const value of [user.generations,user.successful,duration(user.average_seconds),date(user.last_generation),user.errors,user.pending,activeTime(user.active_seconds),user.visits||0,date(user.last_seen)])row.append(element('td',String(value)));
    $('users').append(row);
  }
  $('user-filter').value=info.users.some(u=>u.user_id===selected)?selected:'';
  $('users-empty').hidden=info.users.length>0;
}
async function refreshArchive() {
  const revision=++archiveRevision;
  const query=new URLSearchParams({offset:String(archiveOffset)});
  if($('user-filter').value)query.set('user',$('user-filter').value);
  if($('status-filter').value)query.set('status',$('status-filter').value);
  const info=await api('generations?'+query);
  if(revision!==archiveRevision)return;
  if(info.total&&archiveOffset>=info.total){archiveOffset=0;return refreshArchive();}
  $('generations').replaceChildren();$('archive-count').textContent=`${info.total} примерок`;
  const statuses={success:'Успешно',error:'Ошибка',queued:'В очереди',running:'Генерируется'};
  const feedback={helpful:'Нравится',person:'Не похож(а) на себя',garment:'Вещь отличается',unhelpful:'Не помогло'};
  for(const job of info.jobs) {
    const card=element('article',undefined,'generation'), heading=element('div',undefined,'generation-heading'), title=element('div');
    title.append(element('strong',job.label),element('p',date(job.created)));heading.append(title,element('span',statuses[job.status]||job.status,'status '+job.status));card.append(heading);
    const photos=element('div',undefined,'photos');
    for(const [part,label] of [['person','Человек'],['garment','Товар'],['result','Результат']]){
      const figure=element('figure');figure.append(element('figcaption',label));
      if(job['has_'+part]) {
        const url=`/api/admin/generations/${job.id}/${part}`, button=element('button',undefined,'photo-button'), img=element('img');
        button.type='button';button.setAttribute('aria-label',`Открыть фото: ${label}`);img.src=url;img.alt=label;img.loading='lazy';button.append(img);
        button.onclick=()=>{$('full-photo').src=url;$('full-photo').alt=label;$('photo-title').textContent=`${job.label} · ${label}`;$('photo-download').href=url+'?download=1';$('photo-dialog').showModal();};figure.append(button);
      }else figure.append(element('div',job.status==='error'?'Нет результата':'Пока нет','photo-empty'));
      photos.append(figure);
    }
    card.append(photos);
    card.append(element('p',`Ожидание: ${duration(job.wait_elapsed)} · Генерация: ${duration(job.elapsed)} · Модель: ${job.model||'неизвестна'}`,'job-meta'));
    if(job.feedback)card.append(element('p','Оценка: '+(feedback[job.feedback]||job.feedback),'job-meta'));
    if(job.error)card.append(element('p',job.error,'job-error'));
    if(job.product_url){const link=element('a','Открыть товар ↗','job-meta');link.href=job.product_url;link.target='_blank';link.rel='noopener noreferrer';card.append(link);}
    if(job.prompt){const details=element('details');details.append(element('summary','Промпт этой генерации'),element('pre',job.prompt));card.append(details);}
    card.append(element('p','ID: '+job.id,'job-meta'));$('generations').append(card);
  }
  if(!info.jobs.length)$('generations').append(element('p','Нет примерок с выбранными фильтрами.','hint'));
  $('archive-prev').disabled=archiveOffset===0;$('archive-next').disabled=archiveOffset+info.limit>=info.total;
  $('archive-page').textContent=info.total?`${archiveOffset+1}–${Math.min(archiveOffset+info.limit,info.total)} из ${info.total}`:'0 примерок';
}
for(const id of ['user-filter','status-filter'])$(id).onchange=()=>{archiveOffset=0;refreshArchive().catch(e=>notice(e.message));};
$('archive-prev').onclick=()=>{archiveOffset=Math.max(0,archiveOffset-20);refreshArchive().catch(e=>notice(e.message));};
$('archive-next').onclick=()=>{archiveOffset+=20;refreshArchive().catch(e=>notice(e.message));};
$('refresh-dashboard').onclick=()=>refresh().catch(e=>notice(e.message));
$('photo-close').onclick=()=>{$('photo-dialog').close();};
$('photo-dialog').addEventListener('close',()=>{$('full-photo').removeAttribute('src');$('photo-download').removeAttribute('href');});

(async()=>{try{if(ownerKey){const key=ownerKey;ownerKey=null;await login(key);}else await refresh();}catch(e){if(e.message!=='Войдите с ключом владельца.')notice(e.message);}})();
