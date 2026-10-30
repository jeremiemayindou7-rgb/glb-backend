"""
Sunshare.de Produkt-Scraper
Wird als Modul in main.py eingebunden.
Endpoint: POST /api/import-sunshare
"""

import re
import json
import random
import asyncio
import logging
from typing import Optional
import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
]

def get_headers():
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://www.google.de/",
        "DNT": "1",
    }

def proxy_image(url: str) -> str:
    """Bild-URL normalisieren (direkt, kein Proxy nötig für Shopify CDN)."""
    url = url.strip()
    if url.startswith("//"):
        url = "https:" + url
    elif not url.startswith("http"):
        url = "https://" + url
    return url


def extract_wattage(text: str) -> Optional[str]:
    """Watt-Angabe aus Titel extrahieren (z.B. '800W', '600 Watt')."""
    match = re.search(r'(\d{3,4})\s*[Ww](?:att)?', text)
    return f"{match.group(1)}W" if match else None


def clean_url(url: str) -> str:
    """Bereinigt URL von unsichtbaren Sonderzeichen und Encoding-Problemen."""
    # Soft hyphen (0xAD) und andere unsichtbare Zeichen entfernen
    url = url.encode('utf-8', errors='ignore').decode('utf-8')
    url = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\xad]', '', url)
    return url.strip()


def extract_product_slug(url: str) -> Optional[str]:
    """Extrahiert den Produkt-Slug aus einer Sunshare URL."""
    match = re.search(r'/products/([^/?#]+)', url)
    if not match:
        return None
    slug = match.group(1)
    # Soft Hyphen (0xAD) und andere unsichtbare Bytes aus dem Slug entfernen
    slug = slug.encode('utf-8', errors='ignore').decode('utf-8')
    slug = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\xad­]', '', slug)
    return slug.strip()


async def scrape_sunshare_product(url: str) -> dict:
    """
    Scrapt ein Sunshare-Produkt via Shopify JSON API (zuverlässig, alle Bilder).
    Unterstützt:
    - /products/... (direkte Produkt-URL)
    - /collections/... (erste Produkt-URL aus Kategorie)
    """
    url = clean_url(url)

    async with httpx.AsyncClient(headers=get_headers(), follow_redirects=True, timeout=25.0) as client:

        # Wenn Collection-URL: erste Produkt-URL extrahieren
        if "/collections/" in url:
            resp = await client.get(url)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
            product_link = None
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if "/products/" in href:
                    if not href.startswith("http"):
                        href = "https://sunshare.de" + href
                    product_link = href
                    break
            if not product_link:
                raise ValueError("Kein Produkt in dieser Kategorie gefunden.")
            url = product_link

        # Slug extrahieren für JSON API
        slug = extract_product_slug(url)
        if not slug:
            raise ValueError("Ungültige Produkt-URL.")

        # ── SHOPIFY JSON API – alle Bilder, Preis, Titel, Beschreibung ──
        json_url = f"https://sunshare.de/products/{slug}.json"

        # Retry mit Backoff bei 429
        for attempt in range(3):
            if attempt > 0:
                wait = 2 ** attempt + random.uniform(1, 3)
                logger.info(f"[Sunshare] 429 – warte {wait:.1f}s (Versuch {attempt+1}/3)")
                await asyncio.sleep(wait)

            json_resp = await client.get(
                json_url,
                headers={**get_headers(), "Accept": "application/json"}
            )

            if json_resp.status_code == 429:
                if attempt == 2:
                    raise ValueError(
                        "Sunshare.de blockiert aktuell unsere Anfragen (429 Too Many Requests). "
                        "Bitte warte 5-10 Minuten und versuche es erneut."
                    )
                continue

            json_resp.raise_for_status()
            break

        product_data = json_resp.json().get("product", {})

    # ── Titel ──────────────────────────────────────────────────────
    title = product_data.get("title", "").strip()
    if not title:
        raise ValueError("Produkt-Titel nicht gefunden.")

    # ── Preis (günstigste Variante) ────────────────────────────────
    price_eur = 0.0
    variants = product_data.get("variants", [])
    if variants:
        prices = []
        for v in variants:
            try:
                prices.append(float(v.get("price", 0)))
            except (ValueError, TypeError):
                pass
        if prices:
            price_eur = min(prices)

    # ── Bilder (aus JSON API – vollständige Liste) ─────────────────
    images = []
    for img in product_data.get("images", []):
        src = img.get("src", "")
        if src:
            # Höchste Auflösung: _NNNxNNN aus Shopify-URL entfernen
            src = re.sub(r'_\d+x\d+(\.[a-z]+)', r'\1', src)
            proxied = proxy_image(src)
            if proxied not in images:
                images.append(proxied)
        if len(images) >= 6:
            break

    # ── Beschreibung (HTML → Text) ─────────────────────────────────
    body_html = product_data.get("body_html", "")
    if body_html:
        desc_soup = BeautifulSoup(body_html, "html.parser")
        description = desc_soup.get_text(separator=" ", strip=True)[:1000]
    else:
        description = f"{title} von Sunshare – hochwertige Solaranlage aus Deutschland."

    return {
        "title":       title,
        "description": description,
        "price_eur":   price_eur,
        "images":      images,
        "url":         url,
        "wattage":     extract_wattage(title + " " + description),
    }


async def translate_to_french(text: str) -> str:
    """MyMemory DE→FR Übersetzung."""
    if not text:
        return text
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://api.mymemory.translated.net/get",
                params={"q": text[:500], "langpair": "de|fr"},
            )
            data = resp.json()
            translated = data.get("responseData", {}).get("translatedText", "")
            if translated and translated.lower() != text.lower():
                return translated
    except Exception as e:
        logger.warning(f"[Sunshare] Translation failed: {e}")
    return text


# Einfaches Lingala-Wörterbuch für Solar-Begriffe
LINGALA_SOLAR = {
    "solaranlage": "système solaire",
    "solar": "solaire",
    "balkonkraftwerk": "centrale électrique ya balcon",
    "wechselrichter": "onduleur",
    "modul": "panneau solaire",
    "panel": "panneau",
    "kabel": "câble",
    "halterung": "support",
    "strom": "courant électrique",
    "energie": "énergie",
    "leistung": "puissance",
    "watt": "watt",
    "speicher": "stockage",
    "akku": "batterie",
    "set": "kit",
    "komplettset": "kit complet",
    "plug": "plug",
    "balkon": "balcon",
    "garten": "jardin",
    "dach": "toit",
    "installation": "installation",
}


def basic_lingala_translate(fr_text: str) -> str:
    """Einfache Übersetzung FR→LN via Wörterbuch (Lingala-Basis)."""
    text = fr_text
    replacements = {
        "votre": "na yo",
        "pour": "mpo na",
        "avec": "na",
        "inclus": "ezali na yango",
        "prêt": "sima",
        "immédiatement": "mbala moko",
        "de haute qualité": "ya malamu mingi",
        "allemand": "ya Allemagne",
        "kit complet": "kit complet",
        "livraison": "kotindela",
        "gratuite": "ya ofele",
    }
    for fr, ln in replacements.items():
        text = text.replace(fr, ln)
    return text


async def import_sunshare_product(url: str) -> dict:
    """
    Hauptfunktion: Scrapen + Übersetzen eines Sunshare-Produkts.
    """
    raw = await scrape_sunshare_product(url)

    title_de = raw["title"]
    desc_de  = raw["description"]
    price    = raw["price_eur"]

    # Übersetzung DE→FR
    title_fr = await translate_to_french(title_de)
    desc_fr  = await translate_to_french(desc_de[:300])  # Kurz halten

    # Lingala
    title_ln = basic_lingala_translate(title_fr)
    desc_ln  = basic_lingala_translate(desc_fr)

    # GLB Preis: +20%
    glb_price = round(price * 1.20, 2)

    return {
        "title":          title_de,
        "title_de":       title_de,
        "title_fr":       title_fr,
        "title_ln":       title_ln,
        "description":    desc_de,
        "description_de": desc_de,
        "description_fr": desc_fr,
        "description_ln": desc_ln,
        "price_eur":      price,
        "glb_price_eur":  glb_price,
        "images":         raw["images"],
        "url":            url,
        "wattage":        raw.get("wattage"),
        "category":       "solar_energie",
        "source_type":    "sunshare",
    }

