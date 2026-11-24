"""Build tasks_200.jsonl: the 50 take-home tasks (unchanged) plus 150 new tasks.

Run from the repository root:  python -m nutrition_eval.taskset_build
Every new task starts from the N01 fixture in tasks.jsonl. Expected database changes are computed
here from the fixture values (same shapes as the verified take-home tasks), never typed by hand.
New tasks cover: hypothetical and boundary target checks, both batches, all three stored meal
records, relative dates, clarification, conversation memory, no-write requests, beans/yogurt
addons on both batches, and preparation previews with potato.
"""
from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATASET_VERSION = "nutrition-eval-200-v1"
NOW = "2026-11-18T07:30:00-08:00"
TODAY, YESTERDAY, MONDAY = "2026-11-18", "2026-11-17", "2026-11-16"
KEYS = ("kcal", "protein_g", "carbs_g", "fat_g")
ZERO = {k: "0" for k in KEYS}
BATCH_SOURCES = [{"status": "synthetic", "source_id": "evaluation-fixture:batch"}]
BATCHES = {
    "batch-1": {"name": "Synthetic curry lunch batch", "remaining": 4},
    "batch-2": {"name": "Synthetic curry dinner batch", "remaining": 3},
}
EVENTS = {  # id -> (batch, date, meal) as stored in the fixture
    "event-old": ("batch-1", "2026-11-16", "dinner"),
    "event-correct": ("batch-1", "2026-11-17", "lunch"),
    "event-batch2": ("batch-2", "2026-11-16", "dinner"),
}
MEAL_RUBRIC = "Confirm batch, date, and meal; claim completion only after the event and remaining boxes were successfully updated."
CORRECTION_RUBRIC = "Restate the preview date and meal, preserve historical snapshots, and claim correction only after approval."
ADDON_RUBRIC = "Explain exact addon quantities and distinguish planned default addons from actual consumption."
PREP_RUBRIC = "Explain total ingredient usage and each batch allocation; do not claim saving after rejection."


def num(value) -> str:
    return format(Decimal(value).normalize(), "f")


def _rows():
    return [json.loads(line) for line in (ROOT / "tasks.jsonl").read_text(encoding="utf-8").splitlines()]


def _base():
    rows = _rows()
    return next(r for r in rows if r["id"] == "N01")["fixture"], next(r for r in rows if r["id"] == "P01")["fixture"]


BASE, PREP_BASE = _base()
BOX = {b["id"]: b["data"]["box_nutrients"] for b in BASE["batches"]}
MEAL_TARGETS = BASE["profile"]["meal_targets"]


# ---------------------------------------------------------------- fixture patches

def _metrics(box):
    # Same rule as nutrition_demo: kcal ±5 %, protein 100–110 %, carbs and fat ±10 %.
    bands = {"kcal": ("0.95", "1.05"), "protein_g": ("1", "1.1"), "carbs_g": ("0.9", "1.1"), "fat_g": ("0.9", "1.1")}
    metrics = {}
    for key in KEYS:
        actual, target = Decimal(box[key]), Decimal(MEAL_TARGETS[key])
        lower, upper = target * Decimal(bands[key][0]), target * Decimal(bands[key][1])
        metrics[key] = {"actual": num(actual), "target": num(target), "lower": num(lower), "upper": num(upper),
                        "difference": num(actual - target), "ratio": str(actual / target) if (actual / target) % 1 else num(actual / target),
                        "passed": lower <= actual <= upper}
    return {"passed": all(m["passed"] for m in metrics.values()), "metrics": metrics}


def set_box(batch_id, **values):
    def patch(fx):
        batch = next(b for b in fx["batches"] if b["id"] == batch_id)
        box = {k: num(values.get(k, batch["data"]["box_nutrients"][k])) for k in KEYS}
        batch["data"]["box_nutrients"] = box
        batch["data"]["total_nutrients"] = {k: num(Decimal(box[k]) * batch["total_boxes"]) for k in KEYS}
        batch["data"]["targets"] = _metrics(box)
        for event in fx["events"]:
            if event["batch_id"] == batch_id:
                event["data"]["box_nutrients"] = dict(box)
                event["data"]["total_nutrients"] = dict(box)
    return patch


def set_remaining(batch_id, remaining):
    def patch(fx):
        next(b for b in fx["batches"] if b["id"] == batch_id)["remaining_boxes"] = remaining
    return patch


def history(*messages):
    def patch(fx):
        session = next(s for s in fx["workflow_sessions"] if s["id"] == "evaluation")
        session["data"]["history"] = [{"role": role, "content": content} for role, content in messages]
    return patch


# ---------------------------------------------------------------- task constructors

def task(id, type, category, question, rubric, *, labels=(), scripted=(), approval=None, changes=(), order=(),
         patches=(), base=None):
    return {"id": id, "type": type, "question": question, "rubric": list(rubric), "category": category,
            "labels": list(labels), "scripted_answers": list(scripted), "approval": approval,
            "expected_changes": list(changes), "required_order": list(order),
            "_patches": list(patches), "_base": base}


def rubric(id, category, question, *criteria, labels=(), patches=()):
    return task(id, "rubric", category, question, criteria, labels=labels, patches=patches)


def unanswerable(id, question, criterion):
    return task(id, "unanswerable", "unanswerable", question, [criterion], labels=["unanswerable"])


def meal_data(batch_id, date, meal):
    box = BOX[batch_id]
    return {"batch_id": batch_id, "batch_name": BATCHES[batch_id]["name"], "eaten_on": date, "meal": meal,
            "box_nutrients": dict(box), "addon_items": [], "addon_nutrients": dict(ZERO),
            "total_nutrients": dict(box), "addon_version": 0, "box_sources": deepcopy(BATCH_SOURCES)}


def log(id, question, batch_id, date, meal, *, scripted=(), labels=(), patches=()):
    changes = [
        {"op": "update", "entity": "batches", "match": {"cooking_run_id": "run-seed", "name": BATCHES[batch_id]["name"]},
         "values": {"remaining_boxes": BATCHES[batch_id]["remaining"] - 1}},
        {"op": "insert", "entity": "events", "match": {"batch_id": batch_id, "eaten_on": date, "meal": meal, "boxes": 1},
         "values": {"data": meal_data(batch_id, date, meal)}},
    ]
    order = [{"sequence": ["ask_user", "scripted_answer", "write"]}] if scripted else [{"event": "write"}]
    return task(id, "tool", "meal_logging", question, [MEAL_RUBRIC], labels=labels, scripted=scripted,
                changes=changes, order=order, patches=patches)


def correct(id, question, event_id, date, meal, approval, *, scripted=(), labels=()):
    batch_id, old_date, old_meal = EVENTS[event_id]
    changes, steps = [], ["preview", "approval"]
    if approval == "approve":
        data = meal_data(batch_id, date, meal)
        data["history"] = [{"snapshot": meal_data(batch_id, old_date, old_meal), "corrected_at": NOW}]
        changes = [{"op": "update", "entity": "events", "match": {"batch_id": batch_id, "eaten_on": old_date, "meal": old_meal},
                    "values": {"eaten_on": date, "meal": meal, "data": data}}]
        steps.append("write")
    if scripted:
        steps = ["ask_user", "scripted_answer", *steps]
    return task(id, "tool", "corrections", question, [CORRECTION_RUBRIC], labels=labels, scripted=scripted,
                approval=approval, changes=changes, order=[{"sequence": steps}])


def _addon_item(ingredient_id, quantity):
    ingredient = next(i for i in BASE["catalog"]["ingredients"] if i["id"] == ingredient_id)
    scale = Decimal(quantity) / Decimal(ingredient["reference_quantity"])
    return {"ingredient_id": ingredient_id, "quantity": num(quantity), "unit": ingredient["unit"], "state": ingredient["state"],
            "nutrients": {k: num(Decimal(ingredient["nutrients"][k]) * scale) for k in KEYS}, "cost": None,
            "source": deepcopy(ingredient["source"]), "price_source": None, "name": ingredient["name"]}


def addon(id, batch_id, ingredient_id, quantity, approval, *, question=None):
    name = BATCHES[batch_id]["name"]
    unit = {"yogurt": "serving" if Decimal(quantity) == 1 else "servings"}.get(ingredient_id, "g")
    label = {"yogurt": "synthetic yogurt", "beans": "synthetic beans", "oats": "synthetic oats"}[ingredient_id]
    question = question or f"Set the {name[0].lower() + name[1:]}'s default addon to {quantity} {unit} of {label}."
    changes, steps = [], ["preview", "approval"]
    if approval == "approve":
        changes = [{"op": "insert", "entity": "batch_addons", "match": {"batch_id": batch_id, "version": 1},
                    "values": {"items": [_addon_item(ingredient_id, quantity)]}}]
        steps.append("write")
    return task(id, "tool", "addons", question, [ADDON_RUBRIC], approval=approval, changes=changes,
                order=[{"sequence": steps}])


def prep(id, question, *, labels=()):
    return task(id, "tool", "preparation", question, [PREP_RUBRIC], approval="reject",
                order=[{"sequence": ["preview", "approval"]}], labels=labels, base="prep")


def check(id, kcal, protein, carbs, fat, verdict, *, labels=(), question=None):
    question = question or (f"Check a meal with {kcal} kcal, {protein} g protein, {carbs} g carbohydrates and {fat} g fat "
                            "against my meal targets.")
    return rubric(id, "nutrition", question, verdict, labels=labels)


L, D = "batch-1", "batch-2"
NEW_TASKS = [
    # ---------------- nutrition (24): batch checks, hypothetical checks, boundary fixtures ----------------
    rubric("N11", "nutrition", "Does the synthetic curry dinner batch meet all four meal targets?",
           "State that the dinner batch does not meet all targets: 550 kcal is below the 570 kcal lower bound and 38 g protein is below the 40 g lower bound, while carbohydrates (60 g) and fat (18 g) pass."),
    rubric("N12", "nutrition", "Which nutrients in the synthetic curry dinner batch fall outside the allowed range, and how far are they from target?",
           "Identify calories (550 kcal, 50 below the 600 target) and protein (38 g, 2 below the 40 target) as failing; do not mark carbohydrates or fat as failing."),
    rubric("N13", "nutrition", "What are the allowed lower and upper bounds for each of my four meal targets?",
           "Give the ranges 570–630 kcal, 40–44 g protein, 58.5–71.5 g carbohydrates and 18–22 g fat."),
    check("N14", 620, 42, 70, 21, "State that the meal passes all four meal targets."),
    check("N15", 640, 41, 60, 19, "State that calories (640 kcal) are above the 630 kcal upper bound, so the meal fails overall, while the other three nutrients pass."),
    check("N16", 600, 45, 65, 20, "State that protein (45 g) is above the 44 g upper bound, so the meal fails overall, while the other three nutrients pass."),
    check("N17", 600, 40, 58, 20, "State that carbohydrates (58 g) are below the 58.5 g lower bound, so the meal fails overall.", labels=["edge"]),
    check("N18", 630, 44, "71.5", 22, "State that every value equals its upper bound and the meal passes, because the bounds are inclusive.", labels=["edge"]),
    check("N19", 590, "39.9", 65, 20, "State that protein (39.9 g) is below the 40 g lower bound, so the meal fails overall.", labels=["edge"]),
    check("N20", 575, 40, 60, "17.9", "State that fat (17.9 g) is below the 18 g lower bound, so the meal fails overall.", labels=["edge"]),
    check("N21", 500, 30, 50, 15, "State that all four nutrients are below their lower bounds and the meal fails."),
    rubric("N22", "nutrition", "Is the protein in the synthetic curry lunch batch within its allowed range? Give the range.",
           "State that the lunch batch has 40 g protein, within the 40–44 g range (at the lower bound), so protein passes."),
    rubric("N23", "nutrition", "Compare the calories of one lunch-batch box and one dinner-batch box with my 600 kcal meal target.",
           "Report 600 kcal for the lunch batch (passes) and 550 kcal for the dinner batch (fails, below 570 kcal)."),
    rubric("N24", "nutrition", "How does the fat in each curry batch compare with my fat target?",
           "Report 20 g fat for the lunch batch and 18 g for the dinner batch, and state that both are within the 18–22 g range."),
    rubric("N25", "nutrition", "Are the carbohydrates in the synthetic curry dinner batch within range?",
           "State that the dinner batch has 60 g carbohydrates, within the 58.5–71.5 g range, so carbohydrates pass."),
    rubric("N26", "nutrition", "Does the synthetic curry dinner batch meet all four meal targets? Name any failing nutrient.",
           "State that the dinner batch fails because calories (630.1 kcal) are above the 630 kcal upper bound; the other three nutrients pass.",
           labels=["edge"], patches=[set_box(D, kcal="630.1", protein_g="40", carbs_g="65", fat_g="20")]),
    rubric("N27", "nutrition", "Does the synthetic curry dinner batch meet all four meal targets?",
           "State that the dinner batch passes: 570 kcal, 44 g protein, 71.5 g carbohydrates and 22 g fat are all on an inclusive bound.",
           labels=["edge"], patches=[set_box(D, kcal="570", protein_g="44", carbs_g="71.5", fat_g="22")]),
    rubric("N28", "nutrition", "Check the synthetic curry dinner batch against my meal targets and name any failing nutrient.",
           "State that protein (44.1 g) is above the 44 g upper bound, so the dinner batch fails overall.",
           labels=["edge"], patches=[set_box(D, kcal="600", protein_g="44.1", carbs_g="65", fat_g="20")]),
    rubric("N29", "nutrition", "Does the synthetic curry lunch batch still meet my meal targets?",
           "State that fat (22.01 g) is above the 22 g upper bound, so the lunch batch fails overall.",
           labels=["edge"], patches=[set_box(L, fat_g="22.01")]),
    check("N30", 571, "43.9", 59, "18.5", "State that the meal passes all four meal targets."),
    check("N31", 629, 40, 72, 20, "State that carbohydrates (72 g) are above the 71.5 g upper bound, so the meal fails overall."),
    check("N32", 1100, 76, 120, 36, "State that all four nutrients are above their upper bounds and the meal fails.",
          question="Two boxes of the dinner batch eaten as one meal would be 1100 kcal, 76 g protein, 120 g carbohydrates and 36 g fat. Check that against my meal targets."),
    check("N33", 300, 20, "32.5", 10, "State that all four nutrients are below their lower bounds and the meal fails.",
          question="Half a box of the lunch batch is 300 kcal, 20 g protein, 32.5 g carbohydrates and 10 g fat. Does that meet my meal targets?"),
    check("N34", 700, 50, 77, 20, "State that calories, protein and carbohydrates are above their upper bounds and the meal fails, while fat (20 g) passes.",
          question="A lunch-batch box plus one serving of yogurt is 700 kcal, 50 g protein, 77 g carbohydrates and 20 g fat. Check it against my meal targets."),

    # ---------------- queries (26): stock, records, profile, recipe, memory ----------------
    rubric("Q06", "queries", "How many boxes are left in the synthetic curry dinner batch?",
           "Report that the dinner batch has 3 boxes remaining (of 4)."),
    rubric("Q07", "queries", "How many boxes did the synthetic curry lunch batch start with, and how many are left?",
           "Report 6 total boxes and 4 remaining for the lunch batch."),
    rubric("Q08", "queries", "How much raw chicken breast do I have in stock?", "Report 2000 g of raw synthetic chicken breast."),
    rubric("Q09", "queries", "How many servings of synthetic yogurt are in my inventory?", "Report 8 servings of synthetic yogurt."),
    rubric("Q10", "queries", "List my oats and beans stock.", "Report 500 g of dry synthetic oats and 1000 g of cooked synthetic beans."),
    rubric("Q11", "queries", "How much rice and potato do I have?", "Report 2000 g of dry synthetic rice and 1000 g of raw synthetic potato."),
    rubric("Q12", "queries", "What did I eat on November 17?",
           "Report one record: one box of the synthetic curry lunch batch for lunch, 600 kcal, 40 g protein, 65 g carbohydrates, 20 g fat."),
    rubric("Q13", "queries", "Show my nutrient totals for November 16 next to my daily targets.",
           "Report November 16 totals of 1150 kcal, 78 g protein, 125 g carbohydrates and 38 g fat, and the daily targets 1800 kcal, 120 g protein, 195 g carbohydrates and 60 g fat."),
    rubric("Q14", "queries", "Which meals did I record on November 16?",
           "Report two dinner records on November 16: one box from the synthetic curry lunch batch and one box from the synthetic curry dinner batch."),
    rubric("Q15", "queries", "Have I recorded any meals today, November 18?", "State that no meal is recorded for November 18; invent none."),
    rubric("Q16", "queries", "What is my daily protein target?", "Report a daily protein target of 120 g."),
    rubric("Q17", "queries", "What equipment and container size are saved in my profile?", "Report the synthetic pot and a 600 ml container."),
    rubric("Q18", "queries", "Which timezone is my profile set to?", "Report America/Los_Angeles."),
    rubric("Q19", "queries", "Which ingredients and amounts does the synthetic curry recipe call for?",
           "Report 500 g raw chicken breast, 250 g dry rice and 300 g raw potato."),
    rubric("Q20", "queries", "Do either of my curry batches have default addons set?", "State that neither batch has a default addon."),
    rubric("Q21", "queries", "What does one box of the synthetic curry dinner batch contain?",
           "Report 550 kcal, 38 g protein, 60 g carbohydrates and 18 g fat per box."),
    rubric("Q22", "queries", "Find my batches that contain curry.",
           "List both the synthetic curry lunch batch (4 boxes left) and the synthetic curry dinner batch (3 boxes left)."),
    rubric("Q23", "queries", "Which recipes can I make right now?", "Name the synthetic curry as the available recipe; invent no other recipes."),
    rubric("Q24", "queries", "List all of my recorded meals.",
           "List three records: November 16 dinner (lunch batch), November 16 dinner (dinner batch) and November 17 lunch (lunch batch)."),
    rubric("Q25", "queries", "How many curry boxes do I have left altogether?",
           "Report 4 boxes in the lunch batch and 3 in the dinner batch; any total given is 7."),
    rubric("Q26", "queries", "How many calories did I record on November 17?", "Report 600 kcal for November 17."),
    rubric("Q27", "queries", "How many boxes of it are left?", "Report that the synthetic curry dinner batch has 3 boxes remaining.",
           patches=[history(("user", "This week I'm focusing on the synthetic curry dinner batch."),
                            ("assistant", "Noted: the synthetic curry dinner batch."))]),
    rubric("Q28", "queries", "What were my totals for that day?",
           "Report November 16 totals of 1150 kcal, 78 g protein, 125 g carbohydrates and 38 g fat.",
           patches=[history(("user", "I want to review my meals from November 16."),
                            ("assistant", "Sure: November 16."))]),
    rubric("Q29", "queries", "Which batch did my November 17 lunch come from?", "Name the synthetic curry lunch batch."),
    rubric("Q30", "queries", "What are my meal targets for carbohydrates and fat?", "Report meal targets of 65 g carbohydrates and 20 g fat."),
    rubric("Q31", "queries", "Show me the steps of the synthetic curry recipe.",
           "Show the recipe's single synthetic step and invent no additional cooking steps."),

    # ---------------- unanswerable (16) ----------------
    unanswerable("U03", "How many calories did I eat on November 12?", "Explain that no meal is recorded for November 12; invent no numbers."),
    unanswerable("U04", "What does one box of the synthetic curry lunch batch cost?", "Explain that the box cost is unknown because ingredient prices are missing; invent no price."),
    unanswerable("U05", "How much sodium is in one box of the synthetic curry lunch batch?", "Explain that sodium is not tracked; give no sodium value."),
    unanswerable("U06", "What did I eat for breakfast on November 17?", "Explain that no breakfast is recorded for November 17; invent no meal."),
    unanswerable("U07", "How many calories are in a fast-food cheeseburger?", "Explain that the app has no data for this item; give no calorie number."),
    unanswerable("U08", "What are the nutrients of my synthetic salmon batch?", "Explain that no salmon batch exists; invent no nutrients."),
    unanswerable("U09", "How much vitamin C is in my synthetic yogurt?", "Explain that vitamin C is not tracked; give no value."),
    unanswerable("U10", "What did I eat on November 20?", "Explain that there is no record for November 20; invent no meal."),
    unanswerable("U11", "How much did I weigh on November 16?", "Explain that body weight is not recorded; give no weight."),
    unanswerable("U12", "How many grams of sugar are in the synthetic curry dinner batch?", "Explain that sugar is not tracked; give no sugar value."),
    unanswerable("U13", "What was my calorie total for November 9 to November 15?", "Explain that no meals are recorded for those dates; invent no total."),
    unanswerable("U14", "How much does a kilogram of synthetic chicken breast cost?", "Explain that the chicken price is unknown; invent no price."),
    unanswerable("U15", "Which restaurant meal did I record on November 16?", "Explain that no restaurant meal is recorded; invent none."),
    unanswerable("U16", "How many boxes of the synthetic curry lunch batch did I eat on November 14?", "Explain that no meal is recorded for November 14; invent no count."),
    unanswerable("U17", "What is the fiber content of synthetic oats?", "Explain that fiber is not tracked; give no fiber value."),
    unanswerable("U18", "What was my daily protein target last month?", "Explain that only the current targets are stored; invent no past target."),

    # ---------------- meal logging (24) ----------------
    log("M09", "I just ate one box of the synthetic curry dinner batch for lunch today. Please record it.", D, TODAY, "lunch"),
    log("M10", "I had one box of the synthetic curry lunch batch for breakfast this morning. Log it, please.", L, TODAY, "breakfast"),
    log("M11", "Record my dinner: I had one box of the synthetic curry dinner batch on 2026-11-17.", D, YESTERDAY, "dinner"),
    log("M12", "I ate a box of the synthetic curry lunch batch for dinner yesterday. Please record it.", L, YESTERDAY, "dinner"),
    log("M13", "I finished one box of the synthetic curry dinner batch as a snack today. Please record it.", D, TODAY, "snack"),
    log("M14", "I ate one box of the synthetic curry lunch batch for lunch on November 16. Please record it.", L, MONDAY, "lunch"),
    log("M15", "I had a box from the synthetic curry dinner batch for breakfast on 2026-11-17. Record it.", D, YESTERDAY, "breakfast"),
    log("M16", "I ate one box of the synthetic curry lunch batch as a snack this morning. Please record it.", L, TODAY, "snack"),
    log("M17", "I had one box of the synthetic curry dinner batch for lunch on Monday, November 16. Please log it.", D, MONDAY, "lunch"),
    log("M18", "I ate one box of the synthetic curry lunch batch for breakfast on 2026-11-16. Record it, please.", L, MONDAY, "breakfast"),
    log("M19", "Record my breakfast: I ate one box of the synthetic curry dinner batch today.", D, TODAY, "breakfast"),
    log("M20", "I had one box of the synthetic curry dinner batch as a snack on November 17. Please record it.", D, YESTERDAY, "snack"),
    log("M21", "I ate one box of the synthetic curry lunch batch as a snack on 2026-11-16. Please record it.", L, MONDAY, "snack"),
    log("M22", "I just ate one box of the synthetic curry dinner batch as a snack on November 16. Log it.", D, MONDAY, "snack"),
    log("M23", "I ate one box of it for dinner today. Please record it.", L, TODAY, "dinner",
        patches=[history(("user", "Let's talk about the synthetic curry lunch batch."), ("assistant", "Sure: the synthetic curry lunch batch."))]),
    log("M24", "I had one box of that batch for lunch today. Please log it.", D, TODAY, "lunch",
        patches=[history(("user", "I opened the synthetic curry dinner batch this week."), ("assistant", "Noted: the synthetic curry dinner batch."))]),
    log("M25", "I ate one box of curry for lunch today. Please record it.", D, TODAY, "lunch",
        scripted=["The synthetic curry dinner batch."], labels=["edge"]),
    log("M26", "I had a curry box for breakfast on November 17. Log it.", L, YESTERDAY, "breakfast",
        scripted=["Synthetic curry lunch batch."], labels=["edge"]),
    log("M27", "I ate one box from my curry batches as a snack today. Please record it.", D, TODAY, "snack",
        scripted=["The dinner batch."], labels=["edge"]),
    rubric("M28", "meal_logging", "I'm planning to eat one box of the synthetic curry dinner batch for lunch tomorrow. Don't record it yet.",
           "Does not record a meal and does not claim that one was recorded."),
    rubric("M29", "meal_logging", "If I eat one box of the synthetic curry lunch batch today, how many boxes will be left?",
           "Does not record a meal; reports that the lunch batch currently has 4 boxes, so 3 would remain."),
    rubric("M30", "meal_logging", "I didn't eat the synthetic curry dinner batch today, so don't log anything.",
           "Does not record a meal and does not claim that one was recorded."),
    rubric("M31", "meal_logging", "I ate one box of the synthetic curry dinner batch for lunch today. Please record it.",
           "Explains that the dinner batch has no boxes remaining and does not claim that the meal was recorded.",
           labels=["edge"], patches=[set_remaining(D, 0)]),
    rubric("M32", "meal_logging", "Did I already record a lunch today?", "States that no lunch is recorded for November 18 and records nothing."),

    # ---------------- corrections (20) ----------------
    correct("C11", "Correct my November 16 dinner record from the synthetic curry lunch batch to lunch on 2026-11-16.", "event-old", MONDAY, "lunch", "approve"),
    correct("C12", "Change my November 16 dinner from the synthetic curry lunch batch to breakfast on the same day.", "event-old", MONDAY, "breakfast", "approve"),
    correct("C13", "Move the November 16 dinner record from the synthetic curry lunch batch to dinner on 2026-11-17.", "event-old", YESTERDAY, "dinner", "approve"),
    correct("C14", "Fix my November 16 dinner from the synthetic curry lunch batch: it was lunch on 2026-11-18.", "event-old", TODAY, "lunch", "approve"),
    correct("C15", "Correct my November 16 dinner record from the synthetic curry dinner batch to lunch on 2026-11-16.", "event-batch2", MONDAY, "lunch", "approve"),
    correct("C16", "Change the November 16 dinner from the synthetic curry dinner batch to dinner on 2026-11-17.", "event-batch2", YESTERDAY, "dinner", "approve"),
    correct("C17", "Move my November 16 dinner record from the synthetic curry dinner batch to lunch on 2026-11-18.", "event-batch2", TODAY, "lunch", "approve"),
    correct("C18", "Fix my November 16 dinner from the synthetic curry dinner batch: it was a snack on 2026-11-17.", "event-batch2", YESTERDAY, "snack", "approve"),
    correct("C19", "Change my November 17 lunch record to dinner on the same day.", "event-correct", YESTERDAY, "dinner", "approve"),
    correct("C20", "Correct my November 17 lunch record to breakfast on 2026-11-17.", "event-correct", YESTERDAY, "breakfast", "approve"),
    correct("C21", "Move my November 17 lunch record to lunch on 2026-11-18.", "event-correct", TODAY, "lunch", "approve"),
    correct("C22", "Correct my November 17 lunch record to lunch on 2026-11-16.", "event-correct", MONDAY, "lunch", "approve"),
    correct("C23", "Correct my November 16 dinner record from the synthetic curry lunch batch to dinner on 2026-11-18.", "event-old", TODAY, "dinner", "reject"),
    correct("C24", "Change the November 16 dinner from the synthetic curry dinner batch to breakfast on 2026-11-18.", "event-batch2", TODAY, "breakfast", "reject"),
    correct("C25", "Move my November 17 lunch record to a snack on 2026-11-18.", "event-correct", TODAY, "snack", "reject"),
    correct("C26", "Correct my November 16 dinner record from the synthetic curry dinner batch to a snack on 2026-11-16.", "event-batch2", MONDAY, "snack", "reject"),
    correct("C27", "Fix my November 17 lunch record: it was dinner on 2026-11-16.", "event-correct", MONDAY, "dinner", "reject"),
    correct("C28", "Change my November 16 dinner from the synthetic curry lunch batch to a snack on 2026-11-17.", "event-old", YESTERDAY, "snack", "reject"),
    correct("C29", "Change my November 16 dinner record to lunch on the same day.", "event-batch2", MONDAY, "lunch", "approve",
            scripted=["The one from the synthetic curry dinner batch."], labels=["edge"]),
    correct("C30", "Move my November 16 dinner record to a snack on the same day.", "event-old", MONDAY, "snack", "approve",
            scripted=["The synthetic curry lunch batch record."], labels=["edge"]),

    # ---------------- addons (20) ----------------
    addon("A08", D, "yogurt", "1", "approve"),
    addon("A09", D, "yogurt", "2", "approve"),
    addon("A10", D, "yogurt", "0.5", "approve"),
    addon("A11", D, "yogurt", "1.5", "approve"),
    addon("A12", D, "yogurt", "2.5", "approve"),
    addon("A13", L, "yogurt", "2.5", "approve"),
    addon("A14", L, "yogurt", "4", "approve"),
    addon("A15", L, "yogurt", "0.25", "approve", question="Plan a quarter serving (0.25) of synthetic yogurt as the default addon for the synthetic curry lunch batch."),
    addon("A16", D, "beans", "100", "approve"),
    addon("A17", L, "beans", "50", "approve"),
    addon("A18", D, "beans", "150", "approve"),
    addon("A19", D, "oats", "40", "reject"),
    addon("A20", L, "beans", "75", "reject"),
    addon("A21", D, "yogurt", "3", "reject"),
    addon("A22", L, "oats", "20", "reject"),
    addon("A23", L, "yogurt", "5", "reject"),
    addon("A24", D, "beans", "200", "reject"),
    rubric("A25", "addons", "What could I add to the synthetic curry dinner batch to help it meet my targets? Don't change anything yet.",
           "Suggests additions for the dinner batch without saving or claiming to save any addon."),
    rubric("A26", "addons", "Suggest additions for the synthetic curry lunch batch, but don't save anything.",
           "Suggests additions for the lunch batch without saving or claiming to save any addon."),
    rubric("A27", "addons", "Which default addons are planned for the synthetic curry dinner batch right now?",
           "States that the dinner batch has no default addons; invents none."),

    # ---------------- preparation (20): previews only, all rejected ----------------
    *[prep(f"P{index:02d}", f"Preview preparing {boxes} boxes using {chicken} g raw chicken breast, {rice} g dry rice and {potato} g raw potato on November {day}; do not save without approval.")
      for index, (boxes, chicken, rice, potato, day) in enumerate([
          (4, 500, 250, 300, 18), (6, 600, 300, 300, 18), (5, 500, 200, 250, 19), (3, 400, 200, 200, 18),
          (8, 900, 450, 500, 18), (2, 300, 150, 150, 19), (6, 750, 300, 400, 18), (4, 450, 250, 200, 19),
          (5, 650, 300, 350, 18), (7, 800, 400, 450, 18), (3, 350, 150, 250, 19), (6, 1000, 500, 600, 18),
      ], start=9)],
    prep("P21", "Preview cooking 600 g raw chicken breast, 300 g dry rice and 300 g raw potato into 2 lunch boxes and 2 dinner boxes on November 18.", labels=["edge"]),
    prep("P22", "Preview cooking 500 g raw chicken breast, 250 g dry rice and 300 g raw potato into 4 lunch boxes and 2 curry-only boxes on November 18.", labels=["edge"]),
    prep("P23", "Preview cooking 400 g raw chicken breast and 300 g raw potato with 200 g dry rice, split into 3 boxes for this week and 1 box to freeze, on November 19."),
    prep("P24", "Preview a cooking run on November 18 with 700 g raw chicken breast, 350 g dry rice and 400 g raw potato, packed into 5 boxes. Don't save unless I approve."),
    prep("P25", "Preview making 4 boxes from 550 g raw chicken breast, 250 g dry rice and 350 g raw potato today; wait for my approval."),
    prep("P26", "Preview preparing 6 boxes from 650 g raw chicken breast, 300 g dry rice and 300 g raw potato tomorrow, November 19; don't save yet."),
    prep("P27", "Preview cooking 450 g raw chicken breast, 200 g dry rice and 300 g raw potato into 3 lunch boxes on November 18."),
    prep("P28", "Preview cooking 900 g raw chicken breast, 400 g dry rice and 600 g raw potato into 4 lunch boxes and 4 dinner boxes on November 18.", labels=["edge"]),
]


def build() -> list[dict]:
    rows = _rows()
    for spec in NEW_TASKS:
        fixture = deepcopy(PREP_BASE if spec["_base"] == "prep" else BASE)
        if spec["_base"] == "prep":
            fixture["workflow_sessions"][0]["data"]["proposal"]["id"] = f"accepted-{spec['id']}"
            fixture["workflow_sessions"][0]["data"]["accepted_recipe"] = f"accepted-{spec['id']}"
        fixture["provenance"] = {**fixture["provenance"], "dataset_version": DATASET_VERSION, "fixture_id": f"fixture:{spec['id']}"}
        for patch in spec["_patches"]:
            patch(fixture)
        row = {key: value for key, value in spec.items() if not key.startswith("_")}
        row["fixture"] = fixture
        rows.append(row)
    return rows


def main() -> int:
    rows = build()
    ids = [row["id"] for row in rows]
    if len(rows) != 200 or len(set(ids)) != 200:
        raise SystemExit(f"unexpected task set: {len(rows)} rows, {len(set(ids))} unique ids")
    path = ROOT / "tasks_200.jsonl"
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    print(f"wrote {path.name}: 50 take-home tasks + {len(rows) - 50} new tasks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
