"""Small real OpenRouter connectivity check for a new VPS."""

import asyncio
import sys

import httpx

from .config import Config


async def main() -> int:
    config = Config.from_env()
    proxy = "" if "--direct" in sys.argv else config.openrouter_proxy_url
    async with httpx.AsyncClient(base_url="https://openrouter.ai/api/v1", proxy=proxy or None, timeout=25) as client:
        headers = {"Authorization": f"Bearer {config.openrouter_api_key}"}
        try:
            key = await client.get("/key", headers=headers)
            print(f"Key endpoint HTTP {key.status_code}")
            if key.status_code != 200:
                return 1
            result = await client.post("/chat/completions", headers=headers, json={
                "model": config.openrouter_model,
                "messages": [{"role": "user", "content": "Reply with OK."}],
                "max_tokens": 8,
            })
            print(f"Model request HTTP {result.status_code}")
            if result.status_code != 200:
                print(result.text[:300])
                return 1
            print("OpenRouter reachable through", "proxy" if proxy else "direct connection")
            return 0
        except httpx.HTTPError as exc:
            print(f"Network error: {type(exc).__name__}")
            return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
