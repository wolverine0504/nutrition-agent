const $ = id => document.getElementById(id);
const nutrients = [
  ['kcal', 'Calories', 'kcal'],
  ['protein_g', 'Protein', 'g'],
  ['carbs_g', 'Carbs', 'g'],
  ['fat_g', 'Fat', 'g'],
];
const quantityFormat = new Intl.NumberFormat('en-US', {maximumFractionDigits: 2});
const sections = new Map();
let busy = false, refreshing = false, revision = 0;
let state = null, pendingRequest = null, pendingDecision = null;

function element(tag, text, className) {
  const el = document.createElement(tag);
  if (text != null) el.textContent = text;
  if (className) el.className = className;
  return el;
}

function quantity(value, unit) {
  return value == null ? '—' : `${quantityFormat.format(value)} ${unit}`;
}

function targetAmount(value, unit, metric) {
  const amount = quantity(value, unit);
  if (metric?.passed !== false) return amount;
  return `${amount} · ${Number(metric.actual) < Number(metric.lower) ? 'Low' : 'High'}`;
}

function status(text) { $('status').textContent = text; }

async function api(path, options = {}) {
  const response = await fetch(path, {cache: 'no-store', ...options});
  const data = await response.json();
  if (!response.ok) {
    throw new Error(typeof data.detail === 'string' ? data.detail : 'The request could not be completed.');
  }
  return data;
}

// Keep focused choice buttons and expanded details in place across unchanged poll results.
function renderChanged(section, data, render) {
  const signature = JSON.stringify(data);
  if (sections.get(section) === signature) return;
  sections.set(section, signature);
  render();
}

function drawMessages(messages) {
  renderChanged('messages', messages, () => {
    const log = $('messages');
    log.replaceChildren();
    if (!messages.length) {
      log.append(element('p', 'Check the ingredients on hand, then confirm inventory to start a plan.', 'empty'));
    }
    for (const message of messages) {
      for (const role of ['user', 'assistant']) {
        const article = element('article', null, `bubble ${role}`);
        const label = role === 'user' ? 'Participant' : 'Nutrition Agent';
        article.append(element('span', label, 'speaker'), element('pre', message[role]));
        if (role === 'assistant' && message.activity?.length) {
          const activity = element('details', null, 'activity');
          const text = message.activity.map(x => x.name.replaceAll('_', ' ') + (x.ok ? ' ✓' : ' · needs attention')).join(' → ');
          activity.append(element('summary', 'Tool activity'), element('p', text));
          article.append(activity);
        }
        log.append(article);
      }
    }
    log.scrollTop = log.scrollHeight;
  });
}

function drawGoals(goals) {
  $('goals').hidden = !goals;
  renderChanged('goals', goals, () => {
    for (const [id, values] of [['meal-goals', goals?.meal], ['daily-goals', goals?.daily]]) {
      $(id).replaceChildren();
      if (!values) {
        $(id).append(element('div', 'No goals set.', 'muted'));
        continue;
      }
      for (const [key, label, unit] of nutrients) {
        const line = element('div');
        line.append(element('dt', label), element('dd', quantity(values[key], unit)));
        $(id).append(line);
      }
    }
  });
}

function choice(label, command) {
  const button = element('button', label, 'secondary');
  button.type = 'button';
  button.addEventListener('click', () => send(command));
  return button;
}

function drawChoices(data, reviewing) {
  const recipes = data.stage === 'recipe_selection' && !reviewing ? data.recipe_options ?? [] : [];
  $('recipe-picker').hidden = !recipes.length;
  renderChanged('recipes', recipes, () => {
    $('recipe-options').replaceChildren(...recipes.map(r => choice(r.name, '/select ' + r.id)));
  });

  const meals = reviewing ? null : data.meal_choices;
  $('meal-choices').hidden = !meals?.options.length;
  renderChanged('batch choices', meals, () => {
    const options = meals?.options ?? [];
    const baseLabels = options.map(batch => {
      const date = batch.prepared_on ? `, prepared ${batch.prepared_on}` : '';
      return `${batch.name}${date}, ${batch.remaining_boxes} of ${batch.total_boxes} boxes left`;
    });
    const labels = baseLabels.map((label, index) => {
      const sameLabel = baseLabels.filter(candidate => candidate === label).length > 1;
      const savedAt = options[index].saved_at;
      if (!sameLabel || !savedAt) return label;
      return `${label} · saved ${new Date(savedAt).toLocaleTimeString('en-US')}`;
    });
    const buttons = options.map((batch, index) => {
      const label = labels[index];
      const sameLabel = labels.filter(candidate => candidate === label).length > 1;
      return choice(sameLabel ? `${label} · batch ${index + 1}` : label, `/meal-select ${meals.id} ${batch.id}`);
    });
    $('batch-options').replaceChildren(...buttons);
  });
}

function table(caption, headers, rows) {
  const container = element('div', null, 'table-scroll');
  const result = element('table');
  result.append(element('caption', caption));
  const head = result.createTHead().insertRow();
  for (const label of headers) {
    const cell = element('th', label);
    cell.scope = 'col';
    head.append(cell);
  }
  const body = result.createTBody();
  for (const [label, ...values] of rows) {
    const row = body.insertRow();
    const name = element('th', label);
    name.scope = 'row';
    row.append(name);
    for (const value of values) row.append(element('td', value));
  }
  container.append(result);
  return container;
}

function drawProposal(data, reviewing) {
  const proposal = data.proposal;
  const cooking = data.stage === 'cooking';
  const customizing = data.stage === 'customization' || data.stage === 'customization_limit';
  const confirming = data.stage === 'recipe_confirmation' || data.stage === 'customization_limit';
  const show = !!proposal && (customizing || confirming || cooking) && !reviewing;
  const newAdjustment = customizing && !proposal && data.selected_recipe && !reviewing;
  $('adjustment').hidden = !newAdjustment;
  $('selected-recipe').textContent = data.selected_recipe?.name ?? '';
  $('proposal').hidden = !show;
  $('accept').hidden = !show || cooking;
  $('accept').textContent = proposal && !proposal.passed ? 'Accept outside goals' : 'Accept recipe';
  $('cook-plan').hidden = !show || !cooking;
  $('cooking-note').hidden = $('cook-plan').hidden;
  const shortages = (proposal?.ingredients ?? []).filter(i => Number(i.missing) > 0);
  $('purchase').hidden = !show || cooking || !shortages.some(i => i.name.toLowerCase().includes('chicken'));

  const recalculate = show && !cooking && proposal.boxes > 1 && shortages.length > 0;
  $('recalculate').hidden = !recalculate;
  const adjustmentKey = recalculate ? proposal.id : newAdjustment ? data.selected_recipe.id : null;
  renderChanged('adjustment', [adjustmentKey, recalculate], () => {
    const form = $('adjust-form');
    if (recalculate) {
      $('recalculate-controls').append(form);
      $('recalculate').open = false;
      $('boxes').max = proposal.boxes - 1;
      $('boxes').value = proposal.boxes - 1;
      $('adjust').textContent = 'Recalculate plan';
    } else if (newAdjustment) {
      $('adjustment-controls').append(form);
      $('boxes').max = 99;
      $('boxes').value = data.selected_recipe.default_boxes ?? 4;
      $('adjust').textContent = 'Adjust to my goals';
    }
  });

  renderChanged('proposal', [proposal, cooking, data.goals?.meal], () => {
    $('proposal-details').replaceChildren();
    if (!proposal) return;
    $('proposal-title').textContent = cooking ? 'Cooking plan' : `Recipe proposal · ${proposal.boxes} ${proposal.boxes === 1 ? 'box' : 'boxes'}`;
    if (cooking) {
      $('proposal-note').textContent = 'When you finish cooking, review what you used and how many boxes you made.';
    } else {
      const fit = proposal.passed ? 'Nutrition goals met.' : 'Some nutrition goals are outside your target ranges.';
      const stock = proposal.can_accept
        ? 'Enough ingredients on hand.'
        : proposal.blocked_reason || 'Check the ingredient shortages before accepting.';
      $('proposal-note').textContent = `${fit} ${stock}`;
    }

    const nutrition = nutrients.map(([key, label, unit]) => [
      label,
      quantity(data.goals?.meal?.[key], unit),
      targetAmount(proposal.before?.[key], unit, proposal.before_metrics?.[key]),
      targetAmount(proposal.after?.[key], unit, proposal.after_metrics?.[key]),
    ]);
    $('proposal-details').append(table('Nutrition per box', ['Nutrient', 'Goal', 'Starting', 'Adjusted'], nutrition));

    const ingredients = (proposal.ingredients ?? []).map(item => [
      item.name,
      quantity(item.before, item.unit),
      quantity(item.needed, item.unit),
      quantity(item.available, item.unit),
      Number(item.missing) > 0 ? `${quantity(item.missing, item.unit)} short` : '—',
    ]);
    $('proposal-details').append(table(
      `Ingredients for ${proposal.boxes} ${proposal.boxes === 1 ? 'box' : 'boxes'}`,
      ['Ingredient', 'Starting', 'Needed', 'On hand', 'Shortage'], ingredients,
    ));

    if (proposal.instructions?.length) {
      const steps = element('ol');
      for (const instruction of proposal.instructions) steps.append(element('li', instruction));
      const details = element('details', null, 'cooking-steps');
      details.open = cooking;
      details.append(element('summary', 'Cooking instructions'), steps);
      $('proposal-details').append(details);
    }
  });
}

function syncControls() {
  document.querySelectorAll('button').forEach(button => {
    button.disabled = busy || (button.id === 'accept' && !state?.proposal?.can_accept);
  });
}

function draw(data) {
  state = data;
  $('mode').textContent = data.model_enabled ? 'Live model enabled' : 'Offline · model calls disabled';
  for (const key of ['inventory', 'batches', 'daily']) $(key).textContent = data[key];
  drawGoals(data.goals);
  drawMessages(data.messages);

  const reviewing = !!data.preview;
  $('preview').hidden = !reviewing;
  $('preview-title').textContent = data.preview?.title || 'Review changes';
  $('preview-text').textContent = data.preview?.text ?? '';
  $('confirm-inventory').hidden = data.stage !== 'inventory_confirmation' || reviewing;
  $('new-plan').hidden = data.stage === 'inventory_confirmation' || reviewing;
  drawChoices(data, reviewing);
  drawProposal(data, reviewing);
  syncControls();
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
  syncControls();
  status('Working…');
  try {
    const key = presenter ? 'X-Presenter-Code' : 'X-Interaction-Code';
    const code = $(presenter ? 'presenter-code' : 'code').value;
    const result = await api(path, {
      method: 'POST',
      headers: {'Content-Type': 'application/json', [key]: code},
      body: JSON.stringify(body),
    });
    if (path === '/api/messages' || presenter) {
      pendingRequest = null;
      $('message').value = '';
    }
    if (path === '/api/decisions' || presenter) pendingDecision = null;
    status(result.ok === false ? result.assistant : 'Shared workspace updated.');
    if (presenter) {
      draw(result);
    } else {
      try {
        draw(await api('/api/state'));
      } catch {
        // The write already succeeded. Let normal polling fetch the updated view.
      }
    }
  } catch (error) {
    status(error.message);
  } finally {
    busy = false;
    syncControls();
  }
}

function send(message) {
  if (busy) return;
  // Preserve the same receipt ID on network retries.
  if (!pendingRequest || pendingRequest.message !== message) {
    pendingRequest = {request_id: crypto.randomUUID(), message};
  }
  return mutate('/api/messages', pendingRequest);
}

$('compose').addEventListener('submit', event => {
  event.preventDefault();
  const text = $('message').value.trim();
  if (text) send(text);
});
document.querySelectorAll('[data-command]').forEach(button => {
  button.addEventListener('click', () => send(button.dataset.command));
});
for (const [id, approve] of [['approve', true], ['reject', false]]) {
  $(id).addEventListener('click', () => {
    if (busy || !state?.preview) return;
    if (pendingDecision?.preview_id !== state.preview.id || pendingDecision.approve !== approve) {
      pendingDecision = {request_id: crypto.randomUUID(), preview_id: state.preview.id, approve};
    }
    mutate('/api/decisions', pendingDecision);
  });
}
$('adjust-form').addEventListener('submit', event => {
  event.preventDefault();
  const boxes = $('boxes').value;
  if (boxes) send(boxes === '4' ? '/adjust' : '/adjust ' + boxes);
});
$('purchase').addEventListener('click', () => send('/purchase chicken 200'));
$('cook-plan').addEventListener('click', () => {
  if (state?.stage === 'cooking' && state.proposal) send('/cook-plan ' + state.proposal.id);
});
$('accept').addEventListener('click', () => {
  if (state?.proposal?.can_accept) {
    send('/accept-recipe ' + state.proposal.id + (state.proposal.passed ? '' : ' outside'));
  }
});
$('reset').addEventListener('click', () => {
  if (confirm('Restore the starting ingredients and goals, and clear all batches, meals, and chat?')) {
    mutate('/api/reset', {}, true);
  }
});
document.addEventListener('visibilitychange', refresh);
refresh();
setInterval(refresh, 2500);
