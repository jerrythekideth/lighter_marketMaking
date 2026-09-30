#!/usr/bin/env python3
"""Show / enable / disable "maker-only" mode for the bot's API key (Premium accounts).

    .venv/bin/python tools/maker_only.py status
    .venv/bin/python tools/maker_only.py enable
    .venv/bin/python tools/maker_only.py disable

A maker-only key can only send post-only orders, modify them, and cancel / cancel-all
(docs: API keys -> Maker-only API keys). Everything this bot does fits that, and it means a
leaked key can't be used to take liquidity or send other tx types. Caveats:
  * Lighter enforces a ONE-HOUR cooldown between changes — you can't undo it right away.
  * The setting is changed with an auth token from the key itself, so a thief holding the
    key could turn it off (after the cooldown). It raises the bar; it's not a vault.
After enabling, restart the bot: it refuses to start if it can't arm the dead man's switch,
so you'll know immediately if scheduled cancel-all is blocked for maker-only keys.
"""
import asyncio
import json
import os
import sys

import aiohttp
import lighter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.config import CONFIG  # noqa: E402


async def main(cmd: str) -> None:
    key = int(CONFIG.api_key_index)
    client = lighter.SignerClient(url=CONFIG.base_url, account_index=int(CONFIG.account_index),
                                  api_private_keys={key: CONFIG.api_key_private_key})
    try:
        token, err = client.create_auth_token_with_expiry(api_key_index=key)
        if err:
            raise SystemExit(f"auth token: {err}")
        headers = {"Authorization": token}
        async with aiohttp.ClientSession() as http:
            if cmd in ("enable", "disable"):
                keys = [key] if cmd == "enable" else []
                async with http.post(f"{CONFIG.base_url}/api/v1/setMakerOnlyApiKeys", headers=headers,
                                     data={"account_index": CONFIG.account_index,
                                           "api_key_indexes": json.dumps(keys)}) as r:
                    print(cmd, r.status, await r.text())
            async with http.get(f"{CONFIG.base_url}/api/v1/getMakerOnlyApiKeys", headers=headers,
                                params={"account_index": CONFIG.account_index}) as r:
                data = await r.json(content_type=None)
                print(f"maker-only keys: {data.get('api_key_indexes')}  (bot uses key {key})")
    finally:
        await client.close()


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("status", "enable", "disable"):
        raise SystemExit(__doc__)
    asyncio.run(main(sys.argv[1]))
