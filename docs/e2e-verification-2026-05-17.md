# Polymath E2E Verification Log - 2026-05-17

Scope: Utility model setup/test, Web-toggle tool-capable routing, native web tool call, focused backend regressions, frontend build/audit.

## Test Queries

- Web off: `E2E routing test. Web is off. Reply with exactly: LOCAL_ROUTE_OK`
- Web on: `Use the web_search tool exactly once for this E2E routing test. Search query: Roblox RemoteEvent security server client. Then answer in one short sentence.`
- Utility test marker: `POLYMATH_UTILITY_OK`

## Works Fully Intended

- Frontend rebuild passes with no Vite chunk warnings.
- `npm audit --audit-level=moderate` reports `found 0 vulnerabilities`.
- Focused backend suite passes in Docker: `202 passed`.
- Utility role can be configured from Settings -> Models with the GLM pool entry, selected in the Utility dropdown, saved, and tested from the UI.
- Utility test endpoint resolves the saved Utility pool entry and performs a real deterministic LiteLLM call. Browser result: `OK · 3774 ms · POLYMATH_UTILITY_OK`.
- Web-off chat does not expose or execute `web_search`; the answer returned `LOCAL_ROUTE_OK`.
- Web-on chat shows the visible `AUTO: deepseek-v4-flash` route badge before send.
- Web-on backend routing now matches the UI: logs show `kind=agentic -> deepseek/deepseek-v4-flash`, and both streamed model calls use `deepseek/deepseek-v4-flash`.
- Web-on turn executes one native `web_search` call and completes with `Web · Agentic` visible in the final UI metadata.
- Web pipeline stayed bounded and snippet-only for the test query: `candidates=15`, `fetch_attempts=0`, `final=3`, `snippet_only=True`.

## Partially Works

- Some SearXNG upstream engines emitted transient rate-limit/CAPTCHA warnings, specifically Brave `Too many request` and DuckDuckGo `CAPTCHA`. The Polymath web lane still completed through available results and did not fail the turn.
- Browser polling saw the tool row while it was still running before the final answer appeared. Backend completed normally; a later browser snapshot showed the final assistant answer and `deepseek-v4-flash` metadata.
- Test output still includes dependency warnings: five Pydantic protected-namespace warnings for fields beginning with `model_`, plus one Passlib Python `crypt` deprecation warning.

## Does Not Work

- No targeted PRD behavior remained failing after the routing fix.

## Fixes From The E2E Loop

- Fixed Web toggle UI routing badge so Web behaves like other native tool paths.
- Added a Utility model test endpoint and Settings button so the Utility role is directly verifiable from the interface.
- Fixed backend agentic routing so Web/tool turns cannot be clobbered by `overrides.model` from the chat dropdown.
- Tightened the Phase 1 backend regression test to include an explicit chat model override.
