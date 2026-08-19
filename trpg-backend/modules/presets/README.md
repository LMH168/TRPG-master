<!-- 本文件说明四个预设模组的目录约定，避免原始资料与运行时 ModulePack 混放。 -->
# 预设模组目录

每个预设使用一个稳定 slug 目录，原始文件放在 `source/`，目录元数据放在 `manifest.json`。公开目录数据可以放在 `catalog.json`；`source_only` 表示资料已归档但尚未编译为运行时 ModulePack；后续 Context Builder 和 Game Kernel 只能读取经过版本化校验的运行包。

| 目录 | 模组 | 当前状态 |
| --- | --- | --- |
| `追书人` | 追书人 | `source_only` |
| `银之锁` | 银之锁 | `source_only` |
| `林隙的罪恶` | 林隙的罪恶 | `source_only` |
| `坨子岛` | 坨子岛 | `source_only` |
