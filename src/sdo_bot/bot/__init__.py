from aiogram import Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from . import auth, ideas, menu


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp["throttle"] = ideas.VoteThrottle()
    # Порядок важен: в menu есть обработчик «всего остального текста»
    dp.include_routers(auth.build_router(), ideas.build_router(), menu.build_router())
    return dp


__all__ = ["build_dispatcher"]
