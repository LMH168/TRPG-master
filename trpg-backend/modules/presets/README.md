<!-- 本文件说明四个预设模组的目录约定，避免原始资料与运行时 ModulePack 混放。 -->
# 预设模组目录

每个预设使用一个稳定中文目录，目录元数据放在 `manifest.json`。公开目录数据放在 `catalog.json`，已实现内容放在 `runtime.json`，完整原文归档在 `source/` 并进入 Git。Context Builder 只按权限读取运行包中的少量来源片段，Game Kernel 只执行经过版本化校验的结构化数据，不直接执行 PDF/DOC/DOCX。

| 目录 | 模组 | 当前状态 |
| --- | --- | --- |
| `追书人` | 追书人 | `phase-1c-content-ready` |
| `银之锁` | 银之锁 | `source_only` |
| `林隙的罪恶` | 林隙的罪恶 | `source_only` |
| `坨子岛` | 坨子岛 | `source_only` |
