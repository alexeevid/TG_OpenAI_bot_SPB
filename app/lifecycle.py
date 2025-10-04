async def on_startup(app):  # type: ignore
    # На всякий случай снимаем вебхук и чистим очередь апдейтов,
    # чтобы не ловить Conflict с другим инстансом / прошлой сессией.
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        # не падаем на старте, даже если вебхук уже снят
        pass

async def on_shutdown(app):  # type: ignore
    pass
