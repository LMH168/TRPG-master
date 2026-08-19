"""加载仓库内版本化 ModulePack 的最小运行时目录。

Phase 0 只加载结构化目录数据，不执行 PDF/DOCX 自动解析，也不把原文送入玩家投影。
"""

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
    if runtime_file is None:
        runtime = {}
    else:
        if runtime_file != "runtime.json":
            raise ModulePackError("运行包只能引用同目录 runtime.json")
        runtime = _read_json(pack_dir / runtime_file)
        _validate_runtime(runtime)
    return ModulePack(
        module_id=manifest["module_id"],
        version=manifest["content_version"],
        title=manifest["title"],
        manifest=manifest,
        catalog=catalog,
        runtime=runtime,
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
    missing = [field for field in required if field not in runtime]
    if missing:
        raise ModulePackError(f"运行包缺少字段：{','.join(missing)}")
    for field in ("scenes", "clues", "checkpoints", "endings"):
        if not isinstance(runtime[field], list):
            raise ModulePackError(f"运行包 {field} 必须是数组")
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
