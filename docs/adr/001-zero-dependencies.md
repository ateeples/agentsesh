# ADR 001: Zero Required Dependencies

## Status
Accepted

## Context
AgentSesh is a developer tool that agents install and run inside arbitrary project environments. Adding runtime dependencies creates version conflicts with the host project, increases install time, and introduces supply chain risk.

## Decision
AgentSesh has zero required dependencies. All core functionality uses the Python standard library only. Optional features (MCP server) are gated behind extras (`pip install agentsesh[mcp]`).

## Consequences
- **Positive:** Installs in <2 seconds. No dependency conflicts. No supply chain exposure. Works in any Python 3.10+ environment.
- **Positive:** Forces simpler implementations — no reaching for pandas when a list comprehension works.
- **Negative:** Some features (rich terminal output, async HTTP) require more code than a library would.
- **Accepted trade-off:** The MCP server requires the `mcp` package, but it's optional and isolated.
