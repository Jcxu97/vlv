# claude-code-multi-agent-harness

面向 **Cursor Agent** 的行为约定：在**非琐碎任务**上主动走 **Spec → 执行 → 评审 pass** 闭环，强调 **少追问**、**可验证信源**（DOI / 高星 GitHub / 官方文档），并覆盖 **学术类**（文献、综述、实验设计）的浓缩纪律。  
**不是**可执行插件，**不**随 VLV 应用在运行时加载。

## 与本仓库（VLV）的关系

- 本目录是 **随 [`Jcxu97/vlv`](https://github.com/Jcxu97/vlv) 分发的副本**，便于克隆后查阅或与团队对齐同一套协作约定。
- 应用功能、依赖与发布流程见仓库根目录 [**README.md**](https://github.com/Jcxu97/vlv/blob/main/README.md)。

## 推荐安装方式（全局默认）

Cursor 会从 **用户级**目录自动发现 Skills（见 [Cursor Agent Skills](https://cursor.com/docs/context/skills)）。

| 平台 | 路径 |
|------|------|
| Windows | `%USERPROFILE%\.cursor\skills\claude-code-multi-agent-harness\SKILL.md` |
| macOS / Linux | `~/.cursor/skills/claude-code-multi-agent-harness/SKILL.md` |

将本文件夹 **`claude-code-multi-agent-harness`** 整份复制到上述 `skills` 目录下即可（保留 `SKILL.md` 文件名）。

本仓库**不再**使用项目内 `alwaysApply` 规则强制挂载；若希望所有对话都强化习惯，可在 **Cursor → Settings → Rules → User Rules** 中用纯文本加一句说明（例如提醒遵循该 Skill）。

## 文档入口

- **正文**：同目录下的 [**SKILL.md**](./SKILL.md)（含触发启发、信源优先级、论文与实验设计浓缩节、可选叠加的其他 Skills 表、工作流与反模式）。

## 可选叠加

`SKILL.md` 内的表格列出可与本 harness 叠加的公开 Skills（验证、调试、科学写作、Mermaid / UML 图表等）。安装方式以各仓库 README 为准（常见为 `npx skills add owner/repo`）。

## 许可证

与上游仓库一致：**MIT**（见仓库根目录 `LICENSE`）。
