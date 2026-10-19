'use strict';
// Fragments are not sent to the host or as referrers. Remove the bearer token immediately.
let linkInvitation = new URLSearchParams(location.hash.slice(1)).get('invite');
let linkReferral = new URLSearchParams(location.hash.slice(1)).get('ref');
if (linkInvitation !== null || linkReferral !== null) history.replaceState(null, '', location.pathname + location.search);
const $ = id => document.getElementById(id);
const apiBase = document.querySelector('meta[name="fitme-api"]')?.content || '';
const previewOnly = apiBase === 'preview';
const apiUrl = path => `${previewOnly ? '' : apiBase}${path}`;
const state = { person: null, garments: [], current: null, jobs: [], config: {}, step: 'person', busy: false, requestId: null, poll: null, editingItem: null, importing: false };
const categories = ['Вещь','Футболка','Рубашка','Свитер','Худи','Куртка','Пальто','Джинсы','Брюки','Шорты','Юбка','Платье','Костюм','Кроссовки','Обувь','Кепка','Головной убор','Сумка','Очки','Аксессуар'];
const maxGarments = 6;
const newId = () => crypto.randomUUID ? crypto.randomUUID() : Array.from(crypto.getRandomValues(new Uint8Array(16)), n => n.toString(16).padStart(2, '0')).join('');
let draftDb;
let draftExpires = 0;
const retention = 24 * 3600 * 1000;

function message(text = '') { $('message').textContent = text; $('message').hidden = !text; }
async function api(path, options = {}) {
  if (previewOnly) {
    if (path === '/api/session') return { generation_enabled: false, remaining: 0, jobs: [], invite_required: false, retention_hours: 24 };
    if (path === '/api/photos' && options.method === 'DELETE') return { deleted: true };
    throw new Error('Онлайн-примерка ещё подключается. Пока можно проверить загрузку фото.');
  }
  let response, body;
  try {
    response = await fetch(apiUrl(path), { credentials: 'include', ...options, headers: { 'Content-Type': 'application/json', 'X-Fitme-Request': '1', ...(options.headers || {}) } });
    body = await response.json();
  } catch { throw new Error('Не удалось связаться с примерочной. Проверьте соединение; повторная отправка проверит тот же запрос.'); }
  if (!response.ok) throw new Error(body.error || 'Не удалось выполнить запрос.');
  return body;
}
function openDraftDb() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open('fitme-drafts', 1);
    req.onupgradeneeded = () => req.result.createObjectStore('drafts');
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}
async function draft(action, value) {
  if (!draftDb) return;
  return new Promise((resolve, reject) => {
    const tx = draftDb.transaction('drafts', action === 'get' ? 'readonly' : 'readwrite');
    const store = tx.objectStore('drafts');
    const request = action === 'get' ? store.get('current') : action === 'delete' ? store.delete('current') : store.put(value, 'current');
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}
async function saveDraft() {
  draftExpires = Date.now() + retention;
  try { await draft('put', { person: state.person, garments: state.garments.map(({id,image,category}) => ({id,image,category})), requestId: state.requestId, productUrl: $('product-url').value, created: draftExpires-retention }); }
  catch { /* Browser storage can be unavailable in private mode; keep this tab usable. */ }
}
function buttons() {
  $('to-garment').disabled = !state.person || state.busy || state.importing;
  $('generate').disabled = !state.person || !state.garments.length || state.busy || state.importing || !state.config.generation_enabled || (state.config.invite_required && !state.config.invited) || state.config.remaining === 0;
  $('garment-input').disabled = state.busy || state.importing || state.garments.length >= maxGarments;
  $('change-person').disabled = state.busy || state.importing;
  $('person-input').disabled = state.busy || state.importing;
  $('add-products').hidden = state.step !== 'garment';
  $('add-products').disabled = state.busy || state.importing || state.garments.length >= maxGarments;
  $('recognition-note').textContent = state.config.classification_enabled && (!state.config.invite_required || state.config.invited) ? 'Определим тип вещи автоматически. Нажмите на неё, чтобы изменить подпись.' : 'Нажмите на вещь, чтобы выбрать тип.';
  $('step-result').disabled = !state.current;
  $('invite-banner').hidden = !state.config.invite_required || state.config.invited;
  $('garment-invite').hidden = !state.config.invite_required || state.config.invited;
  renderProfile();
  $('generation-note').textContent = state.config.invite_required && !state.config.invited ? 'Введите приглашение на первом шаге.' : !state.config.generation_enabled ? 'Примерки пока выключены. Можно загрузить фото.' : state.config.remaining === 0 ? 'Тестовые попытки закончились.' : `Осталось попыток: ${state.config.remaining}. Одна кнопка — одна примерка.`;
}
function showStep(step) {
  if (state.busy && step !== 'result') return;
  state.step = step;
  document.querySelector('.app-shell').dataset.step = step;
  for (const name of ['person', 'garment', 'result']) {
    $(`${name}-panel`).hidden = name !== step;
    $(`step-${name}`).classList.toggle('active', name === step);
    $(`step-${name}`).setAttribute('aria-current', name === step ? 'step' : 'false');
  }
  $('mirror-badge').textContent = { person: 'Ваше фото', garment: 'Ваш новый образ', result: 'Ваш образ' }[step];
  $('change-person').hidden = !state.person || step === 'result';
  $('comparison').hidden = step !== 'result' || state.current?.status !== 'success';
  if (step !== 'result') setPhoto(state.person);
  buttons(); message();
}
function setPhoto(src) {
  $('main-photo').hidden = !src;
  $('empty-person').hidden = !!src;
  $('mirror-stage').classList.toggle('has-photo', !!src);
  $('main-photo').alt = 'Ваше фото в примерочной';
  if (src && $('main-photo').getAttribute('src') !== src) {
    $('main-photo').onload = () => {
      if (!window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
        $('main-photo').animate([{opacity:.35, transform:'scale(1.012)'}, {opacity:1, transform:'scale(1)'}], {duration:320, easing:'ease-out'});
      }
    };
    $('main-photo').src = src;
  }
  else if (!src) $('main-photo').removeAttribute('src');
}
function refreshUploads() {
  $('person-upload-label').textContent = state.person ? 'Заменить своё фото' : 'Выбрать своё фото';
  document.querySelector('.app-shell').classList.toggle('has-person', !!state.person);
  renderGarments();
  if (state.step !== 'result') setPhoto(state.person);
  buttons();
}
function renderGarments() {
  $('garment-list').replaceChildren();
  $('garment-count').textContent = `${state.garments.length} / ${maxGarments}`;
  $('garment-zone').classList.toggle('compact', state.garments.length > 0);
  $('garment-zone').hidden = state.garments.length >= maxGarments;
  for (const item of state.garments) {
    const button = document.createElement('button');
    button.type = 'button'; button.className = 'garment-tile'; button.disabled = state.busy;
    button.setAttribute('aria-label', `${item.category}: изменить или удалить`);
    const image = new Image(); image.src = item.image; image.alt = item.category;
    const label = document.createElement('span'); label.textContent = item.recognizing ? 'Определяем…' : item.category;
    button.classList.toggle('recognizing', !!item.recognizing); button.append(image, label);
    button.onclick = () => openItem(item.id);
    $('garment-list').append(button);
  }
  if (state.garments.length && state.garments.length < maxGarments) {
    const add = document.createElement('button'); add.type = 'button'; add.className = 'add-tile';
    add.disabled = state.busy || state.importing; add.setAttribute('aria-label', 'Добавить вещи');
    const plus = document.createElement('strong'); plus.textContent = '+';
    const label = document.createElement('span'); label.textContent = 'Добавить'; add.append(plus, label);
    add.onclick = () => $('garment-input').click(); $('garment-list').append(add);
  }
}
async function recognize(item) {
  if (!state.config.classification_enabled || (state.config.invite_required && !state.config.invited)) return;
  const source = item.image, requestId = state.requestId; const revision = item.revision = (item.revision || 0) + 1;
  item.recognizing = true; renderGarments();
  try {
    const result = await api('/api/classify', {method:'POST', body:JSON.stringify({image:source})});
    if (state.garments.includes(item) && item.image === source && item.revision === revision && state.requestId === requestId && categories.includes(result.category)) {
      item.category = result.category; state.requestId = null;
    }
  } catch { /* Manual labels keep the fitting usable when recognition is unavailable. */ }
  finally {
    if (item.revision === revision) item.recognizing = false;
    renderGarments();
    if (state.editingItem === item.id && $('item-dialog').open) updateItemDialog(item);
    await saveDraft();
  }
}
function updateItemDialog(item) {
  $('item-photo').src = item.image; $('item-category').value = item.category;
  $('item-recognition').textContent = item.recognizing ? 'Определяем тип… Можно выбрать вручную.' : item.category === 'Вещь' ? 'Выберите тип вещи, если он не определился.' : 'Подпись можно изменить.';
}
function openItem(id) {
  if (state.busy || state.importing) return;
  const item = state.garments.find(item => item.id === id); if (!item) return;
  state.editingItem = id; updateItemDialog(item); $('item-dialog').showModal();
}
for (const category of categories) {
  const option = document.createElement('option'); option.value = option.textContent = category; $('item-category').append(option);
}
$('item-close').onclick = () => $('item-dialog').close();
$('item-category').onchange = async () => {
  const item = state.garments.find(item => item.id === state.editingItem); if (!item || state.busy) return;
  item.revision = (item.revision || 0) + 1; item.recognizing = false; item.category = $('item-category').value;
  state.requestId = null; renderGarments(); updateItemDialog(item); await saveDraft();
};
$('item-remove').onclick = async () => {
  if (state.busy) return;
  state.garments = state.garments.filter(item => item.id !== state.editingItem);
  state.requestId = null; $('item-dialog').close(); refreshUploads(); await saveDraft();
};
async function selectFiles(kind, files) {
  if (state.busy || state.importing || !files.length) return;
  const selected = kind === 'person' ? files.slice(0, 1) : files;
  const available = maxGarments - state.garments.length;
  if (kind === 'garment' && selected.length > available) {
    message(`Можно добавить ещё ${available} фото. Выберите до ${maxGarments} вещей на образ.`); return;
  }
  state.importing = true; buttons(); renderGarments(); message();
  try {
    for (const file of selected) {
      const image = await prepareFile(file);
      if (!image) continue;
      state.requestId = null;
      if (kind === 'person') state.person = image;
      else {
        const item = {id:newId(), image, category:'Вещь'};
        state.garments.push(item); recognize(item);
      }
      refreshUploads(); await saveDraft();
    }
    if (kind === 'person') showStep('garment');
  } catch (error) { message(error.message); }
  finally { state.importing = false; buttons(); renderGarments(); }
}
async function loadImage(src) {
  const img = new Image();
  img.src = src;
  await img.decode();
  return img;
}
async function prepareFile(file) {
  if (!file) return null;
  if (file.size > 15 * 1024 * 1024) throw new Error('Это фото больше 15 МБ. Выберите файл поменьше.');
  if (!file.type.startsWith('image/') && !/\.(jpe?g|png|webp|heic|heif)$/i.test(file.name)) throw new Error('Выберите изображение JPG, PNG или WebP.');
  const url = URL.createObjectURL(file);
  try {
    let img;
    try { img = await loadImage(url); }
    catch { throw new Error(/hei[cf]/i.test(file.type + file.name) ? 'Этот браузер не открыл HEIC. Выберите JPG или сделайте скриншот фотографии.' : 'Не получилось открыть фото. Попробуйте JPG, PNG или WebP.'); }
    if (Math.min(img.naturalWidth, img.naturalHeight) < 128) throw new Error('Это изображение слишком маленькое. Нужно хотя бы 128 × 128 пикселей.');
    if (img.naturalWidth * img.naturalHeight > 25_000_000) throw new Error('Слишком большое разрешение. Выберите фото до 25 мегапикселей или его скриншот.');
    const ratio = Math.min(1, 1800 / Math.max(img.naturalWidth, img.naturalHeight));
    const canvas = document.createElement('canvas');
    canvas.width = Math.round(img.naturalWidth * ratio); canvas.height = Math.round(img.naturalHeight * ratio);
    const ctx = canvas.getContext('2d'); ctx.fillStyle = 'white'; ctx.fillRect(0, 0, canvas.width, canvas.height); ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
    return canvas.toDataURL('image/jpeg', .91);
  } finally { URL.revokeObjectURL(url); }
}
for (const kind of ['person', 'garment']) {
  $(`${kind}-input`).addEventListener('change', e => { selectFiles(kind, Array.from(e.target.files)); e.target.value = ''; });
  const zone = $(`${kind}-zone`);
  for (const event of ['dragenter', 'dragover']) zone.addEventListener(event, e => { e.preventDefault(); zone.classList.add('dragover'); });
  for (const event of ['dragleave', 'drop']) zone.addEventListener(event, e => { e.preventDefault(); zone.classList.remove('dragover'); });
  zone.addEventListener('drop', e => selectFiles(kind, Array.from(e.dataTransfer.files)));
}
$('to-garment').onclick = () => showStep('garment');
$('step-person').onclick = () => showStep('person');
$('step-garment').onclick = () => state.person ? showStep('garment') : message('Сначала добавьте своё фото.');
$('step-result').onclick = () => state.current && renderJob(state.current);
$('garment-invite').onclick = () => $('invite-dialog').showModal();
$('change-person').onclick = () => $('person-input').click();
$('add-products').onclick = () => $('garment-input').click();
$('product-url').oninput = () => { state.requestId = null; saveDraft(); };
function renderProfile() {
  const config = state.config;
  $('profile-remaining').textContent = Number.isFinite(config.remaining) ? String(config.remaining) : '—';
  $('profile-invited').textContent = Number.isFinite(config.referral_invited) ? String(config.referral_invited) : '—';
  $('profile-referral-stats').textContent = `Начали примерять: ${config.referral_activated || 0} · Начислено попыток: ${config.referral_bonus || 0}`;
  $('referral-share').hidden = !config.invited;
  $('profile-invite').hidden = !config.invite_required || config.invited;
  if (!config.invited) {
    $('referral-note').textContent = config.invite_required ? 'Примите приглашение, чтобы получить свою ссылку.' : 'Реферальная программа доступна участникам по приглашению.';
    $('referral-link-box').hidden = true;
  }
}
$('profile-invite').onclick = () => { $('history-dialog').close(); $('invite-dialog').showModal(); };
$('referral-share').onclick = async () => {
  const button = $('referral-share'); button.disabled = true;
  try {
    if (!$('referral-url').value) {
      const data = await api('/api/referral-link', {method:'POST', body:'{}'});
      $('referral-url').value = `${location.origin}${location.pathname}#ref=${encodeURIComponent(data.token)}`;
    }
    $('referral-link-box').hidden = false;
    try {
      await navigator.clipboard.writeText($('referral-url').value);
      $('referral-note').textContent = 'Ссылка скопирована. Отправьте её другу!';
    } catch {
      $('referral-url').focus(); $('referral-url').select();
      $('referral-note').textContent = 'Скопируйте выделенную ссылку и отправьте другу.';
    }
  } catch (error) { $('referral-note').textContent = error.message; }
  finally { button.disabled = false; }
};

async function session() {
  const info = await api('/api/session');
  state.config = info; state.jobs = info.jobs; renderRecent(); buttons();
  return info;
}
function renderRecent() {
  $('recent-section').hidden = state.jobs.length === 0;
  $('history-empty').hidden = state.jobs.length > 0;
  $('history-count').hidden = state.jobs.length === 0;
  $('history-count').textContent = state.jobs.length;
  $('recent-list').replaceChildren();
  for (const job of state.jobs) {
    const card = document.createElement('article'); card.className = 'outfit-card';
    const heading = document.createElement('div'); heading.className = 'outfit-heading';
    const title = document.createElement('strong'); title.textContent = 'Образ ' + new Date(job.created * 1000).toLocaleDateString('ru-RU', {day:'numeric',month:'short'});
    const count = document.createElement('span'); const n = job.garments?.length || 1; count.textContent = `${n} вещ${n === 1 ? 'ь' : n > 4 ? 'ей' : 'и'}`;
    heading.append(title, count); card.append(heading);
    const layout = document.createElement('div'); layout.className = 'outfit-layout';
    const button = document.createElement('button'); button.className = 'outfit-photo';
    button.setAttribute('aria-label', job.status === 'success' ? 'Открыть прошлую примерку' : 'Открыть состояние примерки');
    button.classList.toggle('selected', job.id === state.current?.id);
    if (job.status === 'success') { const image = new Image(); image.loading = 'lazy'; image.src = apiUrl(`/api/jobs/${job.id}/result`); image.alt = 'Предыдущая примерка'; button.append(image); }
    else { const label = document.createElement('span'); label.textContent = job.status === 'error' ? 'Не получилась' : 'Создаётся…'; button.append(label); }
    button.onclick = () => { if (state.busy && job.id !== state.current?.id) return; renderJob(job); $('history-dialog').close(); if (['queued', 'running'].includes(job.status)) pollJob(job.id); };
    const items = document.createElement('div'); items.className = 'outfit-items';
    for (const item of job.garments || [{category:'Вещь',index:0}]) {
      const tile = document.createElement('div'); tile.className = 'outfit-item';
      const image = new Image(); image.loading = 'lazy'; image.src = apiUrl(`/api/jobs/${job.id}/garments/${item.index}`); image.alt = item.category;
      const label = document.createElement('span'); label.textContent = item.category; tile.append(image, label); items.append(tile);
    }
    layout.append(button, items); card.append(layout);
    const hint = document.createElement('span'); hint.className = 'outfit-hint'; hint.textContent = job.status === 'success' ? 'Нажмите на фото · до / после' : job.status === 'error' ? 'Нажмите на фото, чтобы изменить образ' : 'Собираем ваш образ'; card.append(hint);
    $('recent-list').append(card);
  }
}
function renderJob(job) {
  state.current = job; state.busy = ['queued', 'running'].includes(job.status);
  renderGarments(); showStep('result');
  $('processing').hidden = !state.busy;
  $('waiting-card').hidden = !state.busy;
  $('result-actions').hidden = job.status !== 'success';
  $('error-actions').hidden = job.status !== 'error';
  $('result-title').textContent = state.busy ? 'Уже примеряем' : job.status === 'success' ? 'Ну как, ваше?' : 'Не получилось';
  $('result-eyebrow').textContent = job.status === 'success' ? 'НОВАЯ ВЕЩЬ. ВАШ ХАРАКТЕР.' : 'ВАША ПРИМЕРОЧНАЯ';
  $('result-description').textContent = state.busy ? 'Фото отправлены. Результат появится здесь автоматически.' : job.status === 'success' ? 'Сравните «До» и «После». Сохраните, если нравится.' : job.error;
  setPhoto(apiUrl(`/api/jobs/${job.id}/${job.status === 'success' ? 'result' : 'person'}`));
  $('main-photo').alt = job.status === 'success' ? 'Созданная виртуальная примерка' : 'Ваше исходное фото';
  if (job.status === 'success') {
    $('download').href = apiUrl(`/api/jobs/${job.id}/result?download=1`);
    $('shop-link').hidden = !job.product_url;
    if (job.product_url) $('shop-link').href = job.product_url;
    else $('shop-link').removeAttribute('href');
    document.querySelectorAll('[data-view]').forEach(b => b.classList.toggle('selected', b.dataset.view === 'after'));
    document.querySelectorAll('[data-rating]').forEach(b => b.classList.toggle('selected', b.dataset.rating === job.feedback));
    $('feedback-note').textContent = job.feedback ? 'Спасибо! Это поможет сделать примерку лучше.' : '';
  }
  buttons(); renderRecent();
}
async function pollJob(id) {
  clearTimeout(state.poll);
  try {
    const job = await api(`/api/jobs/${id}`);
    if (state.current?.id !== id) return;
    renderJob(job);
    if (state.busy) {
      const seconds = Math.max(0, Math.floor(Date.now()/1000 - job.created));
      $('elapsed').textContent = job.status === 'queued' ? 'Ваша примерка в очереди' : `Прошло ${seconds} сек. ${seconds > 60 ? 'Ещё работаем над изображением.' : ''}`;
      state.poll = setTimeout(() => pollJob(id), 1600);
    } else await session();
  } catch {
    if (state.current?.id !== id) return;
    message('Связь прервалась. Проверяем состояние — новую примерку не запускаем.');
    state.poll = setTimeout(() => pollJob(id), 4000);
  }
}
$('generate').onclick = async () => {
  if (state.busy || $('generate').disabled) return;
  const link = $('product-url').value.trim();
  if (link) {
    try { const u = new URL(link); if (!['http:', 'https:'].includes(u.protocol) || u.username || u.password) throw new Error(); }
    catch { message('Проверьте ссылку на магазин или оставьте поле пустым.'); return; }
  }
  state.busy = true; buttons(); message();
  $('generate').textContent = 'Отправляем фотографии…';
  state.requestId ||= crypto.randomUUID ? crypto.randomUUID() : Array.from(crypto.getRandomValues(new Uint8Array(16)), n => n.toString(16).padStart(2, '0')).join('');
  await saveDraft();
  try {
    const job = await api('/api/jobs', { method: 'POST', body: JSON.stringify({ person: state.person, garments: state.garments.map(({image,category}) => ({image,category})), product_url: link, request_id: state.requestId }) });
    state.current = job;
    await session(); renderJob(job); pollJob(job.id);
  } catch (error) {
    state.busy = false; buttons();
    message(error.message || 'Связь прервалась. Повторное нажатие проверит тот же запрос.');
  } finally { $('generate').replaceChildren(document.createTextNode('Примерить образ ')); const star = document.createElement('span'); star.textContent = '✳'; $('generate').append(star); }
};
document.querySelectorAll('[data-view]').forEach(button => button.onclick = () => {
  if (!state.current) return;
  setPhoto(apiUrl(`/api/jobs/${state.current.id}/${button.dataset.view === 'before' ? 'person' : 'result'}`));
  $('main-photo').alt = button.dataset.view === 'before' ? 'Ваше исходное фото' : 'Созданная виртуальная примерка';
  document.querySelectorAll('[data-view]').forEach(b => b.classList.toggle('selected', b === button));
});
document.querySelectorAll('[data-rating]').forEach(button => button.onclick = async () => {
  try {
    await api(`/api/jobs/${state.current.id}/feedback`, { method: 'POST', body: JSON.stringify({ rating: button.dataset.rating }) });
    state.current.feedback = button.dataset.rating;
    document.querySelectorAll('[data-rating]').forEach(b => b.classList.toggle('selected', b === button));
    $('feedback-note').textContent = 'Спасибо! Это поможет сделать примерку лучше.';
  } catch (error) { message(error.message); }
});
async function backToPhotos(clearGarment) {
  clearTimeout(state.poll);
  if (state.busy) return;
  if (state.current) {
    try {
      const job = state.current;
      const restore = async path => {
        const response = await fetch(apiUrl(path), {credentials:'include'});
        if (!response.ok) throw new Error();
        return prepareFile(await response.blob());
      };
      const [person, garments] = await Promise.all([
        restore(`/api/jobs/${job.id}/person`),
        clearGarment ? Promise.resolve([]) : Promise.all((job.garments || [{index:0,category:'Вещь'}]).map(async item => ({
          id:newId(), category:item.category, image:await restore(`/api/jobs/${job.id}/garments/${item.index}`)
        })))
      ]);
      state.person = person; state.garments = garments;
      $('product-url').value = clearGarment ? '' : job.product_url || '';
    } catch {
      message('Не удалось загрузить фотографии образа. Попробуйте ещё раз.'); return;
    }
  }
  if (clearGarment) { state.garments = []; $('product-url').value = ''; }
  state.requestId = null; state.busy = false;
  $('processing').hidden = true;
  refreshUploads(); showStep(state.person ? 'garment' : 'person'); await saveDraft(); await session();
}
$('another').onclick = () => backToPhotos(true);
$('retry-edit').onclick = () => backToPhotos(false);

for (const name of ['history', 'link', 'feedback', 'invite']) {
  $(`${name}-open`).onclick = () => { $(`${name}-dialog`).showModal(); if (name === 'history') { $('history-dialog').scrollTop = 0; session().catch(() => { $('referral-note').textContent = 'Не удалось обновить профиль. Закройте и откройте его ещё раз.'; }); $('history-open').classList.add('selected'); $('fitting-open').classList.remove('selected'); } };
  $(`${name}-close`).onclick = () => $(`${name}-dialog`).close();
}
$('invite-form').onsubmit = async event => {
  event.preventDefault();
  $('invite-submit').disabled = true;
  $('invite-note').textContent = '';
  try {
    await api('/api/invite', { method: 'POST', body: JSON.stringify({code: $('invite-code').value.trim()}) });
    $('invite-code').value = '';
    await session();
    $('invite-dialog').close();
  } catch (error) { $('invite-note').textContent = error.message; }
  finally { $('invite-submit').disabled = false; }
};
$('link-save').onclick = () => {
  if (!$('product-url').reportValidity()) return;
  $('link-open').textContent = $('product-url').value.trim() ? '↗ Ссылка на вещь добавлена' : '＋ Ссылка на вещь · необязательно';
  $('link-dialog').close();
};
$('history-dialog').addEventListener('close', () => { $('history-open').classList.remove('selected'); $('fitting-open').classList.add('selected'); });
$('fitting-open').onclick = () => { if (!state.busy) showStep(state.person ? 'garment' : 'person'); };
async function init() {
  try {
    try { draftDb = await openDraftDb(); const saved = await draft('get');
      if (saved && Date.now()-saved.created < retention) { state.person = saved.person; state.garments = (saved.garments || (saved.garment ? [{id:newId(),image:saved.garment,category:'Вещь'}] : [])).slice(0,maxGarments); state.requestId = saved.garments ? saved.requestId : null; draftExpires = saved.created + retention; $('product-url').value = saved.productUrl || ''; }
      else await draft('delete');
    } catch { /* No persistent browser storage; photos still work in this tab. */ }
    refreshUploads();
    let info = await session();
    if (linkInvitation !== null) {
      try {
        await api('/api/invite', {method:'POST', body:JSON.stringify({code:linkInvitation})});
        linkInvitation = null;
        info = await session();
        message('Приглашение принято. Добавьте своё фото, чтобы начать.');
      } catch (error) {
        $('invite-note').textContent = error.message + ' Откройте ссылку ещё раз для повтора или попросите новую у отправителя.';
        $('invite-dialog').showModal();
        linkInvitation = null;
      }
    }
    if (linkReferral !== null) {
      try {
        info = await api('/api/referral', {method:'POST', body:JSON.stringify({code:linkReferral})});
        info = await session();
        message('Приглашение принято. Запустите первую генерацию, чтобы активировать бонус для друга.');
      } catch (error) { message(error.message + ' Попросите друга отправить ссылку ещё раз.'); }
      linkReferral = null;
    }
    const pending = info.jobs.find(j => ['queued','running'].includes(j.status));
    const latest = info.jobs.find(j => j.request_id === state.requestId);
    if (pending) { renderJob(pending); pollJob(pending.id); }
    else if (latest) renderJob(latest);
    else showStep(state.person ? 'garment' : 'person');
  } catch { message('Не удалось подключиться к примерочной. Обновите страницу.'); }
}
init();
setInterval(async () => {
  if (!draftExpires || Date.now() < draftExpires || state.busy) return;
  draftExpires = 0;
  try { await draft('delete'); } catch { /* Retry clearing on the next visit. */ }
  state.person = state.requestId = null; state.garments = [];
  refreshUploads();
  if (state.step !== 'result') { showStep('person'); message('Срок хранения черновиков истёк. Добавьте фото заново.'); }
}, 60000);

// First-party activity for accepted invitations only; no background/idle time.
let activityVisit = null, activitySequence = 0, activitySeconds = 0;
let activityTick = performance.now(), lastInteraction = performance.now(), lastActivity = performance.now();
let activitySending = false;
for (const event of ['pointerdown','pointermove','keydown','touchstart','scroll']) {
  window.addEventListener(event, () => { lastInteraction = performance.now(); }, {passive:true});
}
function accountActivity() {
  const now = performance.now(), delta = Math.min(5, Math.max(0, (now-activityTick)/1000));
  if (!previewOnly && state.config.invited && document.visibilityState === 'visible' && document.hasFocus() && now-lastInteraction < 120000) {
    if (!activityVisit || now-lastActivity > 1800000) {
      activityVisit = Array.from(crypto.getRandomValues(new Uint8Array(16)), n=>n.toString(16).padStart(2,'0')).join('');
      activitySequence = 0;
    }
    activitySeconds += delta; lastActivity = now;
  }
  activityTick = now;
}
async function flushActivity() {
  accountActivity();
  if (!activityVisit || !activitySeconds || activitySending || !state.config.invited) return;
  const seconds = Math.min(30, activitySeconds); activitySeconds = 0; activitySending = true;
  try {
    await fetch(apiUrl('/api/activity'), {method:'POST',credentials:'include',keepalive:true,
      headers:{'Content-Type':'application/json','X-Fitme-Request':'1'},
      body:JSON.stringify({visit_id:activityVisit,sequence:++activitySequence,seconds})});
  } catch { /* Analytics failure must never interrupt a fitting or retry paid requests. */ }
  finally { activitySending = false; }
}
setInterval(accountActivity, 1000);
setInterval(flushActivity, 30000);
document.addEventListener('visibilitychange', flushActivity);
window.addEventListener('pagehide', flushActivity);
window.addEventListener('blur', flushActivity);
