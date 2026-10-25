#!/usr/bin/env python3
"""Ежедневная сводка новостей: RSS -> Gemini -> Telegram.
Только стандартная библиотека Python, ничего устанавливать не нужно.

Режимы:  python digest.py news     (ежедневный отчёт, по умолчанию)
         python digest.py weekly   (расширенный еженедельный отчёт)
Переменные окружения:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, GEMINI_API_KEY  (секреты GitHub)
  GEMINI_MODEL   (необязательно, по умолчанию gemini-2.5-flash)
  DRY_RUN=1      (ничего не отправлять, только показать, что получилось)
  CONFIG_PATH    (необязательно, путь к другому config.json, для тестов)
"""
import json, os, sys, re, html, urllib.request, urllib.parse, urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

DRY = os.environ.get("DRY_RUN") == "1"
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
UA = {"User-Agent": "Mozilla/5.0 (news-digest-bot)"}


def http_get(url, timeout=25):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def strip_tags(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def parse_date(text):
    if not text:
        return None
    text = text.strip()
    try:
        d = parsedate_to_datetime(text)
    except Exception:
        try:
            d = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except Exception:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d


def local(tag):
    return tag.rsplit("}", 1)[-1]


def parse_feed(xml_bytes):
    root = ET.fromstring(xml_bytes)
    out = []
    for el in root.iter():
        if local(el.tag) not in ("item", "entry"):
            continue
        d = {"title": "", "link": "", "summary": "", "date": None}
        for ch in el:
            t = local(ch.tag)
            if t == "title":
                d["title"] = strip_tags(ch.text)
            elif t == "link":
                d["link"] = ch.attrib.get("href") or (ch.text or "").strip()
            elif t in ("description", "summary", "content") and not d["summary"]:
                d["summary"] = strip_tags(ch.text)[:400]
            elif t in ("pubDate", "published", "updated", "date") and not d["date"]:
                d["date"] = parse_date(ch.text)
        if d["title"]:
            out.append(d)
    return out


def collect(feeds, per_feed, hours_back, total):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)
    items, report = [], []
    for url in feeds:
        try:
            entries = parse_feed(http_get(url))
        except Exception as e:  # лента не открылась - идём дальше, но сообщаем
            report.append(f"НЕ РАБОТАЕТ: {url} ({type(e).__name__}: {e})")
            continue
        fresh = [e for e in entries if e["date"] is None or e["date"] >= cutoff]
        report.append(f"ok: {url} (записей: {len(entries)}, свежих: {len(fresh)})")
        host = urllib.parse.urlparse(url).netloc
        for e in fresh[:per_feed]:
            e["source"] = host
            items.append(e)
    seen, uniq = set(), []
    for e in items:
        key = re.sub(r"\W+", "", e["title"].lower())[:80]
        if key not in seen:
            seen.add(key)
            uniq.append(e)
    return uniq[:total], report


def gemini(prompt):
    key = os.environ["GEMINI_API_KEY"]
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2},
    }).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json", "x-goog-api-key": key})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:500]
        hint = ""
        if e.code in (400, 404):
            try:
                lst = json.load(urllib.request.urlopen(urllib.request.Request(
                    "https://generativelanguage.googleapis.com/v1beta/models",
                    headers={"x-goog-api-key": key}), timeout=30))
                names = [m["name"] for m in lst.get("models", [])
                         if "generateContent" in m.get("supportedGenerationMethods", [])]
                hint = "\nДоступные модели: " + ", ".join(names[:25])
            except Exception:
                pass
        raise RuntimeError(f"Gemini HTTP {e.code}: {detail}{hint}")
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception:
        raise RuntimeError(f"Неожиданный ответ Gemini: {json.dumps(data)[:500]}")


def telegram(text):
    token, chat = os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
    chunks, cur = [], ""
    for para in text.split("\n"):
        if len(cur) + len(para) + 1 > 3800:
            chunks.append(cur)
            cur = ""
        cur += para + "\n"
    if cur.strip():
        chunks.append(cur)
    for c in chunks:
        data = urllib.parse.urlencode({
            "chat_id": chat, "text": c, "disable_web_page_preview": "true"}).encode()
        urllib.request.urlopen(urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data), timeout=30)


COMMON_RULES = """Ты - персональный ИИ-аналитик по новым технологиям, ИИ, ИИ-агентам, автоматизации, цифровой трансформации и технологическим мероприятиям. Все отчёты пиши на русском языке; иностранные материалы переводи без искажения смысла.

НАПРАВЛЕНИЯ МОНИТОРИНГА:
{directions}

ЖЁСТКИЕ ПРАВИЛА ДОСТОВЕРНОСТИ:
1. Используй ТОЛЬКО записи из блока «ЗАПИСИ» ниже. Ничего не добавляй из своих знаний: никаких выдуманных фактов, цифр, дат, цитат, цен, мероприятий.
2. У тебя нет доступа к интернету и ты не можешь проверять информацию по независимым источникам. Поэтому НЕ пиши, что новость «подтверждена независимыми источниками». Если одну и ту же новость сообщают несколько записей из списка, так и напиши («сообщают N источников из списка»). Иначе помечай: «независимо не проверено».
3. Оценка достоверности (высокая / средняя / низкая) отражает только тип источника и то, что видно в записи: официальный блог компании = выше, пересказ или реклама = ниже. Заявления компаний о своих продуктах не выдавай за подтверждённые факты, пиши «компания заявляет».
4. Если в записи нет даты, цены, срока регистрации или другой детали, пиши «не указано». Не домысливай.
5. Исключай дубли и рекламу без информационной ценности. Не включай прошедшие мероприятия в перечень предстоящих.
6. Оценка приоритета X/10 - это твоя приблизительная экспертная оценка по весам: актуальность 25%, практическая применимость 30%, новизна 20%, надёжность источника 15%, соответствие интересам 10%. Пиши её как «оценка (приблизительная)».
7. Строго разделяй: ФАКТЫ (то, что сказано в записи), АНАЛИЗ (твои выводы из фактов), ПРЕДПОЛОЖЕНИЯ (гипотезы, требующие проверки), ПРОГНОЗЫ (возможные сценарии, не утверждения). Указывай неопределённости и уровень уверенности.
8. Главный принцип: не количество, а достоверность, польза и своевременность. Если данных мало, пиши короче и прямо скажи, чего не хватает. Лучше «данных нет», чем выдумка.
9. Формат: ОБЫЧНЫЙ ТЕКСТ для Telegram. Не используй markdown-символы (**, ##, |таблицы|, ```). Заголовки пиши ЗАГЛАВНЫМИ буквами. Таблицы заменяй строками вида: Название | Дата | Место | Формат | Регистрация | Ссылка.
10. К каждому пункту давай ссылку из записи (первоисточник = ссылка из записи; если это пересказ, укажи это).

Дата: {today}. Часовой пояс пользователя: {tz}.
Источники лент (если какая-то не открылась, об этом сказано в служебной строке, не выдумывай её содержимое).
"""

DAILY_FORMAT = """
ФОРМАТ ЕЖЕДНЕВНОГО ОТЧЁТА:

NEWS & EVENTS INTELLIGENCE
Дата: {today}

ГЛАВНЫЕ ТЕХНОЛОГИЧЕСКИЕ НОВОСТИ
(до 5-10 самых значимых, но не больше, чем позволяют данные. Для каждой:)
Название
Краткое содержание (факты): 2-3 предложения.
Почему важно (анализ): ...
Практическая польза: конкретный пример.
Источник: ссылка. Достоверность: высокая/средняя/низкая. Оценка (приблизительная): X/10.

НОВЫЕ ИИ-ИНСТРУМЕНТЫ
(3-5 инструментов: краткое описание, стоимость только если она указана в записи, ссылка. Если инструментов в данных нет, напиши об этом.)

ПРЕДСТОЯЩИЕ МЕРОПРИЯТИЯ
(Ищи только среди записей с пометкой [мероприятия]. Нужны: название и организатор, дата и время с часовым поясом, город или онлайн, тематика, стоимость (если указана), срок регистрации (если указан), ссылка, практическая польза. Если таких записей нет, прямо напиши: «Источники мероприятий не подключены или подходящих данных нет», и ничего не придумывай. Регион интереса: {event_locations}.)

ЧТО РЕКОМЕНДУЮ ИЗУЧИТЬ
(3 наиболее полезных материала из записей, с пояснением.)

ЧТО МОЖНО ПРИМЕНИТЬ НА ПРАКТИКЕ
(конкретные идеи для создания ИИ-агентов, анализа данных, цифровизации госуправления; помечай их как предположения.)

В КОНЦЕ:
Неопределённости и ограничения данных: 2-3 строки.
Уровень уверенности: высокий / средний / низкий.
"""

WEEKLY_FORMAT = """
ФОРМАТ ЕЖЕНЕДЕЛЬНОГО АНАЛИТИЧЕСКОГО ОТЧЁТА:

NEWS & EVENTS INTELLIGENCE - НЕДЕЛЬНЫЙ ОБЗОР
Период и дата: {today}

1. ОСНОВНЫЕ ТЕХНОЛОГИЧЕСКИЕ ТРЕНДЫ (на основании записей за неделю; отметь, на чём основан каждый вывод)
2. ВАЖНЕЙШИЕ ИЗМЕНЕНИЯ В ИИ
3. ПЕРСПЕКТИВНЫЕ ИНСТРУМЕНТЫ
4. НОВЫЕ ВОЗМОЖНОСТИ ДЛЯ АВТОМАТИЗАЦИИ
5. ПРЕДСТОЯЩИЕ МЕРОПРИЯТИЯ НА 30 ДНЕЙ (только из записей [мероприятия]; если данных нет, прямо скажи это)
6. ПРАКТИЧЕСКИЕ РЕКОМЕНДАЦИИ (помечай как анализ или предположения)
7. РИСКИ И ОГРАНИЧЕНИЯ
8. ТРИ ПРИОРИТЕТНЫХ ДЕЙСТВИЯ НА СЛЕДУЮЩУЮ НЕДЕЛЮ

В разделах разделяй факты / анализ / предположения / прогнозы. В конце: неопределённости и уровень уверенности.
"""


def fmt_items(items):
    lines = []
    for i, e in enumerate(items, 1):
        d = e["date"].strftime("%Y-%m-%d %H:%M UTC") if e["date"] else "дата не указана"
        kind = "[мероприятия]" if e.get("kind") == "events" else "[новости]"
        lines.append(f"[{i}] {kind} {e['title']} | {e['source']} | {d} | {e['link']}\n    {e['summary']}")
    return "\n".join(lines)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "news"
    if mode not in ("news", "weekly"):
        print("Режим должен быть news или weekly")
        sys.exit(1)
    cfg_path = os.environ.get("CONFIG_PATH") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    cfg = json.load(open(cfg_path, encoding="utf-8"))
    hours = cfg["weekly_hours_back"] if mode == "weekly" else cfg["hours_back"]
    if not cfg["feeds"] and not cfg["events_feeds"]:
        print("В config.json нет лент. Добавьте RSS-адреса в feeds.")
        sys.exit(1)
    items, report = [], []
    for kind, feeds in (("news", cfg["feeds"]), ("events", cfg["events_feeds"])):
        got, rep = collect(feeds, cfg["max_items_per_feed"], hours, cfg["max_items_total"])
        for e in got:
            e["kind"] = kind
        items += got
        report += rep
    items = items[: cfg["max_items_total"] + 20]
    print("\n".join(report))
    bad = [r for r in report if r.startswith("НЕ РАБОТАЕТ")]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if not items:
        msg = "Отчёт не сформирован: не удалось получить свежие записи.\n" + "\n".join(report)
    else:
        fmt = WEEKLY_FORMAT if mode == "weekly" else DAILY_FORMAT
        prompt = (COMMON_RULES + fmt).format(
            directions="\n".join("- " + d for d in cfg["directions"]),
            today=today, tz=cfg.get("timezone_note", "UTC"),
            event_locations=cfg.get("event_locations", ""))
        prompt += "\nЗАПИСИ:\n" + fmt_items(items)
        if DRY:
            print("--- ПРОМПТ (DRY_RUN) ---\n" + prompt[:6000])
            return
        msg = gemini(prompt)
        if bad:
            msg += "\n\nСлужебно: часть лент не открылась:\n" + "\n".join(bad)
        if not cfg["events_feeds"]:
            msg += "\n\nСлужебно: источники мероприятий пока не подключены."
    if DRY:
        print(msg)
    else:
        telegram(msg)


if __name__ == "__main__":
    main()
