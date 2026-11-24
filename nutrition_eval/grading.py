"""Deterministic business-state and interaction-order graders."""
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from .contracts import EpisodeGrade, GradePart, ScoredEpisode, SuiteResult
from .judge import grade_reply
from .state import normalize_state


_ENTITIES = {
    "inventory",
    "cooking_runs",
    "cooking_ingredients",
    "batches",
    "batch_ingredients",
    "batch_addons",
    "events",
    "inventory_movements",
    "previews",
    "operations",
    "workflow_sessions",
}
_ID_TABLES = {"cooking_runs", "cooking_ingredients", "batches", "events", "previews", "workflow_sessions"}
_NUMERIC_FIELDS = {
    "quantity", "delta", "boxes", "total_boxes", "remaining_boxes", "version", "state_version",
    "kcal", "protein_g", "carbs_g", "fat_g", "container_ml", "reference_quantity",
    "package_quantity", "usd", "cost", "known_subtotal", "total",
}
# The full snapshot retains these for trace/debugging, while grade_state scores final business data.
_OPERATIONAL_TABLES = {"previews", "operations", "workflow_sessions"}


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _matches_value(actual, expected, key=None) -> bool:
    if key in _NUMERIC_FIELDS:
        actual_number = _number(actual)
        expected_number = _number(expected)
        if actual_number is not None and expected_number is not None:
            return actual_number == expected_number
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and _matches_value(actual[key], value, key)
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        return isinstance(actual, (list, tuple)) and len(actual) == len(expected) and all(
            _matches_value(left, right) for left, right in zip(actual, expected)
        )
    if isinstance(actual, (date, datetime)):
        actual = actual.isoformat()
    if isinstance(expected, (date, datetime)):
        expected = expected.isoformat()
    return actual == expected


def _matches(row: dict, pattern: dict) -> bool:
    return all(key in row and _matches_value(row[key], value, key) for key, value in pattern.items())


def _apply_changes(task) -> dict:
    expected = deepcopy(task.fixture)
    for index, change in enumerate(task.expected_changes):
        entity = change.entity
        if entity == "profile":
            profile = expected.get("profile")
            if change.op == "insert":
                if profile is not None:
                    raise ValueError("profile insert requires an absent profile")
                expected["profile"] = {**change.match, **change.values}
                continue
            if not isinstance(profile, dict) or not _matches(profile, change.match):
                raise ValueError("profile change did not match exactly one existing profile")
            if change.op == "update":
                expected["profile"] = {**profile, **deepcopy(change.values)}
            else:
                expected["profile"] = None
            continue

        if entity not in _ENTITIES:
            raise ValueError(f"unsupported expected-change entity: {entity}")
        rows = expected.setdefault(entity, [])
        if not isinstance(rows, list):
            raise ValueError(f"fixture entity {entity} must be a list")
        matches = [row for row in rows if isinstance(row, dict) and _matches(row, change.match)]
        if change.op == "insert":
            record = {**deepcopy(change.match), **deepcopy(change.values)}
            if "id" in record:
                raise ValueError("expected changes cannot prescribe generated ids")
            if entity in _ID_TABLES:
                record["id"] = f"__expected_{entity}_{index}"
            rows.append(record)
            continue
        if len(matches) != 1:
            raise ValueError(
                f"{change.op} on {entity} must match exactly one record; found {len(matches)}"
            )
        matched = matches[0]
        if change.op == "update":
            matched.update(deepcopy(change.values))
        else:
            rows.remove(matched)
    return expected


def grade_state(task, actual: dict) -> GradePart:
    """Apply the task's semantic delta to its fixture and compare full business state."""
    try:
        expected = normalize_state(_apply_changes(task))
    except (KeyError, TypeError, ValueError) as exc:
        return GradePart(False, [f"invalid expected state: {exc}"])
    if not isinstance(actual, dict):
        return GradePart(False, ["actual business state is not an object"])

    reasons = []
    compared_keys = (set(expected) | set(actual)) - _OPERATIONAL_TABLES
    for key in sorted(compared_keys):
        if key not in expected:
            reasons.append(f"unexpected business state field: {key}")
        elif key not in actual:
            reasons.append(f"missing business state field: {key}")
        elif expected[key] != actual[key]:
            reasons.append(f"business state differs in {key}")
    return GradePart(not reasons, reasons)


def _events(episode) -> list[dict]:
    interactions = [
        event
        for event in episode.interactions
        if isinstance(event, dict)
        and event.get("kind") in {"ask_user", "scripted_answer", "preview", "approval", "write"}
    ]
    if interactions:
        return interactions

    events = []
    for row in episode.trace:
        if not isinstance(row, dict) or row.get("type") not in {"tool", "action"}:
            continue
        name = row.get("name")
        if name == "ask_user":
            events.append({"kind": "ask_user", "action": name, "arguments": row.get("arguments", {})})
        elif isinstance(name, str) and name.startswith("preview_"):
            events.append({"kind": "preview", "action": name})
        elif name == "log_meal":
            events.append({"kind": "write", "action": name})
    return events


def _matches_event(event: dict, name: str) -> bool:
    if event.get("kind") == name:
        return True
    return event.get("action") == name


def _matches_sequence_step(event: dict, step) -> bool:
    if isinstance(step, str):
        return _matches_event(event, step)
    if not isinstance(step, dict) or set(step) - {"event", "topic"}:
        return False
    name = step.get("event")
    return (
        isinstance(name, str)
        and _matches_event(event, name)
        and all(event.get(key) == value for key, value in step.items() if key != "event")
    )


def _sequence_label(step) -> str:
    return step if isinstance(step, str) else str(step.get("event", "<invalid>"))


def _first_index(events: list[dict], name: str, start=0):
    return next((index for index in range(start, len(events)) if _matches_event(events[index], name)), None)


def grade_order(task, episode) -> GradePart:
    events = _events(episode)
    reasons = []

    for rule_number, rule in enumerate(task.required_order, start=1):
        if not isinstance(rule, dict):
            reasons.append(f"required-order rule {rule_number} is not an object")
            continue
        sequence = rule.get("sequence")
        if sequence is not None:
            valid_step = lambda step: (
                isinstance(step, str)
                or (
                    isinstance(step, dict)
                    and set(step) <= {"event", "topic"}
                    and isinstance(step.get("event"), str)
                    and all(isinstance(value, str) for value in step.values())
                )
            )
            if not isinstance(sequence, list) or not all(valid_step(step) for step in sequence):
                reasons.append(f"required-order rule {rule_number} has an invalid sequence")
                continue
            cursor = 0
            for step in sequence:
                found = next(
                    (index for index in range(cursor, len(events)) if _matches_sequence_step(events[index], step)),
                    None,
                )
                if found is None:
                    labels = [_sequence_label(item) for item in sequence]
                    reasons.append(f"required order missing or misplaced: {' → '.join(labels)}")
                    break
                cursor = found + 1
            continue

        name = rule.get("event", rule.get("action", rule.get("type")))
        if not isinstance(name, str):
            reasons.append(f"required-order rule {rule_number} needs an event or sequence")
            continue
        matches = [
            (index, event)
            for index, event in enumerate(events)
            if _matches_event(event, name)
            and ("topic" not in rule or event.get("topic") == rule["topic"])
        ]
        if not matches and rule.get("required", True):
            reasons.append(f"required action was not observed: {name}")
            continue
        if not matches:
            continue
        event_index = matches[0][0]
        before = rule.get("before")
        if isinstance(before, str):
            if not any(
                index > event_index and _matches_event(event, before)
                for index, event in enumerate(events)
            ):
                reasons.append(f"{name} must occur before {before}")
        after = rule.get("after")
        if isinstance(after, str):
            if not any(
                index < event_index and _matches_event(event, after)
                for index, event in enumerate(events)
            ):
                reasons.append(f"{name} must occur after {after}")

    approvals = [(index, event) for index, event in enumerate(events) if event.get("kind") == "approval"]
    previews = [(index, event) for index, event in enumerate(events) if event.get("kind") == "preview"]
    writes = [(index, event) for index, event in enumerate(events) if event.get("kind") == "write"]
    if task.expected_changes and not writes:
        reasons.append("expected business changes have no corresponding write action")
    for index, approval in approvals:
        preview_id = approval.get("preview_id")
        if not isinstance(preview_id, str) or not preview_id:
            reasons.append("approval did not identify a displayed preview")
            continue
        if not any(
            preview_index < index and preview.get("preview_id") == preview_id
            for preview_index, preview in previews
        ):
            reasons.append("approval did not follow display of the same preview ID")
    if task.approval:
        if not previews:
            reasons.append("task approval was expected but no preview was displayed")
        if not approvals:
            reasons.append("task approval was expected but not sent")
        elif approvals[0][1].get("outcome") != task.approval:
            reasons.append("approval outcome did not match the task script")
        if task.approval == "approve" and approvals:
            if any(index < approvals[0][0] for index, _ in writes):
                reasons.append("a write occurred before approval")
            if task.expected_changes and not any(index > approvals[0][0] for index, _ in writes):
                reasons.append("approved preview did not produce a write action")
        if task.approval == "reject" and approvals:
            if any(index > approvals[0][0] for index, _ in writes):
                reasons.append("a write occurred after a rejected preview")
    elif approvals:
        reasons.append("an approval was sent without a scripted approval")
    return GradePart(not reasons, reasons)


def grade_episode(task, episode, *, judge_client, judge_config) -> EpisodeGrade:
    state = grade_state(task, episode.final_state)
    order = grade_order(task, episode)
    if episode.status != "complete":
        detail = episode.error or episode.status
        order = GradePart(False, [*order.reasons, f"episode did not complete: {detail}"])
    reply = grade_reply(task, episode, judge_client=judge_client, judge_config=judge_config)
    return EpisodeGrade(
        passed=state.passed and order.passed and reply.passed,
        state=state,
        order=order,
        reply=reply,
        judge_usage=reply.usage,
    )


class JudgeUnavailable(RuntimeError):
    """A judge failure invalidates final suite metrics; partial results are diagnostic only."""

    status = "incomplete"

    def __init__(self, message, partial_results):
        super().__init__(message)
        self.partial_results = partial_results


def grade_suite(tasks, suite: SuiteResult, *, judge_client, judge_config, workers=1) -> list[ScoredEpisode]:
    if suite.status != "complete":
        raise ValueError("cannot grade an incomplete controller suite")
    task_by_id = {task.id: task for task in tasks}
    for episode in suite.episodes:
        if episode.task_id not in task_by_id:
            raise ValueError(f"suite contains an unknown task id: {episode.task_id}")

    def grade(episode):
        return grade_episode(task_by_id[episode.task_id], episode, judge_client=judge_client, judge_config=judge_config)

    from concurrent.futures import ThreadPoolExecutor

    scored = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(grade, episode) for episode in suite.episodes]
        for episode, future in zip(suite.episodes, futures):
            try:
                result = future.result()
            except Exception as exc:
                for pending in futures:
                    pending.cancel()
                raise JudgeUnavailable(
                    f"judge failed for {episode.task_id}/{episode.controller}/{episode.repeat}: {type(exc).__name__}: {exc}",
                    scored,
                ) from exc
            scored.append(ScoredEpisode(task=task_by_id[episode.task_id], episode=episode, grade=result))
    return scored
