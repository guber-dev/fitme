'use strict';
// Fragments are not sent to the host or as referrers. Remove the bearer token immediately.
let linkInvitation = new URLSearchParams(location.hash.slice(1)).get('invite');
if (linkInvitation !== null) history.replaceState(null, '', location.pathname + location.search);
const $ = id => document.getElementById(id);
const apiBase = document.querySelector('meta[name="fitme-api"]')?.content || '';
const previewOnly = apiBase === 'preview';
const apiUrl = path => `${previewOnly ? '' : apiBase}${path}`;
const state = { person: null, garment: null, current: null, jobs: [], config: {}, step: 'person', busy: false, requestId: null, poll: null, crop: null };
let draftDb;
let draftExpires = 0;
const retention = 24 * 3600 * 1000;

function message(text = '') { $('message').textContent = text; $('message').hidden = !text; }
async function api(path, options = {}) {
  if (previewOnly) {
    if (path === '/api/session') return { generation_enabled: false, remaining: 0, jobs: [], invite_required: false, retention_hours: 24 };
    if (path === '/api/photos' && options.method === 'DELETE') return { deleted: true };
    throw new Error('Онлайн-примерка ещё подключается. Пока можно проверить загрузку и обрезку фото.');
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
  try { await draft('put', { person: state.person, garment: state.garment, requestId: state.requestId, productUrl: $('product-url').value, created: draftExpires-retention }); }
  catch { /* Browser storage can be unavailable in private mode; keep this tab usable. */ }
}
function buttons() {
  $('to-garment').disabled = !state.person || state.busy;
  $('generate').disabled = !state.person || !state.garment || !$('consent').checked || state.busy || !state.config.generation_enabled || (state.config.invite_required && !state.config.invited) || state.config.remaining === 0;
  $('step-result').disabled = !state.current;
  $('invite-banner').hidden = !state.config.invite_required || state.config.invited;
  $('generation-note').textContent = state.config.invite_required && !state.config.invited ? 'Введите приглашение на первом шаге.' : !state.config.generation_enabled ? 'Примерки пока выключены. Можно загрузить и обрезать фото.' : state.config.remaining === 0 ? 'Тестовые попытки закончились.' : `Осталось попыток: ${state.config.remaining}. Одна кнопка — одна примерка.`;
}
function showStep(step) {
  if (state.busy && step !== 'result') return;
  state.step = step;
  for (const name of ['person', 'garment', 'result']) {
    $(`${name}-panel`).hidden = name !== step;
    $(`step-${name}`).classList.toggle('active', name === step);
    $(`step-${name}`).setAttribute('aria-current', name === step ? 'step' : 'false');
  }
  $('mirror-badge').textContent = { person: '01 / Ваше фото', garment: '02 / Ваша вещь', result: '03 / Ваш образ' }[step];
  $('comparison').hidden = step !== 'result' || state.current?.status !== 'success';
  if (step !== 'result') setPhoto(state.person);
  buttons(); message();
}
function setPhoto(src) {
  $('main-photo').hidden = !src;
  $('empty-person').hidden = !!src;
  $('mirror-stage').classList.toggle('has-photo', !!src);
  if (src) $('main-photo').src = src;
  else $('main-photo').removeAttribute('src');
}
function refreshUploads() {
  $('person-upload-label').textContent = state.person ? 'Заменить своё фото' : 'Выбрать своё фото';
  $('garment-zone').hidden = !!state.garment;
  $('garment-preview').hidden = !state.garment;
  if (state.garment) $('garment-thumb').src = state.garment;
  else $('garment-thumb').removeAttribute('src');
  if (state.step !== 'result') setPhoto(state.person);
  buttons();
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
async function selectFile(kind, file) {
  if (state.busy) return;
  message();
  try {
    const data = await prepareFile(file);
    if (!data) return;
    state[kind] = data; state.requestId = null;
    refreshUploads(); await saveDraft();
  } catch (error) { message(error.message); }
}
for (const kind of ['person', 'garment']) {
  $(`${kind}-input`).addEventListener('change', e => { selectFile(kind, e.target.files[0]); e.target.value = ''; });
  const zone = $(`${kind}-zone`);
  for (const event of ['dragenter', 'dragover']) zone.addEventListener(event, e => { e.preventDefault(); zone.classList.add('dragover'); });
  for (const event of ['dragleave', 'drop']) zone.addEventListener(event, e => { e.preventDefault(); zone.classList.remove('dragover'); });
  zone.addEventListener('drop', e => selectFile(kind, e.dataTransfer.files[0]));
}
$('to-garment').onclick = () => showStep('garment');
$('step-person').onclick = () => showStep('person');
$('step-garment').onclick = () => state.person ? showStep('garment') : message('Сначала добавьте своё фото.');
$('step-result').onclick = () => state.current && renderJob(state.current);
$('garment-replace').onclick = () => $('garment-input').click();
$('consent').onchange = buttons;
$('product-url').oninput = () => { state.requestId = null; saveDraft(); };

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
    const button = document.createElement('button');
    button.setAttribute('aria-label', job.status === 'success' ? 'Открыть прошлую примерку' : 'Открыть состояние примерки');
    button.classList.toggle('selected', job.id === state.current?.id);
    if (job.status === 'success') { const image = new Image(); image.src = apiUrl(`/api/jobs/${job.id}/result`); image.alt = 'Предыдущая примерка'; button.append(image); }
    else { const label = document.createElement('span'); label.textContent = job.status === 'error' ? 'Не получилась' : 'Создаётся…'; button.append(label); }
    button.onclick = () => { if (state.busy && job.id !== state.current?.id) return; renderJob(job); $('history-dialog').close(); if (['queued', 'running'].includes(job.status)) pollJob(job.id); };
    $('recent-list').append(button);
  }
}
function renderJob(job) {
  state.current = job; state.busy = ['queued', 'running'].includes(job.status);
  showStep('result');
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
    const job = await api('/api/jobs', { method: 'POST', body: JSON.stringify({ person: state.person, garment: state.garment, product_url: link, consent: $('consent').checked, request_id: state.requestId }) });
    state.current = job;
    await session(); renderJob(job); pollJob(job.id);
  } catch (error) {
    state.busy = false; buttons();
    message(error.message || 'Связь прервалась. Повторное нажатие проверит тот же запрос.');
  } finally { $('generate').replaceChildren(document.createTextNode('Примерить на себя ')); const star = document.createElement('span'); star.textContent = '✳'; $('generate').append(star); }
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
  if (!state.person && state.current) {
    try { const res = await fetch(apiUrl(`/api/jobs/${state.current.id}/person`), { credentials: 'include' }); if (!res.ok) throw new Error(); const blob = await res.blob(); state.person = await prepareFile(blob); }
    catch { message('Исходное фото уже недоступно. Добавьте его заново.'); }
  }
  if (clearGarment) { state.garment = null; $('product-url').value = ''; }
  state.requestId = null; state.busy = false;
  $('processing').hidden = true; $('consent').checked = false;
  refreshUploads(); showStep(state.person ? 'garment' : 'person'); await saveDraft(); await session();
}
$('another').onclick = () => backToPhotos(true);
$('retry-edit').onclick = () => backToPhotos(false);

// Cropping is local; no file leaves the device until the user starts generation.
$('crop-open').onclick = async () => {
  try {
    const img = await loadImage(state.garment);
    const scale = Math.min(1, 700 / Math.max(img.naturalWidth, img.naturalHeight));
    const canvas = $('crop-canvas'); canvas.width = Math.round(img.naturalWidth * scale); canvas.height = Math.round(img.naturalHeight * scale);
    state.crop = { img, rect: [0, 0, canvas.width, canvas.height], start: null }; drawCrop(); $('crop-dialog').showModal();
  } catch { message('Не удалось открыть фото для обрезки.'); }
};
function drawCrop() {
  const canvas = $('crop-canvas'), ctx = canvas.getContext('2d'), c = state.crop;
  if (!c) return;
  ctx.drawImage(c.img, 0, 0, canvas.width, canvas.height);
  ctx.fillStyle = '#20332988'; ctx.fillRect(0, 0, canvas.width, canvas.height);
  const [x, y, w, h] = c.rect;
  ctx.save(); ctx.beginPath(); ctx.rect(x, y, w, h); ctx.clip(); ctx.drawImage(c.img, 0, 0, canvas.width, canvas.height); ctx.restore();
  ctx.strokeStyle = '#e0f2b3'; ctx.lineWidth = 3; ctx.strokeRect(x+1, y+1, Math.max(0,w-2), Math.max(0,h-2));
}
function cropPoint(event) {
  const c = $('crop-canvas'), r = c.getBoundingClientRect();
  // The canvas CSS keeps the same aspect ratio, so coordinates map directly.
  return [Math.max(0, Math.min(c.width, (event.clientX-r.left)*c.width/r.width)), Math.max(0, Math.min(c.height, (event.clientY-r.top)*c.height/r.height))];
}
$('crop-canvas').onpointerdown = e => { state.crop.start = cropPoint(e); $('crop-canvas').setPointerCapture(e.pointerId); };
$('crop-canvas').onpointermove = e => {
  if (!state.crop?.start) return;
  const [a,b] = state.crop.start, [x,y] = cropPoint(e);
  state.crop.rect = [Math.min(a,x), Math.min(b,y), Math.abs(a-x), Math.abs(b-y)]; drawCrop();
};
$('crop-canvas').onpointerup = $('crop-canvas').onpointercancel = () => { if (state.crop) state.crop.start = null; };
$('crop-reset').onclick = () => { state.crop.rect = [0,0,$('crop-canvas').width,$('crop-canvas').height]; drawCrop(); };
$('crop-close').onclick = () => $('crop-dialog').close();
$('crop-apply').onclick = async () => {
  const c = state.crop, scale = c.img.naturalWidth / $('crop-canvas').width;
  const [x,y,w,h] = c.rect.map(n => Math.round(n*scale));
  if (w < 128 || h < 128) { $('crop-reset').click(); return; }
  const output = document.createElement('canvas'); output.width = w; output.height = h;
  output.getContext('2d').drawImage(c.img,x,y,w,h,0,0,w,h);
  state.garment = output.toDataURL('image/jpeg',.92); state.requestId = null;
  refreshUploads(); await saveDraft(); $('crop-dialog').close();
};
for (const name of ['history', 'link', 'feedback', 'invite']) {
  $(`${name}-open`).onclick = () => $(`${name}-dialog`).showModal();
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
$('fitting-open').onclick = () => { if (!state.busy) showStep(state.person ? 'garment' : 'person'); };
$('privacy-open').onclick = () => $('privacy-dialog').showModal();
$('privacy-close').onclick = () => $('privacy-dialog').close();
$('delete-photos').onclick = async () => {
  $('delete-photos').disabled = true;
  try {
    await api('/api/photos', { method: 'DELETE' });
    clearTimeout(state.poll); await draft('delete');
    state.person = state.garment = state.current = state.requestId = null; state.busy = false; state.jobs = [];
    $('product-url').value = ''; $('consent').checked = false; $('processing').hidden = true;
    $('delete-note').textContent = 'Ваши фото и примерки удалены.';
    refreshUploads(); showStep('person'); await session();
  } catch (error) { $('delete-note').textContent = error.message; }
  finally { $('delete-photos').disabled = false; }
};
async function init() {
  try {
    try { draftDb = await openDraftDb(); const saved = await draft('get');
      if (saved && Date.now()-saved.created < retention) { state.person = saved.person; state.garment = saved.garment; state.requestId = saved.requestId; draftExpires = saved.created + retention; $('product-url').value = saved.productUrl || ''; }
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
    const pending = info.jobs.find(j => ['queued','running'].includes(j.status));
    const latest = info.jobs.find(j => j.request_id === state.requestId);
    if (pending) { renderJob(pending); pollJob(pending.id); }
    else if (latest) renderJob(latest);
  } catch { message('Не удалось подключиться к примерочной. Обновите страницу.'); }
}
init();
setInterval(async () => {
  if (!draftExpires || Date.now() < draftExpires || state.busy) return;
  draftExpires = 0;
  try { await draft('delete'); } catch { /* Retry clearing on the next visit. */ }
  state.person = state.garment = state.requestId = null;
  refreshUploads();
  if (state.step !== 'result') { showStep('person'); message('Срок хранения черновиков истёк. Добавьте фото заново.'); }
}, 60000);
