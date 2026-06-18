"""
Interactive DOM debug tool — lives in the engine submodule.

Usage (from any parent project):
    python engine/debug_dom.py [--port 18800]

Flow:
  1. Auto-starts engine_service + headed browser if not already running.
  2. User positions their physical mouse anywhere in the browser.
  3. Press Enter → DOM is captured at that exact moment.
  4. Saved to <project_root>/data/dom_debug.html — ask Claude to analyze.
  5. Claude tells you where to move/click next → repeat.
  6. Ctrl+C to exit.

Why it works without closing hover menus:
  page.evaluate() reads the DOM via JavaScript with no mouse events, so
  hover menus stay open as long as the physical mouse is still there.
"""

import argparse
import asyncio
import os
import subprocess
import sys
import time

# engine root  →  D:/.../some_project/engine/
_ENGINE_ROOT  = os.path.dirname(os.path.abspath(__file__))
# project root →  D:/.../some_project/   (where data/ lives)
_PROJECT_ROOT = os.path.dirname(_ENGINE_ROOT)
_CORE_DIR     = os.path.join(_ENGINE_ROOT, "core")

sys.path.insert(0, _CORE_DIR)


def _engine_url(port: int) -> str:
    return f"http://localhost:{port}"


async def _alive(url: str) -> bool:
    try:
        import httpx
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{url}/health", timeout=3)
            return r.status_code == 200
    except Exception:
        return False


async def _wait_ready(url: str, timeout: int = 40) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if await _alive(url):
            return True
        await asyncio.sleep(1)
    return False


async def _start_engine(port: int) -> subprocess.Popen:
    # DATA_DIR must point to the *project* core (where browser_user_data/ and
    # browser_session_sandbox/ live), not the engine submodule's core.
    # This mirrors how tui/app.py sets BROWSER_ENGINE_DATA_DIR = ROOT/core.
    _project_core = os.path.join(_PROJECT_ROOT, "core")
    env = {
        **os.environ,
        "BROWSER_ENGINE_PROJECT_ROOT": _PROJECT_ROOT,
        "BROWSER_ENGINE_DATA_DIR":     _project_core,
    }
    proc = subprocess.Popen(
        [sys.executable, "-u", "engine_service.py"],
        cwd=_CORE_DIR,
        env=env,
    )
    return proc


async def _capture(url: str) -> str:
    import httpx
    async with httpx.AsyncClient() as c:
        r = await c.post(f"{url}/browser/capture_dom", timeout=30)
    r.raise_for_status()
    return r.json().get("path", os.path.join(_PROJECT_ROOT, "data", "dom_debug.html"))


async def main(port: int) -> None:
    import httpx

    url = _engine_url(port)

    print("=" * 58)
    print("  DOM Debug  —  human + Claude cooperative inspector")
    print(f"  Engine: {url}")
    print(f"  Output: {_PROJECT_ROOT}/data/dom_debug.html")
    print("=" * 58)
    print()

    # ── Start engine if needed ───────────────────────────────
    engine_proc = None
    if await _alive(url):
        print("Engine already running.")
    else:
        print("Starting engine … ", end="", flush=True)
        engine_proc = await _start_engine(port)
        if not await _wait_ready(url, 40):
            print("FAILED — start the engine manually then retry.")
            if engine_proc:
                engine_proc.kill()
            return
        print("ready.")

    # ── Open headed browser ──────────────────────────────────
    try:
        async with httpx.AsyncClient() as c:
            r = await c.post(f"{url}/engine/start", json={"headless": False}, timeout=30)
        print(f"Browser: {r.json().get('status', r.text)}")
    except Exception as e:
        print(f"Browser start note: {e}")

    import keyboard

    print()
    print("─" * 58)
    print("  HOW TO USE")
    print("  • Move mouse anywhere in the Gemini browser window")
    print("  • Press F9 (global hotkey) → DOM captured instantly")
    print("    (works from any window, menus stay open!)")
    print("  • Tell Claude: 'analyze the latest capture'")
    print("  • Follow Claude's instructions, press F9 again")
    print("  • Ctrl+C in THIS window to exit")
    print("─" * 58)
    print()
    print("Waiting for F9 …")

    count = 0
    captured = asyncio.Event()
    loop = asyncio.get_event_loop()

    def _on_f9():
        loop.call_soon_threadsafe(captured.set)

    keyboard.add_hotkey("f9", _on_f9)
    try:
        while True:
            await captured.wait()
            captured.clear()
            try:
                path = await _capture(url)
                count += 1
                print(f"  ✓  #{count} saved → {path}")
                print("     Tell Claude to analyze it.\n")
            except Exception as e:
                print(f"  ✗  Capture failed: {e}\n")
    except KeyboardInterrupt:
        print("\nExiting debug session.")
    finally:
        keyboard.remove_hotkey("f9")

    # ── Cleanup ──────────────────────────────────────────────
    if engine_proc:
        print("Stopping engine … ", end="", flush=True)
        try:
            async with httpx.AsyncClient() as c:
                await c.post(f"{url}/engine/stop", timeout=10)
        except Exception:
            pass
        engine_proc.terminate()
        print("done.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Interactive DOM debugger for the browser engine.")
    ap.add_argument("--port", type=int, default=18800, help="Engine service port (default: 18800)")
    args = ap.parse_args()
    asyncio.run(main(args.port))
