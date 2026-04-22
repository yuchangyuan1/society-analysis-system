"""
Run this script ONCE to authenticate your Telegram account.
A session file will be saved so future runs don't need to log in again.

Usage:
    python scripts/telegram_login.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_SESSION_PATH
from telethon.sync import TelegramClient

print("=== Telegram Login ===")
print(f"Session will be saved to: {TELEGRAM_SESSION_PATH}\n")

Path(TELEGRAM_SESSION_PATH).parent.mkdir(parents=True, exist_ok=True)

client = TelegramClient(TELEGRAM_SESSION_PATH, int(TELEGRAM_API_ID), TELEGRAM_API_HASH)
client.start()

me = client.get_me()
print(f"\nLogin successful!")
print(f"  Name    : {me.first_name} {me.last_name or ''}")
print(f"  Username: @{me.username or 'none'}")
print(f"  Phone   : {me.phone}")
print(f"\nSession saved. You can now run main.py normally.")

client.disconnect()
