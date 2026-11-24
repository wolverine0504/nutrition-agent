"""Gold checks for tasks_200.jsonl and safety_tasks.jsonl against the real application service.

Every fixture must seed and read back unchanged. For every task with expected changes, the
correct application operation (log_meal, confirmed correction, confirmed addon plan) must
produce exactly the expected state, so a failing episode means the controller erred, not the task.
"""
import json
import os
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from nutrition_eval.fixtures import episode_store, seed_fixture
from nutrition_eval.grading import grade_state
from nutrition_eval.runner import _catalog_from_fixture
from nutrition_eval.safety import load_safety_tasks
from nutrition_eval.state import normalize_state, read_business_state
from nutrition_eval.taskset_build import EVENTS, NOW, build
from nutrition_eval.tasks import load_tasks, validate_taskset

ROOT = Path(__file__).resolve().parents[1]
TASKS = load_tasks(ROOT / "tasks_200.jsonl")


def test_committed_file_matches_generator():
    rows = [json.loads(line) for line in (ROOT / "tasks_200.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows == json.loads(json.dumps(build()))
    take_home = [json.loads(line) for line in (ROOT / "tasks.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows[:50] == take_home
    validate_taskset(TASKS, expected_count=200)


def _service(store, fixture):
    from nutrition_demo.service import NutritionService
    return NutritionService(store, _catalog_from_fixture(fixture), clock=lambda: datetime.fromisoformat(NOW))


def _ok(result):
    if hasattr(result, "ok"):
        assert result.ok, result
        return result.data
    return result


def _gold(service, task):
    for change in task.expected_changes:
        if change.entity == "events" and change.op == "insert":
            m = change.match
            _ok(service.log_meal(m["batch_id"], m["eaten_on"], m["meal"], "gold-request"))
        elif change.entity == "events" and change.op == "update":
            event_id = next(eid for eid, key in EVENTS.items()
                            if key == (change.match["batch_id"], change.match["eaten_on"], change.match["meal"]))
            preview = _ok(service.preview_correction(event_id, {"date": change.values["eaten_on"], "meal": change.values["meal"]}))
            _ok(service.confirm_preview(preview["id"], "gold-request"))
        elif change.entity == "batch_addons":
            items = [{k: item[k] for k in ("ingredient_id", "quantity", "unit", "state")} for item in change.values["items"]]
            preview = _ok(service.preview_batch_addons(change.match["batch_id"], items))
            _ok(service.confirm_preview(preview["id"], "gold-request"))


@pytest.mark.parametrize("task", TASKS, ids=[t.id for t in TASKS])
def test_gold_operation_reaches_expected_state(task):
    with episode_store(os.environ["TEST_DATABASE_URL"], "gold_check") as store:
        seed_fixture(store, task.fixture)
        _gold(_service(store, task.fixture), task)
        grade = grade_state(task, normalize_state(read_business_state(store)))
    assert grade.passed, grade.reasons


SAFETY = load_safety_tasks(ROOT / "safety_tasks.jsonl")


@pytest.mark.parametrize("task", SAFETY, ids=[t.id for t in SAFETY])
def test_safety_fixture_seeds_unchanged(task):
    with episode_store(os.environ["TEST_DATABASE_URL"], "gold_safety") as store:
        seed_fixture(store, task.fixture)
        grade = grade_state(SimpleNamespace(fixture=task.fixture, expected_changes=[]),
                            normalize_state(read_business_state(store)))
    assert grade.passed, grade.reasons
