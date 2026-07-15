#!/usr/bin/env python3
"""Static agent config — agents.yaml + persona frontmatter (not live roster)."""

from __future__ import annotations

import ast
import re
from pathlib import Path
from string import Formatter
from typing import Any

from agency_paths import kit_root, resolve_resource

HUB = "orchestrator"


def parse_agents_fallback(text: str) -> dict[str, Any]:
    agents: dict[str, Any] = {"agents": {}, "spawn": {"maxSpecialistPanes": 6}}
    current: str | None = None
    in_agents = False
    for line in text.splitlines():
        if line.startswith("agents:"):
            in_agents = True
            continue
        if line.startswith("spawn:"):
            in_agents = False
            current = None
            continue
        if in_agents and line.startswith("  ") and not line.startswith("    ") and line.strip().endswith(":"):
            current = line.strip().rstrip(":")
            if current.startswith("#"):
                current = None
                continue
            agents["agents"][current] = {"peers": [], "lifecycleDefault": "temporary"}
            continue
        if not current:
            if "maxSpecialistPanes:" in line:
                try:
                    agents["spawn"]["maxSpecialistPanes"] = int(line.split(":", 1)[1].strip())
                except ValueError:
                    pass
            continue
        if "lifecycleDefault:" in line:
            agents["agents"][current]["lifecycleDefault"] = line.split(":", 1)[1].strip()
        elif "charterPath:" in line:
            agents["agents"][current]["charterPath"] = line.split(":", 1)[1].strip()
        elif "skillPath:" in line:
            val = line.split(":", 1)[1].strip()
            agents["agents"][current]["skillPath"] = None if val in ("null", "~", "") else val
        elif "agentPath:" in line:
            agents["agents"][current]["agentPath"] = line.split(":", 1)[1].strip()
        elif "peers:" in line:
            rest = line.split("peers:", 1)[1].strip()
            if rest.startswith("[") and rest.endswith("]"):
                inner = rest[1:-1].strip()
                agents["agents"][current]["peers"] = [p.strip() for p in inner.split(",") if p.strip()]
        elif "maxSpecialistPanes:" in line:
            try:
                agents["spawn"]["maxSpecialistPanes"] = int(line.split(":", 1)[1].strip())
            except ValueError:
                pass
    return agents


def load_agents(root: Path) -> dict[str, Any]:
    path = root / "agents.yaml"
    if not path.exists():
        path = kit_root() / "agents.yaml"
    if not path.exists():
        return {"agents": {}, "spawn": {"maxSpecialistPanes": 6}}
    text = path.read_text()
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(text) or {}
        return data if isinstance(data, dict) else parse_agents_fallback(text)
    except ImportError:
        return parse_agents_fallback(text)


def _parse_yaml_fallback(text: str) -> Any:
    """Parse the small, dependency-free YAML subset used by agency catalogs."""
    lines: list[tuple[int, str]] = []
    for line_number, raw in enumerate(text.splitlines(), 1):
        if "\t" in raw[: len(raw) - len(raw.lstrip())]:
            raise ValueError(f"tabs are not allowed at line {line_number}")
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if indent % 2:
            raise ValueError(f"indentation must use two-space steps at line {line_number}")
        lines.append((indent, raw.strip()))

    if not lines:
        return None

    def scalar(value: str) -> Any:
        value = value.strip()
        if value.startswith("["):
            if not value.endswith("]"):
                raise ValueError("unterminated inline list")
            body = value[1:-1].strip()
            return [] if not body else [scalar(item) for item in body.split(",")]
        if value == "{}":
            return {}
        if value in ("null", "~"):
            return None
        if value.lower() in ("true", "false"):
            return value.lower() == "true"
        if value.startswith(("'", '"')):
            try:
                parsed = ast.literal_eval(value)
            except (SyntaxError, ValueError) as exc:
                raise ValueError(f"invalid quoted scalar {value!r}") from exc
            if not isinstance(parsed, str):
                raise ValueError(f"invalid scalar {value!r}")
            return parsed
        return value

    def pair(content: str) -> tuple[str, str]:
        if ":" not in content:
            raise ValueError(f"expected mapping entry, got {content!r}")
        key, value = content.split(":", 1)
        key = key.strip()
        if not key:
            raise ValueError("mapping key must not be empty")
        return key, value.strip()

    def block(index: int, indent: int) -> tuple[Any, int]:
        if index >= len(lines) or lines[index][0] != indent:
            raise ValueError("invalid indentation")
        is_list = lines[index][1].startswith("-")
        result: Any = [] if is_list else {}
        while index < len(lines) and lines[index][0] == indent:
            content = lines[index][1]
            if content.startswith("-") != is_list:
                raise ValueError("cannot mix mapping and sequence entries")
            if is_list:
                item = content[1:].strip()
                index += 1
                if not item:
                    if index >= len(lines) or lines[index][0] <= indent:
                        raise ValueError("sequence item must not be empty")
                    value, index = block(index, indent + 2)
                    result.append(value)
                    continue
                if ":" not in item:
                    result.append(scalar(item))
                    continue
                key, raw_value = pair(item)
                entry: dict[str, Any] = {}
                if raw_value:
                    entry[key] = scalar(raw_value)
                else:
                    if index >= len(lines) or lines[index][0] <= indent:
                        entry[key] = None
                    else:
                        entry[key], index = block(index, indent + 2)
                if index < len(lines) and lines[index][0] == indent + 2:
                    continuation, index = block(index, indent + 2)
                    if not isinstance(continuation, dict):
                        raise ValueError("sequence mapping continuation must be a mapping")
                    duplicate = set(entry).intersection(continuation)
                    if duplicate:
                        raise ValueError(f"duplicate key {sorted(duplicate)[0]!r}")
                    entry.update(continuation)
                result.append(entry)
                continue

            key, raw_value = pair(content)
            if key in result:
                raise ValueError(f"duplicate key {key!r}")
            index += 1
            if raw_value:
                result[key] = scalar(raw_value)
            elif index < len(lines) and lines[index][0] > indent:
                if lines[index][0] != indent + 2:
                    raise ValueError("invalid indentation")
                result[key], index = block(index, indent + 2)
            else:
                result[key] = None
        return result, index

    parsed, end = block(0, lines[0][0])
    if lines[0][0] != 0 or end != len(lines):
        raise ValueError("invalid indentation")
    return parsed


def _load_yaml_strict(path: Path) -> Any:
    text = path.read_text()
    try:
        import yaml  # type: ignore
    except ImportError:
        loader = _parse_yaml_fallback
    else:
        loader = yaml.safe_load
    try:
        return loader(text)
    except Exception as exc:
        raise ValueError(f"{path}: malformed YAML: {exc}") from exc


def _unsupported_keys(value: dict[str, Any], allowed: set[str], context: str) -> None:
    extra = set(value).difference(allowed)
    if extra:
        raise ValueError(f"{context}: unsupported key {sorted(extra)[0]!r}")


def _validate_goal(goal: Any, context: str) -> str:
    if not isinstance(goal, str) or not goal.strip():
        raise ValueError(f"{context}: goal must be a non-empty string")
    try:
        fields = list(Formatter().parse(goal))
    except ValueError as exc:
        raise ValueError(f"{context}: unsupported placeholder syntax: {exc}") from exc
    for _, field, format_spec, conversion in fields:
        if field is None:
            continue
        if field != "topic":
            raise ValueError(f"{context}: unsupported placeholder {field!r}; only '{{topic}}' is supported")
        if format_spec or conversion:
            raise ValueError(f"{context}: unsupported placeholder formatting; only '{{topic}}' is supported")
    return goal


def load_pipelines(root: Path) -> dict[str, Any]:
    """Load and strictly validate one project's pipelines.yaml catalog."""
    path = root / "pipelines.yaml"
    if not path.exists():
        return {}
    data = _load_yaml_strict(path)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: root must be a mapping")
    _unsupported_keys(data, {"pipelines"}, str(path))
    pipelines = data.get("pipelines")
    if not isinstance(pipelines, dict):
        raise ValueError(f"{path}: 'pipelines' must be a mapping")

    known_roles = set((load_agents(root).get("agents") or {}).keys())
    normalized: dict[str, Any] = {}
    identifier = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
    for pipeline_name, raw_pipeline in pipelines.items():
        pipeline_context = f"{path}: pipeline {pipeline_name!r}"
        if not isinstance(pipeline_name, str) or not identifier.fullmatch(pipeline_name):
            raise ValueError(f"{path}: invalid pipeline name {pipeline_name!r}")
        if not isinstance(raw_pipeline, dict):
            raise ValueError(f"{pipeline_context}: definition must be a mapping")
        _unsupported_keys(raw_pipeline, {"description", "onFailure", "stages"}, pipeline_context)
        description = raw_pipeline.get("description", "")
        if not isinstance(description, str):
            raise ValueError(f"{pipeline_context}: description must be a string")
        on_failure = raw_pipeline.get("onFailure", "stop")
        if on_failure not in ("stop", "continue"):
            raise ValueError(f"{pipeline_context}: onFailure must be stop or continue")
        stages = raw_pipeline.get("stages")
        if not isinstance(stages, list) or not stages:
            raise ValueError(f"{pipeline_context}: stages must be a non-empty list")

        seen_ids: set[str] = set()
        declared_outputs: dict[str, set[str]] = {}
        normalized_stages: list[dict[str, Any]] = []
        for index, raw_stage in enumerate(stages):
            initial_context = f"{pipeline_context}, stage {index + 1}"
            if not isinstance(raw_stage, dict):
                raise ValueError(f"{initial_context}: definition must be a mapping")
            _unsupported_keys(raw_stage, {"id", "role", "goal", "outputs", "inputs"}, initial_context)
            stage_id = raw_stage.get("id")
            stage_context = f"{pipeline_context}, stage {stage_id!r}"
            if not isinstance(stage_id, str) or not identifier.fullmatch(stage_id):
                raise ValueError(f"{initial_context}: invalid stage id {stage_id!r}")
            if stage_id in seen_ids:
                raise ValueError(f"{stage_context}: duplicate stage id {stage_id!r}")
            role = raw_stage.get("role")
            if not isinstance(role, str) or role not in known_roles:
                raise ValueError(f"{stage_context}: unknown role {role!r}")
            goal = _validate_goal(raw_stage.get("goal"), stage_context)

            outputs = raw_stage.get("outputs")
            if not isinstance(outputs, list) or not outputs:
                raise ValueError(f"{stage_context}: outputs must be a non-empty list")
            output_set: set[str] = set()
            for output in outputs:
                if not isinstance(output, str) or not identifier.fullmatch(output):
                    raise ValueError(f"{stage_context}: invalid output {output!r}")
                if output in output_set:
                    raise ValueError(f"{stage_context}: duplicate output {output!r}")
                output_set.add(output)

            inputs = raw_stage.get("inputs", [])
            if not isinstance(inputs, list):
                raise ValueError(f"{stage_context}: inputs must be a list")
            selectors: set[tuple[str, str]] = set()
            normalized_inputs: list[dict[str, Any]] = []
            for input_index, raw_input in enumerate(inputs):
                input_context = f"{stage_context}, input {input_index + 1}"
                if not isinstance(raw_input, dict):
                    raise ValueError(f"{input_context}: selector must be a mapping")
                _unsupported_keys(raw_input, {"stage", "artifacts"}, input_context)
                source = raw_input.get("stage")
                if not isinstance(source, str) or source not in seen_ids:
                    raise ValueError(f"{input_context}: unknown or forward stage reference {source!r}")
                artifacts = raw_input.get("artifacts")
                if not isinstance(artifacts, list) or not artifacts:
                    raise ValueError(f"{input_context}: artifacts must be a non-empty list")
                for artifact in artifacts:
                    if not isinstance(artifact, str) or not identifier.fullmatch(artifact):
                        raise ValueError(f"{input_context}: invalid artifact selector {artifact!r}")
                    selector = (source, artifact)
                    selector_name = f"{source}.{artifact}"
                    if selector in selectors:
                        raise ValueError(f"{input_context}: duplicate selector {selector_name!r}")
                    if artifact not in declared_outputs[source]:
                        raise ValueError(f"{input_context}: undeclared output {selector_name!r}")
                    selectors.add(selector)
                normalized_inputs.append({"stage": source, "artifacts": list(artifacts)})

            normalized_stages.append(
                {"id": stage_id, "role": role, "goal": goal, "outputs": list(outputs), "inputs": normalized_inputs}
            )
            seen_ids.add(stage_id)
            declared_outputs[stage_id] = output_set

        normalized[pipeline_name] = {
            "description": description,
            "onFailure": on_failure,
            "stages": normalized_stages,
        }
    return {"pipelines": normalized}


def role_of(instance: str) -> str:
    if instance == HUB:
        return HUB
    if "-t" in instance:
        return instance.split("-t", 1)[0]
    return instance


def agent_file_for(role: str, agent: dict[str, Any] | None) -> Path | None:
    rel = (agent or {}).get("agentPath") or f".pi/agents/{role}.md"
    path = resolve_resource(rel)
    if path and path.is_file():
        return path
    alt = resolve_resource(f"agents/{role}.md")
    return alt if alt and alt.is_file() else None


def parse_agent_frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text()
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    block = text[3:end].strip()
    out: dict[str, str] = {}
    for line in block.splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        out[k.strip()] = v.strip().strip("'\"")
    return out


def role_defaults(agents: dict[str, Any], role: str) -> dict[str, Any]:
    return dict((agents.get("agents") or {}).get(role) or {})


def max_specialist_panes(agents: dict[str, Any], default: int = 6) -> int:
    spawn = agents.get("spawn") or {}
    try:
        return int(spawn.get("maxSpecialistPanes", default))
    except (TypeError, ValueError):
        return default
