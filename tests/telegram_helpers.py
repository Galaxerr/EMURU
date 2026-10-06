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


class DeterministicTelegram:
    """The worker's execution seam without PTB, native sessions, or guard state."""

    def __init__(self, execute):
        self._execute = execute
        self.notices = []

    def identity(self):
        return 7, "emuru"

    async def wait_ready(self):
        pass

    def route(self, payload):
        return "owner"

    async def execute(self, payload, session_key):
        return await self._execute(payload)

    async def notice(self, uid, status=None):
        self.notices.append(uid)
