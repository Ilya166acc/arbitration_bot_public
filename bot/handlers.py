import asyncio
import logging
import os
import threading
from html import escape

from aiogram.filters.command import CommandObject
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

logger = logging.getLogger(__name__)

# Максимальное время на одну проверку. asyncio.wait_for не умеет убивать
# поток с Playwright, но он гарантирует, что бот перестанет ждать и
# ответит пользователю, а не зависнет на часы.
CHECK_TIMEOUT_SECONDS = int(os.getenv("CHECK_TIMEOUT_SECONDS", "300"))

# Глобальная блокировка для проверок (защищает от параллельных запусков).
_check_lock = asyncio.Lock()

# Дополнительный флаг занятости. Нужен потому, что после тайм-аута поток
# check_all_clients продолжает работу: снимает флаг только он сам, поэтому
# новый /check не запустит второй браузер параллельно первому.
_check_busy = threading.Event()


def _check_in_thread(user_id: int):
    """Выполняет блокирующую проверку и всегда снимает флаг занятости.

    Снимает флаг именно поток-исполнитель (finally), поэтому исключение или
    отмена ожидания со стороны event loop не оставляют проверку «залоченной»
    навсегда.
    """
    try:
        return check_all_clients(user_id)
    finally:
        _check_busy.clear()


def html_value(value: object) -> str:
    return escape(str(value) if value is not None else "—")


def is_valid_inn(inn: str) -> bool:
    if not inn.isascii() or not inn.isdigit() or len(inn) not in (10, 12):
        return False
    digits = list(map(int, inn))

    def checksum(weights: tuple[int, ...]) -> int:
        return sum(digit * weight for digit, weight in zip(digits, weights)) % 11 % 10

    if len(inn) == 10:
        return digits[9] == checksum((2, 4, 10, 3, 5, 9, 4, 6, 8))
    return (
        digits[10] == checksum((7, 2, 4, 10, 3, 5, 9, 4, 6, 8))
        and digits[11] == checksum((3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8))
    )


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
            header = f"📄 <b>Часть {html_value(i)}/{html_value(total)}</b>\n\n"
        else:
            header = ""

        await message.answer(header + part, parse_mode="HTML")

        if i < total:
            await asyncio.sleep(0.5)


@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        f"👋 Здравствуйте, {html_value(message.from_user.full_name)}!\n\n"
        "Я — бот для мониторинга арбитражных дел.\n\n"
        "Что я умею:\n"
        "• /clients — показать список клиентов\n"
        "• /check — запустить проверку вручную\n"
        "• /report — прислать последний отчёт\n"
        "• /help — справка\n\n"
        "Проверки запускаются автоматически по расписанию.",
        parse_mode="HTML"
    )


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "📖 <b>Справка по командам</b>\n\n"
        "/start — приветствие\n"
        "/clients — список отслеживаемых клиентов\n"
        "/add <code>&lt;ИНН&gt; [дни]</code> — добавить клиента\n"
        "/del <code>&lt;ИНН&gt;</code> — удалить клиента\n"
        "/load — загрузить клиентов из CSV\n"
        "/check — запустить проверку сейчас\n"
        "/report — последний отчёт\n"
        "/help — эта справка",
        parse_mode="HTML"
    )


@router.message(Command("clients"))
async def cmd_clients(message: Message):
    user_id = message.from_user.id
    clients = await asyncio.to_thread(get_all_clients, user_id)
    if not clients:
        await message.answer("👥 Список клиентов пуст.\n\nДобавьте командой /add или загрузите через /load.")
        return

    text = f"👥 <b>Клиенты ({html_value(len(clients))})</b>\n\n"
    for c in clients:
        text += f"• {html_value(c['name'])} — ИНН <code>{html_value(c['inn'])}</code> (за {html_value(c['days_back'])} дн.)\n"

    await send_long_message(message, text)


@router.message(Command("load"))
async def cmd_load(message: Message):
    user_id = message.from_user.id
    await message.answer("📂 Загружаю клиентов из CSV...")
    # pandas.read_csv + пачка INSERT'ов: блокирующая работа, уводим в поток.
    result = await asyncio.to_thread(load_clients_from_csv, user_id)

    if "error" in result:
        await message.answer(f"❌ {html_value(result['error'])}", parse_mode="HTML")
        return

    text = (
        f"✅ <b>Загрузка завершена</b>\n\n"
        f"Добавлено: {html_value(result['added'])}\n"
        f"Пропущено (уже были): {html_value(result['skipped'])}\n"
        f"Всего в базе: {html_value(result['total_in_db'])}\n"
    )
    if result["errors"]:
        text += f"\n⚠️ Ошибки:\n" + "\n".join(html_value(error) for error in result["errors"])

    await message.answer(text, parse_mode="HTML")


@router.message(Command("add"))
async def cmd_add(message: Message, command: CommandObject):
    user_id = message.from_user.id
    args = (command.args or "").split()
    
    if not 1 <= len(args) <= 2:
        await message.answer(
            "📝 <b>Формат:</b> <code>/add &lt;ИНН&gt; [дней]</code>\nПример: <code>/add 7707083893 30</code>",
            parse_mode="HTML"
        )
        return

    inn = args[0].strip()
    days_back = 30
    if len(args) > 1:
        try:
            days_back = int(args[1])
        except ValueError:
            await message.answer("❌ Период должен быть числом от 1 до 365.")
            return
    if not 1 <= days_back <= 365:
        await message.answer("❌ Период должен быть от 1 до 365 дней.")
        return

    if not is_valid_inn(inn):
        await message.answer(f"❌ Неверный ИНН: <code>{html_value(inn)}</code>", parse_mode="HTML")
        return

    auto_name = f"Клиент (ИНН {inn})"

    # Одиночный INSERT в локальный SQLite занимает микросекунды —
    # ради него не стоит прыгать в поток; тяжёлый I/O вынесен отдельно.
    if add_client(user_id, inn, auto_name, days_back):
        await message.answer(f"✅ Клиент добавлен: <b>{html_value(auto_name)}</b> (за {html_value(days_back)} дн.)", parse_mode="HTML")
    else:
        await message.answer(f"ℹ️ Клиент с ИНН <code>{html_value(inn)}</code> уже есть.", parse_mode="HTML")


@router.message(Command("del"))
async def cmd_del(message: Message, command: CommandObject):
    user_id = message.from_user.id
    args = (command.args or "").split()
    if len(args) != 1:
        await message.answer("📝 <b>Формат:</b> <code>/del &lt;ИНН&gt;</code>", parse_mode="HTML")
        return

    inn = args[0].strip()
    client = await asyncio.to_thread(get_client_by_inn, user_id, inn)

    if not client:
        await message.answer(f"❌ Клиент с ИНН <code>{html_value(inn)}</code> не найден.", parse_mode="HTML")
        return

    cases_deleted = await asyncio.to_thread(delete_client_cases, user_id, inn)
    await asyncio.to_thread(delete_client, user_id, inn)

    await message.answer(
        f"🗑 Удалён клиент <b>{html_value(client['name'])}</b> (ИНН <code>{html_value(inn)}</code>)\nУдалено дел: {html_value(cases_deleted)}",
        parse_mode="HTML"
    )


@router.message(Command("check"))
async def cmd_check(message: Message):
    user_id = message.from_user.id

    # Проверка блокировки (если уже идет проверка или предыдущая ещё
    # завершается в фоне после тайм-аута)
    if _check_lock.locked() or _check_busy.is_set():
        await message.answer("⏳ Проверка уже выполняется. Пожалуйста, подождите.")
        return

    # Захватываем блокировку и запускаем проверку.
    # async with гарантирует release даже при исключении/отмене — дедлока нет.
    async with _check_lock:
        if _check_busy.is_set():
            # Пока ждали блокировку, её мог занять другой обработчик.
            await message.answer("⏳ Проверка уже выполняется. Пожалуйста, подождите.")
            return

        _check_busy.set()
        await message.answer("🔍 Запускаю проверку. Это может занять несколько минут...")

        try:
            results, errors = await asyncio.wait_for(
                asyncio.to_thread(_check_in_thread, user_id),
                timeout=CHECK_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            # Поток с Playwright может продолжать работу, поэтому _check_busy
            # остаётся выставленным до его фактического завершения.
            logger.warning(
                "⏱ Проверка не уложилась в %s с и была прервана.", CHECK_TIMEOUT_SECONDS
            )
            await message.answer(
                "⏰ Проверка прервана из-за тайм-аута "
                f"({html_value(CHECK_TIMEOUT_SECONDS)} сек.). "
                "Часть клиентов могла остаться непроверенной — попробуйте позже.",
                parse_mode="HTML",
            )
            return
        except Exception as e:
            # Поток мог вообще не запуститься — снимаем флаг, чтобы /check
            # не оказался заблокированным навсегда.
            _check_busy.clear()
            logger.exception("Критическая ошибка при запуске проверки")
            await message.answer(f"❌ Критическая ошибка при запуске: {html_value(e)}", parse_mode="HTML")
            return

        # Если во время проверки были ошибки, выводим их
        if errors:
            error_text = "⚠️ <b>Во время проверки возникли ошибки:</b>\n" + "\n".join(html_value(error) for error in errors)
            await message.answer(error_text, parse_mode="HTML")

        if not results:
            if not errors:
                await message.answer("✅ Проверка завершена. Новых дел не найдено.")
            return

        # Формируем отчет
        total_new = sum(len(cases) for cases in results.values())
        text = f"🔔 <b>Новые дела за проверку</b> (всего: {html_value(total_new)})\n\n"

        for client_name, cases in results.items():
            text += f"👤 <b>{html_value(client_name)}</b>\n"
            for case in cases:
                text += (
                    f"  • Дело № {html_value(case['case_number'])}\n"
                    f"    Суд: {html_value(case['court'])}\n"
                    f"    Истец: {html_value(case['plaintiff'])}\n"
                    f"    Ответчик: {html_value(case['defendant'])}\n"
                    f"    Поступило: {html_value(case['receipt_date'])}\n"
                    f"    🔗 {html_value(case['url'])}\n\n"
                )

        await send_long_message(message, text)


@router.message(Command("report"))
async def cmd_report(message: Message):
    user_id = message.from_user.id
    # SQLite-запросы синхронные: выносим их из event loop.
    clients = await asyncio.to_thread(get_all_clients, user_id)
    
    if not clients:
        await message.answer("👥 Список клиентов пуст. Сначала добавьте их через /add или /load.")
        return

    text = "📊 <b>Ваш текущий отчёт по делам из базы</b>\n\n"
    has_cases = False

    for client in clients:
        cases = await asyncio.to_thread(get_cases_by_client, user_id, client["inn"])
        
        if cases:
            has_cases = True
            text += f"👤 <b>{html_value(client['name'])}</b> (ИНН <code>{html_value(client['inn'])}</code>)\n"
            for case in cases:
                text += (
                    f"  • Дело № {html_value(case['case_number'])}\n"
                    f"    Суд: {html_value(case['court'])}\n"
                    f"    Поступило: {html_value(case['receipt_date'])}\n"
                    f"    🔗 {html_value(case['url'])}\n\n"
                )

    if not has_cases:
        await message.answer("В базе пока нет сохранённых дел. Запустите /check, чтобы найти их.")
        return

    await send_long_message(message, text)
