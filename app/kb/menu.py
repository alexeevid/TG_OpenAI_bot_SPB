from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="/start"), KeyboardButton(text="/help")],
        [KeyboardButton(text="/dialog_stats"), KeyboardButton(text="/settings")],
        [KeyboardButton(text="/rag"), KeyboardButton(text="/upload")],
    ],
    resize_keyboard=True,
    one_time_keyboard=False
)
