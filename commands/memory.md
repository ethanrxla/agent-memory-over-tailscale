---
description: Pull project-scoped context from the agent-memory hub (brief, or search when given a query)
argument-hint: [optional search query]
allowed-tools: Bash(python3:*)
---

Relevant context from the shared agent-memory hub for the current project:

!`python3 "$HOME/.claude/skills/agent-memory/memory.py" context "$ARGUMENTS"`

Ground your work in the context above. If it reports no prior context, start fresh — don't invent history. Don't redo work or re-litigate decisions already recorded there. If it surfaced open threads relevant to the task, pick up from them.
