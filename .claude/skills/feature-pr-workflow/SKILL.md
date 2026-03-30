---
name: feature-pr-workflow-enforcer
description: Ensure all new features are developed using proper branching and pull request workflows. Use when planning, implementing, or modifying features.
---

# Feature PR Workflow Enforcer

## Purpose

Enforce disciplined development practices:

- Every new feature must go through a branch + PR workflow
- Prevent uncontrolled direct changes to main branches
- Maintain clean and traceable development history

---

## When to Apply

Use this skill whenever:

- A new feature is requested
- A change affects functionality
- Refactoring introduces behavior changes
- Multiple files/modules are impacted

---

## Core Rules

### 1. Always Use a Branch

- Never implement directly on main or master
- Create a feature branch

Format:
feature/<short-description>

Example:
feature/add-forecast-module

---

### 2. Pull Request Required

Every feature must:

- Be submitted via a Pull Request (PR)
- Include:
  - Clear title
  - Description of changes
  - Scope
  - Testing notes

---

### 3. Exception Rule (Important)

If:

- There is an existing open PR
- AND the user explicitly approves adding changes to it

Then:

- The new changes may be added to the same PR

Otherwise:

- ALWAYS create a new PR

---

### 4. Feature Scope Discipline

Each PR should:

- Focus on a single feature or improvement
- Avoid mixing unrelated changes
- Be small and reviewable

---

## Required PR Structure

### PR Title

Format:
feat: <short feature description>

Examples:
feat: add account management module
feat: implement transaction filtering

---

### PR Description Template

## Summary

What this PR does

## Changes

- List of changes

## UI Changes (if applicable)

Describe frontend changes

## Backend Changes (if applicable)

Describe API/data changes

## Testing

How this was tested

## Notes

Any important considerations

---

## Workflow Steps

1. Understand the feature
2. Define scope
3. Create branch
4. Implement backend changes
5. Implement frontend changes
6. Test locally
7. Prepare PR
8. Submit for review

---

## Enforcement Behavior

When this skill is active:

If user asks to add a feature:

- Respond with:
  - Branch name
  - PR title
  - PR description
  - Implementation plan

If user tries to bypass PR:

- Politely enforce workflow
- Explain why PR is required

If unclear:

- Ask whether to:
  - create a new PR
  - OR extend an existing one

---

## Output Expectations

Always include:

- Branch name
- PR title
- PR description
- Summary of changes

Optional:

- File/module breakdown
- Step-by-step implementation

---

## Anti-Patterns (Reject)

- Direct commits to main
- Large, unfocused PRs
- Mixing multiple features
- Missing PR descriptions
- No testing strategy
