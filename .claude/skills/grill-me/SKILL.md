---
name: grill-me
description: Interview the user relentlessly about a plan or design until reaching shared understanding, resolving each branch of the decision tree. Use when user wants to stress-test a plan, get grilled on their design, or mentions "grill me".
model: sonnet
---

Interview the user about their plan until the decisions that matter are settled.

## How to ask

- Use the **AskUserQuestion tool**, never plain-text questions.
- Ask **up to 4 independent questions in one call** (the tool takes up to 4). Ask one alone only when its answer
  changes what you would ask next.
- Give each question 2–4 concrete options, the one you recommend first with "(Recommended)". The user always has
  "Other".
- Don't ask what the code or files answer: check them yourself, briefly.

## Scope

- Work through the open decisions most consequential first.
- A decision with an obvious default is not a question: take the default and list it as assumed in the summary.
- Stop after about 12 questions, or sooner when what is left is minor, and offer to go on.

## Flow

1. After each batch of answers, write the decisions down (plan or file) in one editing pass, not one per answer.
2. Acknowledge in one line, then ask the next batch.
3. Finish with a short summary: the decisions, and the defaults you assumed.
