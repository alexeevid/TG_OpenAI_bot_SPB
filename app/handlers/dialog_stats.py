from aiogram import Router, types
from aiogram.filters import Command
from app.services.stats_service import get_user_stats

router = Router()

@router.message(Command("dialog_stats"))
async def handle_dialog_stats(message: types.Message):
    stats = get_user_stats(message.from_user.id)
    await message.answer(stats)
