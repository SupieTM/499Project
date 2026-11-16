const app = document.querySelector('#app');
const photoInput = document.querySelector('#photo-input');


const PHOTO_SLOTS = [
  { name: 'Identification number', hint: 'Close-up of the stamped casting number. Keep it sharp and readable.' },
  { name: 'Corner 1', hint: 'Whole casting from the first corner, looking down.' },
  { name: 'Corner 2', hint: 'Whole casting from the second corner.' },
  { name: 'Corner 3', hint: 'Whole casting from the third corner.' },
  { name: 'Corner 4', hint: 'Whole casting from the fourth corner.' },
];

const state = {
  screen: 'start',
  photos: [null, null, null, null, null], // { file, url }
  activeSlot: 0,
  delivery: '',
  result: null,          
  selectedCasing: '',    
  previousCasing: '',
  corrected: false,
  parts: [],             
  error: '',
  recorded: '',
};

const screenOrder = ['start', 'capture', 'review', 'result', 'confirmation'];

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function pct(value) {
  return `${Math.round((Number(value) || 0) * 100)}%`;
}

function photoCount() {
  return state.photos.filter(Boolean).length;
}

function setScreen(screen) {
  state.screen = screen;
  render();
  app.focus({ preventScroll: true });
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function updateStepper() {
  const map = { correction: 'result', error: 'review', processing: 'review' };
  const activeIndex = screenOrder.indexOf(map[state.screen] || state.screen);
  document.querySelectorAll('.step').forEach((step, index) => {
    step.classList.toggle('is-active', index === activeIndex);
    step.classList.toggle('is-complete', index < activeIndex);
    step.disabled = index > activeIndex || state.screen === 'processing';
  });
}

function renderPhotos(editable = true) {
  return PHOTO_SLOTS.map((slot, index) => {
    const photo = state.photos[index];
    if (!photo) {
      return `
        <button class="empty-photo" type="button" data-add-photo="${index}">
          <span><strong>${slot.name}</strong>${slot.hint}</span>
        </button>`;
    }
    return `
      <article class="photo-card">
        <div class="photo-visual" style="background-image:url('${photo.url}')"></div>
        <div class="photo-meta"><span>${slot.name}</span>${editable ? `<button type="button" data-remove-photo="${index}">Remove</button>` : ''}</div>
      </article>`;
  }).join('');
}

function startScreen() {
  return `
    <section class="screen">
      <header class="screen-header">
        <p class="eyebrow">New delivery</p>
        <h2>Check in a casing</h2>
        <p>Take pictures of a delivered casing, review the result, and confirm the check-in.</p>
      </header>
      <div class="screen-body">
        <section class="instruction-card">
          <h3>Basic process</h3>
          <ol>
            <li>Start a new check-in.</li>
            <li>Take one close-up of the identification number.</li>
            <li>Take four pictures of the casing, one from each corner.</li>
            <li>Review the identification result and confidence.</li>
            <li>Confirm or correct the matched casing and submit.</li>
          </ol>
        </section>
        <label class="field">Delivery (optional)<input id="delivery-input" value="${esc(state.delivery)}" placeholder="Delivery name or number" /></label>
      </div>
      <footer class="screen-actions">
        <span class="muted">No information is submitted until final confirmation.</span>
        <button class="primary-button" id="start-checkin" type="button">Start new check-in</button>
      </footer>
    </section>`;
}

function captureScreen() {
  const enough = photoCount() === PHOTO_SLOTS.length;
  return `
    <section class="screen">
      <header class="screen-header">
        <p class="eyebrow">Step 2 of 5</p>
        <h2>Capture casing photos</h2>
        <p>Add all five photos. The identification number photo is read for the casting number, and the four corner photos are matched against the CAD files.</p>
      </header>
      <div class="screen-body">
        <div class="photo-guidance">
          <div class="guide"><b>Identification number</b><span>Fill the frame with the number. Avoid glare.</span></div>
          <div class="guide"><b>Corner photos</b><span>Include the complete casing in every shot.</span></div>
          <div class="guide"><b>Background</b><span>Plain backgrounds give the best cutouts.</span></div>
        </div>
        ${!enough ? `<div class="alert warning">${photoCount()} of 5 photos added. All five are required before continuing.</div>` : ''}
        <div class="photo-grid">${renderPhotos(true)}</div>
      </div>
      <footer class="screen-actions">
        <button class="secondary-button" type="button" data-go="start">Cancel</button>
        <button class="primary-button" type="button" data-go="review" ${enough ? '' : 'disabled'}>Review photos</button>
      </footer>
    </section>`;
}

function reviewScreen() {
  return `
    <section class="screen">
      <header class="screen-header">
        <p class="eyebrow">Step 3 of 5</p>
        <h2>Review selected photos</h2>
        <p>Confirm that the casing and its identification number are visible before sending the images for identification.</p>
      </header>
      <div class="screen-body review-layout">
        <div class="preview-stack">${state.photos.map((p, i) => `<div class="mini-photo" style="background-image:url('${p.url}')" title="${PHOTO_SLOTS[i].name}" aria-label="${PHOTO_SLOTS[i].name}"></div>`).join('')}</div>
        <section class="review-panel">
          <h3>Image quality check</h3>
          <p class="muted">The employee makes the final decision about whether the photos are usable.</p>
          <ul class="check-list">
            <li>${photoCount()} images selected</li>
            <li>Identification number is readable</li>
            <li>Full casing visible in all four corner photos</li>
            <li>No obvious blur or obstruction</li>
          </ul>
        </section>
      </div>
      <footer class="screen-actions">
        <button class="secondary-button" type="button" data-go="capture">Retake or remove photos</button>
        <button class="primary-button" type="button" id="identify">Identify casing</button>
      </footer>
    </section>`;
}

function processingScreen() {
  return `
    <section class="screen">
      <div class="processing">
        <div>
          <h2>Processing photos...</h2>
          <p class="muted">Reading the casting number, removing backgrounds, and matching against the CAD files.<br>The first run can take a few minutes while the models load.</p>
        </div>
      </div>
    </section>`;
}

function candidateRows(rows) {
  if (!rows.length) return '<tr><td class="muted">No candidates found</td><td></td></tr>';
  return rows.map(r => `<tr><td><b>${esc(r.part)}</b>${r.note ? `<br><span class="muted">${esc(r.note)}</span>` : ''}</td><td>${pct(r.score)}</td></tr>`).join('');
}

function cadRenderBox() {
  if (!state.selectedCasing) {
    return '<div class="casing-illustration">No CAD render</div>';
  }
  const url = `/cad-render/${encodeURIComponent(state.selectedCasing)}`;
  return `<div class="casing-illustration" style="background:#fff url('${url}') center/contain no-repeat" title="CAD render of ${esc(state.selectedCasing)}" aria-label="CAD render of ${esc(state.selectedCasing)}"></div>`;
}

function resultScreen() {
  const r = state.result;
  const c = r.comparison;
  const match = c.systems_match;
  const ocrText = r.ocr_results.length ? r.ocr_results.map(o => `${o.text} (${pct(o.confidence)})`).join(', ') : 'No number read';
  const shownConfidence = state.corrected ? null : r.best_confidence;

  const referenceRows = r.reference_top_5.map(x => ({ part: x.part, score: x.reference_confidence, note: `Read "${x.ocr_text}"` }));
  const cadRows = r.cad_top_5.map(x => ({ part: x.part, score: x.score }));

  return `
    <section class="screen">
      <header class="screen-header">
        <p class="eyebrow">Step 4 of 5</p>
        <h2>Review identification result</h2>
        <p>${match ? 'The number on the casting and the CAD match agree.' : 'The number on the casting and the CAD match do not agree. Check the casting before submitting.'} Confirm the result or enter the correct casing.</p>
      </header>
      <div class="screen-body result-layout">
        ${cadRenderBox()}
        <section class="result-card">
          <span class="label">${state.corrected ? 'Manually entered casing' : 'Best match'}</span>
          <h3>${esc(state.selectedCasing)}</h3>
          <p>${state.corrected ? 'Entered by employee' : (match ? 'Number and CAD shape match' : 'Needs review')}</p>
          ${shownConfidence !== null ? `
            <div class="confidence"><div class="confidence-track"><div class="confidence-fill" style="width:${pct(shownConfidence)}; ${match ? '' : 'background:var(--warning)'}"></div></div><strong>${pct(shownConfidence)}</strong></div>
            <span class="muted">${match ? 'Combined confidence (number + CAD)' : 'CAD confidence only'}</span>` : ''}
          <div class="confirmation-grid">
            <div><span class="label">From number</span><span class="value">${esc(c.reference_part || 'None')}</span><br><span class="muted">${pct(c.reference_confidence)}</span></div>
            <div><span class="label">From CAD shape</span><span class="value">${esc(c.cad_part || 'None')}</span><br><span class="muted">${pct(c.cad_confidence)}</span></div>
          </div>
          <p class="muted">Number read: ${esc(ocrText)}</p>
        </section>
      </div>
      <div class="screen-body result-layout" style="padding-top:0">
        <section class="result-card">
          <span class="label">Top 5 from casting number</span>
          <table class="candidate-table">${candidateRows(referenceRows)}</table>
        </section>
        <section class="result-card">
          <span class="label">Top 5 from CAD shape match</span>
          <table class="candidate-table">${candidateRows(cadRows)}</table>
        </section>
      </div>
      <div class="screen-body" style="padding-top:0">
        <div class="alert ${match ? 'success' : 'warning'}"><b>${match ? 'MATCH.' : 'REVIEW.'}</b> The check-in record will not be created until you confirm this casing.</div>
      </div>
      <footer class="screen-actions">
        <button class="secondary-button" type="button" data-go="review">Back to photos</button>
        <div class="button-row">
          <button class="secondary-button" type="button" id="open-correction">Correct identification</button>
          <button class="primary-button" type="button" id="confirm-match">Confirm and submit</button>
        </div>
      </footer>
    </section>`;
}

function correctionScreen() {
  const r = state.result;
  const suggested = [...new Set([...r.reference_top_5.map(x => x.part), ...r.cad_top_5.map(x => x.part)])];
  const search = state.selectedCasing.toLowerCase();
  const fromIndex = state.parts.filter(p => !suggested.includes(p) && (!search || p.toLowerCase().includes(search))).slice(0, 10);
  const options = [...suggested, ...fromIndex];

  return `
    <section class="screen">
      <header class="screen-header">
        <p class="eyebrow">Manual correction</p>
        <h2>Enter the matched casing</h2>
        <p>Type the casing number or choose it from the list. The list shows the system's candidates first, then matching parts from the CAD files.</p>
      </header>
      <div class="screen-body">
        <label class="field">Matched casing number<input id="casing-search" value="${esc(state.selectedCasing)}" placeholder="Type the casing number" autocomplete="off" /></label>
        <div class="search-results">
          ${options.map(id => `<button class="search-option ${state.selectedCasing === id ? 'is-selected' : ''}" type="button" data-select-casing="${esc(id)}"><span><b>${esc(id)}</b>${suggested.includes(id) ? '<br><span class="muted">System candidate</span>' : ''}</span><span>${state.selectedCasing === id ? 'Selected' : 'Choose'}</span></button>`).join('')}
        </div>
        <div class="alert warning">A manually corrected identification is saved with the check-in for later model review.</div>
      </div>
      <footer class="screen-actions">
        <button class="secondary-button" type="button" id="cancel-correction">Cancel correction</button>
        <button class="primary-button" type="button" id="use-selected">Use this casing</button>
      </footer>
    </section>`;
}

function errorScreen() {
  return `
    <section class="screen">
      <header class="screen-header">
        <p class="eyebrow">Error</p>
        <h2>We could not complete this step</h2>
        <p>Your photos are still loaded, so you can try again without retaking them.</p>
      </header>
      <div class="screen-body">
        <div class="alert danger"><b>Something went wrong.</b> ${esc(state.error)}</div>
      </div>
      <footer class="screen-actions">
        <button class="secondary-button" type="button" data-go="capture">Change photos</button>
        <button class="primary-button" type="button" id="${state.result ? 'retry-submit' : 'identify'}">Try again</button>
      </footer>
    </section>`;
}

function confirmationScreen() {
  const r = state.result;
  return `
    <section class="screen">
      <header class="screen-header">
        <p class="eyebrow">Step 5 of 5</p>
        <h2>Check-in complete</h2>
        <p>The casing record has been submitted successfully.</p>
      </header>
      <div class="screen-body">
        <div class="success-mark">✓</div>
        <div class="alert success"><b>${esc(state.selectedCasing)}</b> was added to the delivery tracking record.</div>
        <div class="confirmation-grid">
          <div><span class="label">Matched casing</span><span class="value">${esc(state.selectedCasing)}</span></div>
          <div><span class="label">Confidence</span><span class="value">${state.corrected ? 'Manual entry' : pct(r.best_confidence)}</span></div>
          <div><span class="label">System decision</span><span class="value">${r.comparison.systems_match ? 'MATCH' : 'REVIEW'}</span></div>
          <div><span class="label">Delivery</span><span class="value">${esc(state.delivery || 'Not entered')}</span></div>
          <div><span class="label">Photos</span><span class="value">${photoCount()} attached</span></div>
          <div><span class="label">Recorded</span><span class="value">${esc(state.recorded)}</span></div>
        </div>
      </div>
      <footer class="screen-actions">
        <span></span>
        <button class="primary-button" id="new-checkin" type="button">Check in another casing</button>
      </footer>
    </section>`;
}

function render() {
  const screens = {
    start: startScreen,
    capture: captureScreen,
    review: reviewScreen,
    processing: processingScreen,
    result: resultScreen,
    correction: correctionScreen,
    error: errorScreen,
    confirmation: confirmationScreen,
  };
  app.innerHTML = screens[state.screen]();
  updateStepper();
  bindScreenEvents();
}

async function identify() {
  setScreen('processing');
  const form = new FormData();
  form.append('id_photo', state.photos[0].file);
  state.photos.slice(1).forEach(p => form.append('photos', p.file));

  try {
    const response = await fetch('/api/identify', { method: 'POST', body: form });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Identification failed.');
    state.result = data;
    state.selectedCasing = data.best_part || '';
    state.corrected = false;
    setScreen('result');
  } catch (err) {
    state.result = null;
    state.error = err.message;
    setScreen('error');
  }
}

async function submitCheckin() {
  if (!state.selectedCasing.trim()) {
    state.error = 'Enter the matched casing before submitting.';
    setScreen('error');
    return;
  }
  const r = state.result;
  try {
    const response = await fetch('/api/checkin', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        part: state.selectedCasing,
        system_part: r.best_part,
        confidence: r.best_confidence,
        decision: r.comparison.systems_match ? 'MATCH' : 'REVIEW',
        corrected: state.corrected,
        delivery: state.delivery,
        run_id: r.run_id,
      }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Submission failed.');
    state.recorded = data.recorded;
    setScreen('confirmation');
  } catch (err) {
    state.error = err.message;
    setScreen('error');
  }
}

async function loadParts() {
  if (state.parts.length) return;
  try {
    const response = await fetch('/api/parts');
    const data = await response.json();
    state.parts = data.parts || [];
  } catch {
    state.parts = [];
  }
}

function bindScreenEvents() {
  app.querySelectorAll('[data-go]').forEach(button => button.addEventListener('click', () => setScreen(button.dataset.go)));

  app.querySelectorAll('[data-add-photo]').forEach(button => button.addEventListener('click', () => {
    state.activeSlot = Number(button.dataset.addPhoto);
    photoInput.click();
  }));

  app.querySelectorAll('[data-remove-photo]').forEach(button => button.addEventListener('click', () => {
    const index = Number(button.dataset.removePhoto);
    if (state.photos[index]?.url) URL.revokeObjectURL(state.photos[index].url);
    state.photos[index] = null;
    render();
  }));

  app.querySelectorAll('[data-select-casing]').forEach(button => button.addEventListener('click', () => {
    state.selectedCasing = button.dataset.selectCasing;
    render();
  }));

  const search = document.querySelector('#casing-search');
  search?.addEventListener('input', event => {
    state.selectedCasing = event.target.value;
    const pos = event.target.selectionStart;
    render();
    const box = document.querySelector('#casing-search');
    box.focus();
    box.setSelectionRange(pos, pos);
  });

  document.querySelector('#delivery-input')?.addEventListener('input', e => { state.delivery = e.target.value; });
  document.querySelector('#start-checkin')?.addEventListener('click', () => setScreen('capture'));
  document.querySelector('#identify')?.addEventListener('click', identify);
  document.querySelector('#confirm-match')?.addEventListener('click', submitCheckin);
  document.querySelector('#retry-submit')?.addEventListener('click', submitCheckin);

  document.querySelector('#open-correction')?.addEventListener('click', async () => {
    state.previousCasing = state.selectedCasing;
    await loadParts();
    setScreen('correction');
  });
  document.querySelector('#cancel-correction')?.addEventListener('click', () => {
    state.selectedCasing = state.previousCasing;
    setScreen('result');
  });
  document.querySelector('#use-selected')?.addEventListener('click', () => {
    state.selectedCasing = state.selectedCasing.trim();
    state.corrected = state.selectedCasing !== state.result.best_part;
    setScreen('result');
  });

  document.querySelector('#new-checkin')?.addEventListener('click', () => {
    state.photos.forEach(p => p?.url && URL.revokeObjectURL(p.url));
    state.photos = [null, null, null, null, null];
    state.result = null;
    state.selectedCasing = '';
    state.corrected = false;
    state.error = '';
    state.recorded = '';
    setScreen('start');
  });
}

photoInput.addEventListener('change', event => {
  const file = event.target.files[0];
  if (file) {
    const old = state.photos[state.activeSlot];
    if (old?.url) URL.revokeObjectURL(old.url);
    state.photos[state.activeSlot] = { file, url: URL.createObjectURL(file) };
  }
  photoInput.value = '';
  state.screen = 'capture';
  render();
});

document.querySelectorAll('.step').forEach(step => step.addEventListener('click', () => {
  if (step.disabled) return;
  if (step.dataset.step === 'result' && !state.result) return;
  if (step.dataset.step === 'confirmation' && !state.recorded) return;
  setScreen(step.dataset.step);
}));

render();
