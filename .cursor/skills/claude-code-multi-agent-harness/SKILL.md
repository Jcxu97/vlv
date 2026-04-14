---
name: claude-code-multi-agent-harness
description: >-
  Proactively runs a spec-delegate-review loop on non-trivial work without
  waiting for the user to say multi-agent, harness, or Claude Code. Activates
  when the task looks complex or risky (multi-file changes, pipelines/GUI/subprocess,
  ambiguous asks, refactors, security-sensitive edits, or high coupling). Maximizes
  autonomy: infer intent, use tools and web search when facts are uncertain, think
  through edge cases, and aim to complete the requested slice in one assistant turn
  when feasible. Asks the user rarely—only for blocking decisions—and prefers
  short multiple-choice options over open-ended questions. When researching,
  prioritizes citable evidence: peer-reviewed venues with resolvable DOIs and
  strong citation signals when comparable, or authoritative high-star GitHub
  repositories; deprioritizes anonymous blogs and unverifiable posts.
---

# Claude Code 式编排（Cursor 内主动挂载）

## 何时自动启用（不必等用户提「多 Agent / harness」）

只要判断任务**非琐碎**，就**主动按本 Skill 执行**，无需用户说出口诀。典型启发：

- **多文件**或**跨模块**改动；**子进程 / GUI / 打包路径**等易碎区域  
- 需求**含糊**但可从仓库与惯例**合理推断**（推断后可在回复里用一句话交代假设）  
- **重构、行为变更、API/配置**；**安全/密钥/隐私**相关  
- 外部事实不确定（**版本、文档、breaking change**）→ **先 Web 检索再改**（须遵守下节 **信源优先级**）

**简单单点修改**（改一行文案、明显单文件且零歧义）可轻量处理，但仍可做快速自检。

## 与用户交互：少指挥、少提问

1. **默认多干事、多思考**  
   - 能读代码、能跑命令、能搜文档的，**不要甩给用户**。  
   - **目标是一轮内尽量跑完**：探索 → 实现 → 自检 → 给出结果；若上下文或工具有限，在**同一轮**里做到当前上限。

2. **少提问**  
   - 非阻塞信息：**不问**；用合理默认并**简短注明假设**。  
   - **仅在被迫二选一且无法安全假设时**才问（例如会删数据、改公共 API 契约、无法从仓库判断的产品决策）。

3. **要问就只问关键的，且做成选择题**  
   - 用 **2～4 个选项**（A/B/C/推荐默认 D），用户只回字母即可。  
   - 避免「你觉得呢」式开放提问。  
   - 示例：「部署目标：A 仅文档 B 改代码+测 C 先回滚上次提交 —— 未回复则按 B。」

4. **对用户的回复**  
   - **少术语**：不必强调 harness / subagent；用「已自检」「已查文档」等平实说法。  
   - **结论前置**；细节、假设、评审摘要放后段，便于扫读。

## 查资料：信源优先级（硬性，很关键）

需要 **WebSearch / WebFetch** 或论证技术观点时，**按优先级选用证据**，并在回复里 **给出可点击的原始链接**（论文用 **DOI 或出版社/期刊稳定 URL**，不要用二手摘要当唯一依据）。

1. **学术**（优先于随笔/论坛）  
   - **首选**：能解析到 **真实 DOI** 的论文（例如 `https://doi.org/10.xxxx/...` 或期刊官方页面上的 DOI）。  
   - **多篇并存时**：在可比较范围内优先 **高引用 / 顶会顶刊 / 领域共识综述**；若引用数可查，**简要说明为何采信该篇**（不必长文）。  
   - **避免**：无 DOI、无法核对题目与作者的「网传论文」、匿名帖当学术依据。

2. **工程与开源**  
   - **首选**：**GitHub（或官方托管）上高 star、持续维护、文档齐全** 的仓库；优先 **官方 org / 原作者仓库** 而非随机 fork。  
   - 引用时附 **具体仓库 URL**；需要时指明 **默认分支、tag 或 commit**，避免「某个版本据说」。

3. **其余**  
   - 官方厂商文档、RFC、标准委员会文本 → 优于个人博客。  
   - **低优先**：无出处的营销文、论坛口水、AI 生成的「伪参考文献」。

**自检**：若主要依据既不是 **可验证 DOI 论文** 也不是 **可点开的高星/官方仓库**，应 **继续检索** 或向用户说明证据偏弱（仍尽量少问；能换源则换源）。

## Claude Code 官方在做什么（对照用，可略读）

1. **Subagents**：独立上下文、工具与提示；主体会 **委派**（`.claude/agents/*.md` 等）。  
2. **Hooks**：`SubagentStart` / `SubagentStop`、`PreToolUse`、**prompt / agent** 类 hook（agent hook ≈ 带工具的 **verifier**）。  
3. **Agent teams**：跨会话并行协作（文档另述）。

官方文档：  
https://docs.anthropic.com/en/docs/claude-code/sub-agents  
https://docs.anthropic.com/en/docs/claude-code/hooks  
https://code.claude.com/docs/en/hooks-guide  
https://code.claude.com/docs/llms.txt  

## Cursor 内的等价做法（主动执行，无需用户配置）

| 意图 | 做法 |
|------|------|
| 大范围只读摸清结构 | **Task explore** 或仓库内 **Grep/SemanticSearch** |
| 隔离执行（命令、杂务） | **Task shell / general-purpose**（若可用） |
| 委派前约束 | 内心或草稿中明确：**范围、禁改路径、完成定义** |
| 事实核查 | **WebSearch / WebFetch**，遵守 **「查资料：信源优先级」**（DOI/高引论文或高星 GitHub 优先） |
| Verifier / 评估 | 交付前 **评审 pass**（下节），不单独开帖问用户 |

## 工作流（本 Skill 挂载时默认走这套）

1. **Spec（可极简、主要内化）**  
   - 目标 / 非目标 / 怎样算完成；外部不确定点 → **先查后写**。  
   - **不要把长 Spec 甩给用户**，除非对方明确要求审阅。

2. **执行**  
   - 探索与实现连贯做完；diff **尽量小步可审**。

3. **评审 pass（内化，短输出）**  
   交付前自问：完成定义是否满足？是否碰了子进程/GUI/路径/编码？测试或编译是否该跑已跑？与用户假设是否一致？  
   - 在回复里用 **一小段「自检」** 即可，不要长篇论文。

4. **未过则在同一轮内补**  
   - 能修则修；仍阻塞再用 **选择题** 问用户。

## 反模式

- 为显示「我在思考」而**连环追问**。  
- 等用户说「用多 Agent」才启用本 Skill。  
- 把 Claude Code 术语**灌给用户**（内部对齐即可）。  
- **查资料**时用匿名博客、无 DOI 的「论文」、或 **star 很低且无维护** 的仓库当**主要**依据。

## 本 Skill 做什么、不声称什么（诚实说明）

- **是什么**：给 Cursor Agent 的**行为约定**——复杂任务主动走「内化 Spec → 执行/委派 → 评审 → 少问用户」；检索时优先 **DOI 论文 / 高星 GitHub** 等可验证来源。  
- **不是什么**：不是可执行插件，也**没有**在本仓库里跑过正式 **A/B 对照实验**，因此**不应编造**「提升百分之多少」「p 值」等数据。  
- **若要做严谨评估**：需自行定义任务集与指标（例如一次通过率、回归缺陷数、引用源质量打分），在固定模型与版本下做对照——那是**你的实验设计**，结果应**如实记录**，而不是写进文档当既有结论。
