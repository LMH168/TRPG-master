"""加载仓库内版本化 ModulePack 的最小运行时目录。

Phase 0 只加载结构化目录数据，不执行 PDF/DOCX 自动解析，也不把原文送入玩家投影。
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ModulePackError(ValueError):
    """ModulePack 缺少必需字段或引用不完整。"""


@dataclass(frozen=True, slots=True)
class ModulePack:
    """经过静态校验、可供 Phase 0 安装的模组目录。"""

    module_id: str
    version: str
    title: str
    manifest: dict[str, Any]
    catalog: dict[str, Any]
    runtime: dict[str, Any]
    content_hash: str


def load_module_pack(pack_dir: Path) -> ModulePack:
    """读取并校验一个 ModulePack 的 manifest/catalog 引用。"""

    manifest = _read_json(pack_dir / "manifest.json")
    catalog = _read_json(pack_dir / "catalog.json")
    runtime_file = manifest.get("runtime_file")
    for field in ("module_id", "title", "content_version", "catalog_file"):
        if not isinstance(manifest.get(field), str) or not manifest[field].strip():
            raise ModulePackError(f"manifest 缺少字段：{field}")
    if manifest["catalog_file"] != "catalog.json":
        raise ModulePackError("Phase 0 只允许 manifest 引用同目录 catalog.json")
    if catalog.get("title") != manifest["title"]:
        raise ModulePackError("manifest 与 catalog 的标题不一致")
    if not isinstance(catalog.get("story_pages"), list):
        raise ModulePackError("catalog.story_pages 必须是数组")
    _validate_source_file(pack_dir, manifest)
    if runtime_file is None:
        runtime = {}
        content_hash = _hash_json({"catalog": catalog})
    else:
        if runtime_file != "runtime.json":
            raise ModulePackError("运行包只能引用同目录 runtime.json")
        runtime = _read_json(pack_dir / runtime_file)
        _validate_runtime(runtime)
        content_hash = _hash_json(runtime)
        expected_hash = manifest.get("runtime_sha256")
        if expected_hash is not None and expected_hash != content_hash:
            raise ModulePackError("运行包哈希与 manifest 不一致")
    return ModulePack(
        module_id=manifest["module_id"],
        version=manifest["content_version"],
        title=manifest["title"],
        manifest=manifest,
        catalog=catalog,
        runtime=runtime,
        content_hash=content_hash,
    )


def load_preset(module_id: str, root: Path | None = None) -> ModulePack:
    """按稳定 module_id 查找仓库内预设，找不到时抛出明确错误。"""

    base = root or Path(__file__).resolve().parents[2] / "modules" / "presets"
    normalized_id = module_id.replace("-", "_")
    for pack_dir in base.iterdir():
        if pack_dir.is_dir() and (pack_dir / "manifest.json").exists():
            manifest = _read_json(pack_dir / "manifest.json")
            if str(manifest.get("module_id", "")).replace("-", "_") == normalized_id:
                return load_module_pack(pack_dir)
    raise ModulePackError(f"预设模组不存在：{module_id}")


def _read_json(path: Path) -> dict[str, Any]:
    """读取一个 JSON 对象文件，并统一转换解析错误。"""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ModulePackError(f"无法读取模组文件：{path}") from exc
    if not isinstance(value, dict):
        raise ModulePackError(f"模组文件必须是 JSON 对象：{path}")
    return value


def _hash_json(value: dict[str, Any]) -> str:
    """对规范化 JSON 计算稳定哈希，供房间冻结实际运行内容。"""

    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validate_source_file(pack_dir: Path, manifest: dict[str, Any]) -> None:
    """校验 manifest 声明的完整原文，避免运行数据失去可追溯来源。"""

    source_file = manifest.get("source_file")
    expected_hash = manifest.get("source_sha256")
    if source_file is None and expected_hash is None:
        return
    if not isinstance(source_file, str) or not source_file.startswith("source/"):
        raise ModulePackError("manifest.source_file 必须位于 source 目录")
    if not isinstance(expected_hash, str) or len(expected_hash) != 64:
        raise ModulePackError("manifest.source_sha256 必须是 SHA-256")
    source_path = pack_dir / source_file
    try:
        actual_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ModulePackError(f"无法读取模组原文：{source_path}") from exc
    if actual_hash != expected_hash:
        raise ModulePackError("模组原文哈希与 manifest 不一致")


def _validate_runtime(runtime: dict[str, Any]) -> None:
    """校验运行包的顶层结构，防止安装不完整的剧情数据。"""

    required = (
        "schema_version",
        "initial_scene_id",
        "scenes",
        "skills",
        "clues",
        "checkpoints",
        "endings",
    )
    if runtime.get("schema_version") == 2:
        required += (
            "locations",
            "objects",
            "facts",
            "npcs",
            "situations",
            "timeline",
            "source_fragments",
            "runtime_index",
            "actions",
        )
    missing = [field for field in required if field not in runtime]
    if missing:
        raise ModulePackError(f"运行包缺少字段：{','.join(missing)}")
    collection_fields = ("scenes", "skills", "clues", "checkpoints", "endings")
    if runtime.get("schema_version") == 2:
        collection_fields += (
            "locations",
            "objects",
            "facts",
            "npcs",
            "situations",
            "timeline",
            "source_fragments",
            "actions",
        )
    for field in collection_fields:
        if not isinstance(runtime[field], list):
            raise ModulePackError(f"运行包 {field} 必须是数组")
    if runtime.get("schema_version") == 2:
        _validate_v2_objects(runtime, collection_fields)
    # actions 是场景公开交互的通用声明；旧运行包没有时保持空集合。
    allowed_actions = {"inspect_target", "talk_to_npc", "choose_option"}
    for action in runtime.get("actions", []):
        if not isinstance(action, dict):
            raise ModulePackError("运行包 action 必须是对象")
        for field in ("scene_id", "target_id", "label"):
            if not isinstance(action.get(field), str) or not action[field]:
                raise ModulePackError(f"运行包 action 缺少字段：{field}")
        if action.get("action") not in allowed_actions:
            raise ModulePackError("运行包 action.action 不受支持")
        for field in ("aliases", "requires_clues"):
            value = action.get(field, [])
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise ModulePackError(f"运行包 action.{field} 必须是字符串数组")
        outcome = action.get("outcome")
        if outcome is not None:
            if not isinstance(outcome, dict):
                raise ModulePackError("运行包 action.outcome 必须是对象")
            for field in ("clues", "facts"):
                value = outcome.get(field, [])
                if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                    raise ModulePackError(f"运行包 action.outcome.{field} 必须是字符串数组")
            for field in ("scene_id", "location_id", "ending_id"):
                value = outcome.get(field)
                if value is not None and not isinstance(value, str):
                    raise ModulePackError(f"运行包 action.outcome.{field} 必须是字符串")
    # 披露门禁属于模组语义，不写死在主持代码中；未配置的旧运行包保持兼容。
    for guard in runtime.get("disclosure_guards", []):
        if not isinstance(guard, dict) or not isinstance(guard.get("term"), str):
            raise ModulePackError("运行包 disclosure_guard 必须包含 term")
        required = guard.get("requires_any_clues", [])
        if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
            raise ModulePackError("运行包 disclosure_guard.requires_any_clues 必须是字符串数组")
    for checkpoint in runtime["checkpoints"]:
        if not isinstance(checkpoint, dict):
            raise ModulePackError("运行包 checkpoint 必须是对象")
        for field in ("id", "scene_id", "skill"):
            if not isinstance(checkpoint.get(field), str) or not checkpoint[field]:
                raise ModulePackError(f"运行包 checkpoint 缺少字段：{field}")
        # targets/aliases 是通用语义绑定；缺失时兼容旧运行包，存在时必须是字符串数组。
        for field in ("targets", "aliases", "requires_clues"):
            value = checkpoint.get(field, [])
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise ModulePackError(f"运行包 checkpoint.{field} 必须是字符串数组")
        available_hours = checkpoint.get("available_hours")
        if available_hours is not None and (
            not isinstance(available_hours, dict)
            or any(
                not isinstance(available_hours.get(field), int)
                or not 0 <= available_hours[field] <= 23
                for field in ("start", "end")
            )
        ):
            raise ModulePackError("运行包 checkpoint.available_hours 必须包含 0-23 的 start/end")
        # 检定后果留在模组运行包中，Kernel 只解释通用的线索、场景和时间字段。
        for field in ("success_outcome", "failure_outcome"):
            outcome = checkpoint.get(field)
            if outcome is None:
                continue
            if not isinstance(outcome, dict):
                raise ModulePackError(f"运行包 checkpoint.{field} 必须是对象")
            for list_field in ("clues", "facts"):
                value = outcome.get(list_field, [])
                if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                    raise ModulePackError(
                        f"运行包 checkpoint.{field}.{list_field} 必须是字符串数组"
                    )
            scene_id = outcome.get("scene_id")
            if scene_id is not None and not isinstance(scene_id, str):
                raise ModulePackError(f"运行包 checkpoint.{field}.scene_id 必须是字符串")
            advance_minutes = outcome.get("advance_minutes", 0)
            if not isinstance(advance_minutes, int) or advance_minutes < 0:
                raise ModulePackError(f"运行包 checkpoint.{field}.advance_minutes 必须是非负整数")


def _validate_v2_objects(runtime: dict[str, Any], collection_fields: tuple[str, ...]) -> None:
    """校验 v2 对象的公共元数据以及运行时会解引用的稳定 ID。"""

    ids_by_collection: dict[str, set[str]] = {}
    for collection in collection_fields:
        ids: set[str] = set()
        for item in runtime[collection]:
            if not isinstance(item, dict):
                raise ModulePackError(f"运行包 {collection} 项必须是对象")
            item_id = item.get("id")
            if not isinstance(item_id, str) or not item_id:
                raise ModulePackError(f"运行包 {collection} 项缺少 id")
            if item_id in ids:
                raise ModulePackError(f"运行包 {collection} 存在重复 id：{item_id}")
            ids.add(item_id)
            if item.get("schema_version") != 1:
                raise ModulePackError(f"运行包 {collection}.{item_id} schema_version 无效")
            if item.get("visibility") not in {"public", "conditional", "keeper"}:
                raise ModulePackError(f"运行包 {collection}.{item_id} visibility 无效")
            if item.get("policy_origin") not in {
                "ruleset",
                "module",
                "product_default",
                "keeper_override",
            }:
                raise ModulePackError(f"运行包 {collection}.{item_id} policy_origin 无效")
            refs = item.get("source_refs")
            if (
                not isinstance(refs, list)
                or not refs
                or not all(isinstance(ref, str) and ref for ref in refs)
            ):
                raise ModulePackError(f"运行包 {collection}.{item_id} source_refs 无效")
            if (
                item.get("visibility") == "keeper"
                and any(
                    isinstance(item.get(field), str) and item.get(field)
                    for field in ("label", "text", "name", "content")
                )
                and item.get("player_text")
            ):
                raise ModulePackError(f"运行包 {collection}.{item_id} 混入玩家文本")
        ids_by_collection[collection] = ids

    fragment_ids = ids_by_collection["source_fragments"]
    for collection in collection_fields:
        if collection == "source_fragments":
            continue
        for item in runtime[collection]:
            for ref in item["source_refs"]:
                if (
                    ref.startswith("fragment:")
                    and ref.removeprefix("fragment:") not in fragment_ids
                ):
                    raise ModulePackError(f"运行包 {collection}.{item['id']} 来源引用不存在：{ref}")

    location_ids = ids_by_collection["locations"]
    scene_ids = ids_by_collection["scenes"]
    skill_ids = ids_by_collection["skills"]
    for scene in runtime["scenes"]:
        if scene.get("location_id") not in location_ids:
            raise ModulePackError(f"场景 {scene['id']} 引用了不存在的地点")
    for checkpoint in runtime["checkpoints"]:
        if checkpoint.get("scene_id") not in scene_ids or checkpoint.get("skill") not in skill_ids:
            raise ModulePackError(f"检定 {checkpoint['id']} 引用了不存在的场景或技能")
    if runtime.get("initial_scene_id") not in scene_ids:
        raise ModulePackError("initial_scene_id 引用了不存在的场景")
    if runtime.get("initial_location_id") not in location_ids:
        raise ModulePackError("initial_location_id 引用了不存在的地点")
    if not isinstance(runtime.get("runtime_index"), dict):
        raise ModulePackError("runtime_index 必须是对象")
    _validate_v2_references(runtime, ids_by_collection)


def _validate_v2_references(
    runtime: dict[str, Any], ids_by_collection: dict[str, set[str]]
) -> None:
    """解析 v2 跨对象引用，禁止把悬空 ID 留到游戏运行时才暴露。"""

    location_ids = ids_by_collection["locations"]
    scene_ids = ids_by_collection["scenes"]
    clue_ids = ids_by_collection["clues"]
    fact_ids = ids_by_collection["facts"]
    ending_ids = ids_by_collection["endings"]
    for item in runtime["objects"] + runtime["npcs"]:
        if item.get("location_id") not in location_ids:
            raise ModulePackError(f"运行对象 {item['id']} 引用了不存在的地点")
    for npc in runtime["npcs"]:
        if not set(npc.get("knowledge_fact_ids", [])) <= fact_ids:
            raise ModulePackError(f"NPC {npc['id']} 引用了不存在的事实")
    for situation in runtime["situations"]:
        if situation.get("scene_id") not in scene_ids:
            raise ModulePackError(f"情境 {situation['id']} 引用了不存在的场景")
    for collection in ("locations", "objects", "facts", "npcs", "situations"):
        for item in runtime[collection]:
            if not set(item.get("requires_clues", [])) <= clue_ids:
                raise ModulePackError(f"运行对象 {item['id']} 引用了不存在的线索")
    for action in runtime["actions"]:
        outcome = action.get("outcome", {})
        if action.get("scene_id") not in scene_ids:
            raise ModulePackError(f"动作 {action['id']} 引用了不存在的场景")
        if not set(action.get("requires_clues", [])) <= clue_ids:
            raise ModulePackError(f"动作 {action['id']} 引用了不存在的线索")
        if isinstance(outcome, dict):
            if not set(outcome.get("clues", [])) <= clue_ids:
                raise ModulePackError(f"动作 {action['id']} 产生不存在的线索")
            if outcome.get("scene_id") not in {None, *scene_ids}:
                raise ModulePackError(f"动作 {action['id']} 进入不存在的场景")
            if outcome.get("ending_id") not in {None, *ending_ids}:
                raise ModulePackError(f"动作 {action['id']} 进入不存在的结局")

    index_targets = {
        "object_ids": ids_by_collection["objects"],
        "npc_ids": ids_by_collection["npcs"],
        "fact_ids": fact_ids,
        "situation_ids": ids_by_collection["situations"],
        "source_fragment_ids": ids_by_collection["source_fragments"],
    }
    for scene_id, entry in runtime["runtime_index"].items():
        if scene_id not in scene_ids or not isinstance(entry, dict):
            raise ModulePackError(f"runtime_index 场景无效：{scene_id}")
        for field, valid_ids in index_targets.items():
            values = entry.get(field, [])
            if not isinstance(values, list) or not set(values) <= valid_ids:
                raise ModulePackError(f"runtime_index.{scene_id}.{field} 引用无效")
