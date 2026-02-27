import os
import json
from typing import Dict, Any, List, Optional
from datetime import datetime

from openai import OpenAI
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    CallbackQueryHandler,
    filters,
)

# ======================
# CONFIG
# ======================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Не знайдено TELEGRAM_BOT_TOKEN у змінних середовища.")
if not OPENAI_API_KEY:
    raise RuntimeError("Не знайдено OPENAI_API_KEY у змінних середовища.")

# Можеш змінити модель, якщо потрібно
OPENAI_MODEL = "gpt-5.2"

client = OpenAI(api_key=OPENAI_API_KEY)

# ======================
# UI
# ======================
MAIN_KB = ReplyKeyboardMarkup(
    [
        ["Сніданок 🍳", "Обід 🍲"],
        ["Вечеря 🍝", "Свято 🎉"],
        ["З холодильника 🧊", "Скинути холодильник 🗑️"],
        ["Меню на тиждень 📅", "Категорії покупок 🧺"],
        ["PDF меню 🧾", "Налаштування ⚙️"],
        ["Допомога ❓"],
    ],
    resize_keyboard=True,
)

SETTINGS_KB = ReplyKeyboardMarkup(
    [
        ["Порції", "Час (хв)"],
        ["Обмеження/алергії", "Бюджет"],
        ["Назад ⬅️"],
    ],
    resize_keyboard=True,
)

def meal_actions_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔁 Ще варіант", callback_data="regen")],
            [InlineKeyboardButton("🧩 Замінити інгредієнт", callback_data="swap")],
        ]
    )

# ======================
# PREFS STORAGE
# ======================
DEFAULT_PREFS = {
    "servings": 2,
    "max_minutes": 30,
    "restrictions": "без обмежень",
    "budget": "звичайний",
}

def _prefs_path(user_id: int) -> str:
    os.makedirs("data", exist_ok=True)
    return os.path.join("data", f"prefs_{user_id}.json")

def load_prefs(user_id: int) -> Dict[str, Any]:
    path = _prefs_path(user_id)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return DEFAULT_PREFS.copy()

def save_prefs(user_id: int, prefs: Dict[str, Any]) -> None:
    with open(_prefs_path(user_id), "w", encoding="utf-8") as f:
        json.dump(prefs, f, ensure_ascii=False, indent=2)

# ======================
# OPENAI helpers
# ======================
def extract_text_from_response(resp) -> str:
    # Найчастіше є output_text
    if hasattr(resp, "output_text") and isinstance(resp.output_text, str) and resp.output_text.strip():
        return resp.output_text.strip()

    # Запасний варіант: пройти по output/content
    chunks: List[str] = []
    try:
        for item in (resp.output or []):
            for c in (getattr(item, "content", None) or []):
                t = getattr(c, "text", None)
                if isinstance(t, str):
                    chunks.append(t)
    except Exception:
        pass
    return "\n".join(chunks).strip()

def safe_json_loads(text: str) -> Dict[str, Any]:
    text = (text or "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(text[start : end + 1])
        raise

MEAL_SCHEMA = {
    "name": "MealPlan",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "title": {"type": "string"},
            "meal_type": {"type": "string"},
            "servings": {"type": "integer", "minimum": 1},
            "total_time_minutes": {"type": "integer", "minimum": 1},
            "difficulty": {"type": "string", "enum": ["легко", "середньо", "складно"]},
            "ingredients": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "item": {"type": "string"},
                        "amount": {"type": "string"},
                    },
                    "required": ["item", "amount"],
                },
            },
            "steps": {"type": "array", "minItems": 1, "items": {"type": "string"}},
            "tips": {"type": "array", "items": {"type": "string"}},
            "possible_allergens": {"type": "array", "items": {"type": "string"}},
            "shopping_list": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "title",
            "meal_type",
            "servings",
            "total_time_minutes",
            "difficulty",
            "ingredients",
            "steps",
            "possible_allergens",
            "shopping_list",
        ],
    },
    "strict": True,
}

WEEK_SCHEMA = {
    "name": "WeekMenu",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "week_menu": {
                "type": "array",
                "minItems": 7,
                "maxItems": 7,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "day": {"type": "string"},
                        "breakfast": MEAL_SCHEMA["schema"],
                        "lunch": MEAL_SCHEMA["schema"],
                        "dinner": MEAL_SCHEMA["schema"],
                    },
                    "required": ["day", "breakfast", "lunch", "dinner"],
                },
            },
            "holiday_meal": MEAL_SCHEMA["schema"],
            "combined_shopping_list": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["week_menu", "holiday_meal", "combined_shopping_list"],
    },
    "strict": True,
}

SHOP_SCHEMA = {
    "name": "ShoppingByCategory",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "categories": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "name": {"type": "string"},
                        "items": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["name", "items"],
                },
            }
        },
        "required": ["categories"],
    },
    "strict": True,
}

def openai_generate_json(prompt: str, schema: Dict[str, Any]) -> Dict[str, Any]:
    resp = client.responses.create(
        model=OPENAI_MODEL,
        input=prompt,
        response_format={"type": "json_schema", "json_schema": schema},
    )
    text = extract_text_from_response(resp)
    return safe_json_loads(text)

# ======================
# PROMPTS
# ======================
def build_meal_prompt(meal_type: str, prefs: Dict[str, Any], extra: str = "", fridge_only: Optional[str] = None) -> str:
    fridge_line = ""
    if fridge_only:
        fridge_line = (
            f"\nЄ вдома (використай ПЕРШОЧЕРГОВО і максимально з цього): {fridge_only}\n"
            "Можна додати лише базові дрібниці (сіль, перець, олія, вода) і максимум 1-3 додаткових продукти.\n"
        )
    return f"""
Ти — кулінарний помічник. Згенеруй 1 варіант страви.

Тип: {meal_type}
Порції: {prefs['servings']}
Макс. час: {prefs['max_minutes']} хв
Обмеження/алергії/вподобання: {prefs['restrictions']}
Бюджет: {prefs['budget']}
Додаткові побажання: {extra or "немає"}
{fridge_line}

Вимоги:
- Прості, реальні продукти.
- Чіткі кроки рецепту.
- Інгредієнти з кількостями.
- Дай список покупок.
- Познач можливі алергени.
Відповідай СТРОГО у JSON за схемою MealPlan.
""".strip()

def build_swap_prompt(original_meal: Dict[str, Any], swap_text: str, prefs: Dict[str, Any]) -> str:
    return f"""
Ось початкова страва у JSON:
{json.dumps(original_meal, ensure_ascii=False)}

Зроби заміну(и) інгредієнтів за правилом:
{swap_text}

Правила:
- Збережи тип страви, порції і загальний стиль.
- Онови інгредієнти, кроки і список покупок.
- Урахуй обмеження/алергії: {prefs['restrictions']}
- Макс. час: {prefs['max_minutes']} хв
Відповідай СТРОГО у JSON за схемою MealPlan.
""".strip()

def build_week_prompt(prefs: Dict[str, Any], extra: str = "", fridge_only: Optional[str] = None) -> str:
    fridge_line = ""
    if fridge_only:
        fridge_line = (
            f"\nЄ вдома (використай ПЕРШОЧЕРГОВО): {fridge_only}\n"
            "Можна додати лише базові дрібниці і максимум 1-3 додаткових продукти на страву.\n"
        )
    return f"""
Згенеруй меню на 7 днів (понеділок-неділя): сніданок, обід, вечеря.
Для кожної страви дотримуйся правил як у MealPlan.

Порції: {prefs['servings']}
Макс. час (на страву): {prefs['max_minutes']} хв
Обмеження/алергії: {prefs['restrictions']}
Бюджет: {prefs['budget']}
Додаткові побажання: {extra or "немає"}
{fridge_line}

ДОДАТКОВО: згенеруй 1 окрему святкову страву (holiday_meal) у форматі MealPlan.

Сформуй combined_shopping_list — об’єднаний список покупок на весь тиждень + святкова страва
(без повторів, по можливості згрупуй логічно).

Відповідай СТРОГО у JSON за WEEK_SCHEMA.
""".strip()

def build_shop_prompt(items: List[str]) -> str:
    return f"""
Розклади список покупок по категоріях (українською).
Категорії роби практичні, напр.: Овочі/Фрукти, М'ясо/Риба, Молочне, Бакалія, Спеції/Соуси, Напої, Інше.
Прибери дублікати, виправ дрібні помилки, але НЕ вигадуй нові позиції.

Список:
{json.dumps(items, ensure_ascii=False)}

Відповідай СТРОГО у JSON за SHOP_SCHEMA.
""".strip()

# ======================
# FORMATTERS
# ======================
def format_meal(m: Dict[str, Any]) -> str:
    ing = "\n".join([f"• {x['item']} — {x['amount']}" for x in m["ingredients"]])
    steps = "\n".join([f"{i+1}) {s}" for i, s in enumerate(m["steps"])])
    tips = "\n".join([f"• {t}" for t in (m.get("tips") or [])]) or "—"
    allergens = ", ".join(m.get("possible_allergens") or []) or "—"
    shop = "\n".join([f"• {s}" for s in (m.get("shopping_list") or [])]) or "—"

    return (
        f"🍽️ *{m['title']}*\n"
        f"Тип: {m['meal_type']}\n"
        f"Порції: {m['servings']}\n"
        f"Час: {m['total_time_minutes']} хв\n"
        f"Складність: {m['difficulty']}\n\n"
        f"*Інгредієнти:*\n{ing}\n\n"
        f"*Рецепт:*\n{steps}\n\n"
        f"*Поради:*\n{tips}\n\n"
        f"*Можливі алергени:* {allergens}\n\n"
        f"*Список покупок:*\n{shop}\n"
    )

def format_shop_categories(data: Dict[str, Any]) -> str:
    out = ["🧺 *Список покупок по категоріях*\n"]
    for cat in data.get("categories", []):
        name = (cat.get("name") or "").strip()
        items = cat.get("items") or []
        if not name or not items:
            continue
        out.append(f"*{name}*")
        out.extend([f"• {x}" for x in items])
        out.append("")
    return "\n".join(out).strip()

def normalize_meal_type(button_text: str) -> Optional[str]:
    mapping = {
        "Сніданок 🍳": "сніданок",
        "Обід 🍲": "обід",
        "Вечеря 🍝": "вечеря",
        "Свято 🎉": "святкова страва",
    }
    return mapping.get(button_text)

# ======================
# PDF
# ======================
def build_week_pdf(week: Dict[str, Any], filename: str) -> str:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    from reportlab.lib.units import cm

    os.makedirs("data", exist_ok=True)
    path = os.path.join("data", filename)

    c = canvas.Canvas(path, pagesize=A4)
    width, height = A4
    x = 2 * cm
    y = height - 2 * cm

    def write_line(line: str, dy: float = 14):
        nonlocal y
        if y < 2 * cm:
            c.showPage()
            y = height - 2 * cm
        c.drawString(x, y, (line or "")[:120])
        y -= dy

    c.setTitle("Menu_Week")
    c.setFont("Helvetica-Bold", 14)
    write_line("Меню на тиждень", dy=18)
    c.setFont("Helvetica", 10)
    write_line(f"Згенеровано: {datetime.now().strftime('%Y-%m-%d %H:%M')}", dy=16)
    write_line("")

    # Меню по днях
    for day in week.get("week_menu", []):
        c.setFont("Helvetica-Bold", 12)
        write_line(f"{day.get('day','')}", dy=16)
        c.setFont("Helvetica", 10)

        for label, key in [("Сніданок", "breakfast"), ("Обід", "lunch"), ("Вечеря", "dinner")]:
            meal = day.get(key, {})
            title = meal.get("title", "—")
            mins = meal.get("total_time_minutes", "—")
            diff = meal.get("difficulty", "—")
            write_line(f"  {label}: {title} ({mins} хв, {diff})")
        write_line("")

    # Святкова страва
    holiday = week.get("holiday_meal")
    if holiday:
        c.setFont("Helvetica-Bold", 12)
        write_line("Святкова страва (окремо)", dy=16)
        c.setFont("Helvetica", 10)
        write_line(f"• {holiday.get('title','—')} ({holiday.get('total_time_minutes','—')} хв, {holiday.get('difficulty','—')})")
        write_line("")

    # Об'єднаний список покупок
    c.setFont("Helvetica-Bold", 12)
    write_line("Список покупок (об'єднаний)", dy=16)
    c.setFont("Helvetica", 10)
    for item in week.get("combined_shopping_list", []):
        write_line(f"• {item}")

    # Категоризований список покупок (якщо є)
    shop_cat = week.get("shopping_by_category")
    if shop_cat and shop_cat.get("categories"):
        write_line("")
        c.setFont("Helvetica-Bold", 12)
        write_line("Список покупок (по категоріях)", dy=16)
        c.setFont("Helvetica", 10)

        for cat in shop_cat["categories"]:
            name = (cat.get("name") or "").strip()
            items = cat.get("items") or []
            if not name or not items:
                continue
            c.setFont("Helvetica-Bold", 10)
            write_line(name, dy=14)
            c.setFont("Helvetica", 10)
            for it in items:
                write_line(f"• {it}", dy=12)
            write_line("", dy=10)

    c.save()
    return path

# ======================
# CORE ACTIONS
# ======================
async def generate_and_send_meal(update: Update, context: ContextTypes.DEFAULT_TYPE, meal_type: str, extra: str = ""):
    user_id = update.effective_user.id
    prefs = context.user_data.get("prefs") or load_prefs(user_id)
    fridge = context.user_data.get("fridge")

    await update.message.reply_text("Генерую варіант… ⏳")
    prompt = build_meal_prompt(meal_type, prefs, extra=extra, fridge_only=fridge)

    try:
        meal = openai_generate_json(prompt, MEAL_SCHEMA)
    except Exception:
        await update.message.reply_text("Не вдалося згенерувати 😕 Спробуй ще раз.", reply_markup=MAIN_KB)
        return

    context.user_data["last_meal"] = meal
    context.user_data["last_meal_context"] = {"meal_type": meal_type, "extra": extra, "fridge": fridge}

    await update.message.reply_text(format_meal(meal), parse_mode="Markdown", reply_markup=meal_actions_kb())

async def generate_week(update: Update, context: ContextTypes.DEFAULT_TYPE, extra: str = ""):
    user_id = update.effective_user.id
    prefs = context.user_data.get("prefs") or load_prefs(user_id)
    fridge = context.user_data.get("fridge")

    await update.message.reply_text("Генерую меню на тиждень… ⏳", reply_markup=MAIN_KB)
    prompt = build_week_prompt(prefs, extra=extra, fridge_only=fridge)

    try:
        week = openai_generate_json(prompt, WEEK_SCHEMA)
    except Exception:
        await update.message.reply_text("Не вдалося згенерувати тиждень 😕 Спробуй ще раз.", reply_markup=MAIN_KB)
        return

    # Категоризація покупок (кеш)
    try:
        items = week.get("combined_shopping_list") or []
        if items:
            shop_prompt = build_shop_prompt(items)
            week["shopping_by_category"] = openai_generate_json(shop_prompt, SHOP_SCHEMA)
    except Exception:
        pass

    context.user_data["last_week"] = week

    # Коротко в чат
    lines = ["📅 *Меню на тиждень (коротко)*\n"]
    for d in week["week_menu"]:
        lines.append(f"*{d['day']}*")
        lines.append(f"• Сніданок: {d['breakfast']['title']}")
        lines.append(f"• Обід: {d['lunch']['title']}")
        lines.append(f"• Вечеря: {d['dinner']['title']}\n")

    h = week["holiday_meal"]
    lines.append("🎉 *Святкова страва (окремо)*")
    lines.append(f"• {h['title']} ({h['total_time_minutes']} хв, {h['difficulty']})\n")

    await update.message.reply_text("\n".join(lines)[:3900], parse_mode="Markdown", reply_markup=MAIN_KB)

    # PDF одразу
    await send_week_pdf(update, context)

async def send_week_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    week = context.user_data.get("last_week")
    if not week:
        await update.message.reply_text("Немає тижневого меню. Спочатку натисни «Меню на тиждень 📅».", reply_markup=MAIN_KB)
        return

    filename = f"menu_week_{update.effective_user.id}_{int(datetime.now().timestamp())}.pdf"
    try:
        pdf_path = build_week_pdf(week, filename)
        await update.message.reply_document(
            document=open(pdf_path, "rb"),
            filename="menu_week.pdf",
            caption="PDF: меню на тиждень + святкова страва + покупки (і по категоріях) 🛒",
        )
    except Exception:
        await update.message.reply_text("Не вдалося створити PDF 😕", reply_markup=MAIN_KB)

# ======================
# HANDLERS
# ======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prefs = load_prefs(update.effective_user.id)
    context.user_data["prefs"] = prefs
    context.user_data["mode"] = None
    context.user_data["fridge"] = None
    context.user_data["pending_extra"] = ""
    context.user_data["last_meal"] = None
    context.user_data["last_meal_context"] = None
    context.user_data["last_week"] = None

    await update.message.reply_text(
        "Привіт! Обери тип страви 😊\n"
        "Я згенерую повний комплект: страва + інгредієнти + рецепт + час + покупки.\n"
        "Можеш написати побажання текстом (напр. «без м’яса») і потім натиснути кнопку — я врахую.",
        reply_markup=MAIN_KB,
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Що вмію:\n"
        "• Сніданок/Обід/Вечеря/Свято — повний рецепт\n"
        "• 🔁 Ще варіант — нова ідея\n"
        "• 🧩 Замінити інгредієнт — `старе -> нове`\n"
        "• З холодильника 🧊 — готую з того, що є\n"
        "• Меню на тиждень 📅 — 7 днів + 1 святкова страва + PDF\n"
        "• Категорії покупок 🧺 — групую покупки\n"
        "• PDF меню 🧾 — ще раз згенерувати PDF з останнього тижня\n\n"
        "/prefs — показати налаштування",
        reply_markup=MAIN_KB,
    )

async def prefs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prefs = context.user_data.get("prefs") or load_prefs(update.effective_user.id)
    await update.message.reply_text(
        "Твої налаштування:\n"
        f"• Порції: {prefs['servings']}\n"
        f"• Макс. час: {prefs['max_minutes']} хв\n"
        f"• Обмеження/алергії: {prefs['restrictions']}\n"
        f"• Бюджет: {prefs['budget']}\n",
        reply_markup=MAIN_KB,
    )

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    action = query.data
    user_id = query.from_user.id
    prefs = context.user_data.get("prefs") or load_prefs(user_id)

    if action == "regen":
        last_ctx = context.user_data.get("last_meal_context")
        if not last_ctx:
            await query.message.reply_text("Спочатку згенеруй страву кнопками 😊", reply_markup=MAIN_KB)
            return

        meal_type = last_ctx["meal_type"]
        extra = last_ctx.get("extra", "")
        context.user_data["fridge"] = last_ctx.get("fridge")

        await query.message.reply_text("Генерую ще варіант… ⏳")
        prompt = build_meal_prompt(meal_type, prefs, extra=extra, fridge_only=context.user_data.get("fridge"))

        try:
            new_meal = openai_generate_json(prompt, MEAL_SCHEMA)
        except Exception:
            await query.message.reply_text("Не вийшло згенерувати 😕", reply_markup=MAIN_KB)
            return

        context.user_data["last_meal"] = new_meal
        context.user_data["last_meal_context"] = {"meal_type": meal_type, "extra": extra, "fridge": context.user_data.get("fridge")}
        await query.message.reply_text(format_meal(new_meal), parse_mode="Markdown", reply_markup=meal_actions_kb())
        return

    if action == "swap":
        if not context.user_data.get("last_meal"):
            await query.message.reply_text("Спочатку згенеруй страву 😊", reply_markup=MAIN_KB)
            return

        context.user_data["mode"] = "await_swap"
        await query.message.reply_text(
            "Напиши заміну у форматі:\n"
            "`старе -> нове`\n"
            "Приклади:\n"
            "• молоко -> рослинне молоко\n"
            "• курка -> нут\n"
            "• пшениця -> безглютенове борошно",
            parse_mode="Markdown",
            reply_markup=MAIN_KB,
        )
        return

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = (update.message.text or "").strip()

    prefs = context.user_data.get("prefs") or load_prefs(user_id)
    context.user_data["prefs"] = prefs
    mode = context.user_data.get("mode")

    # ----- Menu actions -----
    if text == "Допомога ❓":
        await help_cmd(update, context)
        return

    if text == "Налаштування ⚙️":
        context.user_data["mode"] = "settings_menu"
        await update.message.reply_text("Що налаштуємо?", reply_markup=SETTINGS_KB)
        return

    if text == "Назад ⬅️":
        context.user_data["mode"] = None
        await update.message.reply_text("Повертаю в головне меню.", reply_markup=MAIN_KB)
        return

    # ----- Fridge -----
    if text == "З холодильника 🧊":
        context.user_data["mode"] = "await_fridge"
        await update.message.reply_text(
            "Напиши, що є вдома (через кому).\n"
            "Напр.: яйця, рис, помідори, сир",
            reply_markup=MAIN_KB,
        )
        return

    if text == "Скинути холодильник 🗑️":
        context.user_data["fridge"] = None
        context.user_data["mode"] = None
        await update.message.reply_text(
            "Ок ✅ Режим «з холодильника» вимкнено.",
            reply_markup=MAIN_KB,
        )
        return

    if mode == "await_fridge":
        context.user_data["fridge"] = text
        context.user_data["mode"] = None
        await update.message.reply_text(
            "Запам’ятав 👍 Тепер натисни Сніданок/Обід/Вечеря/Свято або «Меню на тиждень 📅».",
            reply_markup=MAIN_KB,
        )
        return

    # ----- Week / PDF / Categories -----
    if text == "Меню на тиждень 📅":
        extra = context.user_data.pop("pending_extra", "")
        await generate_week(update, context, extra=extra)
        return

    if text == "PDF меню 🧾":
        await send_week_pdf(update, context)
        return

    if text == "Категорії покупок 🧺":
        week = context.user_data.get("last_week")
        if not week:
            await update.message.reply_text(
                "Спочатку згенеруй «Меню на тиждень 📅», щоб був список покупок 🙂",
                reply_markup=MAIN_KB,
            )
            return

        # Якщо вже є — показуємо одразу
        if week.get("shopping_by_category"):
            await update.message.reply_text(format_shop_categories(week["shopping_by_category"]), parse_mode="Markdown", reply_markup=MAIN_KB)
            return

        items = week.get("combined_shopping_list") or []
        if not items:
            await update.message.reply_text("У мене нема combined_shopping_list 😕", reply_markup=MAIN_KB)
            return

        await update.message.reply_text("Групую список покупок… ⏳", reply_markup=MAIN_KB)
        try:
            shop_prompt = build_shop_prompt(items)
            shop_cat = openai_generate_json(shop_prompt, SHOP_SCHEMA)
            week["shopping_by_category"] = shop_cat
            context.user_data["last_week"] = week
            await update.message.reply_text(format_shop_categories(shop_cat), parse_mode="Markdown", reply_markup=MAIN_KB)
        except Exception:
            await update.message.reply_text("Не вийшло згрупувати список 😕", reply_markup=MAIN_KB)
        return

    # ----- Settings -----
    if text in ["Порції", "Час (хв)", "Обмеження/алергії", "Бюджет"]:
        context.user_data["mode"] = f"set_{text}"
        await update.message.reply_text(
            f"Введи значення для: {text}\n"
            "Приклади:\n"
            "• Порції: 2\n"
            "• Час (хв): 20\n"
            "• Обмеження/алергії: без м’яса, без молока\n"
            "• Бюджет: економ / звичайний / преміум",
            reply_markup=SETTINGS_KB,
        )
        return

    if mode == "set_Порції":
        try:
            prefs["servings"] = max(1, int(text))
            save_prefs(user_id, prefs)
            context.user_data["mode"] = "settings_menu"
            await update.message.reply_text(f"✅ Порції: {prefs['servings']}", reply_markup=SETTINGS_KB)
        except ValueError:
            await update.message.reply_text("Введи число, напр. 2", reply_markup=SETTINGS_KB)
        return

    if mode == "set_Час (хв)":
        try:
            prefs["max_minutes"] = max(5, int(text))
            save_prefs(user_id, prefs)
            context.user_data["mode"] = "settings_menu"
            await update.message.reply_text(f"✅ Макс. час: {prefs['max_minutes']} хв", reply_markup=SETTINGS_KB)
        except ValueError:
            await update.message.reply_text("Введи число, напр. 25", reply_markup=SETTINGS_KB)
        return

    if mode == "set_Обмеження/алергії":
        prefs["restrictions"] = text
        save_prefs(user_id, prefs)
        context.user_data["mode"] = "settings_menu"
        await update.message.reply_text(f"✅ Обмеження: {prefs['restrictions']}", reply_markup=SETTINGS_KB)
        return

    if mode == "set_Бюджет":
        prefs["budget"] = text
        save_prefs(user_id, prefs)
        context.user_data["mode"] = "settings_menu"
        await update.message.reply_text(f"✅ Бюджет: {prefs['budget']}", reply_markup=SETTINGS_KB)
        return

    # ----- Swap flow -----
    if mode == "await_swap":
        last_meal = context.user_data.get("last_meal")
        if not last_meal:
            context.user_data["mode"] = None
            await update.message.reply_text("Немає страви для заміни. Згенеруй спочатку 😊", reply_markup=MAIN_KB)
            return

        if "->" not in text:
            await update.message.reply_text("Формат: `старе -> нове`", parse_mode="Markdown", reply_markup=MAIN_KB)
            return

        context.user_data["mode"] = None
        await update.message.reply_text("Переробляю рецепт… ⏳", reply_markup=MAIN_KB)

        try:
            prompt = build_swap_prompt(last_meal, text, prefs)
            new_meal = openai_generate_json(prompt, MEAL_SCHEMA)
        except Exception:
            await update.message.reply_text("Не вийшло зробити заміну 😕", reply_markup=MAIN_KB)
            return

        context.user_data["last_meal"] = new_meal
        # контекст лишається той самий (для regen)
        await update.message.reply_text(format_meal(new_meal), parse_mode="Markdown", reply_markup=meal_actions_kb())
        return

    # ----- Generate meal buttons -----
    meal_type = normalize_meal_type(text)
    if meal_type:
        extra = context.user_data.pop("pending_extra", "")
        await generate_and_send_meal(update, context, meal_type, extra=extra)
        return

    # ----- Free text: save as pending_extra -----
    context.user_data["pending_extra"] = text
    await update.message.reply_text(
        "Записав побажання ✅\n"
        "Тепер натисни Сніданок/Обід/Вечеря/Свято або «Меню на тиждень 📅».",
        reply_markup=MAIN_KB,
    )

def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("prefs", prefs_cmd))

    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app.run_polling()

if __name__ == "__main__":
    main()
