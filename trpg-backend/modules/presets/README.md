<!-- 本文件说明四个预设模组的目录约定，避免原始资料与运行时 ModulePack 混放。 -->
# 预设模组目录

每个预设使用一个稳定中文目录，目录元数据放在 `manifest.json`。公开目录数据可以放在 `catalog.json`，已实现内容放在 `runtime.json`。原始模组文件只保留在开发者本机的 `source/`，不进入 Git；Context Builder 和 Game Kernel 只能读取经过版本化校验的结构化运行包。

| 目录 | 模组 | 当前状态 |
| --- | --- | --- |
| `追书人` | 追书人 | `phase-1c` |
| `银之锁` | 银之锁 | `source_only` |
| `林隙的罪恶` | 林隙的罪恶 | `source_only` |
| `坨子岛` | 坨子岛 | `source_only` |
