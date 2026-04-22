"""
Helper: search for Telegram public channels by keyword and show their usernames.
Use this to find channel usernames to put in TELEGRAM_CHANNELS in .env

Usage:
    python scripts/telegram_find_channels.py "5G COVID"
    python scripts/telegram_find_channels.py "conspiracy theory"
"""
import sys
import asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_SESSION_PATH
from telethon import TelegramClient
from telethon.tl.functions.contacts import SearchRequest


async def find_channels(query: str):
    client = TelegramClient(
        TELEGRAM_SESSION_PATH, int(TELEGRAM_API_ID), TELEGRAM_API_HASH
    )
    await client.start()

    print(f"\nSearching public channels for: '{query}'\n")
    result = await client(SearchRequest(q=query, limit=20))

    channels = [c for c in result.chats if hasattr(c, "username") and c.username]
    if not channels:
        print("No public channels found. Try a different keyword.")
    else:
        print(f"Found {len(channels)} channels:\n")
        print(f"  {'Username':<30} {'Members':>8}  Title")
        print(f"  {'-'*30} {'-'*8}  -----")
        for c in channels:
            members = getattr(c, "participants_count", "?")
            title = (c.title or "").encode("ascii", "replace").decode("ascii")
            print(f"  {c.username or '':<30} {str(members):>8}  {title}")

        print(f"\nTo use these channels, add to .env:")
        usernames = ",".join(c.username for c in channels[:5] if c.username)
        print(f"  TELEGRAM_CHANNELS={usernames}")

    await client.disconnect()


if __name__ == "__main__":
    query = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "news"
    asyncio.run(find_channels(query))
