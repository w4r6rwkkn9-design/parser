#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Парсер объявлений ParuVendu.fr (раздел "Seconde main" / annonces).

Что делает:
  - парсит список объявлений выбранной категории (несколько страниц пагинации);
  - вытаскивает: id, заголовок, цену, город, описание, тип продавца, кол-во фото, ссылку;
  - умеет сортировать по "свежести" двумя способами:
      * быстро (по умолчанию) — по числовому id карточки (чем больше id, тем новее
        объявление; это внутренний авто-инкремент базы, а не дата — прокси, не факт);
      * точно (--fetch-dates) — заходит на страницу каждого объявления и читает
        реальное "Il y a X минут/часов/дней" рядом с объявлением. Медленнее
        (1 доп. запрос на объявление), но даёт точную свежесть.
  - сохраняет результат в CSV и/или JSON.

Структура карточки объявления и HTML верстка были сверены вживую на
https://www.paruvendu.fr/annonces/meubles/ (сентябрь 2026). Сайт может
поменять вёрстку — тогда часть селекторов в parse_card() придётся поправить,
сам механизм (requests + BeautifulSoup) на сервер-рендеренный HTML остаётся
рабочим, JS/браузер не нужен.

ВАЖНО: подтверждён вживую только раздел "seconde main" (annonces/<slug>/).
Для Insomnia/Auto-moto/Immobilier и т.п. другие разделы сайта могут иметь
другую вёрстку карточек — если нужно, скажи, добью отдельно под них.

Использование:
  python3 paruvendu_scraper.py --list-categories
  python3 paruvendu_scraper.py --category meubles --pages 2 --limit 30 --out annonces.csv
  python3 paruvendu_scraper.py --url "https://www.paruvendu.fr/annonces/meubles/" --fetch-dates
"""

import argparse
import csv
import json
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta
from typing import Optional

import requests
from bs4 import BeautifulSoup

BASE = "https://www.paruvendu.fr"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.5",
}

# Категории раздела "Seconde main" (annonces). Слаг "meubles" проверен вживую
# и гарантированно работает. Остальные — сгенерированы из официальных
# названий подкатегорий сайта (транслит + дефисы), это обычная для таких
# сайтов схема URL, но не проверялась запросом на сервер — если слаг
# окажется неверным, скрипт скажет об этом и предложит использовать --url.
CATEGORIES = {
    "meubles": "Meubles",                       # проверено вживую
    "bijoux-et-montres": "Bijoux et montres",
    "chaussures": "Chaussures",
    "maroquinerie": "Maroquinerie",
    "vetements": "Vêtements",
    "audio-et-hifi": "Audio et hifi",
    "consoles-et-jeux-videos": "Consoles et jeux vidéos",
    "materiel-informatique": "Matériel informatique",
    "photo-video-tv": "Photo / Vidéo / TV",
    "telephones-et-tablettes": "Téléphones et tablettes",
    "bricolage": "Bricolage",
    "decoration": "Décoration",
    "electromenager": "Électroménager",
    "jardin": "Jardin",
    "puericulture": "Puériculture",
    "jeux-jouets": "Jeux / Jouets",
    "mobilier-enfants": "Mobilier enfants",
    "vetements-enfants": "Vêtements enfants",
    "chaussures-enfants": "Chaussures enfants",
    "cd-et-vinyles": "CD et vinyles",
    "dvd-et-blu-ray": "DVD et blu-ray",
    "livres-et-bd": "Livres et BD",
    "objets-de-collection": "Objets de collection",
    "billetterie": "Billetterie",
    "cuisine": "Cuisine",
    "instruments-de-musique": "Instruments de musique",
    "sports": "Sports",
    "velos": "Vélos",
}


@dataclass
class Listing:
    id: str
    title: str
    price: Optional[str]
    location: Optional[str]
    description: str
    seller_type: Optional[str]
    photo_count: Optional[str]
    url: str
    category: str
    posted_relative: Optional[str] = None   # "Il y a 12 minutes" (если --fetch-dates)
    posted_minutes_ago: Optional[int] = None  # то же самое числом, для сортировки
    fetched_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))


def slugify_hint(text: str) -> str:
    """Просто для отладки/подсказок — не используется в основной логике."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return text


def build_list_url(category_or_url: str, page: int) -> str:
    """Строит URL страницы листинга. Принимает либо слаг категории
    (например 'meubles'), либо полный URL категории, скопированный из браузера."""
    if category_or_url.startswith("http"):
        base = category_or_url.rstrip("/")
        sep = "&" if "?" in base else "?"
        return f"{base}/" if page == 1 and "?" not in base else f"{base}{sep}p={page}"
    slug = category_or_url.strip("/")
    url = f"{BASE}/annonces/{slug}/"
    if page > 1:
        url += f"?p={page}"
    return url


def fetch(url: str, session: requests.Session, timeout: int = 15) -> Optional[str]:
    try:
        resp = session.get(url, headers=HEADERS, timeout=timeout)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as e:
        print(f"  [!] ошибка запроса {url}: {e}", file=sys.stderr)
        return None


def parse_price(card) -> Optional[str]:
    price_el = card.select_one("div.text-red")
    if not price_el:
        return None
    return price_el.get_text(strip=True) or None


def parse_card(card, category: str) -> Optional[Listing]:
    """Разбирает один блок div.debarras-annonce[data-id] в Listing."""
    ad_id = card.get("data-id")

    link = card.select_one("a[title][href*='/annonces/']")
    if link is None:
        link = card.select_one("a[href*='/annonces/']")
    if link is None:
        return None

    url = link.get("href", "").strip()
    title = (link.get("title") or "").strip()

    location_el = card.select_one("span.px-2.text-black")
    location = location_el.get_text(strip=True) if location_el else None

    desc_el = card.select_one("p.text-sm.text-justify")
    description = ""
    if desc_el:
        # первый span.font-semibold внутри дублирует заголовок — вырезаем его
        bold = desc_el.select_one("span.font-semibold")
        if bold:
            bold.extract()
        description = desc_el.get_text(" ", strip=True)

    seller_el = card.select_one("span.pseudoinfo")
    seller_type = seller_el.get_text(strip=True) if seller_el else None

    photo_el = card.select_one("div.text-white.absolute span.text-sm.font-bold")
    photo_count = photo_el.get_text(strip=True) if photo_el else None

    if not title:
        h = card.select_one("span.font-semibold.block")
        title = h.get_text(strip=True) if h else "(без названия)"

    return Listing(
        id=ad_id or re.sub(r"\D", "", url.rsplit("/", 1)[-1])[:10],
        title=title,
        price=parse_price(card),
        location=location,
        description=description,
        seller_type=seller_type,
        photo_count=photo_count,
        url=url,
        category=category,
    )


def parse_listing_page(html: str, category: str) -> list:
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select("div.debarras-annonce[data-id]")
    results = []
    for card in cards:
        item = parse_card(card, category)
        if item:
            results.append(item)
    return results


REL_TIME_RE = re.compile(
    r"Il y a\s+(\d+)\s*(minute|heure|jour|semaine|mois|an)s?", re.IGNORECASE
)

MINUTES_PER_UNIT = {
    "minute": 1,
    "heure": 60,
    "jour": 60 * 24,
    "semaine": 60 * 24 * 7,
    "mois": 60 * 24 * 30,
    "an": 60 * 24 * 365,
}


def fetch_relative_date(url: str, session: requests.Session) -> (Optional[str], Optional[int]):
    """Заходит на страницу объявления и вытаскивает 'Il y a N единиц'."""
    html = fetch(url, session)
    if not html:
        return None, None
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    m = REL_TIME_RE.search(text)
    if not m:
        return None, None
    n, unit = int(m.group(1)), m.group(2).lower()
    minutes = n * MINUTES_PER_UNIT.get(unit, 0)
    return m.group(0), minutes


def scrape(category_or_url: str, pages: int, limit: int, fetch_dates: bool,
           delay: float) -> list:
    session = requests.Session()
    all_items = []
    seen_ids = set()

    for page in range(1, pages + 1):
        url = build_list_url(category_or_url, page)
        print(f"[*] страница {page}: {url}")
        html = fetch(url, session)
        if html is None:
            print(f"  [!] страница не отдалась (404 или ошибка сети) — стоп")
            break
        items = parse_listing_page(html, category_or_url)
        if not items:
            print("  [!] объявлений на странице не найдено — либо конец, либо изменилась вёрстка")
            break
        new_items = [i for i in items if i.id not in seen_ids]
        for i in new_items:
            seen_ids.add(i.id)
        all_items.extend(new_items)
        print(f"  -> найдено {len(new_items)} новых объявлений (всего {len(all_items)})")

        if len(all_items) >= limit and not fetch_dates:
            break
        time.sleep(delay)

    if fetch_dates:
        print("[*] уточняю дату публикации по каждому объявлению (--fetch-dates)...")
        for i, item in enumerate(all_items, 1):
            rel, minutes = fetch_relative_date(item.url, session)
            item.posted_relative = rel
            item.posted_minutes_ago = minutes
            if i % 5 == 0:
                print(f"  -> обработано {i}/{len(all_items)}")
            time.sleep(delay)
        all_items.sort(key=lambda x: (x.posted_minutes_ago is None, x.posted_minutes_ago or 0))
    else:
        # без захода в объявления сортируем по числовому id (прокси свежести)
        all_items.sort(key=lambda x: int(x.id) if str(x.id).isdigit() else 0, reverse=True)

    return all_items[:limit]


def save_csv(items: list, path: str):
    if not items:
        print("[!] нечего сохранять — список пуст")
        return
    fields = list(asdict(items[0]).keys())
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for it in items:
            w.writerow(asdict(it))
    print(f"[+] сохранено {len(items)} объявлений -> {path}")


def save_json(items: list, path: str):
    with open(path, "w", encoding="utf-8") as f:
        json.dump([asdict(i) for i in items], f, ensure_ascii=False, indent=2)
    print(f"[+] сохранено {len(items)} объявлений -> {path}")


def save_txt(items: list, path: str):
    """Человекочитаемый текстовый файл — каждое объявление отдельным блоком.
    Ссылка стоит отдельной голой строкой (без "Ссылка: " перед ней) —
    так её надёжнее подхватывают автолинковщики в Word/браузере/некоторых
    редакторах. Но 100% кликабельность гарантирует только .html (см. save_html) —
    обычный .txt/Блокнот кликабельные ссылки не умеет в принципе, это ограничение
    самого Блокнота, а не файла."""
    with open(path, "w", encoding="utf-8") as f:
        for n, it in enumerate(items, 1):
            freshness = it.posted_relative or f"id {it.id} (чем больше id, тем новее)"
            f.write(f"{n}. {it.title}\n")
            f.write(f"   Цена: {it.price or '?'}\n")
            f.write(f"   Город: {it.location or '?'}\n")
            f.write(f"   Свежесть: {freshness}\n")
            f.write(f"   Продавец: {it.seller_type or '?'}   Фото: {it.photo_count or '?'}\n")
            if it.description:
                f.write(f"   Описание: {it.description}\n")
            f.write(f"{it.url}\n")
            f.write("\n")
    print(f"[+] сохранено {len(items)} объявлений -> {path}")


def save_html(items: list, path: str):
    """HTML-файл с настоящими кликабельными ссылками — открывается в любом
    браузере, ссылки там гарантированно синие и кликабельные (в отличие от .txt)."""
    rows = []
    for n, it in enumerate(items, 1):
        freshness = it.posted_relative or f"id {it.id}"
        desc = (it.description or "").replace("<", "&lt;").replace(">", "&gt;")
        title = (it.title or "").replace("<", "&lt;").replace(">", "&gt;")
        rows.append(f"""
        <div style="border:1px solid #ddd; border-radius:8px; padding:12px; margin-bottom:10px; font-family:sans-serif;">
          <div style="font-weight:bold; font-size:15px;">{n}. <a href="{it.url}" target="_blank">{title}</a></div>
          <div style="color:#444; font-size:13px; margin-top:4px;">
            Цена: {it.price or '?'} &nbsp;|&nbsp; Город: {it.location or '?'} &nbsp;|&nbsp;
            Свежесть: {freshness} &nbsp;|&nbsp; Продавец: {it.seller_type or '?'} &nbsp;|&nbsp; Фото: {it.photo_count or '?'}
          </div>
          <div style="color:#666; font-size:13px; margin-top:6px;">{desc}</div>
        </div>""")
    html = f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8"><title>ParuVendu — объявления</title></head>
<body style="max-width:800px; margin:20px auto;">
<h2 style="font-family:sans-serif;">Объявления ParuVendu ({len(items)})</h2>
{''.join(rows)}
</body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[+] сохранено {len(items)} объявлений -> {path} (открой двойным кликом — ссылки кликабельны)")


def main():
    ap = argparse.ArgumentParser(description="Парсер объявлений ParuVendu.fr")
    ap.add_argument("--category", help=f"слаг категории, см. --list-categories")
    ap.add_argument("--url", help="полный URL страницы категории (альтернатива --category)")
    ap.add_argument("--pages", type=int, default=1, help="сколько страниц пагинации обойти")
    ap.add_argument("--limit", type=int, default=30, help="макс. кол-во объявлений в результате")
    ap.add_argument("--fetch-dates", action="store_true",
                     help="точная свежесть: зайти в каждое объявление за реальной датой (медленнее)")
    ap.add_argument("--delay", type=float, default=1.0, help="пауза между запросами, сек")
    ap.add_argument("--out", default="paruvendu_annonces.csv", help="путь для сохранения (.csv, .json, .txt или .html)")
    ap.add_argument("--list-categories", action="store_true", help="показать доступные категории и выйти")
    args = ap.parse_args()

    if args.list_categories:
        print("Доступные категории (--category <слаг>):\n")
        for slug, label in CATEGORIES.items():
            mark = " (проверено вживую)" if slug == "meubles" else ""
            print(f"  {slug:28s} — {label}{mark}")
        print("\nЕсли слаг не сработает (сайт вернёт 404) — скопируй прямую ссылку "
              "на категорию из браузера и передай её через --url.")
        return

    target = args.url or args.category
    if not target:
        ap.error("укажи --category <слаг> (см. --list-categories) или --url <ссылка на категорию>")

    print(f"[*] категория/URL: {target}")
    print(f"[*] страниц: {args.pages}, лимит: {args.limit}, точные даты: {args.fetch_dates}\n")

    items = scrape(target, args.pages, args.limit, args.fetch_dates, args.delay)

    if not items:
        print("\n[!] ничего не найдено. Возможные причины:")
        print("    - неверный слаг категории -> используй --url с прямой ссылкой")
        print("    - сайт изменил вёрстку -> нужно поправить селекторы в parse_card()")
        print("    - сайт отдал капчу/блокировку -> попробуй увеличить --delay")
        return

    print(f"\n[+] итог: {len(items)} объявлений")
    for it in items[:5]:
        freshness = it.posted_relative or f"id={it.id}"
        print(f"  - [{freshness}] {it.title} — {it.price or '?'} — {it.location or '?'}")

    if args.out.endswith(".json"):
        save_json(items, args.out)
    elif args.out.endswith(".txt"):
        save_txt(items, args.out)
    elif args.out.endswith(".html"):
        save_html(items, args.out)
    else:
        save_csv(items, args.out)


if __name__ == "__main__":
    main()
