from __future__ import annotations
from sqlalchemy import text
from datetime import datetime
from typing import List, Tuple
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from sqlalchemy import select, func
from bot.settings import load_settings
from bot.db.session import get_session
from bot.db import models as M
from bot.openai_helper import chat, transcribe_audio, generate_image
from bot.knowledge_base.indexer import sync_kb
from bot.knowledge_base.retriever import retrieve_context

_settings = load_settings()

def _user_is_admin(user_id: int) -> bool:
    try:
        ids = [int(x.strip()) for x in (_settings.admin_user_ids or '').split(',') if x.strip()]
        return user_id in ids
    except:
        return False

def _ensure_user_and_dialog(tg_id:int):
    with get_session() as s:
        dbu = s.execute(select(M.User).where(M.User.tg_user_id == tg_id)).scalar_one_or_none()
        if dbu is None:
            dbu = M.User(tg_user_id=tg_id, is_admin=_user_is_admin(tg_id), is_allowed=True, lang='ru'); s.add(dbu); s.commit()
        dlg = s.execute(select(M.Dialog).where(M.Dialog.user_id==dbu.id, M.Dialog.is_deleted==False).order_by(M.Dialog.created_at.desc())).scalar_one_or_none()
        if dlg is None:
            dlg = M.Dialog(user_id=dbu.id, title=datetime.now().strftime('%Y-%m-%d | диалог'), style='expert', model=_settings.openai_model); s.add(dlg); s.commit()
        return dbu, dlg

# было: async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
import logging

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = (update.effective_chat.id if update.effective_chat else None)
    user = update.effective_user

    # создаём пользователя/диалог, но не падаем, если что-то пошло не так
    try:
        if user:
            _ensure_user_and_dialog(user.id)
    except Exception:
        logging.exception("start: _ensure_user_and_dialog failed")

    text = (
        "Привет! Я помогу искать ответы в документах из БЗ.\n"
        "Откройте /kb (кнопки внутри) или задайте вопрос.\n\n"
        "/help — список команд."
    )

    # отвечаем по-любому каналу: message, callback, либо напрямую в чат
    if update.message:
        await update.message.reply_text(text)
    elif update.callback_query:
        try:
            await update.callback_query.edit_message_text(text)
        except Exception:
            await update.callback_query.message.reply_text(text)
    elif chat_id is not None:
        await context.bot.send_message(chat_id, text)

async def help_cmd(update, context):
    await update.message.reply_text('/start /help /reset /stats\n/dialogs, /dialog <id>\n/kb, /kb_diag\n/model, /mode\n/img <prompt>\n/web <query>\n/whoami, /grant <id>, /revoke <id>')

async def whoami(update, context):
    user=update.effective_user
    with get_session() as s:
        dbu=s.execute(select(M.User).where(M.User.tg_user_id==user.id)).scalar_one_or_none()
        if not dbu: await update.message.reply_text('Не зарегистрирован'); return
        await update.message.reply_text(f'Ваш id={user.id}\nadmin={dbu.is_admin}\nallowed={dbu.is_allowed}')

async def grant(update, context):
    if not _user_is_admin(update.effective_user.id):
        await update.message.reply_text('Доступ ограничён.'); return
    if not context.args: await update.message.reply_text('Использование: /grant <tg_id>'); return
    tg_id=int(context.args[0])
    with get_session() as s:
        u=s.execute(select(M.User).where(M.User.tg_user_id==tg_id)).scalar_one_or_none()
        if not u: u=M.User(tg_user_id=tg_id, is_admin=False, is_allowed=True, lang='ru'); s.add(u)
        else: u.is_allowed=True
        s.commit()
    await update.message.reply_text(f'Пользователь {tg_id} добавлен/разрешён.')

async def revoke(update, context):
    if not _user_is_admin(update.effective_user.id):
        await update.message.reply_text('Доступ ограничён.'); return
    if not context.args: await update.message.reply_text('Использование: /revoke <tg_id>'); return
    tg_id=int(context.args[0])
    with get_session() as s:
        u=s.execute(select(M.User).where(M.User.tg_user_id==tg_id)).scalar_one_or_none()
        if u: u.is_allowed=False; s.commit()
    await update.message.reply_text(f'Пользователь {tg_id} запрещён.')

async def health(update, context):
    try:
        with get_session() as s:
            s.execute(text("SELECT 1"))
        await update.message.reply_text("OK: DB connection")
    except Exception:
        logging.exception("/health failed")
        await update.message.reply_text("FAIL: DB connection")

async def stats(update, context):
    try:
        with get_session() as s:
            dialogs = s.execute(select(func.count(M.Dialog.id))).scalar() or 0
            messages = s.execute(select(func.count(M.Message.id))).scalar() or 0
            docs = s.execute(select(func.count(M.KbDocument.id))).scalar() or 0
        await update.message.reply_text(f"Диалогов: {dialogs}\nСообщений: {messages}\nДокументов в БЗ: {docs}")
    except Exception:
        logging.exception("/stats failed")
        await update.message.reply_text("⚠ Что-то пошло не так. Попробуйте ещё раз.")

async def reset(update, context):
    context.user_data.clear()
    await update.message.reply_text('Контекст текущего диалога очищен.')

FILTERS = ['all','connected','available']

from telegram import InlineKeyboardMarkup, InlineKeyboardButton

def _kb_build_keyboard(docs, page:int, pages:int, filter_name:str, connected_ids:set[int], is_admin:bool):
    rows=[]
    for d in docs:
        checked = '☑' if d.id in connected_ids else '☐'
        label = f"{checked} {d.path.split('/')[-1]}"
        rows.append([InlineKeyboardButton(label, callback_data=f"kb:toggle:{d.id}:{page}:{filter_name}")])
    nav=[]
    if page>1:
        nav.append(InlineKeyboardButton('« Назад', callback_data=f"kb:list:{page-1}:{filter_name}"))
    nav.append(InlineKeyboardButton(f'{page}/{pages}', callback_data='kb:nop'))
    if page<pages:
        nav.append(InlineKeyboardButton('Вперёд »', callback_data=f"kb:list:{page+1}:{filter_name}"))
    if nav: rows.append(nav)
    filt = [InlineKeyboardButton(('🔵 ' if filter_name=='all' else '')+'Все', callback_data='kb:list:1:all'),
            InlineKeyboardButton(('🔵 ' if filter_name=='connected' else '')+'Подключённые', callback_data='kb:list:1:connected'),
            InlineKeyboardButton(('🔵 ' if filter_name=='available' else '')+'Доступные', callback_data='kb:list:1:available')]
    rows.append(filt)
    if is_admin:
        rows.append([InlineKeyboardButton('🔄 Синхронизация', callback_data='kb:sync')])
    rows.append([InlineKeyboardButton('📁 Статус БЗ', callback_data='kb:status')])
    return InlineKeyboardMarkup(rows)

async def kb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _kb_show(update.effective_user.id, update, context, 1, 'all', new_message=True)

async def _kb_show(tg_user_id:int, update_or_callback, context, page:int, filter_name:str, new_message:bool=False):
    dbu, dlg = _ensure_user_and_dialog(tg_user_id)
    PAGE=8
    with get_session() as s:
        conn_ids = {r[0] for r in s.execute(select(M.DialogKbLink.document_id).where(M.DialogKbLink.dialog_id==dlg.id)).all()}
        q = select(M.KbDocument).where(M.KbDocument.is_active==True)
        if filter_name=='connected':
            if conn_ids:
                q = q.where(M.KbDocument.id.in_(conn_ids))
            else:
                docs=[]
                keyboard=_kb_build_keyboard(docs, 1, 1, filter_name, conn_ids, dbu.is_admin)
                text='Нет подключённых документов.'
                return await _kb_reply(update_or_callback, keyboard, text, new_message)
        elif filter_name=='available':
            if conn_ids:
                q = q.where(M.KbDocument.id.notin_(conn_ids))
        total = len(s.execute(q).scalars().all())
        pages = max(1, (total + PAGE - 1)//PAGE)
        page = max(1, min(page, pages))
        docs = s.execute(q.order_by(M.KbDocument.path).offset((page-1)*PAGE).limit(PAGE)).scalars().all()
    kb = _kb_build_keyboard(docs, page, pages, filter_name, conn_ids, dbu.is_admin)
    text = 'Меню БЗ: выберите документы для подключения к активному диалогу.'
    await _kb_reply(update_or_callback, kb, text, new_message)

async def _kb_reply(update_or_callback, keyboard, text, new_message=False):
    if hasattr(update_or_callback, 'message') and new_message:
        await update_or_callback.message.reply_text(text, reply_markup=keyboard)
    else:
        try:
            await update_or_callback.callback_query.edit_message_text(text, reply_markup=keyboard)
        except Exception:
            await update_or_callback.callback_query.message.reply_text(text, reply_markup=keyboard)

async def kb_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    data = q.data or ''
    user_id = update.effective_user.id
    if data.startswith('kb:list:'):
        _,_,page,filter_name = data.split(':',3)
        await _kb_show(user_id, update, context, int(page), filter_name)
        return
    if data.startswith('kb:toggle:'):
        _,_,doc_id,page,filter_name = data.split(':',4)
        dbu, dlg = _ensure_user_and_dialog(user_id)
        with get_session() as s:
            link = s.execute(select(M.DialogKbLink).where(M.DialogKbLink.dialog_id==dlg.id, M.DialogKbLink.document_id==int(doc_id))).scalar_one_or_none()
            if link:
                s.delete(link); s.commit()
            else:
                s.add(M.DialogKbLink(dialog_id=dlg.id, document_id=int(doc_id))); s.commit()
        await _kb_show(user_id, update, context, int(page), filter_name)
        return
    if data=='kb:sync':
        if not _user_is_admin(user_id):
            await q.edit_message_text('Доступ ограничён.'); return
        with get_session() as s:
            res = sync_kb(s)
        await q.edit_message_text(f'Синхронизация завершена: {res}'); return
    if data=='kb:status':
        with get_session() as s:
            docs = s.execute(select(func.count(M.KbDocument.id))).scalar() or 0
            chunks = s.execute(select(func.count(M.KbChunk.id))).scalar() or 0
        await q.edit_message_text(f'Документов: {docs}\nЧанков: {chunks}')
        return
    if data=='kb:nop':
        return

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    import traceback, logging
    logging.exception("Unhandled error: %s", traceback.format_exc())
    # по возможности сообщим пользователю
    try:
        if hasattr(update, "message") and update.message:
            await update.message.reply_text("⚠ Что-то пошло не так. Попробуйте ещё раз.")
        elif hasattr(update, "callback_query") and update.callback_query:
            await update.callback_query.message.reply_text("⚠ Ошибка обработчика. Попробуйте ещё раз.")
    except Exception:
        pass

async def dialogs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    dbu, _ = _ensure_user_and_dialog(user.id)
    with get_session() as s:
        ds = s.execute(select(M.Dialog).join(M.User, M.User.id==M.Dialog.user_id).where(M.User.tg_user_id==user.id, M.Dialog.is_deleted==False).order_by(M.Dialog.created_at.desc())).scalars().all()
    rows=[]
    for d in ds:
        rows.append([InlineKeyboardButton(f'📄 {d.title or d.id}', callback_data=f'dlg:open:{d.id}'),
                     InlineKeyboardButton('✏️', callback_data=f'dlg:rename:{d.id}'),
                     InlineKeyboardButton('📤', callback_data=f'dlg:export:{d.id}'),
                     InlineKeyboardButton('🗑', callback_data=f'dlg:delete:{d.id}')])
    if not rows:
        await update.message.reply_text('Диалогов нет.')
        return
    await update.message.reply_text('Мои диалоги:', reply_markup=InlineKeyboardMarkup(rows))

async def dialog_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; await q.answer()
    data=q.data or ''
    if data.startswith('dlg:open:'):
        dlg_id=int(data.split(':')[-1])
        with get_session() as s:
            dlg=s.get(M.Dialog, dlg_id)
            if dlg:
                await q.edit_message_text(f'Открыт диалог #{dlg_id}: {dlg.title}')
        return
    if data.startswith('dlg:rename:'):
        dlg_id=int(data.split(':')[-1])
        context.user_data['rename_dialog_id']=dlg_id
        await q.edit_message_text('Введите новое название диалога:')
        return
    if data.startswith('dlg:export:'):
        dlg_id=int(data.split(':')[-1])
        with get_session() as s:
            msgs=s.execute(select(M.Message).where(M.Message.dialog_id==dlg_id).order_by(M.Message.created_at)).scalars().all()
        content = ['# Экспорт диалога\n']
        for m in msgs:
            role = 'Пользователь' if m.role=='user' else 'Бот'
            content.append(f'**{role}:**\n{m.content}\n')
        data_bytes='\n'.join(content).encode('utf-8')
        await q.message.reply_document(document=InputFile.from_bytes(data_bytes, filename=f'dialog_{dlg_id}.md'), caption='Экспорт готов')
        return
    if data.startswith('dlg:delete:'):
        dlg_id=int(data.split(':')[-1])
        with get_session() as s:
            dlg=s.get(M.Dialog, dlg_id)
            if dlg: dlg.is_deleted=True; s.commit()
        await q.edit_message_text(f'Диалог #{dlg_id} удалён')
        return

async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if 'rename_dialog_id' in context.user_data:
        dlg_id = context.user_data.pop('rename_dialog_id')
        new_title = update.message.text.strip()[:100]
        with get_session() as s:
            dlg=s.get(M.Dialog, dlg_id)
            if dlg:
                dlg.title=new_title; s.commit()
        await update.message.reply_text('Название сохранено.')
        return
    await handle_text_message(update, context)

async def voice_message(update, context):
    v=update.message.voice or update.message.audio or update.message.video_note
    if not v: return
    f=await context.bot.get_file(v.file_id)
    fp=f"/tmp/{v.file_unique_id}.ogg"; await f.download_to_drive(custom_path=fp)
    text=await transcribe_audio(fp)
    await update.message.reply_text(f'Распознано: {text}')
    await handle_text_message(update, context, override_text=text)

async def img_cmd(update, context):
    prompt=' '.join(context.args) if context.args else ''
    if not prompt: await update.message.reply_text('Использование: /img <описание>'); return
    img_bytes=await generate_image(prompt)
    await update.message.reply_photo(photo=img_bytes, caption='Готово.')

async def model_cmd(update, context):
    await update.message.reply_text(f'Текущая модель: {_settings.openai_model} (список динамический можно добавить позже)')

async def mode_cmd(update, context):
    await update.message.reply_text('Режимы: ceo | expert | pro | user (переключение для диалога — TODO)')

async def handle_text_message(update, context, override_text: str|None=None):
    user=update.effective_user
    text=override_text or (update.message and update.message.text) or ''
    if not text: return
    with get_session() as s:
        dbu=s.execute(select(M.User).where(M.User.tg_user_id==user.id)).scalar_one_or_none()
        if not dbu or (not dbu.is_allowed and not dbu.is_admin):
            await update.message.reply_text('Доступ ограничён. Обратитесь к администратору.'); return
        dlg=s.execute(select(M.Dialog).where(M.Dialog.user_id==dbu.id, M.Dialog.is_deleted==False).order_by(M.Dialog.created_at.desc())).scalar_one_or_none()
        if not dlg:
            dlg=M.Dialog(user_id=dbu.id, title=datetime.now().strftime('%Y-%m-%d | диалог'), style='expert', model=_settings.openai_model); s.add(dlg); s.commit()
        try:
            ctx_rows=await retrieve_context(s, dlg.id, text, _settings.kb_top_k)
        except Exception:
            ctx_rows=[]

        context_block=''; cites=[]
        for r in ctx_rows:
            snippet=(r['content'] or '')[:300].replace('\n',' ')
            context_block+=f'- {snippet}\n'
            cites.append(f"{r['path']}")

        sys_prompt='Ты — аккуратный ассистент. Если используется БЗ, обязательно ссылайся на источники.'
        msgs=[{'role':'system','content':sys_prompt}]
        if context_block:
            msgs.append({'role':'system','content':'Контекст из БЗ:\n'+context_block})
        msgs.append({'role':'user','content':text})

        try:
            answer=await chat(msgs, model=dlg.model or _settings.openai_model, max_tokens=800)
        except Exception:
            await update.message.reply_text('⚠ Что-то пошло не так. Попробуйте ещё раз позже.'); return

    if cites:
        answer+='\n\nИсточники: '+ '; '.join(sorted(set(cites))[:5])
    await update.message.reply_text(answer)


async def kb_diag(update, context):
    with get_session() as s:
        docs = s.execute(select(func.count(M.KbDocument.id))).scalar() or 0
        chunks = s.execute(select(func.count(M.KbChunk.id))).scalar() or 0
        links = s.execute(select(func.count(M.DialogKbLink.id))).scalar() or 0
    await update.message.reply_text(f"БЗ: документов={docs}, чанков={chunks}, связей={links}")

def build_app()->Application:
    app=Application.builder().token(_settings.telegram_bot_token).build()
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('help', help_cmd))
    app.add_handler(CommandHandler('whoami', whoami))
    app.add_handler(CommandHandler('grant', grant))
    app.add_handler(CommandHandler('revoke', revoke))
    app.add_handler(CommandHandler('stats', stats))
    app.add_handler(CommandHandler('reset', reset))
    app.add_handler(CommandHandler('kb_diag', kb_diag))
    app.add_handler(CommandHandler('kb', kb))
    app.add_handler(CallbackQueryHandler(kb_cb, pattern=r'^kb:'))
    app.add_handler(CommandHandler('dialogs', dialogs_cmd))
    app.add_handler(CallbackQueryHandler(dialog_cb, pattern=r'^dlg:'))
    app.add_handler(CommandHandler('img', img_cmd))
    app.add_handler(CommandHandler('model', model_cmd))
    app.add_handler(CommandHandler('mode', mode_cmd))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO | filters.VIDEO_NOTE, voice_message))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), text_router))
    app.add_handler(CommandHandler('health', health))
    app.add_error_handler(on_error)
    return app
