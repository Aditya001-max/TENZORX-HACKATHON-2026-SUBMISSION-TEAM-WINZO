
const API = location.origin;          // backend served from same host
const $   = (id) => document.getElementById(id);

const state = {
  sessionId: null,
  collected: {},
  currentSlot: null,
  stream: null,
  recognition: null,
  isRecognising: false,
  agentSpeaking: false,
  ageInterval: null,
  ws: null,
  geo: null,
};

/*  screen routing  */
function show(screen) {
  document.querySelectorAll('.screen').forEach(s => s.classList.remove('active'));
  $(screen).classList.add('active');
}

/*  TTS (browser SpeechSynthesis)  */
const synth = window.speechSynthesis;
let voices = [];
function loadVoices() {
  voices = synth.getVoices();
}
loadVoices();
if (synth.onvoiceschanged !== undefined) synth.onvoiceschanged = loadVoices;

function speak(text, onEnd) {
  if (!synth) { onEnd?.(); return; }
  synth.cancel();
  const u = new SpeechSynthesisUtterance(text);
  u.rate = 1.0; u.pitch = 1.05;
  // pick a female English voice if available
  const v = voices.find(v => /female/i.test(v.name)) ||
            voices.find(v => /Samantha|Karen|Tessa|Google UK English Female|Google US English/i.test(v.name)) ||
            voices.find(v => v.lang?.startsWith('en'));
  if (v) u.voice = v;
  u.onstart = () => {
    state.agentSpeaking = true;
    $('agentAvatar').classList.add('speaking');
    $('agentSpeaking').classList.add('active');
  };
  u.onend = () => {
    state.agentSpeaking = false;
    $('agentAvatar').classList.remove('speaking');
    $('agentSpeaking').classList.remove('active');
    onEnd?.();
  };
  synth.speak(u);
}

/*  transcript UI  */
function appendTranscript(speaker, text) {
  const wrap = $('transcript');
  const div = document.createElement('div');
  div.className = `turn ${speaker}`;
  div.innerHTML = `<div class="who">${speaker === 'agent' ? 'MAYA' : 'YOU'}</div>${escapeHtml(text)}`;
  wrap.appendChild(div);
  wrap.scrollTop = wrap.scrollHeight;
}
function escapeHtml(s) {
  return s.replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

/*  captured data UI  */
const FIELD_LABELS = {
  full_name: 'Name', declared_age: 'Age',
  employment_type: 'Employment', employer_name: 'Employer',
  monthly_income: 'Income (/mo)', loan_purpose: 'Purpose',
  loan_amount_requested: 'Loan Amount', pan: 'PAN', consent: 'Consent',
};

function renderCaptured() {
  const el = $('captured');
  const keys = Object.keys(FIELD_LABELS).filter(k => state.collected[k] !== undefined && state.collected[k] !== null);
  if (keys.length === 0) {
    el.innerHTML = `<div class="empty">No data captured yet.</div>`;
    return;
  }
  el.innerHTML = keys.map(k => {
    let v = state.collected[k];
    if (k === 'monthly_income' || k === 'loan_amount_requested') v = '₹' + Number(v).toLocaleString('en-IN');
    if (k === 'consent') v = v ? '✓ Given' : '✗ Not Given';
    if (k === 'employment_type' || k === 'loan_purpose') v = String(v).replace(/_/g, ' ');
    return `<div class="row"><div class="k">${FIELD_LABELS[k]}</div><div class="v">${escapeHtml(String(v))}</div></div>`;
  }).join('');
}

/*  API helpers  */
async function api(path, opts = {}) {
  const res = await fetch(API + path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  if (!res.ok) throw new Error(`API ${path}: ${res.status}`);
  return res.json();
}

/*  session start  */
async function startSession() {
  const r = await api('/api/session/start', { method: 'POST' });
  state.sessionId = r.session_id;
  $('sessionIdDisplay').textContent = r.session_id;

  // open WebSocket for live updates
  try {
    state.ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/session/${r.session_id}`);
    state.ws.onmessage = (e) => {
      try { handleWsEvent(JSON.parse(e.data)); } catch {}
    };
  } catch {}

  // greeting
  state.currentSlot = r.greeting.next_slot;
  appendTranscript('agent', r.greeting.agent_message);
  speak(r.greeting.agent_message, () => {
    // start age estimation polling once Maya finishes greeting
    startAgeEstimationLoop();
  });
}

function handleWsEvent(ev) {
  // We push from server too; UI already updated on REST responses.
  // Hook for future: live admin dashboard updates.
}

/*  camera + mic  */
async function startMedia() {
  try {
    state.stream = await navigator.mediaDevices.getUserMedia({
      video: { width: 640, height: 480, facingMode: 'user' },
      audio: true,
    });
    $('localVideo').srcObject = state.stream;
  } catch (err) {
    alert('Camera/Microphone permission is required for the video call.\n\n' + err.message);
    throw err;
  }
}

/*  geo  */
async function captureGeo() {
  if (!navigator.geolocation) { $('sigGeo').textContent = 'Unavailable'; return; }
  return new Promise((resolve) => {
    navigator.geolocation.getCurrentPosition(
      async (pos) => {
        state.geo = pos.coords;
        $('sigGeo').textContent = '✓ Locked';
        $('sigGeo').classList.add('ok');
        await api(`/api/session/${state.sessionId}/geo`, {
          method: 'POST',
          body: JSON.stringify({
            latitude: pos.coords.latitude,
            longitude: pos.coords.longitude,
            accuracy: pos.coords.accuracy,
          })
        });
        resolve();
      },
      () => { $('sigGeo').textContent = 'Denied'; $('sigGeo').classList.add('bad'); resolve(); },
      { timeout: 8000 }
    );
  });
}

/*  age estimation snapshot loop  */
function startAgeEstimationLoop() {
  if (state.ageInterval) clearInterval(state.ageInterval);
  // run once immediately, then every 6s
  runAgeEstimation();
  state.ageInterval = setInterval(runAgeEstimation, 6000);
}

async function runAgeEstimation() {
  const video = $('localVideo');
  if (!video.videoWidth) return;
  const canvas = $('snapshotCanvas');
  canvas.width = video.videoWidth;
  canvas.height = video.videoHeight;
  const ctx = canvas.getContext('2d');
  // mirror to match what user sees
  ctx.save();
  ctx.scale(-1, 1);
  ctx.drawImage(video, -canvas.width, 0, canvas.width, canvas.height);
  ctx.restore();
  const dataUrl = canvas.toDataURL('image/jpeg', 0.8);

  try {
    const r = await api(`/api/session/${state.sessionId}/age`, {
      method: 'POST',
      body: JSON.stringify({ image_data_url: dataUrl }),
    });
    if (r.age?.face_detected) {
      $('sigFace').textContent = '✓ Yes';
      $('sigFace').classList.add('ok'); $('sigFace').classList.remove('bad');
      $('sigAge').textContent = r.age.estimated_age + ' yr';
      drawFaceBox(r.age.face_box, video);
    } else {
      $('sigFace').textContent = 'No';
      $('sigFace').classList.add('bad');
      $('sigAge').textContent = '—';
      $('faceBox').style.display = 'none';
    }
    if (r.liveness) {
      $('sigLive').textContent = (r.liveness.is_live ? '✓ ' : '⚠ ') + r.liveness.score;
      $('sigLive').classList.toggle('ok', r.liveness.is_live);
      $('sigLive').classList.toggle('bad', !r.liveness.is_live);
    }
  } catch (e) { console.warn('age est failed', e); }
}

function drawFaceBox(box, video) {
  if (!box) return;
  const panel = video.parentElement;
  const rect = video.getBoundingClientRect();
  const panelRect = panel.getBoundingClientRect();
  const sx = rect.width / video.videoWidth;
  const sy = rect.height / video.videoHeight;
  const fb = $('faceBox');
  // mirror the x axis to match the mirrored video
  const xOnScreen = video.videoWidth - box[0] - box[2];
  fb.style.display = 'block';
  fb.style.left = (rect.left - panelRect.left + xOnScreen * sx) + 'px';
  fb.style.top  = (rect.top  - panelRect.top  + box[1] * sy) + 'px';
  fb.style.width = (box[2] * sx) + 'px';
  fb.style.height = (box[3] * sy) + 'px';
}

/*  speech recognition (push-to-talk)  */
function setupSpeech() {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) {
    $('micBtn').disabled = true;
    $('micBtnText').textContent = 'Speech not supported';
    $('typeMode').click();
    return;
  }
  const rec = new SR();
  rec.lang = 'en-IN';
  rec.continuous = false;
  rec.interimResults = false;

  rec.onresult = (e) => {
    const text = e.results[0][0].transcript;
    sendCustomerText(text);
  };
  rec.onerror = (e) => { console.warn('STT error', e); state.isRecognising = false; updateMicBtn(); };
  rec.onend = () => { state.isRecognising = false; updateMicBtn(); };
  state.recognition = rec;
}

function updateMicBtn() {
  const btn = $('micBtn');
  const txt = $('micBtnText');
  if (state.isRecognising) {
    btn.classList.add('recording');
    txt.textContent = 'Listening… release to send';
  } else {
    btn.classList.remove('recording');
    txt.textContent = 'Hold to Speak';
  }
}

function startListening() {
  if (state.agentSpeaking) synth.cancel();
  if (!state.recognition || state.isRecognising) return;
  try {
    state.recognition.start();
    state.isRecognising = true;
    updateMicBtn();
  } catch {}
}

function stopListening() {
  if (state.recognition && state.isRecognising) {
    try { state.recognition.stop(); } catch {}
  }
}

/*  core conversation loop  */
async function sendCustomerText(text) {
  if (!text || !text.trim()) return;
  appendTranscript('customer', text);

  try {
    const resp = await api(`/api/session/${state.sessionId}/turn`, {
      method: 'POST',
      body: JSON.stringify({
        customer_text: text,
        current_slot: state.currentSlot,
        collected: state.collected,
      }),
    });
    state.collected = resp.collected || state.collected;
    state.currentSlot = resp.next_slot;
    renderCaptured();
    appendTranscript('agent', resp.agent_message);
    speak(resp.agent_message, async () => {
      if (resp.completed) await finalize();
    });
  } catch (e) {
    console.error(e);
    appendTranscript('agent', '(network error — please try again)');
  }
}

/*  finalize: risk, offer, end  */
async function finalize() {
  try {
    if (state.ageInterval) clearInterval(state.ageInterval);
    const r = await api(`/api/session/${state.sessionId}/finalize`, { method: 'POST' });
    showResult(r);
    speak(r.closing_message);
  } catch (e) {
    alert('Finalisation failed: ' + e.message);
  }
}

function showResult(r) {
  // stop streams
  if (state.stream) state.stream.getTracks().forEach(t => t.stop());

  show('result');
  const badge = $('resultBadge');
  badge.classList.remove('approve', 'refer', 'reject');
  if (r.decision === 'APPROVE') {
    badge.classList.add('approve'); badge.textContent = 'Approved';
    $('resultTitle').textContent = 'You\'re pre-approved.';
    $('resultSub').textContent = 'Here is your personalised loan offer based on your profile.';
  } else if (r.decision === 'REFER') {
    badge.classList.add('refer'); badge.textContent = 'Under Review';
    $('resultTitle').textContent = 'Almost there.';
    $('resultSub').textContent = 'A preliminary offer is shown below. Our team will reach out within 24 hours to finalise.';
  } else {
    badge.classList.add('reject'); badge.textContent = 'Not Eligible';
    $('resultTitle').textContent = 'We can\'t extend an offer right now.';
    $('resultSub').textContent = 'Please review the reasons below. You can reapply after addressing them.';
  }

  if (r.offer?.eligible) {
    const po = r.offer.primary_offer;
    $('offerAmount').textContent = '₹' + po.loan_amount.toLocaleString('en-IN');
    $('offerTenure').textContent = po.tenure_months + ' mo';
    $('offerRate').textContent   = po.interest_rate + '%';
    $('offerEmi').textContent    = '₹' + Math.round(po.emi).toLocaleString('en-IN');

    const tbl = $('offerTable');
    tbl.innerHTML = `<thead><tr><th>Tenure</th><th>Loan Amount</th><th>Rate</th><th>EMI</th><th>Total Interest</th></tr></thead>` +
      '<tbody>' + r.offer.all_offers.map(o =>
        `<tr><td>${o.tenure_months} mo</td><td>₹${o.loan_amount.toLocaleString('en-IN')}</td><td>${o.interest_rate}%</td><td>₹${Math.round(o.emi).toLocaleString('en-IN')}</td><td>₹${Math.round(o.total_interest).toLocaleString('en-IN')}</td></tr>`
      ).join('') + '</tbody>';
    $('offerCard').style.display = 'block';
  } else {
    $('offerCard').style.display = 'none';
  }

  // Explanations
  const list = $('explainList');
  const items = [];
  items.push(`Risk score: ${r.risk_score}/100 (${r.risk_band})`);
  items.push(`Bureau score: ${r.bureau_score}`);
  items.push(`Propensity-to-pay score: ${r.propensity_score}/100`);
  if (r.policy_failures?.length) items.push(`Policy failures: ${r.policy_failures.join(', ')}`);
  if (r.policy_warnings?.length) items.push(`Policy warnings: ${r.policy_warnings.join(', ')}`);
  list.innerHTML = items.map(i => `<li>${escapeHtml(i)}</li>`).join('');
}

/*  end / restart / report  */
async function endCall() {
  if (!confirm('End the call now? Your offer will be generated based on what we have so far.')) return;
  await finalize();
}

async function downloadReport() {
  if (!state.sessionId) return;
  const r = await api(`/api/session/${state.sessionId}/report`);
  const blob = new Blob([JSON.stringify(r, null, 2)], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `loan-wizard-${state.sessionId}.json`;
  a.click();
}

/*  wire up UI  */
function wireUp() {
  $('startBtn').addEventListener('click', () => show('consent'));
  $('backBtn').addEventListener('click', () => show('landing'));

  $('proceedBtn').addEventListener('click', async () => {
    show('call');
    setupSpeech();
    try {
      await startMedia();
    } catch { return; }
    await startSession();
    await captureGeo();
  });

  // push-to-talk
  const mic = $('micBtn');
  mic.addEventListener('mousedown', startListening);
  mic.addEventListener('mouseup', stopListening);
  mic.addEventListener('mouseleave', stopListening);
  mic.addEventListener('touchstart', (e) => { e.preventDefault(); startListening(); });
  mic.addEventListener('touchend', (e) => { e.preventDefault(); stopListening(); });

  // type mode toggle
  $('typeMode').addEventListener('click', (e) => {
    e.preventDefault();
    $('textInputWrap').style.display = 'block';
    $('textInput').focus();
  });
  $('textInput').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && e.target.value.trim()) {
      const text = e.target.value.trim();
      e.target.value = '';
      sendCustomerText(text);
    }
  });

  $('endBtn').addEventListener('click', endCall);
  $('acceptBtn').addEventListener('click', () => alert('Offer accepted!\n\nA loan officer will call you within 1 hour to complete disbursement.'));
  $('reportBtn').addEventListener('click', downloadReport);
  $('restartBtn').addEventListener('click', () => location.reload());
}

window.addEventListener('DOMContentLoaded', wireUp);
