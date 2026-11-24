from pathlib import Path
import json
from decimal import Decimal,ROUND_CEILING
from .models import Nutrients, ZERO, decimal
from .nutrition import scale_nutrients, per_box, used_cost, aggregate_cost, validate_targets, checked_quantity

DATA_DIR = Path(__file__).resolve().parent.parent / 'demo_data'


class Catalog:
    def __init__(self, data, manifest=None):
        self.data = data
        self.ingredients = {i['id']: i for i in data['ingredients']}
        self.recipes = {r['id']: r for r in data['recipes']}
        self.manifest = manifest or {}

    @classmethod
    def load(cls, directory=DATA_DIR):
        p=Path(directory)
        return cls(json.loads((p/'catalog.json').read_text()),json.loads((p/'source_manifest.json').read_text()))

    def ingredient(self, ingredient_id):
        if ingredient_id not in self.ingredients:
            raise ValueError('Unknown ingredient; specify product, nutrition label and unit first')
        return self.ingredients[ingredient_id]

    def recipe(self, recipe_id):
        if recipe_id not in self.recipes:
            raise ValueError('Unknown recipe')
        return self.recipes[recipe_id]

    def snapshot(self, item):
        ref = self.ingredient(item['ingredient_id'])
        qty = checked_quantity(item['quantity'],item['unit'],item['state'],ref)
        if ref.get('nutrients') is None:
            raise ValueError('Missing nutrition: clarify brand/label before calculation')
        nutrients=scale_nutrients(Nutrients.from_dict(ref['nutrients']), qty, ref['reference_quantity'])
        price=ref.get('price')
        return {**item, 'quantity':qty, 'nutrients':nutrients,
                'cost':used_cost(price['usd'],qty,price['quantity']) if price else None,
                'source':ref['source'], 'price_source':price, 'name':ref['name']}

    def calculate(self, recipe_id, items, boxes, targets=None):
        self.recipe(recipe_id)
        if len({i['ingredient_id'] for i in items}) != len(items):
            raise ValueError('Duplicate ingredient')
        snapshots=[self.snapshot(i) for i in items]
        total=sum((s['nutrients'] for s in snapshots),ZERO)
        each=per_box(total,boxes)
        return {'recipe_id':recipe_id, 'boxes':boxes, 'instructions':self.customized_instructions(recipe_id,items,boxes), 'items':snapshots, 'total_nutrients':total,
                'nutrients':{k:getattr(each,k) for k in each.__dataclass_fields__},
                'cost':aggregate_cost(s['cost'] for s in snapshots),
                'targets':validate_targets(each,targets) if targets else None,
                'source_statuses': sorted({s['source']['status'] for s in snapshots})}

    def validate_proposal(self, recipe_id, items):
        recipe=self.recipe(recipe_id)
        ids={i['ingredient_id'] for i in items if decimal(i['quantity']) > 0}
        # Essential ingredients cannot be removed. Optional substitutions are explicit, not implicit.
        if not set(recipe['essential_ingredients']) <= ids:
            raise ValueError('Dish identity: essential ingredients missing')
        original=set(i['ingredient_id'] for i in recipe['ingredients']) | set(recipe.get('unquantified_ingredient_ids',[]))
        allowed=original | set(recipe.get('allowed_substitutions',[]))
        if len(ids-original)>1 or not ids <= allowed:
            raise ValueError('Unapproved substitution; preserve dish identity')
        for i in items:
            self.snapshot(i)
        return True

    def recommend(self, inventory, profile):
        available={i['ingredient_id']:decimal(i['quantity']) for i in inventory}
        result=[]
        for recipe in self.recipes.values():
            missing=[]
            for item in recipe['ingredients']:
                need=decimal(item['quantity'])-available.get(item['ingredient_id'],Decimal(0))
                if need>0:
                    ref=self.ingredient(item['ingredient_id'])
                    missing.append({**item,'quantity':need,'name':ref['name'],
                                    'product_url':ref.get('product_url'), 'link_status':ref.get('link_status','missing'),
                                    'packages_needed':int((need/decimal(ref['package_quantity'],positive=True)).to_integral_value(rounding=ROUND_CEILING))
                                    if ref.get('package_quantity') and ref.get('package_unit')==item['unit'] else None})
            costs=[]
            for item in recipe['ingredients']:
                ref=self.ingredient(item['ingredient_id']);price=ref.get('price')
                costs.append(used_cost(price['usd'],item['quantity'],price['quantity']) if price else None)
            result.append({**recipe,'shopping':missing,'cost':aggregate_cost(costs),
                           'capacity_note':'Capacity is a reference; verify your equipment and actual portions.',
                           'known_equipment':profile.get('equipment',[])})
        return sorted(result,key=lambda r:len(r['shopping']))

    def suggest_additions(self, box, targets):
        result=[]
        for ingredient in self.ingredients.values():
            if not ingredient.get('addon_serving') or not ingredient.get('nutrients'):
                continue
            item={'ingredient_id':ingredient['id'],'quantity':ingredient['addon_serving'],
                  'unit':ingredient['unit'],'state':ingredient['state']}
            snapshot=self.snapshot(item)
            whole=box+snapshot['nutrients']
            result.append({'items':[item], 'meal_nutrients':whole,
                           'targets':validate_targets(whole,targets), 'source':snapshot['source']})
        return result


    def _demo_curry_instructions(self, recipe, items, boxes):
        quantities = {item['ingredient_id']: decimal(item['quantity']) for item in items}
        quantities = {ingredient_id: quantity for ingredient_id, quantity in quantities.items() if quantity > 0}

        def amount(ingredient_id):
            return format(quantities[ingredient_id].normalize(), 'f') + ' ' + self.ingredient(ingredient_id)['unit']

        def ingredient_list(ids):
            return ' and '.join(f"{amount(i)} of {self.ingredient(i)['name'].lower()}" for i in ids if i in quantities)

        water = quantities.get('water', Decimal(0))
        roux = quantities.get('curry_block', Decimal(0))
        curry_water = roux * decimal(recipe['demo_curry_water_ml_per_g_roux'])
        rice_water = water - curry_water
        if 'rice' not in quantities:
            curry_water = water

        steps = []
        if 'rice' in quantities:
            rice_liquid = f" with {rice_water.normalize():f} mL of water" if rice_water > 0 else ''
            steps.append(f"Cook {amount('rice')} of dry white rice{rice_liquid}. Follow your rice cooker or package directions.")
        if 'broccoli' in quantities:
            steps.append(f"Roast {amount('broccoli')} of frozen broccoli separately until tender.")
        steps.append('Chop the vegetables. Cut the raw chicken on a separate board.')
        if 'oil' in quantities:
            steps.append(f"Heat {amount('oil')} of olive oil in the curry pot.")
        aromatics = ingredient_list(['onion', 'garlic'])
        if aromatics:
            order = ' Add the garlic last.' if 'onion' in quantities and 'garlic' in quantities else ''
            steps.append(f"Sauté {aromatics}.{order}")
        vegetables = ingredient_list(['potato', 'carrot', 'mushroom'])
        if vegetables:
            if curry_water > 0 and ('rice' not in quantities or rice_water > 0):
                steps.append(f'Add {vegetables} and {curry_water.normalize():f} mL of water to the pot. Simmer until the vegetables begin to soften.')
            else:
                steps.append(f'Add {vegetables} to the pot. Confirm the water amounts before simmering.')
        if 'chicken' in quantities:
            steps.append(f"Add {amount('chicken')} of raw chicken breast. Simmer until the chicken reaches 165°F (74°C), measured with a food thermometer.")
        if 'curry_block' in quantities:
            steps.append(f"Take the pot off the heat and dissolve {amount('curry_block')} of curry roux. Return to low heat and stir until thickened.")
        sides = ' and '.join(side for side in ('rice', 'broccoli') if side in quantities)
        serving = f' with the {sides}' if sides else ''
        steps.append(f'Divide the curry{serving} into {boxes} equal portions. Record the actual ingredients used and number of boxes made.')
        return steps

    def customized_instructions(self,recipe_id,items,boxes):
        recipe=self.recipe(recipe_id)
        if 'demo_curry_water_ml_per_g_roux' in recipe:
            return self._demo_curry_instructions(recipe, items, boxes)
        by_id={i['ingredient_id']:i for i in items}
        def amount(id,divisor=1):
            i=by_id[id]
            return format(decimal(i['quantity'])/divisor,'f')+' '+i['unit']
        def names(ids):return '、'.join(self.ingredient(i)['name'] for i in ids if i in by_id)
        result=['Prepare the confirmed ingredient quantities; split cooking according to equipment capacity.']
        if recipe_id=='curry':
            if 'rice' in by_id:result.append('First cook dry rice: '+amount('rice')+'; confirm liquid for the rice and equipment.')
            if 'broccoli' in by_id:result.append('Roast frozen broccoli separately: '+amount('broccoli')+'; keep separate according to the source procedure.')
            aromatics=names(['onion','garlic'])
            if aromatics:result.append('First saute '+aromatics+'; briefly saute garlic last if used.')
            result.append('Divide '+names(['potato','carrot'])+' between two pots; simmer with the confirmed water quantity until nearly cooked.')
            if 'chicken' in by_id:result.append('Add chicken breast later to each pot: '+amount('chicken',2)+'; return to a simmer and verify source doneness with a thermometer.')
            if 'curry_block' in by_id:result.append('Turn off heat and dissolve curry roux in each pot: '+amount('curry_block',2)+'; stir over low heat to thicken.')
        elif recipe_id=='nikujaga':
            result+=['Dry-brine under refrigeration and cut vegetables according to the source; first cook dry rice: '+amount('rice')+'。' if 'rice' in by_id else 'Dry-brine under refrigeration and cut vegetables according to the source.',
                     'Saute the recipe vegetables in two pots, add confirmed stock and seasonings, and simmer vegetables until nearly cooked.']
            if 'pork_loin' in by_id:result.append('Add pork loin later in batches to each pot: '+amount('pork_loin',2)+'; check center doneness and rest according to the source.')
            result.append('If needed, reduce some sauce separately to coat the meat; avoid prolonged simmering.')
            if 'broccoli' in by_id:result.append('Broccoli: '+amount('broccoli')+'; heat separately.')
        elif recipe_id=='bbq':
            if 'pork_shoulder' in by_id:result.append('Pork shoulder: '+amount('pork_shoulder')+'; pressure-cook according to the equipment and source, then shred after releasing pressure.')
            if 'bbq_sauce' in by_id:result.append('Mix in BBQ sauce: '+amount('bbq_sauce')+'。')
            result.append('Cook macaroni according to the package, drain, and mix in the confirmed butter, dairy, and cheese.')
            if 'broccoli' in by_id:result.append('Broccoli: '+amount('broccoli')+'; roast separately and keep outputs with and without vegetables in separate batches.')
        elif recipe_id=='rice_pot':
            result.append('Work sequentially in two pots, using half the confirmed total in each.')
            if 'beef' in by_id:result.append('Saute beef in each pot: '+amount('beef',2)+'; then saute the recipe vegetables.')
            if 'broth' in by_id:result.append('Cancel; use stock per pot: '+amount('broth',2)+' to deglaze; this is the entire stock quantity per pot.')
            if 'rice' in by_id:result.append('Layer dry rice into each pot: '+amount('rice',2)+'; do not stir down and verify the contents are below the half-full line.')
            result.append('Source timing: High for 4 minutes, natural release for 10 minutes; verify the actual rice, liquid, and equipment.')
            if 'canned_chicken' in by_id:result.append('Finally mix drained chicken breast into each pot: '+amount('canned_chicken',2)+'; then heat and measure temperature according to the source.')
            if 'broccoli' in by_id:result.append('Heat broccoli separately: '+amount('broccoli')+'。')
        else:result+=recipe['steps']
        result.append('Planned output: '+str(boxes)+' boxes; confirm actual usage, output count, and batch composition after cooking. Source timings are references; verify doneness against sources and measurements.')
        return result

    def evidence(self,recipe_id,steps=None):
        """Small stable citations: the model selects existing text, never authors the evidence."""
        import re
        recipe=self.recipe(recipe_id)
        result=[{'id':'step:'+str(i+1),'page':None,'text':text,'kind':'confirmed_or_source_step'}
                for i,text in enumerate(steps if steps is not None else recipe['steps'])]
        if not any(r['recipe_id']==recipe_id for r in self.manifest.get('recipes',[])):return result
        path=DATA_DIR/'sources'/(recipe_id+'.txt')
        if not path.exists():return result
        text=path.read_text()
        pages=re.split(r'PAGE\s+(\d+)[^\n]*\n',text)
        for index in range(1,len(pages),2):
            page=int(pages[index]);body=pages[index+1]
            chunks=[];current=''
            for line in body.splitlines():
                if len(current)+len(line)>400 and current:
                    chunks.append(current.strip());current=''
                current+=line+'\n'
            if current.strip():chunks.append(current.strip())
            for i,chunk in enumerate(chunks):
                result.append({'id':f'source:{page}:{i+1}','page':page,'text':chunk,'kind':'historical_source'})
            if recipe_id=='curry' and page==2:
                summary=body.split('1 Prepare the ingredients')[0].strip()
                result.append({'id':'source:2:summary','page':2,'text':summary,'kind':'historical_source'})
        return result
