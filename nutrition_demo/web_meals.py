"""Resolve explicit one-box meal logs and guarded batch choices in the web demo."""
import re
from uuid import uuid4

from .intent import declares_consumption, normalized
from .models import Result


_CONSUMPTION = re.compile(
    r"i (?:ate|just ate|have (?:just )?eaten|had|finished) "
    r"(?:one|1|a) (?:box|portion) (?:from|of) (?:the )?(?P<reference>.+)"
)
_TIME = re.compile(r"(?:,? )(?:(?:on )?(today|yesterday|\d{4}-\d{2}-\d{2})|for (breakfast|lunch|dinner|snack))$")
_NO_ADDONS = re.compile(
    r"(?:,? with no add[ -]?ons|\. i (?:didn't|did not) eat any add[ -]?ons)$"
)
_RECENT = " batch i just saved"
_CHOICE = "/meal-select"
_PENDING = "web_pending_meal"


def _meal_details(message):
    text = normalized(message).rstrip(".!")
    no_addons = _NO_ADDONS.search(text)
    if no_addons:
        text = text[:no_addons.start()]
    if not declares_consumption(text):
        return None
    match = _CONSUMPTION.fullmatch(text)
    if not match:
        return None
    reference = match["reference"]
    meal = day = None
    while (suffix := _TIME.search(reference)) is not None:
        date_label, meal_label = suffix.groups()
        if (date_label and day) or (meal_label and meal):
            return None
        day = date_label or day
        meal = meal_label or meal
        reference = reference[:suffix.start()]
    # Leave compound requests and explicit add-on details to the agent.
    if re.search(r"\b(?:and|with|without|plus|except)\b|[,.!?]", reference):
        return None
    recent = reference.endswith(_RECENT)
    suffix = _RECENT if recent else " batch"
    reference = reference.removesuffix(suffix)
    count = re.fullmatch(r"([1-9]\d{0,8})[- ]box\s+(.+)", reference)
    dish = count[2] if count else reference
    return {
        "dish": dish, "total_boxes": int(count[1]) if count else None,
        "recent": recent, "eaten_on": day or "today", "meal": meal,
        "no_addons": bool(no_addons),
    }


def _matches_dish(batch, query):
    if query in (normalized(batch["name"]), batch["id"].casefold(), batch["recipe_id"].casefold()):
        return True
    return batch["recipe_id"] == "curry" and query in {"curry", "curry chicken", "chicken curry"}


class WebMeals:
    def __init__(self, service, workflow):
        self.service = service
        self.workflow = workflow

    @staticmethod
    def recognizes(message):
        return message.split(maxsplit=1)[:1] == [_CHOICE] or _meal_details(message) is not None

    def _batches(self, eaten_on):
        with self.service.store.transaction() as database:
            batches = database.execute("""
                SELECT b.id, b.name, b.total_boxes, b.remaining_boxes,
                       r.recipe_id, r.prepared_on, o.created_at AS saved_at,
                       COALESCE(a.data->'items', '[]'::jsonb) AS default_addons
                  FROM batches b
                  JOIN cooking_runs r ON r.id = b.cooking_run_id
                  LEFT JOIN operations o ON o.request_id = r.request_id
                  LEFT JOIN LATERAL (
                    SELECT data FROM batch_addons
                    WHERE batch_id = b.id ORDER BY version DESC LIMIT 1
                  ) a ON TRUE
                 WHERE b.remaining_boxes > 0
                 ORDER BY o.created_at DESC NULLS LAST, r.prepared_on DESC, b.name, b.id
            """).fetchall()
        return [batch for batch in batches if batch["prepared_on"].isoformat() <= eaten_on]

    def _available_choices(self, pending):
        return [batch for batch in self._batches(pending["eaten_on"]) if batch["id"] in pending["batch_ids"]]

    def choices(self, state=None):
        if state is None:
            state = self.workflow.load()
        pending = state.get(_PENDING)
        if not pending:
            return None
        options = [
            {
                "id": batch["id"], "name": batch["name"], "total_boxes": batch["total_boxes"],
                "remaining_boxes": batch["remaining_boxes"], "prepared_on": batch["prepared_on"].isoformat(),
                "saved_at": batch["saved_at"].isoformat() if batch["saved_at"] else None,
            }
            for batch in self._available_choices(pending)
        ]
        return {"id": pending["id"], "options": options} if options else None

    def _log(self, batch, details, request_id, state):
        if batch["default_addons"] and not details["no_addons"]:
            return Result(False, {}, "This batch has default add-ons. Repeat what you ate and include any add-ons, or say “with no add-ons.”")
        result = self.service.log_meal(
            batch_id=batch["id"], eaten_on=details["eaten_on"], meal=details["meal"],
            request_id="web-meal:" + request_id, addon_override=[],
        )
        if not result.ok:
            return result
        state.pop(_PENDING, None)
        state["pending_consumption"] = None
        state["waiting_question"] = None
        self.workflow.save(state)
        meal = f" for {details['meal']}" if details["meal"] else ""
        count = result.data["remaining_boxes"]
        unit = "box" if count == 1 else "boxes"
        return Result(True, {"message": f"Logged one box of {batch['name']}{meal}. {count} {unit} remaining."})

    def _select(self, parts, request_id, state):
        pending = state.get(_PENDING)
        if len(parts) != 3 or not pending or parts[1] != pending["id"]:
            return Result(False, {}, "That batch choice is no longer current. Describe the meal again.")
        batch = next((batch for batch in self._available_choices(pending) if batch["id"] == parts[2]), None)
        if not batch:
            return Result(False, {}, "That batch is no longer available. Choose another batch or describe the meal again.")
        return self._log(batch, pending, request_id, state)

    def handle(self, message, request_id):
        parts = message.split()
        state = self.workflow.load()
        if state.get("pending_preview"):
            return Result(False, {}, "Approve or reject the current preview before logging a meal.")
        if parts[:1] == [_CHOICE]:
            return self._select(parts, request_id, state)
        details = _meal_details(message)
        if not details:
            return Result(False, {}, "Specify one box from a prepared batch you ate.")
        try:
            details["eaten_on"] = self.service._day(details["eaten_on"])
        except ValueError as error:
            return Result(False, {}, str(error))
        matches = [
            batch for batch in self._batches(details["eaten_on"])
            if _matches_dish(batch, details["dish"])
            and (details["total_boxes"] is None or batch["total_boxes"] == details["total_boxes"])
        ]
        if details["recent"] and matches and all(batch["saved_at"] for batch in matches):
            latest = max(batch["saved_at"] for batch in matches)
            matches = [batch for batch in matches if batch["saved_at"] == latest]
        if state.pop(_PENDING, None):
            self.workflow.save(state)
        if not matches:
            return Result(False, {}, "I couldn’t find an available batch that matches. Check Prepared batches and try again.")
        if len(matches) == 1:
            return self._log(matches[0], details, request_id, state)
        state[_PENDING] = {
            "id": str(uuid4()), "batch_ids": [batch["id"] for batch in matches],
            "eaten_on": details["eaten_on"], "meal": details["meal"], "no_addons": details["no_addons"],
        }
        self.workflow.save(state)
        return Result(True, {"message": "Which batch did you eat? Choose a batch to log one box."})
