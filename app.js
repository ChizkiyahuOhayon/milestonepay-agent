'use strict';
let csrf = '', state = null, selected = '', proposal = null, recommendation = null;
const $ = id => document.getElementById(id);
const explanations = {
  authorize: 'Reserve funds after Sandbox buyer approval. This does not release payment to the freelancer.',
  capture: 'Charge the buyer against the authorization and release payment. Accept the actual delivery before signing.',
  void: 'Cancel the uncaptured authorization and release the hold. It cannot be captured afterwards.',
  refund: 'Return the displayed amount to the buyer. A completed refund cannot be reversed here.'
};
function node(tag, text, className) { const el = document.createElement(tag); if (text !== undefined) el.textContent = text; if (className) el.className = className; return el; }
function notice(message, error = false) { $('notice').textContent = message; $('notice').className = error ? 'error' : ''; }
async function api(path, data) {
  const response = await fetch(path, data === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-MilestonePay-Token': csrf }, body: JSON.stringify(data) });
  const body = await response.json(); if (!response.ok) throw new Error(body.error || 'Request failed'); return body;
}
async function action(task, button) {
  if (button) button.disabled = true;
  try { await task(); } catch (error) { notice(error.message, true); }
  finally { if (button) button.disabled = false; }
}
function switchStep(step) {
  document.querySelectorAll('.step').forEach(el => { el.hidden = el.id !== step; });
  document.querySelectorAll('[data-step]').forEach(el => el.classList.toggle('active', el.dataset.step === step));
}
function field(label, type, value = '', placeholder = '') {
  const wrapper = node('label', label); const input = node(type === 'textarea' ? 'textarea' : 'input'); input.value = value; input.placeholder = placeholder; input.required = true; wrapper.append(input); return [wrapper, input];
}
function addMilestone(milestone) {
  const card = node('div', undefined, 'milestone-field'); card.dataset.id = milestone?.id || 'm' + Date.now().toString(36) + Math.random().toString(36).slice(2, 5);
  const head = node('div', undefined, 'section-title'); head.append(node('strong', 'MILESTONE ' + ($('milestone-fields').children.length + 1)));
  const remove = node('button', 'Remove', 'text-button'); remove.type = 'button'; remove.addEventListener('click', () => { card.remove(); dirtyPlan(); }); head.append(remove); card.append(head);
  const pair = node('div', undefined, 'two-inputs');
  const [titleLabel, title] = field('Milestone title', 'input', milestone?.title, 'e.g. First design concept'); title.dataset.field = 'title'; title.maxLength = 127;
  const [amountLabel, amount] = field('Agreed amount', 'input', milestone?.amount, 'e.g. 30.00'); amount.dataset.field = 'amount'; amount.pattern = '[0-9]+\\.[0-9]{2}'; amount.inputMode = 'decimal';
  pair.append(titleLabel, amountLabel); card.append(pair);
  const [criteriaLabel, criteria] = field('Acceptance criteria · one per line', 'textarea', milestone?.acceptance_criteria, 'e.g. PDF containing two concepts\nEditable source file'); criteria.dataset.field = 'acceptance_criteria'; criteria.maxLength = 5000; card.append(criteriaLabel);
  $('milestone-fields').append(card);
}
function dirtyPlan() { proposal = null; $('signature-dialog').close(); $('plan-status').textContent = 'Unsaved changes. Confirm the updated terms before preparing a payment.'; }
function currentRecord() { return state?.milestones[selected]; }
function payButton(operation, label) {
  const button = node('button', label); button.dataset.operation = operation; button.type = 'button'; button.classList.toggle('suggested', recommendation === operation);
  button.addEventListener('click', () => action(async () => {
    proposal = await api('/api/proposal', { version: state.version, milestone: selected, operation });
    const args = proposal.arguments; $('signature-title').textContent = 'Confirm ' + operation;
    $('signature-details').replaceChildren();
    for (const [key, value] of [['Action', operation], ['Milestone', state.agreement.milestones.find(m => m.id === selected).title], ['Amount', args.currency + ' ' + args.amount], ['Plan version', args.plan_version], ['Step', proposal.stage === 'create' ? 'Create buyer checkout' : operation === 'authorize' ? 'Place funds on hold' : 'Signed ' + operation]]) { $('signature-details').append(node('dt', key), node('dd', value)); }
    $('signature-explanation').textContent = proposal.stage === 'create' ? 'Create a Sandbox checkout for buyer approval. This request does not place a hold yet; you will sign a separate authorization after approval.' : explanations[operation]; $('signature-form').reset(); $('signature-dialog').showModal();
  }, button)); return button;
}
function render() {
  const select = $('milestone-select'); select.replaceChildren();
  if (state) {
    if (!state.milestones[selected]) selected = Object.keys(state.milestones)[0];
    for (const m of state.agreement.milestones) { const option = node('option', m.title); option.value = m.id; select.append(option); } select.value = selected;
  } else { select.append(node('option', 'Confirm a plan first')); }
  const record = currentRecord(), terms = state?.agreement.milestones.find(m => m.id === selected);
  document.querySelectorAll('.milestone-view').forEach(el => {
    el.replaceChildren(); if (!record) { el.append(node('p', 'Confirm a plan in Agree to get started.', 'muted')); return; }
    const card = node('article', undefined, 'milestone-card'); card.append(node('span', record.status.replaceAll('_', ' '), 'status'), node('h3', terms.title), node('p', record.amount.currency_code + ' ' + record.amount.value, 'amount'));
    card.append(node('p', 'Human confirmed · Refunded: ' + record.refunded, 'small muted'));
    const list = node('ul'); terms.acceptance_criteria.split('\n').filter(Boolean).forEach(c => list.append(node('li', c))); card.append(list);
    if (record.pending_operation) card.append(node('p', 'A request needs reconciliation. Recheck PayPal, then review a new signed retry of the same action.', 'small'));
    if (record.remote_status) card.append(node('p', 'Last PayPal check: ' + record.remote_status + (record.remote_authorization_status ? ' · Authorization ' + record.remote_authorization_status : ''), 'small muted'));
    el.append(card);
  });
  $('fund-actions').replaceChildren(); $('settle-actions').replaceChildren();
  $('refund-form').hidden = true; $('recheck').disabled = !record?.order_id;
  if (!record) return;
  $('delivery-note').value = record.delivery;
  if (['PLANNED', 'AWAITING_APPROVAL'].includes(record.status)) $('fund-actions').append(payButton('authorize', record.status === 'PLANNED' ? 'Review Sandbox reservation' : 'Authorize approved order'));
  if (record.status === 'AWAITING_APPROVAL' && record.approval_url) { const link = node('a', 'Open Sandbox buyer checkout ↗', 'checkout-link'); link.href = record.approval_url; link.target = '_blank'; link.rel = 'noopener noreferrer'; $('fund-actions').prepend(link); }
  if (record.status === 'AUTHORIZED') { $('settle-actions').append(payButton('capture', 'Accept & review release'), payButton('void', 'Review cancellation')); }
  if (['CAPTURED', 'PARTIALLY_REFUNDED'].includes(record.status)) { $('refund-form').hidden = false; $('refund-value').value = record.requested_refund || (Number(record.amount.value) - Number(record.refunded)).toFixed(2); $('settle-actions').append(payButton('refund', 'Review refund')); }
  const locked = Object.values(state.milestones).some(m => m.status !== 'PLANNED' || m.pending_operation);
  $('plan-form').querySelectorAll('input,select,textarea,button').forEach(el => { el.disabled = locked; });
  $('plan-status').textContent = locked ? 'Terms locked after checkout begins. Existing funds stay attached to these terms.' : 'Confirmed plan · version ' + state.version.slice(0, 8) + ' · human entered';
}
async function audit() {
  const entries = await api('/api/audit'); $('audit').replaceChildren();
  if (!entries.length) $('audit').append(node('li', 'No confirmed actions yet.', 'muted'));
  for (const entry of entries.slice(-40).reverse()) {
    const li = node('li'), details = node('details'), summary = node('summary', entry.kind.replaceAll('_', ' '));
    const time = node('time', new Date(entry.utc).toLocaleTimeString() + ' · #' + entry.seq); time.dateTime = entry.utc; summary.append(time); details.append(summary, node('pre', JSON.stringify(entry.details, null, 2))); li.append(details); $('audit').append(li);
  }
}
async function refresh(loadForm = false) {
  const result = await api('/api/state'); csrf = result.csrf; state = result.state;
  $('connection').textContent = result.payment_status;
  $('feature').querySelector('[value="suggest"]').hidden = !result.recommendations_enabled;
  if (loadForm && state) { $('currency').value = state.agreement.currency; $('total').value = state.agreement.total; $('milestone-fields').replaceChildren(); state.agreement.milestones.forEach(addMilestone); }
  render(); await audit();
}
async function ask(feature, question) {
  const chat = $('chat'); const human = node('article', undefined, 'bubble human'); human.append(node('span', 'YOU', 'small muted'), node('p', question)); chat.append(human);
  const waiting = node('article', 'Checking locally…', 'bubble'); chat.append(waiting); chat.scrollTop = chat.scrollHeight;
  try {
    const result = await api('/api/ai', { feature, question, milestone: selected || null, operation: $('explain-operation').value });
    waiting.replaceChildren(node('span', result.source === 'local_ai' ? 'AI ADVICE · VERIFY YOURSELF' : 'AI UNAVAILABLE · STATIC GUIDANCE', 'small muted'), node('p', result.text));
    waiting.classList.toggle('suggested', Boolean(result.action));
    if (result.checklist) { const list = node('ul'); for (const item of result.checklist) { const li = node('li'); li.append(node('strong', ({ met: 'Met', uncertain: 'Uncertain', not_met: 'Not met' })[item.verdict] + ' · ' + item.criterion), node('p', item.reason)); list.append(li); } waiting.append(list); }
    recommendation = result.action || null;
    document.querySelectorAll('[data-operation]').forEach(el => el.classList.toggle('suggested', el.dataset.operation === recommendation));
    await audit();
  } catch (error) { waiting.textContent = error.message; throw error; }
  finally { chat.scrollTop = chat.scrollHeight; }
}
document.querySelectorAll('[data-step]').forEach(button => button.addEventListener('click', () => switchStep(button.dataset.step)));
$('add-milestone').addEventListener('click', () => { addMilestone(); dirtyPlan(); });
$('plan-form').addEventListener('input', dirtyPlan);
$('plan-form').addEventListener('submit', event => { event.preventDefault(); action(async () => {
  const milestones = [...$('milestone-fields').children].map(card => { const m = { id: card.dataset.id, source_quote: 'Human entered and confirmed in the agreement form' }; card.querySelectorAll('[data-field]').forEach(el => { m[el.dataset.field] = el.value.trim(); }); return m; });
  await api('/api/plan', { agreement: { currency: $('currency').value, total: $('total').value.trim(), milestones } });
  await refresh(); notice('Terms confirmed. No payment has been sent.'); switchStep('fund');
}, event.submitter); });
$('milestone-select').addEventListener('change', () => { selected = $('milestone-select').value; proposal = null; recommendation = null; render(); });
$('delivery-form').addEventListener('submit', event => { event.preventDefault(); action(async () => { await api('/api/delivery', { version: state?.version, milestone: selected, notes: $('delivery-note').value }); await refresh(); notice('Delivery note saved. Review the files before releasing funds.'); }, event.submitter); });
$('refund-form').addEventListener('submit', event => { event.preventDefault(); action(async () => { await api('/api/refund-amount', { version: state.version, milestone: selected, amount: $('refund-value').value.trim() }); await refresh(); notice('Refund amount confirmed. Review and sign to send it.'); }, event.submitter); });
$('recheck').addEventListener('click', event => action(async () => { await api('/api/recheck', { version: state.version, milestone: selected }); await refresh(); notice('PayPal status rechecked. No new money operation was sent.'); }, event.target));
$('signature-form').addEventListener('submit', event => { event.preventDefault(); action(async () => {
  if (!proposal) throw new Error('Prepare a new proposal first');
  const ident = proposal.proposal_id; const signed = await api('/api/sign', { proposal_id: ident, signer: $('signer').value, accepted: $('accepted').checked });
  try { await api('/api/execute', { proposal_id: ident, signature: signed.signature }); notice('Sandbox request confirmed by the service. Check the current status below.'); }
  finally { proposal = null; $('signature-dialog').close(); await refresh(); }
}, event.submitter); });
$('cancel-signature').addEventListener('click', () => { proposal = null; $('signature-dialog').close(); });
$('feature').addEventListener('change', () => { $('explain-label').hidden = $('feature').value !== 'explain'; });
$('chat-form').addEventListener('submit', event => { event.preventDefault(); const question = $('question').value.trim(); action(async () => { await ask($('feature').value, question); $('question').value = ''; }, event.submitter); });
$('review-delivery').addEventListener('click', event => action(() => ask('review', 'Compare my saved delivery note with each agreed acceptance criterion.'), event.target));
$('refresh-audit').addEventListener('click', event => action(audit, event.target));
addMilestone(); action(async () => { await refresh(true); if (location.search.includes('checkout=')) { switchStep('fund'); notice('Returned from Sandbox checkout. Recheck the order, then review and sign an authorization. This page does not send a payment automatically.'); } });
