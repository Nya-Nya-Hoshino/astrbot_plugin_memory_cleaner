import re
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star
from astrbot.api import logger

class Main(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}

    @filter.command("memory_cleaner")
    async def memory_cleaner_cmd(self, event: AstrMessageEvent):
        user_name = event.get_sender_name()
        yield event.plain_result(f"Hello, {user_name}! — 记忆清洗")

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        pass
