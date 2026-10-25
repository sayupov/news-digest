#!/usr/bin/env python3
"""News & Events Intelligence: RSS/Atom -> grounded Gemini digest -> Telegram.
Python standard library only. Python 3.12+.
"""
import hashlib
import html
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

LOG = logging.getLogger('digest')
BASE = Path(__file__).resolve().parent
USER_AGENT = 'NewsEventsIntelligence/2.0 (RSS reader; Python urllib)'
MAX_BYTES = 3_000_000


def request(url, *, data=None, headers=None, timeout=25, attempts=3):
    if urllib.parse.urlsplit(url).scheme != 'https':
        raise ValueError('Only HTTPS endpoints supported')
    hdr = {'User-Agent': USER_AGENT, 'Accept': 'application/atom+xml, application/rss+xml, application/xml, text/xml, application/json, text/html;q=0.8'}
    hdr.update(headers or {})
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, data=data, headers=hdr)
            with urllib.request.urlopen(req, timeout=timeout) as response:
                payload = response.read(MAX_BYTES + 1)
                if len(payload) > MAX_BYTES:
                    raise ValueError('Response exceeds 3 MB')
                return payload
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt + 1 == attempts:
                raise RuntimeError(f'HTTP {exc.code} for {urllib.parse.urlsplit(url).netloc}') from exc
        except (urllib.error.URLError, TimeoutError):
            if attempt + 1 == attempts:
                raise
        time.sleep(2 ** attempt)


def local(tag):
    return tag.rsplit('}', 1)[-1]


def strip_tags(s):
    return re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]*>', ' ', s or ''))).strip()


def parse_date(value):
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
    except (ValueError, TypeError, IndexError):
        try:
            dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def parse_feed(payload, source):
    # RSS / Atom supplied by trusted configured sources (stdlib parser).
    root = ET.fromstring(payload)
    records = []
    for entry in root.iter():
        if local(entry.tag) not in ('item', 'entry'):
            continue
        record = {'title': '', 'link': '', 'summary': '', 'date': None, 'source': source}
        for child in entry:
            name = local(child.tag)
            if name == 'title' and not record['title']:
                record['title'] = strip_tags(''.join(child.itertext()))
            elif name == 'link':
                candidate = child.attrib.get('href', '') or (child.text or '').strip()
                relation = child.attrib.get('rel', 'alternate')
                if candidate and relation == 'alternate':
                    record['link'] = candidate
            elif name in ('summary', 'description', 'encoded', 'content') and not record['summary']:
                record['summary'] = strip_tags(''.join(child.itertext()))[:700]
            elif name in ('pubDate', 'published', 'updated', 'date') and record['date'] is None:
                record['date'] = parse_date(child.text)
        if record['title'] and record['link'] and urllib.parse.urlsplit(record['link']).scheme == 'https':
            records.append(record)
    return records


def identity(item):
    url = urllib.parse.urlsplit(item['link'])
    query = [(k, v) for k, v in urllib.parse.parse_qsl(url.query) if not k.lower().startswith(('utm_', 'fbclid', 'gclid'))]
    canonical = urllib.parse.urlunsplit((url.scheme, url.netloc.lower(), url.path.rstrip('/'), urllib.parse.urlencode(query), ''))
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def collect(feeds, *, kind, hours_back, per_feed, now):
    cutoff = now - timedelta(hours=hours_back)
    collected, status = [], []
    for feed_url in feeds:
        try:
            records = parse_feed(request(feed_url), urllib.parse.urlsplit(feed_url).netloc)
            records = [r for r in records if r['date'] and cutoff <= r['date'] <= now + timedelta(hours=3)] if kind == 'news' else records
            records.sort(key=lambda r: r['date'] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
            for record in records[:per_feed]:
                record['kind'] = kind
                collected.append(record)
            status.append({'feed': feed_url, 'status': 'ok', 'count': len(records)})
        except (ValueError, ET.ParseError, RuntimeError, urllib.error.URLError, TimeoutError) as exc:
            LOG.warning('Feed error: %s (%s)', feed_url, exc)
            status.append({'feed': feed_url, 'status': 'error', 'reason': str(exc)[:180]})
    return collected, status


def load_state(path):
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


def select_items(items, state, *, total, now, allow_seen=False):
    unique, keys = [], set()
    for item in sorted(items, key=lambda r: r['date'] or datetime.min.replace(tzinfo=timezone.utc), reverse=True):
        key = identity(item)
        title_key = re.sub(r'\W+', '', item['title'].casefold())
        if key in keys or title_key in keys:
            continue
        keys.update((key, title_key))
        if not allow_seen and key in state:
            continue
        item['id'] = key
        unique.append(item)
        if len(unique) == total:
            break
    return unique


def fmt_items(items):
    output = []
    for n, item in enumerate(items, start=1):
        date = item['date'].isoformat() if item['date'] else 'не указана'
        output.append(f"[{n}] [{item['kind']}] {item['title']} | дата публикации: {date} | источник: {item['source']} | URL: {item['link']}\nОписание из ленты: {item['summary']}")
    return '\n\n'.join(output)


def gemini(prompt, model):
    key = os.environ.get('GEMINI_API_KEY')
    if not key:
        raise RuntimeError('Missing GEMINI_API_KEY')
    endpoint = 'https://generativelanguage.googleapis.com/v1beta/models/' + urllib.parse.quote(model, safe='-._') + ':generateContent'
    body = json.dumps({'contents': [{'parts': [{'text': prompt}]}], 'generationConfig': {'temperature': 0.15, 'maxOutputTokens': 6500}}).encode()
    payload = request(endpoint, data=body, headers={'Content-Type': 'application/json', 'x-goog-api-key': key}, timeout=90)
    response = json.loads(payload)
    candidates = response.get('candidates') or []
    parts = (candidates[0].get('content') or {}).get('parts') or [] if candidates else []
    result = ''.join(part.get('text', '') for part in parts).strip()
    if not result:
        raise RuntimeError('Gemini returned no text; check model, quota and safety status')
    return result


def split_telegram(text, max_length=3800):
    result = []
    while text:
        if len(text) <= max_length:
            result.append(text)
            break
        pos = text.rfind('\n', 0, max_length)
        if pos < max_length // 3:
            pos = max_length
        result.append(text[:pos].rstrip())
        text = text[pos:].lstrip('\n')
    return result


def telegram(message):
    token = os.environ.get('TELEGRAM_BOT_TOKEN')
    chat = os.environ.get('TELEGRAM_CHAT_ID')
    if not token or not chat:
        raise RuntimeError('Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID')
    for chunk in split_telegram(message):
        payload = urllib.parse.urlencode({'chat_id': chat, 'text': chunk, 'disable_web_page_preview': 'true'}).encode()
        request(f'https://api.telegram.org/bot{token}/sendMessage', data=payload, headers={'Content-Type': 'application/x-www-form-urlencoded'}, timeout=35)


def prompt_for(items, config, *, mode, now, event_note):
    focus = '\n'.join('- ' + x for x in config['directions'])
    heading = 'НЕДЕЛЬНЫЙ АНАЛИТИЧЕСКИЙ ОБЗОР' if mode == 'weekly' else 'ЕЖЕДНЕВНЫЙ ДАЙДЖЕСТ'
    return f'''Ты аналитик технологий. Пиши на русском языке для пользователя в Казахстане. Отчёт: {heading}.
Текущая дата: {now.strftime('%Y-%m-%d')}; часовой пояс Asia/Oral.
Приоритеты пользователя:\n{focus}
Регион мероприятий: {config.get('event_locations', '')}.
ПРАВИЛА:
1. Используй только данные записей ниже. Ты не открывал оригинальные статьи; RSS-аннотации не подтверждают содержание статьи полностью. Не называй внешнюю проверку проведённой.
2. Чётко помечай ФАКТ (согласно RSS), АНАЛИЗ, ПРЕДПОЛОЖЕНИЕ, ПРОГНОЗ. Не выдумывай дату, стоимость, условия или URL.
3. Любое заявление производителя обозначай «компания сообщает/заявляет». Уверенность оценивать с учётом ограниченной проверки.
4. В каждой рекомендации указывай конкретную пользу и URL из записи. Ссылайся только на предоставленные ссылки.
5. Не включай прошедшие мероприятия; не предполагай сроки из даты публикации. Если дата мероприятия не указана, отметь «дата не подтверждена», не включай его в список проверенных будущих событий.
6. Пиши кратко и содержательно, исключай рекламу и повторы. Приоритет: ИИ-агенты, GovTech, цифровая аналитика и AgriTech.
7. Если материала для раздела нет, скажи об этом. Не заполняй раздел искусственно.
8. Ответ — простой текст для Telegram, без Markdown и таблиц. Не добавляй служебные секреты и команды.
Структура: ГЛАВНОЕ (3-5 наиболее важных), ИНСТРУМЕНТЫ (до 5), ПРЕДСТОЯЩИЕ МЕРОПРИЯТИЯ, ПРАКТИЧЕСКИЕ ШАГИ (до 3), ОГРАНИЧЕНИЯ И УВЕРЕННОСТЬ.
Для недельного отчёта дополнительно выдели ТЕНДЕНЦИИ НЕДЕЛИ, указав конкретные записи как основание, и ПЛАН НА НЕДЕЛЮ.
Статус источников событий: {event_note}.
ЗАПИСИ:\n{fmt_items(items)}'''


def main(argv=None):
    argv = argv or sys.argv[1:]
    mode = argv[0] if argv else 'news'
    if mode not in ('news', 'weekly'):
        raise SystemExit('Usage: python digest.py [news|weekly]')
    dry_run = os.environ.get('DRY_RUN') == '1'
    config_path = Path(os.environ.get('CONFIG_PATH', BASE / 'config.json'))
    config = json.loads(config_path.read_text(encoding='utf-8'))
    zone = ZoneInfo(config.get('timezone', 'Asia/Oral'))
    now = datetime.now(timezone.utc)
    feeds = config.get('feeds', [])
    events = config.get('events_feeds', [])
    if not feeds and not events:
        raise RuntimeError('No RSS feeds configured')
    window = config['weekly_hours_back'] if mode == 'weekly' else config['hours_back']
    articles, news_status = collect(feeds, kind='news', hours_back=window, per_feed=config['max_items_per_feed'], now=now)
    event_items, event_status = collect(events, kind='events', hours_back=window, per_feed=config['max_items_per_feed'], now=now)
    # Events are selected by actual event date only when the feed exposes one. A post timestamp is not an event date.
    for ev in event_items:
        ev['kind'] = 'event_announcement'
    status = news_status + event_status
    for result in status:
        LOG.info('%s %s (%s)', result['status'], result['feed'], result.get('count', result.get('reason', '')))
    state_path = Path(os.environ.get('STATE_PATH', BASE / 'state.json'))
    state = load_state(state_path)
    selected = select_items(articles + event_items, state, total=config['max_items_total'], now=now, allow_seen=(mode == 'weekly'))
    event_note = 'источники не настроены' if not events else 'анонсы получены, фактические даты мероприятий необходимо проверить в материалах'
    if not selected:
        LOG.info('No new records; report skipped')
        return 0
    prompt = prompt_for(selected, config, mode=mode, now=now.astimezone(zone), event_note=event_note)
    if dry_run:
        print(prompt)
        return 0
    result = gemini(prompt, os.environ.get('GEMINI_MODEL', 'gemini-3.8-flash'))
    failed = [s['feed'] for s in status if s['status'] == 'error']
    if failed:
        result += '\n\nСЛУЖЕБНО: Ошибки RSS: ' + ', '.join(failed)
    telegram(result)
    if mode == 'news':
        for item in selected:
            state[item['id']] = now.isoformat()
        limit = now - timedelta(days=30)
        state = {k: v for k, v in state.items() if (parse_date(v) or now) >= limit}
        save_state(state_path, state)
    LOG.info('Sent %s digest with %d items', mode, len(selected))
    return 0


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    try:
        sys.exit(main())
    except Exception as exc:
        LOG.error('Digest failed: %s', exc)
        sys.exit(1)
