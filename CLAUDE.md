@AGENTS.md

## Claude Code specifics
- The Higgsfield MCP server is declared in `.mcp.json`; authenticate it with `/mcp` on first use.
- To render items parked in `awaiting_render`, use the `higgsfield-render` skill
  (`.claude/skills/higgsfield-render/SKILL.md`). Always preflight cost before generating.
