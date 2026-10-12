# AGENTS.md

本專案每個 session 的入口是 [`CLAUDE.md`](CLAUDE.md),動手前請先完整閱讀。它摘錄規則的原則與邊界;規則的權威順序是 `DECISION_PROTOCOL.md`、`AGENT_DISPATCH.md`、現行競賽的操作手冊,衝突時以這三者為準。

- 第一個動作:`git pull --ff-only`,然後 `python -m football_ai.competitions`,見 `CLAUDE.md`「第一個動作」。
- 操作手冊:`.claude/skills/*/SKILL.md`,現行競賽為 `.claude/skills/epl-2026-27/SKILL.md`。
- 多 agent 腳本:`.claude/workflows/*.js`。
