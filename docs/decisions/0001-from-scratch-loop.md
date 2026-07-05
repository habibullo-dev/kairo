# ADR-0001: Build the agent loop from scratch on the raw Messages API

- **Status:** Accepted
- **Date:** 2026-07-06
- **Context phase:** Milestone 1 (MVP)

## Context

Jarvis needs an agent loop: a cycle where the model calls tools, the harness
executes them, and results feed back until the model is done. Mature options exist
to get this for free — the Claude Agent SDK, LangChain/LangGraph, and others provide
the loop, tool plumbing, and context management out of the box.

The project has two goals, and they rank in this order: **(1) learn agent
engineering deeply; (2) produce a genuinely useful assistant.** API cost is not a
constraint (company-provided), so "cheaper/faster to build" is not a strong pull.

## Decision

Build the agent loop, tool registry, permission gate, memory, and observability
**from scratch on the raw Anthropic Messages API**, using the `anthropic` SDK only
as a thin transport (streaming, retries, typed usage). No agent framework.

One deliberate seam preserves the option to adopt more later: the loop depends on an
`LLMClient` **interface**, not the SDK, and tools are plain classes behind a
registry. MCP or a higher-level SDK can be adapted in behind those boundaries
without rewriting the loop.

## Rationale

- **The loop is the learning payload.** The subtle, load-bearing rules — one
  `tool_result` per `tool_use` id, errors/denials returned to the model as results,
  verbatim replay of assistant (incl. thinking) blocks, result truncation, the
  max-iteration guard — are exactly what a framework hides. Implementing them once,
  with tests that pin each, is the point.
- **Safety must be legible.** "Every side effect passes through a permission gate
  and lands in an audit log" is a property we can only *guarantee* if we own the
  path from model output to side effect. A framework's tool runner would blur that
  boundary.
- **Extensibility on our terms.** Adding a tool is one file + one policy line;
  adding an interface is a new event consumer. We designed those seams; we
  understand their costs.
- **Cost isn't the deciding factor.** Since spend is not a constraint, the usual
  "framework saves money/time" argument carries little weight against the learning
  and control benefits.

## Consequences

- **Upside:** Full understanding and control of the loop and safety model; a clean
  interface seam (`LLMClient`) that made going from a fake client to live streaming
  a zero-loop-change swap; a test suite that runs the entire loop with no network.
- **Cost:** We maintain plumbing a framework would provide — retries (delegated to
  the SDK), streaming event handling, content-block serialization, context
  management (compaction lands in Phase 2). More code to own.
- **Revisit if:** we need many third-party tool integrations fast (adopt **MCP**
  behind the tool registry) or server-managed stateful agents with hosted tool
  execution (consider **Managed Agents**). Neither changes this decision for the
  MVP; both fit behind the interfaces we already have.
