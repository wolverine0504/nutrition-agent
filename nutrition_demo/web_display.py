"""Readable views of public demo data, with internal IDs kept out of captions."""
from decimal import Decimal

from .display import preview as detailed_preview, render
from .models import Result
from .web_planning import amount


def nutrition(values):
    return ' · '.join(f'{amount(values[key])} {unit}{label}' for key, unit, label in (
        ('kcal', 'kcal', ''), ('protein_g', 'g', ' protein'),
        ('carbs_g', 'g', ' carbs'), ('fat_g', 'g', ' fat'),
    ))


def inventory(workflow, items):
    return '\n'.join(f"{workflow.ingredient_name(i['ingredient_id'])}: {amount(i['quantity'])} {i['unit']}"
                     for i in items) or 'No ingredients on hand.'


def batches(rows):
    return '\n\n'.join(f"{b['name']} · Remaining {b['remaining_boxes']}/{b['total_boxes']} boxes\n"
                       f"Per box: {nutrition(b['box_nutrients'])}" for b in rows) or 'No batches prepared yet.'


def recipe_text(workflow, recipe):
    parts = [recipe['name'], inventory(workflow, recipe['ingredients'])]
    parts.extend(f'{index}. {step}' for index, step in enumerate(recipe.get('steps', []), start=1))
    return '\n'.join(parts)


def daily(data, *, today=True):
    if not data['events']:
        return 'No meals logged today.\nLog a prepared box to see your progress.' if today else f"No meals logged for {data['day']}."
    count = data['events']
    return f"{count} meal{'s' if count != 1 else ''} logged {'today' if today else 'for ' + data['day']}.\n{nutrition(data['total'])}"


def money(cost):
    if cost.get('complete'):
        return f"Cost per box: US${amount(cost['total'])}"
    if Decimal(str(cost.get('known_subtotal') or 0)):
        return f"Known cost per box: US${amount(cost['known_subtotal'])} (some prices missing)"
    return 'Cost per box: unavailable (prices not provided)'


def preview(workflow, data):
    kind, changes = data['kind'], data['changes']
    title, lines = 'Review change', []
    if kind == 'preparation':
        outputs = [row for row in changes if 'batch' in row]
        box_count = sum(row['boxes'] for row in outputs)
        title = (f"Save {box_count} {'box' if box_count == 1 else 'boxes'} of {outputs[0]['batch']}?"
                 if len(outputs) == 1 else f'Save {len(outputs)} batches ({box_count} boxes)?')
        lines.append('Ingredients to deduct from inventory')
        lines.extend(f"{workflow.ingredient_name(row['ingredient_id'])}: {amount(row['consume'])} {row['unit']}"
                     for row in changes if 'consume' in row)
        for row in outputs:
            lines += ['', f"{row['batch']} · {row['boxes']} boxes",
                      f"Per box: {nutrition(row['box_nutrients'])}", money(row['box_cost'])]
            if row.get('targets'):
                labels = {'kcal': 'Calories', 'protein_g': 'Protein', 'carbs_g': 'Carbs', 'fat_g': 'Fat'}
                missed = [f"{labels[key]}: {amount(metric['actual'])} (target range "
                          f"{amount(metric['lower'])}–{amount(metric['upper'])})"
                          for key, metric in row['targets']['metrics'].items() if not metric['passed']]
                lines.extend(missed or ['All per-meal goals met.'])
        for row in changes:
            if 'disposition' in row:
                item = row['disposition']
                lines.append(f"{item['kind'].capitalize()}: {workflow.ingredient_name(item['cooking_ingredient_id'])}, "
                             f"{amount(item['quantity'])} {item['unit']}")
    elif kind == 'inventory':
        title = 'Update ingredient inventory?'
        lines = [f"{workflow.ingredient_name(row['ingredient_id'])}: {amount(row['before'])} → "
                 f"{amount(row['after'])} {row['unit']}" for row in changes]
    elif kind == 'correction':
        title = 'Update the logged meal?'
        for row in changes:
            if 'before_event' in row:
                before, after = row['before_event'], row['after_event']
                lines += [f"Before: {before['batch_name']} · {before['eaten_on']} · {before.get('meal') or 'meal'}",
                          f"After: {after['batch_name']} · {after['eaten_on']} · {after.get('meal') or 'meal'}",
                          f"Nutrition after correction: {nutrition(after['total_nutrients'])}"]
                if before['addon_items'] != after['addon_items']:
                    lines += ['Previous add-ons:', inventory(workflow, before['addon_items']) if before['addon_items'] else 'None',
                              'Updated add-ons:', inventory(workflow, after['addon_items']) if after['addon_items'] else 'None']
            if 'inventory_delta' in row:
                lines.append(f"{workflow.ingredient_name(row['ingredient_id'])}: "
                             f"{amount(row['inventory_delta'])} {row['unit']} inventory change")
    elif kind == 'profile':
        title = 'Update nutrition goals?'
        for row in changes:
            profile = row.get('profile', {})
            for key, label in (('meal_targets', 'Per meal'), ('daily_targets', 'Per day')):
                if profile.get(key):
                    lines.append(f"{label}: {nutrition(profile[key])}")
            if profile.get('timezone'):
                lines.append(f"Timezone: {profile['timezone']}")
            if profile.get('container_ml') is not None:
                lines.append(f"Container size: {amount(profile['container_ml'])} mL")
            if profile.get('equipment'):
                lines.append(f"Equipment: {', '.join(profile['equipment'])}")
    else:
        lines.append(detailed_preview(data).rsplit('\nPreview ID: ', 1)[0])
    return {'id': data['id'], 'kind': kind, 'title': title,
            'text': '\n'.join(lines) or data['summary']}


def reply(workflow, result, message):
    if not result.ok:
        return render(result)
    data = result.data
    if data.get('batch_ids'):
        ids = set(data['batch_ids'])
        saved = [b for b in workflow.service.batch_summary().data['batches'] if b['id'] in ids]
        details = ', '.join(f"{b['total_boxes']} {'box' if b['total_boxes'] == 1 else 'boxes'} of {b['name']}" for b in saved)
        return f'Saved {details}. Inventory updated.' if details else 'Batch saved. Inventory updated.'
    if data.get('rejected'):
        return 'Preview rejected. Nothing was changed.'
    if data.get('updated_inventory'):
        return 'Inventory updated. The plan now shows whether there is enough stock.'
    if data.get('kind') and 'changes' in data:
        return 'Review the preview below before saving.'
    if data.get('inventory_confirmed'):
        return 'Inventory confirmed. Choose a recipe below or type its name.'
    if 'selected_recipe' in data and 'tool_results' not in data:
        option = workflow.recipe_option(data['selected_recipe'])
        return f"Selected {option['name']}. Use Adjust to my goals to calculate a plan."
    if data.get('accepted_recipe') and 'tool_results' not in data:
        return 'Recipe accepted. After cooking, confirm what you used and how many boxes you made.'
    if 'calculation' in data:
        return 'Recipe adjusted. Review the nutrition and inventory check below.'
    if 'items' in data and 'addon_version' not in data:
        return inventory(workflow, data['items'])
    if 'inventory' in data:
        return 'Review the ingredient quantities, then choose Confirm inventory.'
    if 'batches' in data:
        return batches(data['batches'])
    if 'scope' in data:
        return daily(data, today=message in {'/daily', '/daily today'})
    if 'recipe' in data:
        return recipe_text(workflow, data['recipe'])
    if 'recipes' in data:
        return '\n\n'.join(recipe_text(workflow, recipe) for recipe in data['recipes']) or 'No recipes available.'
    if 'event_id' in data and 'remaining_boxes' in data:
        meal = f" for {data['meal']}" if data.get('meal') else ''
        remaining = data['remaining_boxes']
        return f"Logged one box of {data['batch_name']}{meal}. {remaining} {'box' if remaining == 1 else 'boxes'} remaining."
    if 'tool_results' in data:
        results = data['tool_results']
        successful = {record['name'] for record in results if record['result']['ok']}
        if any(r['result']['ok'] and 'changes' in r['result']['data'] and 'kind' in r['result']['data'] for r in results):
            return 'Review the preview below before saving.'
        if 'propose_recipe' in successful:
            return 'Recipe adjusted. Review the nutrition and inventory check below.'
        if 'select_recipe' in successful:
            return 'Recipe selected. Use Adjust to my goals to calculate a plan.'
        # Display the latest question, if any, and the same names and quantities
        # used by the sidebar for read results from the live bot.
        lines = [data['message']] if data.get('waiting_question') or data.get('limit_reached') else []
        for record in results:
            tool = record['result']
            if record['name'] == 'ask_user' and tool['ok']:
                continue
            if tool['data'] or tool.get('error'):
                lines.append(reply(workflow, Result(**tool), message))
        return '\n\n'.join(lines) or 'No results to display.'
    return render(result)
