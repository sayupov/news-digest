#!/usr/bin/env python3
"""Ежедневная сводка новостей: RSS -> Gemini -> Telegram.
Только стандартная библиотека Python, ничего устанавливать не нужно.

Режимы:  python digest.py news     (по умолчанию)
         python digest.py events   (подборка мероприятий на неделю из events_feeds)
Переменные окружения:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, GEMINI_API_KEY  (секреты GitHub)
  GEMINI_MODEL   (необязательно, по умолчанию gemini-2.0-flash)
  DRY_RUN=1      (ничего не отправлять, только показать, что получилось)
"""
import json, os, sys, re, html, urllib.request, urllib.parse, urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

DRY = os.environ.get("DRY_RUN") == "1"
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
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


def fmt_items(items):
    lines = []
    for i, e in enumerate(items, 1):
        d = e["date"].strftime("%Y-%m-%d %H:%M UTC") if e["date"] else "дата неизвестна"
        lines.append(f"[{i}] {e['title']} | {e['source']} | {d} | {e['link']}\n    {e['summary']}")
    return "\n".join(lines)


NEWS_PROMPT = """Ты помощник, который готовит утреннюю сводку новостей. Язык ответа: {lang}.
Интересующие темы: {topics}.
СТРОГИЕ ПРАВИЛА:
- Используй ТОЛЬКО записи из списка ниже. Ничего не добавляй из своих знаний, не выдумывай факты, цифры, цитаты и даты.
- Если в записи нет нужной детали, не домысливай. Если сведений мало, так и напиши.
- К каждому пункту указывай источник (сайт) и ссылку из записи.
- Если разные источники сообщают разное, отметь расхождение.
- Если запись похожа на слух или сообщение без подтверждения, пометь «не подтверждено».
- Выбери 8-12 самых важных пунктов, сгруппируй по темам, по 1-2 предложения на пункт.
- В конце одной строкой напиши, о чём в этой подборке данных нет.
Дата: {today}.

ЗАПИСИ:
{items}
"""

EVENTS_PROMPT = """Ты помощник, который составляет подборку мероприятий на предстоящую неделю. Язык ответа: {lang}.
СТРОГИЕ ПРАВИЛА:
- Используй ТОЛЬКО записи ниже. Не выдумывай мероприятия, даты, время, места и цены.
- Если дата, время или место в записи не указаны, так и напиши «не указано» и дай ссылку для проверки.
- Для каждого мероприятия: название, дата и время, место, ссылка, источник.
- Отсортируй по дате. Если записей нет или они не про мероприятия, честно напиши, что подходящих данных нет.
Сегодня: {today}.

ЗАПИСИ:
{items}
"""


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "news"
    cfg = json.load(open(os.path.join(os.path.dirname(__file__), "config.json"), encoding="utf-8"))
    feeds = cfg["events_feeds"] if mode == "events" else cfg["feeds"]
    hours = cfg["events_hours_back"] if mode == "events" else cfg["hours_back"]
    if not feeds:
        print(f"В config.json нет лент для режима '{mode}'. Добавьте RSS-адреса.")
        sys.exit(1)
    items, report = collect(feeds, cfg["max_items_per_feed"], hours, cfg["max_items_total"])
    print("\n".join(report))
    bad = [r for r in report if r.startswith("НЕ РАБОТАЕТ")]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if not items:
        msg = "Сводка не сформирована: не удалось получить свежие записи.\n" + "\n".join(report)
    else:
        tpl = EVENTS_PROMPT if mode == "events" else NEWS_PROMPT
        prompt = tpl.format(lang=cfg["language"], topics=", ".join(cfg["topics"]),
                            today=today, items=fmt_items(items))
        if DRY:
            print("--- ПРОМПТ (DRY_RUN) ---\n" + prompt[:3000])
            return
        msg = gemini(prompt)
        if bad:
            msg += "\n\nСлужебно: часть лент не открылась:\n" + "\n".join(bad)
    if DRY:
        print(msg)
    else:
        telegram(msg)


if __name__ == "__main__":
    main()
