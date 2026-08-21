# AI 开发入口

修改、调试或审查本仓库前，必须完整阅读并遵循根目录的 [`CONTRIBUTING.md`](CONTRIBUTING.md)。其中定义了项目架构、数据契约、数据源插件化、缓存与性能要求、测试矩阵以及 PR 复审和合并标准。

同时遵守以下规则：

- 先理解调用链和现有测试，再进行修改。
- 保持实现简单、改动范围最小，不处理无关问题。
- 不覆盖工作区已有修改，不虚构测试或审查结果。
- 以实际验证结果作为完成标准。

## Agent skills

### Issue tracker

本仓库的 issue 走 GitHub Issues（通过 `gh` CLI 操作）。详见 `docs/agents/issue-tracker.md`。

### Triage labels

五个标准 triage 角色直接使用角色名作为标签：`needs-triage` / `needs-info` / `ready-for-agent` / `ready-for-human` / `wontfix`。详见 `docs/agents/triage-labels.md`。

### Domain docs

单上下文（single-context）布局：`CONTEXT.md` 在仓库根目录，ADR 在 `docs/adr/`。详见 `docs/agents/domain.md`。
