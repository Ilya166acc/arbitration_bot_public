import asyncio
from aiogram import Router
from aiogram.filters import CommandStart, Command
from aiogram.types import Message

from database.db import (
    get_all_clients, 
    add_client, 
    delete_client, 
    delete_client_cases, 
    get_client_by_inn,
    get_cases_by_client  # <--- Добавлен недостающий импорт для /report
)
from core.loader import load_clients_from_csv
from core.checker import check_all_clients

router = Router()
MAX_MESSAGE_LENGTH = 3800


def split_report(text: str, max_len: int = MAX_MESSAGE_LENGTH) -> list[str]:
    if len(text) <= max_len:
        return [text]

    parts = []
    current = ""
    for block in text.split("\n  • "):
        if current:
            candidate = current + "\n  • " + block
        else:
            candidate = block

        if len(candidate) > max_len:
            if current:
                parts.append(current)
            current = "  • " + block
        else:
            current = candidate

    if current:
        parts.append(current)

    return parts


async def send_long_message(message: Message, text: str) -> None:
    parts = split_report(text)
    total = len(parts)

    for i, part in enumerate(parts, start=1):
        if total > 1:
            header = f"📄 *Часть {i}/{total}*\n\n"
        else:
            header = ""

        try:
            await message.answer(header + part, parse_mode="Markdown")
        except Exception:
            await message.answer(header + part)

        if i < total:
            await asyncio.sleep(0.5)


@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        f"👋 Здравствуйте, {message.from_user.full_name}!\n\n"
        "Я — бот для мониторинга арбитражных дел.\n\n"
        "Что я умею:\n"
        "• /clients — показать список клиентов\n"
        "• /check — запустить проверку вручную\n"
        "• /report — прислать последний отчёт\n"
        "• /help — справка\n\n"
        "Проверки запускаются автоматически по расписанию."
    )


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "📖 *Справка по командам*\n\n"
        "/start — приветствие\n"
        "/clients — список отслеживаемых клиентов\n"
        "/add `<ИНН> [дни]` — добавить клиента\n"
        "/del `<ИНН>` — удалить клиента\n"
        "/load — загрузить клиентов из CSV\n"
        "/check — запустить проверку сейчас\n"
        "/report — последний отчёт\n"
        "/help — эта справка",
        parse_mode="Markdown"
    )


@router.message(Command("clients"))
async def cmd_clients(message: Message):
    user_id = message.from_user.id
    clients = get_all_clients(user_id)
    if not clients:
        await message.answer("👥 Список клиентов пуст.\n\nДобавьте командой /add или загрузите через /load.")
        return

    text = f"👥 *Клиенты ({len(clients)})*\n\n"
    for c in clients:
        text += f"• {c['name']} — ИНН `{c['inn']}` (за {c['days_back']} дн.)\n"

    await send_long_message(message, text)


@router.message(Command("load"))
async def cmd_load(message: Message):
    user_id = message.from_user.id
    await message.answer("📂 Загружаю клиентов из CSV...")
    result = load_clients_from_csv(user_id)

    if "error" in result:
        await message.answer(f"❌ {result['error']}")
        return

    text = (
        f"✅ *Загрузка завершена*\n\n"
        f"Добавлено: {result['added']}\n"
        f"Пропущено (уже были): {result['skipped']}\n"
        f"Всего в базе: {result['total_in_db']}\n"
    )
    if result["errors"]:
        text += f"\n⚠️ Ошибки:\n" + "\n".join(result["errors"])

    await message.answer(text, parse_mode="Markdown")


@router.message(Command("add"))
async def cmd_add(message: Message):
    user_id = message.from_user.id
    args = message.text.replace("/add", "").strip().split()
    
    if len(args) < 1:
        await message.answer(
            "📝 *Формат:* `/add <ИНН> [дней]`\nПример: `/add 7712345678 30`",
            parse_mode="Markdown"
        )
        return

    inn = args[0].strip()
    days_back = 30
    if len(args) > 1:
        if args[1].isdigit():
            days_back = int(args[1])
        else:
            await message.answer("❌ Период должен быть числом.")
            return

    if not inn.isdigit() or len(inn) not in (10, 12):
        await message.answer(f"❌ Неверный ИНН: `{inn}`", parse_mode="Markdown")
        return

    auto_name = f"Клиент (ИНН {inn})"

    if add_client(user_id, inn, auto_name, days_back):
        await message.answer(f"✅ Клиент добавлен: *{auto_name}* (за {days_back} дн.)", parse_mode="Markdown")
    else:
        await message.answer(f"ℹ️ Клиент с ИНН `{inn}` уже есть.", parse_mode="Markdown")


@router.message(Command("del"))
async def cmd_del(message: Message):
    user_id = message.from_user.id
    args = message.text.replace("/del", "").strip().split()
    if not args:
        await message.answer("📝 *Формат:* `/del <ИНН>`", parse_mode="Markdown")
        return

    inn = args[0].strip()
    client = get_client_by_inn(user_id, inn)

    if not client:
        await message.answer(f"❌ Клиент с ИНН `{inn}` не найден.", parse_mode="Markdown")
        return

    cases_deleted = delete_client_cases(user_id, inn)
    delete_client(user_id, inn)

    await message.answer(
        f"🗑 Удалён клиент *{client['name']}* (ИНН `{inn}`)\nУдалено дел: {cases_deleted}",
        parse_mode="Markdown"
    )


@router.message(Command("check"))
async def cmd_check(message: Message):
    user_id = message.from_user.id
    await message.answer("🔍 Запускаю проверку. Это может занять несколько минут...")
    
    try:
        results, errors = await asyncio.to_thread(check_all_clients, user_id)
    except Exception as e:
        await message.answer(f"❌ Критическая ошибка при запуске: {e}")
        return

    if errors:
        error_text = "⚠️ *Во время проверки возникли ошибки:*\n" + "\n".join(errors)
        await message.answer(error_text, parse_mode="Markdown")

    if not results:
        if not errors:
            await message.answer("✅ Проверка завершена. Новых дел не найдено.")
        return

    total_new = sum(len(cases) for cases in results.values())
    text = f"🔔 *Новые дела за проверку* (всего: {total_new})\n\n"

    for client_name, cases in results.items():
        text += f"👤 *{client_name}*\n"
        for case in cases:
            text += (
                f"  • Дело № {case['case_number']}\n"
                f"    Суд: {case['court']}\n"
                f"    Истец: {case['plaintiff']}\n"
                f"    Ответчик: {case['defendant']}\n"
                f"    Поступило: {case['receipt_date']}\n"
                f"    🔗 {case['url']}\n\n"
            )

    await send_long_message(message, text)


@router.message(Command("report"))
async def cmd_report(message: Message):
    user_id = message.from_user.id
    clients = get_all_clients(user_id)
    
    if not clients:
        await message.answer("👥 Список клиентов пуст. Сначала добавьте их через /add или /load.")
        return

    text = "📊 *Ваш текущий отчёт по делам из базы*\n\n"
    has_cases = False

    for client in clients:
        cases = get_cases_by_client(user_id, client["inn"])
        
        if cases:
            has_cases = True
            text += f"👤 *{client['name']}* (ИНН `{client['inn']}`)\n"
            for case in cases:
                text += (
                    f"  • Дело № {case['case_number']}\n"
                    f"    Суд: {case['court']}\n"
                    f"    Поступило: {case['receipt_date']}\n"
                    f"    🔗 {case['url']}\n\n"
                )

    if not has_cases:
        await message.answer("В базе пока нет сохранённых дел. Запустите /check, чтобы найти их.")
        return

    await send_long_message(message, text)