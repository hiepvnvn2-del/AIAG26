"""Windows-safe server runner for RPA 2.0.

Guarantees WindowsProactorEventLoopPolicy is used even with uvicorn --reload,
preventing NotImplementedError when Playwright spawns Chromium subprocesses.
"""
import sys
import asyncio

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    try:
        import uvicorn.loops.asyncio
        uvicorn.loops.asyncio.asyncio_setup = lambda use_subprocess=False: asyncio.set_event_loop_policy(
            asyncio.WindowsProactorEventLoopPolicy()
        )
    except Exception:
        pass

import uvicorn

if __name__ == "__main__":
    print("🚀 Starting RPA 2.0 Server with Windows Proactor Event Loop...")
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
    )
