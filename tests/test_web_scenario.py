import json
from decimal import Decimal

from nutrition_demo.catalog import Catalog, DATA_DIR
from nutrition_demo.models import Nutrients


def load_scenario():
    return json.loads((DATA_DIR / 'web-scenario.json').read_text())


def test_curry_chicken_starts_with_real_ingredients_and_separate_goals():
    scenario = load_scenario()
    catalog = Catalog(scenario['catalog'])
    original = Catalog.load()
    recipe = catalog.recipe('curry')
    stock = {item['ingredient_id']: Decimal(item['quantity']) for item in scenario['inventory']}

    assert scenario['synthetic'] is True
    assert recipe['name'] == 'curry chicken'
    assert stock == {
        'chicken': 1000, 'potato': 600, 'onion': 300, 'carrot': 250, 'curry_block': 92,
        'rice': 600, 'broccoli': 500, 'garlic': 30, 'oil': 60, 'water': 1500,
    }
    for item in scenario['inventory']:
        ingredient = catalog.ingredient(item['ingredient_id'])
        assert ingredient['nutrients'] == original.ingredient(item['ingredient_id'])['nutrients']
        assert (item['unit'], item['state']) == (ingredient['unit'], ingredient['state'])
    assert scenario['profile']['meal_targets'] == {
        'kcal': '1000', 'protein_g': '70', 'carbs_g': '100', 'fat_g': '20',
    }
    assert scenario['profile']['daily_targets'] == {
        'kcal': '1800', 'protein_g': '120', 'carbs_g': '195', 'fat_g': '60',
    }


def test_curry_chicken_reference_passes_with_one_resolvable_shortage():
    scenario = load_scenario()
    catalog = Catalog(scenario['catalog'])
    reference = scenario['reference_plan']
    recipe = catalog.recipe(reference['recipe_id'])
    targets = Nutrients.from_dict(scenario['profile']['meal_targets'])
    base = catalog.calculate(recipe['id'], recipe['ingredients'], reference['boxes'], targets)
    adjusted = catalog.calculate(recipe['id'], reference['items'], reference['boxes'], targets)

    assert reference['boxes'] == 4
    assert catalog.validate_proposal(recipe['id'], reference['items'])
    assert not base['targets']['passed']
    assert base['nutrients']['protein_g'] < targets.protein_g
    assert base['nutrients']['carbs_g'] > targets.carbs_g
    assert adjusted['targets']['passed']

    stock = {item['ingredient_id']: Decimal(item['quantity']) for item in scenario['inventory']}
    shortage = {
        item['ingredient_id']: item['quantity'] - stock[item['ingredient_id']]
        for item in adjusted['items']
        if item['quantity'] > stock[item['ingredient_id']]
    }
    assert shortage == {'chicken': 160}
    chicken = next(item for item in adjusted['items'] if item['ingredient_id'] == 'chicken')
    assert stock['chicken'] + 200 - chicken['quantity'] == 40


def test_curry_chicken_instructions_follow_the_plan_quantities_and_yield():
    scenario = load_scenario()
    catalog = Catalog(scenario['catalog'])
    reference = scenario['reference_plan']
    base = catalog.recipe('curry')['ingredients']
    for items, rice, broth, chicken, boxes in (
        (base, '650 mL', '360 mL', '960 g', 4),
        (reference['items'], '500 mL', '360 mL', '1160 g', 4),
        ([{**item, 'quantity': str(Decimal(item['quantity']) / 2)} for item in reference['items']],
         '250 mL', '180 mL', '580 g', 2),
    ):
        instructions = catalog.customized_instructions('curry', items, boxes)
        assert any(rice in step and 'rice' in step for step in instructions)
        assert any(broth in step and 'water' in step for step in instructions)
        assert any(chicken in step and 'chicken breast' in step for step in instructions)
        assert any(f'{boxes} equal' in step for step in instructions)
        assert not any('two pots' in step or 'Divide  between' in step for step in instructions)
