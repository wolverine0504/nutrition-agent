"""Presentation planning actions built on the shared calculator and workflow."""
from decimal import Decimal
from .models import Nutrients, Result, decimal
from .workflow import Workflow

PROPOSAL_STAGES = {'recipe_confirmation', 'customization_limit'}
EDITABLE_STAGES = PROPOSAL_STAGES | {'customization'}


def amount(value):
    return format(Decimal(str(value)).quantize(Decimal('0.01')), 'f').rstrip('0').rstrip('.')


class WebWorkflow(Workflow):
    def __init__(self, service, session_id, scenario):
        super().__init__(service, session_id)
        self.scenario = scenario

    def ingredient_name(self, ingredient_id):
        item = self.service.catalog.ingredients.get(ingredient_id)
        return item['name'] if item else 'Unlisted ingredient'

    def recipe_option(self, recipe_id):
        recipe = self.service.catalog.recipes.get(recipe_id)
        return {'id': recipe_id, 'name': recipe['name'],
                'default_boxes': recipe.get('default_requested_boxes', 6)} if recipe else None

    def selection_command(self, message):
        name = ' '.join(message.casefold().strip().rstrip('.!?').split())
        state = self.load()
        if state['stage'] == 'recipe_selection':
            for recipe in self.service.catalog.recipes.values():
                choices = [recipe['id'], recipe['name'], *recipe.get('aliases', [])]
                if name in {choice.casefold() for choice in choices}:
                    return '/select ' + recipe['id']
        return None

    def stock_check(self, ingredients):
        rows = self.service.list_inventory()
        if not rows.ok:
            raise RuntimeError('Cannot check inventory')
        stock = {row['ingredient_id']: row for row in rows.data['items']}
        result = {}
        for item in ingredients:
            key = item['ingredient_id']
            row = stock.get(key)
            available = Decimal(str(row['quantity'])) if row and (
                row['unit'], row['state']
            ) == (item['unit'], item['state']) else Decimal(0)
            needed = Decimal(str(item['quantity']))
            result[key] = {
                'name': self.ingredient_name(key), 'unit': item['unit'],
                'needed': needed, 'available': available, 'missing': max(Decimal(0), needed - available),
            }
        return result

    def scaled(self, items, boxes, base_boxes):
        return [{**item, 'quantity': format(Decimal(str(item['quantity'])) * boxes / base_boxes, 'f')}
                for item in items]

    def proposal_view(self, state):
        proposal = state.get('proposal')
        if not proposal or state['stage'] not in (PROPOSAL_STAGES | {'cooking', 'preview_confirmation'}):
            return None
        recipe = self.service.catalog.recipe(proposal['calculation']['recipe_id'])
        base_boxes = recipe.get('default_requested_boxes', 6)
        base_items = self.scaled(recipe['ingredients'], proposal['boxes'], base_boxes)
        original = {i['ingredient_id']: i['quantity'] for i in base_items}
        calculation = proposal['calculation']
        targets = self.proposal_targets(proposal)
        baseline = self.service.effective_catalog().calculate(recipe['id'], base_items, proposal['boxes'], targets)
        proposed_ids = {item['ingredient_id'] for item in proposal['items']}
        removed = [{**item, 'quantity': '0'} for item in base_items if item['ingredient_id'] not in proposed_ids]
        current = self.stock_check([*proposal['items'], *removed])
        ingredients = [{**row, 'before': original.get(key, '0')}
                       for key, row in current.items()]
        goals_changed = self.current_goals() != targets
        shortage = any(row['missing'] for row in ingredients)
        return {
            'id': proposal['id'],
            'passed': calculation['targets']['passed'],
            'can_accept': state['stage'] in PROPOSAL_STAGES and not state.get('pending_preview')
            and not shortage and not goals_changed,
            'blocked_reason': 'Your goals changed. Calculate a new proposal.' if goals_changed else
            'Check the ingredient shortages before accepting.' if shortage else '',
            'boxes': proposal['boxes'], 'before': baseline['nutrients'],
            'after': calculation['nutrients'], 'before_metrics': baseline['targets']['metrics'],
            'after_metrics': calculation['targets']['metrics'],
            'ingredients': ingredients, 'instructions': calculation['instructions'],
        }

    def adjust(self, boxes):
        state = self.load()
        reference = self.scenario['reference_plan']
        if state['stage'] not in EDITABLE_STAGES or state.get('pending_preview'):
            return Result(False, {}, 'Select a recipe before adjusting your plan.')
        if state['selected_recipe'] != reference['recipe_id']:
            return Result(False, {}, 'The offline example is available for curry chicken.')
        if not self.has_reference_goals():
            return Result(False, {}, 'The offline example uses the demo’s starting per-meal goals. For different goals, ask the live bot to calculate a proposal.')
        return self.propose(self.scaled(reference['items'], boxes, reference['boxes']), boxes)

    def has_reference_goals(self):
        return self.current_goals() == Nutrients.from_dict(self.scenario['profile']['meal_targets'])

    def current_goals(self):
        saved = self.service.get_profile().data.get('meal_targets')
        return Nutrients.from_dict(saved) if saved else None

    @staticmethod
    def proposal_targets(proposal):
        return Nutrients.from_dict({
            key: row['target'] for key, row in proposal['calculation']['targets']['metrics'].items()
        })

    def propose(self, items, boxes):
        state = self.load()
        if state['stage'] not in EDITABLE_STAGES or state.get('pending_preview'):
            return Result(False, {}, 'Select a recipe before adjusting your plan.')
        try:
            for item in items:
                decimal(item['quantity'], positive=True)
        except (ValueError, KeyError):
            return Result(False, {}, 'Proposed ingredient quantities must be positive.')
        result = super().propose(items, boxes)
        if result.ok:
            state = self.load()
            state['waiting_question'] = None
            self.save(state)
        return result

    def track_preview(self, result):
        if result.ok:
            state = self.load()
            state.update(
                pending_preview=result.data['id'], pending_preview_kind=result.data['kind'],
                preview_return_stage=state['stage'], stage='preview_confirmation', waiting_question=None,
            )
            self.save(state)
        return result

    def purchase_chicken(self, grams):
        if self.load().get('pending_preview'):
            return Result(False, {}, 'Approve or reject the open preview first.')
        if not 0 < grams <= 100000:
            return Result(False, {}, 'Enter a positive chicken quantity in grams.')
        return self.track_preview(self.service.preview_inventory_change([
            {'ingredient_id': 'chicken', 'quantity': str(grams), 'unit': 'g', 'state': 'raw'},
        ], mode='purchase'))

    def cook_plan(self, proposal_id):
        state = self.load()
        proposal = state.get('proposal')
        if state['stage'] != 'cooking' or not proposal or not state.get('accepted_recipe'):
            return Result(False, {}, 'Accept a recipe before reporting what you cooked.')
        if proposal_id != state['accepted_recipe'] or proposal_id != proposal['id']:
            return Result(False, {}, 'The cooking plan changed. Review the current plan before reporting what you used.')
        items = proposal['items']
        recipe = self.service.catalog.recipe(state['selected_recipe'])
        allocations = [{'cooking_ingredient_id': i['ingredient_id'], 'quantity': i['quantity'], 'unit': i['unit']}
                       for i in items]
        return self.track_preview(self.service.preview_preparation(recipe['id'], items, [
            {'name': recipe['name'], 'boxes': proposal['boxes'], 'allocations': allocations},
        ], prepared_on='today'))

    def command(self, message):
        parts = message.split()
        command = parts[0] if parts else ''
        state = self.load()
        if command == '/inventory-ok' and state['stage'] != 'inventory_confirmation':
            return Result(False, {}, 'Start a new plan before confirming inventory again.')
        if command == '/select' and state['stage'] not in (EDITABLE_STAGES | {'recipe_selection'}):
            return Result(False, {}, 'Confirm inventory before selecting a recipe.')
        if command == '/accept-recipe':
            proposal = state.get('proposal')
            if state['stage'] not in PROPOSAL_STAGES or not proposal or state.get('pending_preview'):
                return Result(False, {}, 'There is no recipe proposal to accept now.')
            if len(parts) < 2 or parts[1] != proposal['id']:
                return Result(False, {}, 'Review the current recipe proposal.')
            if any(row['missing'] for row in self.stock_check(proposal['items']).values()):
                return Result(False, {}, 'This plan needs more ingredients. Review the shortage before accepting.')
            if self.current_goals() != self.proposal_targets(proposal):
                return Result(False, {}, 'The per-meal goals changed. Calculate a new proposal before accepting.')
        result = super().command(message)
        if not result.ok:
            return result
        if command == '/select' or command == '/new-plan' or result.data.get('batch_ids'):
            current = self.load()
            current['waiting_question'] = None
            if command == '/new-plan':
                current.pop('web_pending_meal', None)
                current['pending_consumption'] = None
            if result.data.get('batch_ids') and state.get('pending_preview_kind') == 'preparation':
                current.update(proposal=None, selected_recipe=None, accepted_recipe=None, attempts=0)
            self.save(current)
        return result
