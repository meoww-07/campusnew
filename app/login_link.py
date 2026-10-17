"""Print a one-time web login link without Telegram (local development).

    python -m app.login_link [telegram_chat_id]

Creates the user if needed. With no chat id it uses the local demo user
(the one `python -m app.seed 1000` loads). The link is valid for 15 minutes.
"""
from __future__ import annotations

import argparse

from app import repo
from app.config import get_settings
from app.db import init_db

DEMO_CHAT_ID = 1000


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("telegram_chat_id", type=int, nargs="?", default=DEMO_CHAT_ID)
    args = ap.parse_args()
    settings = get_settings()
    conn = init_db(settings.db_path)
    user = repo.get_user_by_chat_id(conn, args.telegram_chat_id) or \
        repo.upsert_user(conn, args.telegram_chat_id, "Local User")
    token = repo.create_login_token(conn, user.id)
    print(f"{settings.app_base_url}/login?token={token}")


if __name__ == "__main__":
    main()
