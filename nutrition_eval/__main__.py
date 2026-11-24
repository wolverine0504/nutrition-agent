"""Run the evaluation harness or its explicitly labeled fake-client dry run."""
import argparse
from datetime import datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

import openai
from psycopg.conninfo import conninfo_to_dict

from nutrition_demo.tools import TOOL_VERSION

from .contracts import EpisodeResult, EvalConfig, ScoredEpisode, SuiteResult
from .controllers import agent_factory
from .baseline import BASELINE_VERSION, baseline_factory
from .episode import RESPONSE_CAPTURE_VERSION
from .grading import JudgeUnavailable, _apply_changes, grade_episode, grade_suite
from .judge import JUDGE_INSTRUCTIONS, JUDGE_SCHEMA, JUDGE_VERSION
from .report import summarize_runs, write_run_artifacts
from .state import normalize_state
from .tasks import load_tasks, validate_taskset


def _parser():
    parser = argparse.ArgumentParser(prog="python -m nutrition_eval")
    parser.add_argument("--tasks", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true", help="use synthetic fake clients; no live model or database calls")
    return parser


def _database_migration_manifest() -> list[dict[str, str]]:
    directory = Path(__file__).resolve().parents[1] / "nutrition_demo" / "migrations"
    return [
        {"version": path.name, "sha256": sha256(path.read_bytes()).hexdigest()}
        for path in sorted(directory.glob("*.sql"))
    ]


def _judge_schema_sha256() -> str:
    payload = json.dumps(JUDGE_SCHEMA, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(payload.encode("utf-8")).hexdigest()


def _source_tree_sha256() -> str:
    root = Path(__file__).resolve().parents[1]
    paths = [root / "pyproject.toml", root / "uv.lock", root / ".python-version"]
    for directory_name in ("nutrition_demo", "nutrition_eval"):
        paths.extend(
            path
            for path in (root / directory_name).rglob("*")
            if path.is_file() and path.suffix in {".py", ".sql"}
        )
    digest = sha256()
    for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _contains_secret_setting(value) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).lower()
            if normalized in {"api_key", "openai_api_key", "database_url", "test_database_url", "database_password"}:
                return True
            if _contains_secret_setting(child):
                return True
    elif isinstance(value, list):
        return any(_contains_secret_setting(child) for child in value)
    return False


def _load_config(path: Path) -> tuple[dict, EvalConfig]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read evaluation config: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("Evaluation config must be a JSON object")
    if _contains_secret_setting(raw):
        raise ValueError("Evaluation config must not contain API keys or database credentials")
    if raw.get("baseline_version") != BASELINE_VERSION:
        raise ValueError("Baseline version does not match frozen config")
    if raw.get("response_capture_version") != RESPONSE_CAPTURE_VERSION:
        raise ValueError("Response capture version does not match frozen config")
    if raw.get("judge_version") != JUDGE_VERSION:
        raise ValueError(f"Judge version mismatch; expected {JUDGE_VERSION}")
    if raw.get("tool_version") != TOOL_VERSION:
        raise ValueError(f"Tool version mismatch; expected {TOOL_VERSION}")
    if raw.get("openai_sdk_version") != openai.__version__:
        raise ValueError(
            f"OpenAI SDK mismatch; configured {raw.get('openai_sdk_version')}; installed {openai.__version__}"
        )
    if raw.get("judge_store") is not False:
        raise ValueError("Judge requires store=false")
    migrations = _database_migration_manifest()
    if not migrations or raw.get("database_migrations") != migrations:
        raise ValueError("Database migration manifest does not match frozen config")
    if raw.get("database_migration_version") != migrations[-1]["version"]:
        raise ValueError("database_migration_version does not match the migration manifest")
    expected_instructions_hash = sha256(JUDGE_INSTRUCTIONS.encode("utf-8")).hexdigest()
    if raw.get("judge_instructions_sha256") != expected_instructions_hash:
        raise ValueError("Judge prompt hash does not match frozen config")
    if raw.get("judge_schema_sha256") != _judge_schema_sha256():
        raise ValueError("Judge schema hash does not match frozen config")
    try:
        now = datetime.fromisoformat(raw["now"])
        config = EvalConfig(
            now=now,
            model_id=raw["model_id"],
            reasoning_effort=raw.get("reasoning_effort"),
            max_model_calls=int(raw["max_model_calls"]),
            max_tool_calls=int(raw["max_tool_calls"]),
            max_clarifications=int(raw["max_clarifications"]),
            max_output_tokens=int(raw["max_output_tokens"]),
            timeout_seconds=int(raw["timeout_seconds"]),
            judge_model_id=raw["judge_model_id"],
            judge_parameters=dict(raw["judge_parameters"]),
            task_version=raw["task_version"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Invalid or missing evaluation config fields: {exc}") from exc
    if now.tzinfo is None or raw.get("timezone") != "America/Los_Angeles":
        raise ValueError("Fixed clock must have a timezone and use America/Los_Angeles")
    return raw, config


def _validate_test_database() -> dict:
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        raise ValueError("TEST_DATABASE_URL is required; no personal database fallback")
    try:
        target = conninfo_to_dict(dsn)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid TEST_DATABASE_URL") from exc
    if target.get("host") != "db_test" or target.get("dbname") != "nutrition_test":
        raise ValueError("Only db_test/nutrition_test is allowed")
    personal_dsn = os.environ.get("DATABASE_URL")
    if personal_dsn:
        try:
            personal = conninfo_to_dict(personal_dsn)
        except (TypeError, ValueError):
            personal = {}
        if personal and (personal.get("host"), personal.get("dbname")) == (target.get("host"), target.get("dbname")):
            raise ValueError("TEST_DATABASE_URL points to the personal DATABASE_URL; refusing to start")
    return {"host": target.get("host"), "dbname": target.get("dbname")}


def _preflight(args):
    raw_config, config = _load_config(args.config)
    tasks = load_tasks(args.tasks)
    task_hash = sha256(args.tasks.read_bytes()).hexdigest()
    if args.dry_run:
        if not 3 <= len(tasks) <= 5 or not all(task.id.startswith("D") for task in tasks):
            raise ValueError("Fake-client dry run requires 3–5 development tasks")
        database = None
    else:
        validate_taskset(tasks, expected_count=int(raw_config.get("formal_task_count", 50)))
        if task_hash != raw_config.get("formal_taskset_sha256"):
            raise ValueError("Formal taskset hash does not match eval_config.json; verify and freeze the version first")
        program_commit = os.environ.get("EVAL_PROGRAM_COMMIT", "")
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", program_commit):
            raise ValueError("Formal evaluation requires EVAL_PROGRAM_COMMIT (40 or 64 hexadecimal characters)")
        database = _validate_test_database()
        if not os.environ.get("OPENAI_API_KEY"):
            raise ValueError("Formal evaluation requires OPENAI_API_KEY before controllers start")
    from .runner import eval_workers

    eval_workers()  # reject a bad EVAL_WORKERS before any paid call
    program_commit = os.environ.get("EVAL_PROGRAM_COMMIT") or None
    output_dir = Path(args.output)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError(f"Output directory is not empty: {output_dir}")
    config_hash = sha256(args.config.read_bytes()).hexdigest()
    return raw_config, config, tasks, task_hash, database, output_dir, config_hash, program_commit


class _FakeUsage:
    def model_dump(self):
        return {"input_tokens": 0, "output_tokens": 0}


class _FakeJudgeResponse:
    status = "completed"
    usage = _FakeUsage()

    def __init__(self, output_text):
        self.output_text = output_text


class _FakeJudge:
    def __init__(self):
        self.responses = self

    def create(self, **request):
        payload = json.loads(request["input"])
        results = [
            {"index": criterion["index"], "passed": True, "reason": "Deterministic fake-client sample."}
            for criterion in payload["criteria"]
        ]
        return _FakeJudgeResponse(json.dumps({"criteria": results}))


def _dry_interactions(task, repeat, controller_name):
    interactions = [{"kind": "episode_start", "repeat": repeat}]
    interactions.append({"kind": "request", "message": task.request})
    preview_id = f"preview-dry-{task.id}-{controller_name}-{repeat}"
    for rule in task.required_order:
        names = rule.get("sequence", [])
        if names:
            for name in names:
                if name == "ask_user":
                    interactions.append({"kind": "ask_user", "topic": "event", "question": "Please clarify the synthetic task."})
                elif name == "scripted_answer":
                    if task.scripted_answers:
                        answer = task.scripted_answers[0]
                        interactions.append({"kind": "scripted_answer", "message": answer})
                        interactions.append({"kind": "request", "message": answer})
                elif name == "preview":
                    interactions.append({"kind": "preview", "preview_id": preview_id})
                elif name == "approval":
                    interactions.append({"kind": "approval", "preview_id": preview_id, "outcome": task.approval})
                else:
                    interactions.append({"kind": name})
        elif rule.get("event"):
            interactions.append({"kind": rule["event"]})
    if task.approval and not any(item.get("kind") == "approval" for item in interactions):
        interactions.extend([
            {"kind": "preview", "preview_id": preview_id},
            {"kind": "approval", "preview_id": preview_id, "outcome": task.approval},
        ])
    if task.expected_changes and not any(item.get("kind") == "write" for item in interactions):
        interactions.append({"kind": "write", "action": "synthetic_write"})
    interactions.append({"kind": "reply", "reply": "Synthetic dry run: this response was not generated by a model."})
    return interactions


def _run_fake_dry_run(tasks, config):
    episodes = []
    scored = []
    judge = _FakeJudge()
    judge_config = {"model_id": "fake-judge"}
    for task in tasks:
        expected_state = normalize_state(_apply_changes(task))
        for controller_name, repeats in (("agent", 3), ("baseline", 1)):
            for repeat in range(1, repeats + 1):
                interactions = _dry_interactions(task, repeat, controller_name)
                trace = [
                    {"type": "tool", "name": event.get("action", event["kind"]), "arguments": {}, "result": {"ok": True}}
                    for event in interactions
                    if event.get("kind") in {"ask_user", "preview", "write"}
                ]
                episode = EpisodeResult(
                    task_id=task.id,
                    controller=controller_name,
                    repeat=repeat,
                    status="complete",
                    reply="Synthetic dry run: this response was not generated by a model.",
                    trace=trace,
                    interactions=interactions,
                    final_state=expected_state,
                turns=sum(event.get("kind") in {"ask_user", "reply"} for event in interactions),
                    model_calls=0,
                    tool_calls=len(trace),
                    input_tokens=0,
                    output_tokens=0,
                    latency_ms=0,
                    error=None,
                )
                episodes.append(episode)
                grade = grade_episode(task, episode, judge_client=judge, judge_config=judge_config)
                scored.append(ScoredEpisode(task, episode, grade))
    return SuiteResult("complete", episodes), scored


def main(argv=None) -> int:
    from .runner import eval_workers

    args = _parser().parse_args(argv)
    try:
        raw_config, config, tasks, task_hash, database, output_dir, config_hash, program_commit = _preflight(args)
    except ValueError as exc:
        print(f"Evaluation did not start: {exc}", file=sys.stderr)
        return 2

    metadata = {
        "run_mode": "dry_run" if args.dry_run else "formal",
        "program_commit": program_commit,
        "source_tree_sha256": _source_tree_sha256(),
        "eval_config_sha256": config_hash,
        "frozen_eval_config": raw_config,
        "task_count": len(tasks),
        "task_version": config.task_version,
        "taskset_sha256": task_hash,
        "dataset_version": raw_config.get("dataset_version"),
        "fixture_version": raw_config.get("fixture_version"),
        "controller_model_id": config.model_id,
        "controller_reasoning_effort": config.reasoning_effort,
        "judge_model_id": config.judge_model_id,
        "judge_parameters": dict(config.judge_parameters),
        "judge_version": raw_config.get("judge_version"),
        "response_capture_version": RESPONSE_CAPTURE_VERSION,
        "baseline_version": BASELINE_VERSION,
        "judge_instructions": JUDGE_INSTRUCTIONS,
        "judge_schema": JUDGE_SCHEMA,
        "judge_instructions_sha256": raw_config["judge_instructions_sha256"],
        "judge_schema_sha256": raw_config["judge_schema_sha256"],
        "tool_version": raw_config.get("tool_version"),
        "openai_sdk_version": openai.__version__,
        "controller_limits": {
            "max_model_calls": config.max_model_calls,
            "max_tool_calls": config.max_tool_calls,
            "max_clarifications": config.max_clarifications,
            "max_output_tokens": config.max_output_tokens,
            "timeout_seconds": config.timeout_seconds,
        },
        "database_migrations": _database_migration_manifest(),
        "database_migration_version": raw_config["database_migration_version"],
        "model_service_host": urlsplit(os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").hostname,
        "planned_controller_episodes": len(tasks) * 4,
        "planned_judge_calls": len(tasks) * 4,
        "eval_workers": eval_workers(),
    }
    if database:
        metadata["database"] = database

    if args.dry_run:
        suite, results = _run_fake_dry_run(tasks, config)
        summary = summarize_runs(results)
        write_run_artifacts(output_dir, suite, results, summary, metadata=metadata)
        print(f"Fake-client dry run complete; not a live model comparison: {output_dir}")
        return 0

    from .runner import run_suite

    suite = run_suite(tasks, agent_factory, baseline_factory, config)
    if suite.status != "complete":
        write_run_artifacts(output_dir, suite, [], None, metadata=metadata)
        print(f"Evaluation incomplete; partial episodes saved: {output_dir}", file=sys.stderr)
        return 3

    client = openai.OpenAI(
        api_key=os.environ.get("OPENAI_API_KEY"),
        base_url=os.environ.get("OPENAI_BASE_URL") or None,
        max_retries=0,
        timeout=config.timeout_seconds,
    )
    try:
        results = grade_suite(tasks, suite, judge_client=client, judge_config=config, workers=eval_workers())
    except JudgeUnavailable as exc:
        incomplete = SuiteResult("incomplete", suite.episodes, str(exc))
        write_run_artifacts(output_dir, incomplete, exc.partial_results, None, metadata=metadata)
        print(f"Judge interrupted; partial episodes saved without final metrics: {output_dir}", file=sys.stderr)
        return 3

    summary = summarize_runs(results)
    write_run_artifacts(output_dir, suite, results, summary, metadata=metadata)
    print(f"Evaluation complete: {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
