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


def load_module_pack(pack_dir: Path) -> ModulePack:
    """读取并校验一个 ModulePack 的 manifest/catalog 引用。"""

    manifest = _read_json(pack_dir / "manifest.json")
    catalog = _read_json(pack_dir / "catalog.json")
    for field in ("module_id", "title", "content_version", "catalog_file"):
        if not isinstance(manifest.get(field), str) or not manifest[field].strip():
            raise ModulePackError(f"manifest 缺少字段：{field}")
    if manifest["catalog_file"] != "catalog.json":
        raise ModulePackError("Phase 0 只允许 manifest 引用同目录 catalog.json")
    if catalog.get("title") != manifest["title"]:
        raise ModulePackError("manifest 与 catalog 的标题不一致")
    if not isinstance(catalog.get("story_pages"), list):
        raise ModulePackError("catalog.story_pages 必须是数组")
    return ModulePack(
        module_id=manifest["module_id"],
        version=manifest["content_version"],
        title=manifest["title"],
        manifest=manifest,
        catalog=catalog,
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
