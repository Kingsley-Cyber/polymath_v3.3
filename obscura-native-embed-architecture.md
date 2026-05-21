# Obscura Native Embed: Deterministic CDP Fallback Architecture

## Design Goal
Embed Obscura as a **deterministic fallback/replacement** at the browser runtime layer so it is used automatically when Playwright (Chromium) cannot render or retrieve a page. No LLM involvement in the decision — ruled by env vars, URL patterns, and runtime failure detection.

---

## Architecture Overview

```
Browser Tool (tools/browser.py)   Search Fetch (search pipeline)
        │                                 │
        └──────────┬──────────────────────┘
                   ▼
        ┌──────────────────────────────────┐
        │    helpers/browser_runtime.py    │  ← NEW abstraction layer
        │    (Deterministic Router)        │
        │                                  │
        │  Rules:                          │
        │   1. If OBSCURA_COMMAND is set   │
        │      AND action supports Obscura │
        │      → route to Obscura CDP       │
        │   2. If Playwright fails → retry │
        │      through Obscura             │
        │   3. If screenshot or full DOM   │
        │      manipulation → Playwright   │
        │   4. For simple page fetch →     │
        │      Obscura (faster, lighter)   │
        └──────────┬──────────────────────┘
                   │
        ┌──────────┴──────────────────────┐
        │         CDP Gateway             │
        │                                  │
        ├── Playwright (primary)           │
        │   - Full CDP surface              │
        │   - Screenshots, form, auth       │
        │   - Multi-tab management          │
        │                                  │
        └── Obscura (secondary/fallback)   │
            - Fast page fetch               │
            - JS rendering (V8)             │
            - Stealth/anti-detection        │
            - Cloudflare bypass             │
            - MCP adapter available         │
```

---

## Implementation

### 1. Environment-Based Mode Switch

```python
# helpers/browser_config.py (new or add to existing config)
import os

BROWSER_MODE_OBSCURA = "obscura"
BROWSER_MODE_PLAYWRIGHT = "playwright"
BROWSER_MODE_AUTO = "auto"

# Determined by env var, NOT by LLM
def get_browser_mode():
    """Return one of: 'playwright', 'obscura', 'auto'.
    Controlled by env var POLYMATH_BROWSER_MODE."""
    mode = os.environ.get("POLYMATH_BROWSER_MODE", "auto").strip().lower()
    if mode in (BROWSER_MODE_OBSCURA, BROWSER_MODE_PLAYWRIGHT, BROWSER_MODE_AUTO):
        return mode
    return BROWSER_MODE_AUTO  # default

def get_obscura_command():
    """Path to Obscura binary. Must be set to enable Obscura."""
    return os.environ.get("OBSCURA_COMMAND", "")

def is_obscura_available():
    return bool(get_obscura_command()) and os.path.exists(get_obscura_command())
```

### 2. Deterministic Action Routing

```python
# helpers/browser_routing.py (new)

# Actions that ONLY Playwright can do
PLAYWRIGHT_ONLY_ACTIONS = {
    "screenshot",       # Obscura explicitly lacks this
    "screenshot_file",
    "upload_file",      # Requires real DOM file input
    "clipboard",        # Requires system clipboard
    "drag",             # Complex mouse event sequences
    "hover",            # Requires hover state rendering
    "double_click",     # Requires event propagation
    "right_click",      # Context menu trigger
}

# Actions that Obscura CAN do (via CDP)
OBSCURA_CAPABLE_ACTIONS = {
    "open",             # Navigate to URL
    "navigate",         # Navigate to URL
    "content",          # Extract page content (text/html)
    "evaluate",         # JS evaluation via V8
    "click",            # Basic click via CDP Input
    "type",             # Type text via CDP Input
    "type_submit",
    "scroll",
    "back",
    "forward",
    "reload",
    "state",            # Get page state
    "detail",           # Get element detail
}

# Actions that BOTH can do — preference based on mode
COMMON_ACTIONS = {
    "open", "navigate", "content", "evaluate",
    "click", "type", "scroll", "back", "forward", "reload", "state"
}

def route_to_playwright(action: str) -> bool:
    """Deterministic decision: should this action use Playwright?"""
    mode = get_browser_mode()
    
    if action in PLAYWRIGHT_ONLY_ACTIONS:
        return True  # Must use Playwright
    
    if mode == BROWSER_MODE_PLAYWRIGHT:
        return True  # User forced Playwright
    
    if mode == BROWSER_MODE_OBSCURA:
        return False  # User forced Obscura
    
    # AUTO mode: use Playwright as primary
    return True

def route_to_obscura(action: str) -> bool:
    """Should we try Obscura for this action?"""
    if not is_obscura_available():
        return False
    if action in PLAYWRIGHT_ONLY_ACTIONS:
        return False
    return True
```

### 3. Runtime Proxy (The Core Change)

This is the file that replaces or wraps `helpers/playwright.py` in the browser plugin:

```python
# plugins/_browser/helpers/cdp_proxy.py (new, or modify runtime.py)

"""
CDP Proxy: Unified interface over Playwright and Obscura.
Called by the browser tool instead of calling Playwright directly.
"""

import asyncio
import subprocess
import os
import json
from typing import Any, Optional

from . import config
from . import routing  # our new routing module


class CdpProxy:
    """
    Manages both Playwright and Obscura backends.
    Routes calls based on deterministic rules + fallback on failure.
    """
    
    def __init__(self):
        self._playwright_runtime = None
        self._obscura_process: Optional[subprocess.Popen] = None
        self._obscura_port = 9222
        
    async def start(self):
        """Start both backends if configured."""
        # Always start Playwright (primary)
        from .playwright import PlaywrightRuntime
        self._playwright_runtime = PlaywrightRuntime()
        await self._playwright_runtime.start()
        
        # Start Obscura if available
        if routing.is_obscura_available():
            await self._start_obscura()
    
    async def _start_obscura(self):
        """Start Obscura CDP server as subprocess."""
        obs_cmd = routing.get_obscura_command()
        if not obs_cmd:
            return
        self._obscura_process = await asyncio.create_subprocess_exec(
            obs_cmd, "serve", "--port", str(self._obscura_port),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        # Wait for CDP server to be ready
        await asyncio.sleep(0.5)
    
    async def call(self, action: str, *args, **kwargs) -> Any:
        """
        Unified call with deterministic routing + fallback.
        """
        primary_runtime = self._playwright_runtime
        
        if routing.route_to_playwright(action):
            # Try Playwright first
            try:
                return await primary_runtime.call(action, *args, **kwargs)
            except Exception as e:
                # Playwright failed — check if we should fall back
                error_str = str(e).lower()
                is_bot_block = any(
                    keyword in error_str 
                    for keyword in ["blocked", "captcha", "403", "cloudflare", 
                                    "timeout", "crash", "connection refused"]
                )
                if is_bot_block and routing.route_to_obscura(action):
                    # Fallback to Obscura
                    return await self._obscura_call(action, *args, **kwargs)
                raise  # Not recoverable
        
        elif routing.route_to_obscura(action):
            # Direct to Obscura for page fetch tasks
            return await self._obscura_call(action, *args, **kwargs)
        
        else:
            raise RuntimeError(f"No backend available for action: {action}")
    
    async def _obscura_call(self, action: str, *args, **kwargs) -> Any:
        """Call Obscura via its CDP server."""
        if action == "open":
            url = args[0] if args else kwargs.get("url", "")
            return await self._obscura_navigate(url)
        elif action == "content":
            # Use Obscura's fetch --dump text
            return await self._obscura_fetch_text(kwargs.get("url", ""))
        elif action == "evaluate":
            script = args[0] if args else kwargs.get("script", "")
            return await self._obscura_evaluate(script)
        # ... more action mappings
    
    async def _obscura_navigate(self, url: str) -> dict:
        """Use Obscura's CDP to navigate and return result."""
        # Via Obscura CDP /json/new endpoint
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"http://127.0.0.1:{self._obscura_port}/json/new",
                params={"url": url},
                timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                return await resp.json()
    
    async def _obscura_fetch_text(self, url: str) -> str:
        """Fetch page text content via Obscura CLI for bulk extraction."""
        obs_cmd = routing.get_obscura_command()
        proc = await asyncio.create_subprocess_exec(
            obs_cmd, "fetch", url, "--dump", "text", "--quiet",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            timeout=30,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"Obscura fetch failed: {stderr.decode()}")
        return stdout.decode()
```

### 4. Integration Point: Modify the Browser Tool

The existing `tools/browser.py` needs one change: instead of calling `get_runtime()` which returns a Playwright-only runtime, it calls the new `CdpProxy`:

```python
# In tools/browser.py, change:

from plugins._browser.helpers.cdp_proxy import CdpProxy

class Browser(Tool):
    async def execute(self, ...):
        # Before: runtime = await get_runtime(self.agent.context.id, agent=self.agent)
        # After:
        runtime = await CdpProxy.get_instance()  # Singleton
        
        result = await runtime.call(action, browser_id, url, ref, ...)
```

### 5. Integration Point: Web Search Page Fetch

For your deterministic pipeline's page fetch step (step 4c in `polymath-rag-web-architecture.md`), add a direct Obscura call:

```python
# In search/page_fetcher.py (proposed file)

async def fetch_page(url: str) -> str:
    """Fetch full page content.
    Uses Obscura if available, falls back to aiohttp if not.
    """
    obs_cmd = os.environ.get("OBSCURA_COMMAND")
    if obs_cmd and os.path.exists(obs_cmd):
        return await obscura_fetch(obs_cmd, url)
    return await http_fetch(url)

def should_fetch_full_pages() -> bool:
    """Deterministic: check env/config, not LLM."""
    return os.environ.get("FETCH_FULL_PAGES", "false").lower() == "true"
```

---

## Configuration

```yaml
# settings.yaml (for Polymath)
browser:
  mode: auto                       # "playwright" | "obscura" | "auto"
  fallback_on_failure: true         # If Playwright fails, try Obscura
  
obscura:
  command: /a0/usr/workdir/obscura-src/target/release/obscura
  port: 9222
  stealth: false                    # Enable stealth features
  timeout_ms: 30000
  max_concurrent_fetches: 10
```

```bash
# .env
OBSCURA_COMMAND=/a0/usr/workdir/obscura-src/target/release/obscura
POLYMATH_BROWSER_MODE=auto          # playwright | obscura | auto
FETCH_FULL_PAGES=true
```

---

## Deterministic Routing Rules (Summary)

| Scenario | Routes to | Why |
|----------|-----------|-----|
| `POLYMATH_BROWSER_MODE=playwright` | Playwright | User forced Playwright |
| `POLYMATH_BROWSER_MODE=obscura` | Obscura | User forced Obscura |
| `OBSCURA_COMMAND` not set | Playwright | Obscura unavailable |
| Action = `screenshot` | Playwright | Obscura can't screenshot |
| Action = `open` (page fetch) | Playwright → Obscura on failure | Auto mode: try primary, fall back |
| Action = `content` (JS-heavy page) | Playwright → Obscura on failure | Cloudflare bypass via Obscura stealth |
| Action = `click` / `type` | Playwright | Obscura has basic CDP Input but Playwright is more reliable |
| `FETCH_FULL_PAGES=true` | Obscura (bypasses browser tool entirely) | Faster, lighter for bulk fetches |
| Playwright timeout/bot block | Obscura | Automatic fallback to stealth |

---

## Build / Activation Steps

```bash
# 1. Build Obscura binary (already built at obscura-src/target/release/obscura)
cd /a0/usr/workdir/obscura-src
cargo build --release -p obscura-cli --bin obscura --no-default-features

# 2. Set environment variables
export OBSCURA_COMMAND="/a0/usr/workdir/obscura-src/target/release/obscura"
export POLYMATH_BROWSER_MODE="auto"
export FETCH_FULL_PAGES="true"

# 3. Start the agent normally — Obscura will be auto-detected and started
# as a sidecar CDP server when the browser tool is first called.
```

---

## File Changes Summary

| File | Change |
|------|--------|
| `plugins/_browser/helpers/browser_config.py` | **New** — read env vars: mode, obscura command, thresholds |
| `plugins/_browser/helpers/browser_routing.py` | **New** — deterministic route/fallback rules for each action |
| `plugins/_browser/helpers/cdp_proxy.py` | **New** — unified proxy over Playwright + Obscura; manages Obscura subprocess lifecycle |
| `plugins/_browser/tools/browser.py` | **Modify** — replace `get_runtime()` call with `CdpProxy.get_instance().call()` |
| `search/page_fetcher.py` | **New** (or add to existing) — direct Obscura call for pipeline page fetches |
| `.env` or `settings.yaml` | **Modify** — add OBSCURA_COMMAND, POLYMATH_BROWSER_MODE, FETCH_FULL_PAGES |

No changes to `agent.py`, the monologue loop, or any prompt files — this is purely a **backend substitution** at the tool/runtime layer.
