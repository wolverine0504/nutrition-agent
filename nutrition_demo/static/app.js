const $ = id => document.getElementById(id);
let busy = false, refreshing = false, revision = 0;
let state = null, renderedMessages = '', pendingRequest = null, pendingDecision = null;
function status(text) { $('status').textContent = text; }
async function api(path, options = {}) {
  const response = await fetch(path, {cache: 'no-store', ...options});
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'The request could not be completed.');
  return data;
}
function draw(data) {
  state = data;
  $('mode').textContent = data.model_enabled ? 'Live model enabled' : 'Offline · model calls disabled';
  $('stage').textContent = data.stage.replaceAll('_', ' ');
  for (const key of ['inventory', 'batches', 'daily']) $(key).textContent = data[key];
  const signature = JSON.stringify(data.messages);
  if (signature !== renderedMessages) {
    renderedMessages = signature;
    $('messages').replaceChildren();
    if (!data.messages.length) {
      const p = document.createElement('p'); p.className = 'empty';
      p.textContent = 'Start with a batch query, then try logging a meal and correcting its date.';
      $('messages').append(p);
    }
    for (const message of data.messages) {
      for (const role of ['user', 'assistant']) {
        const article = document.createElement('article'); article.className = `bubble ${role}`;
        const label = document.createElement('span'); label.className = 'speaker';
        label.textContent = role === 'user' ? 'Participant' : 'Nutrition Agent';
        const content = document.createElement('pre'); content.textContent = message[role];
        article.append(label, content);
        if (role === 'assistant' && message.activity.length) {
          const activity = document.createElement('div'); activity.className = 'activity';
          activity.textContent = 'Tool activity: ' + message.activity.map(x => x.name.replaceAll('_', ' ') + (x.ok ? ' ✓' : ' · needs attention')).join(' → ');
          article.append(activity);
        }
        $('messages').append(article);
      }
    }
    $('messages').scrollTop = $('messages').scrollHeight;
  }
  $('preview').hidden = !data.preview;
  $('preview-text').textContent = data.preview?.text ?? '';
  $('proposal').hidden = !data.proposal || data.stage === 'cooking' || !!data.preview;
  $('proposal-note').textContent = data.proposal?.passed ? 'All four nutrient targets pass.' : 'Some targets are unmet. Accepting keeps that status visible.';
}
async function refresh() {
  if (busy || refreshing || document.hidden) return;
  refreshing = true;
  const startedAt = revision;
  const isCurrent = () => startedAt === revision && !busy;
  try {
    const data = await api('/api/state');
    if (isCurrent()) draw(data);
  } catch (error) {
    if (isCurrent() && !state) status(error.message);
  } finally {
    refreshing = false;
  }
}
async function mutate(path, body, presenter = false) {
  if (busy) return;
  busy = true;
  revision += 1;
  document.querySelectorAll('button').forEach(x => x.disabled = true);
  status('Working…');
  try {
    const key = presenter ? 'X-Presenter-Code' : 'X-Interaction-Code';
    const result = await api(path, {method: 'POST', headers: {'Content-Type': 'application/json', [key]: $(presenter ? 'presenter-code' : 'code').value}, body: JSON.stringify(body)});
    if (path === '/api/messages' || presenter) {
      pendingRequest = null;
      $('message').value = '';
    }
    if (path === '/api/decisions' || presenter) pendingDecision = null;
    status(result.ok === false ? result.assistant : 'Shared workspace updated.');
    draw(presenter ? result : await api('/api/state'));
  } catch (error) { status(error.message); }
  finally { busy = false; document.querySelectorAll('button').forEach(x => x.disabled = false); }
}
function send(message) {
  // Preserve the same receipt ID on network retries; changing the message gets a new ID.
  if (!pendingRequest || pendingRequest.message !== message) pendingRequest = {request_id: crypto.randomUUID(), message};
  return mutate('/api/messages', pendingRequest);
}
$('compose').addEventListener('submit', event => { event.preventDefault(); const text = $('message').value.trim(); if (text) send(text); });
document.querySelectorAll('[data-command]').forEach(button => button.addEventListener('click', () => send(button.dataset.command)));
for (const [id, approve] of [['approve', true], ['reject', false]]) $(id).addEventListener('click', () => {
  if (!state?.preview) return;
  if (pendingDecision?.preview_id !== state.preview.id || pendingDecision.approve !== approve) {
    pendingDecision = {request_id: crypto.randomUUID(), preview_id: state.preview.id, approve};
  }
  mutate('/api/decisions', pendingDecision);
});
$('accept').addEventListener('click', () => { if (state?.proposal) send('/accept-recipe ' + state.proposal.id + (state.proposal.passed ? '' : ' outside')); });
$('reset').addEventListener('click', () => { if (confirm('Replace the shared conversation and all records with synthetic demo data?')) mutate('/api/reset', {}, true); });
document.addEventListener('visibilitychange', refresh);
refresh(); setInterval(refresh, 2500);
