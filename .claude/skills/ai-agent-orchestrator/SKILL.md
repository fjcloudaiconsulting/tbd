---
name: ai-agent-orchestrator
description: Design and enforce multi-agent workflows with safe automation, approvals, and task delegation. Use when implementing AI features or autonomous workflows.
---

# AI Agent Orchestrator

## Purpose

Enable safe, structured, and extensible multi-agent AI workflows with human oversight.

---

## When to Use

- Adding AI features
- Designing automation flows
- Implementing multi-agent systems
- Integrating OpenAI, Claude, or Ollama

---

## Core Concepts

### Agents

Each agent must have a clear role:

- Planner → breaks down tasks
- Executor → performs actions
- Reviewer → validates results
- Approver → human or human-in-the-loop

---

### Workflow Model

1. User input
2. Planner agent creates tasks
3. Executor agents perform actions
4. Reviewer agent validates
5. Human approval (if required)
6. Final execution

---

## Approval System (Mandatory)

AI must NEVER:

- Create transactions directly
- Modify financial data without approval

Instead:

- Generate proposals
- Require confirmation

---

## Agent Roles

### Planner Agent

- Converts natural language into structured tasks

### Executor Agent

- Performs actions (API calls, DB changes via services)

### Reviewer Agent

- Validates correctness and safety

### Approval Layer

- User confirms before execution

---

## Integration Design

### Provider Abstraction

All AI providers must be abstracted:

/ai/providers
openai.py
anthropic.py
ollama.py

Use a common interface:

- generate_text()
- structured_output()
- function_call()

---

## Prompt Design Rules

- Always include context
- Use structured outputs (JSON)
- Avoid free-form destructive actions

---

## Safety Rules

- No direct DB writes by AI
- No silent operations
- Always log AI decisions

---

## Logging & Audit

Track:

- prompts
- responses
- decisions
- approvals

---

## UX Guidelines

- Show AI suggestions clearly
- Allow editing before approval
- Provide confidence indicators

---

## Enforcement Behavior

If unsafe:

- Block execution
- Require approval

If unclear:

- Ask user

---

## Anti-Patterns

- Fully autonomous financial actions
- Hidden AI decisions
- No audit trail
- Tight coupling to one provider

---

## Output Expectations

Always include:

- Agent roles involved
- Workflow steps
- Approval points
- API/design impact
