"""Run repeated Agent and fixed-baseline episodes in isolated test schemas."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import os
import re

from nutrition_demo.catalog import Catalog

from .contracts import SuiteResult
from .episode import run_episode
from .fixtures import episode_store, seed_fixture


def _catalog_from_fixture(fixture: dict) -> Catalog:
    data = fixture.get("catalog")
    if not isinstance(data, dict) or not isinstance(data.get("ingredients"), list) or not isinstance(data.get("recipes"), list):
        raise ValueError("fixture catalog must contain ingredients and recipes lists")
    provenance = fixture.get("provenance", {})
    manifest = provenance.get("source_manifest", {}) if isinstance(provenance, dict) else {}
    if not isinstance(manifest, dict):
        raise ValueError("fixture provenance source_manifest must be an object")
    return Catalog(data, manifest)


def _schema_prefix(controller_name: str, task_id: str, repeat: int) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", task_id.lower()).strip("_") or "task"
    marker = "a" if controller_name == "agent" else "b"
    return f"eval_{marker}_{slug[:10]}_{repeat}"


def eval_workers() -> int:
    """Parallel episodes; EVAL_WORKERS=1 (the default) keeps the original sequential run."""
    value = os.environ.get("EVAL_WORKERS", "1")
    if not value.isdigit() or not 1 <= int(value) <= 16:
        raise ValueError("EVAL_WORKERS must be an integer from 1 to 16")
    return int(value)


def _run_one(dsn, task, controller_name, factory, repeat, config, limits):
    # Every episode gets its own schema (prefix + uuid), so episodes never share state.
    with episode_store(dsn, _schema_prefix(controller_name, task.id, repeat)) as store:
        seed_fixture(store, task.fixture)
        catalog = _catalog_from_fixture(task.fixture)
        controller = factory(store, catalog, config)
        if controller is None:
            raise ValueError(f"{controller_name} factory returned no controller")
        episode = run_episode(task, controller, store, repeat=repeat, now=config.now, limits=limits)
        return replace(episode, controller=controller_name)


def run_suite(tasks, agent_factory, baseline_factory, config, *, workers=None) -> SuiteResult:
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        return SuiteResult("incomplete", [], "TEST_DATABASE_URL is required; never fall back to DATABASE_URL")

    limits = {
        "max_model_calls": config.max_model_calls,
        "max_tool_calls": config.max_tool_calls,
        "max_clarifications": config.max_clarifications,
        "timeout_seconds": config.timeout_seconds,
    }
    factories = (("agent", agent_factory, 3), ("baseline", baseline_factory, 1))
    jobs = [
        (task, controller_name, factory, repeat)
        for task in tasks
        for controller_name, factory, repeat_count in factories
        for repeat in range(1, repeat_count + 1)
    ]
    workers = workers or eval_workers()

    episodes = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_run_one, dsn, task, name, factory, repeat, config, limits)
                   for task, name, factory, repeat in jobs]
        # Results are collected in job order, so episodes.jsonl keeps the sequential ordering.
        for (task, controller_name, _, repeat), future in zip(jobs, futures):
            try:
                episodes.append(future.result())
            except Exception as exc:
                for pending in futures:
                    pending.cancel()
                return SuiteResult(
                    "incomplete",
                    episodes,
                    f"setup failed before {controller_name} episode {task.id}/{repeat}: {type(exc).__name__}: {exc}",
                )
    return SuiteResult("complete", episodes)
