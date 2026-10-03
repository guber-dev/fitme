'use strict';
const $ = id => document.getElementById(id);
let ownerKey = new URLSearchParams(location.hash.slice(1)).get('admin');
if (location.hash) history.replaceState(null, '', location.pathname);
function notice(text='') { $('notice').textContent=text; $('notice').hidden=!text; }
function loggedIn(value) { $('login-panel').hidden=value; $('owner-panel').hidden=!value; if(!value){$('invite-url').value='';$('new-link').hidden=true;$('invitations').replaceChildren();} }
async function api(path, body) {
  let response;
  try { response=await fetch('/api/admin/'+path,{method:body===undefined?'GET':'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-Fitme-Request':'1'},body:body===undefined?undefined:JSON.stringify(body)}); }
  catch { throw new Error('Нет соединения. Обновите список перед повторным созданием ссылки.'); }
  const result=await response.json();
  if(!response.ok) {if(response.status===401)loggedIn(false);throw new Error(result.error||'Не удалось выполнить запрос.');}
  return result;
}
async function refresh(){
  const info=await api('invitations');loggedIn(true);
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
(async()=>{try{if(ownerKey){const key=ownerKey;ownerKey=null;await login(key);}else await refresh();}catch(e){if(e.message!=='Войдите с ключом владельца.')notice(e.message);}})();
