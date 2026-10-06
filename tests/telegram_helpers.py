"""Synthetic Telegram text updates shared by queue, runtime, and launcher tests."""


def update(uid=1, text="hello", date=100000, *, owner_id=42):
    return {
        "update_id": uid,
        "message": {
            "message_id": uid,
            "date": date,
            "chat": {"id": owner_id, "type": "private"},
            "from": {"id": owner_id, "is_bot": False},
            "text": text,
        },
    }
