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

def remove_stray_continuation_bytes(data: bytes):
    """
    Entfernt isolierte Continuation Bytes (0x80-0xBF) ohne gültigen Start-Byte.

    UTF-8 Sequenzlängen:
      1-Byte:  0x00-0x7F        (ASCII)
      2-Byte:  0xC2-0xDF + 0x80-0xBF
      3-Byte:  0xE0-0xEF + 0x80-0xBF * 2
      4-Byte:  0xF0-0xF4 + 0x80-0xBF * 3
      Ungültig: 0x80-0xBF (stray), 0xC0-0xC1 (overlong), 0xF5+ (out of range)

    WICHTIG: 0xAD ist nur ungültig als ISOLIERTES Byte.
    - í = C3 AD (gültig, 2-Byte-Sequenz) → wird BEIBEHALTEN
    - Э = D0 AD (gültig, 2-Byte-Sequenz) → wird BEIBEHALTEN
    - U+00AD = C2 AD (gültig, 2-Byte-Sequenz) → wird BEIBEHALTEN
    - 0xAD allein (sunshare.de Bug) → wird ENTFERNT
    """
    result = bytearray()
    removed = []
    i = 0
    while i < len(data):
        byte = data[i]
        if 0x00 <= byte <= 0x7F:
            # ASCII – immer gültig
            result.append(byte)
            i += 1
        elif 0xC2 <= byte <= 0xDF:
            # 2-Byte Sequenz: start + 1 continuation
            if i + 1 < len(data) and 0x80 <= data[i + 1] <= 0xBF:
                result.extend(data[i:i + 2])
                i += 2
            else:
                removed.append((i, byte))
                i += 1
        elif 0xE0 <= byte <= 0xEF:
            # 3-Byte Sequenz: start + 2 continuations
            if (i + 2 < len(data)
                    and 0x80 <= data[i + 1] <= 0xBF
                    and 0x80 <= data[i + 2] <= 0xBF):
                result.extend(data[i:i + 3])
                i += 3
            else:
                removed.append((i, byte))
                i += 1
        elif 0xF0 <= byte <= 0xF4:
            # 4-Byte Sequenz: start + 3 continuations
            if (i + 3 < len(data)
                    and 0x80 <= data[i + 1] <= 0xBF
                    and 0x80 <= data[i + 2] <= 0xBF
                    and 0x80 <= data[i + 3] <= 0xBF):
                result.extend(data[i:i + 4])
                i += 4
            else:
                removed.append((i, byte))
                i += 1
        else:
            # Stray continuation byte (0x80-0xBF) ODER ungültiger Start (0xC0,0xC1,0xF5+)
            removed.append((i, byte))
            i += 1
    return bytes(result), removed


def parse_sunshare_json_safe(raw_bytes: bytes, log=None) -> dict:
    """
    Parst Shopify JSON-Response von sunshare.de mit präzisem Encoding-Handling.

    Strategie (4-stufig):
    1. Strict UTF-8 (Normalfall – kein Overhead)
    2. Bei UnicodeDecodeError: Position + Kontext loggen, stray bytes präzise entfernen
    3. Erneut strict UTF-8 auf bereinigten Bytes
    4. Letzter Fallback: errors='replace' (nie ohne Log)

    Garantiert:
    - Deutsche Umlaute (ä=C3A4, ö=C3B6, ü=C3BC, ß=C39F) bleiben erhalten
    - Französische Zeichen (é=C3A9, ç=C3A7, à=C3A0) bleiben erhalten
    - Arabisch, Chinesisch, Kyrillisch (alle mehrbyte UTF-8) bleiben erhalten
    - Nur echte Fehlbytes werden entfernt, nie gültige Sequenzen
    """
    if log is None:
        log = logging.getLogger(__name__)

    # ── Schritt 1: Strict UTF-8 (Normalfall, kein Overhead) ──────────
    try:
        return json.loads(raw_bytes.decode('utf-8')).get('product', {})
    except UnicodeDecodeError as e:
        prev_byte = raw_bytes[e.start - 1] if e.start > 0 else 0
        log.warning(
            f"[Sunshare] UnicodeDecodeError: Stray Byte 0x{raw_bytes[e.start]:02x} "
            f"an Position {e.start} (vorher: 0x{prev_byte:02x}, "
            f"Reason: {e.reason})"
        )

    # ── Schritt 2: Stray Continuation Bytes präzise entfernen ────────
    cleaned, removed = remove_stray_continuation_bytes(raw_bytes)
    for pos, b in removed:
        log.warning(f"[Sunshare] Entferntes Byte: 0x{b:02x} an Position {pos}")

    # ── Schritt 3: Strict UTF-8 auf bereinigten Daten ────────────────
    try:
        return json.loads(cleaned.decode('utf-8')).get('product', {})
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        log.error(f"[Sunshare] Schritt 3 fehlgeschlagen ({type(e).__name__}): {e}")

    # ── Schritt 4: Letzter Fallback (mit explizitem Logging) ─────────
    log.warning("[Sunshare] Fallback: errors='replace' aktiv – prüfe die Response-Quelle")
    text = cleaned.decode('utf-8', errors='replace').replace('�', '')
    try:
        return json.loads(text).get('product', {})
    except json.JSONDecodeError as e:
        raise ValueError(f"Sunshare JSON-Response konnte nicht verarbeitet werden: {e}")


def get_headers():
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
        # Accept-Encoding wird NICHT gesetzt – httpx übernimmt automatische
        # Dekomprimierung (gzip/deflate/br). Wenn wir es manuell setzen,
        # bekommt json_resp.content die rohen komprimierten Bytes.
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

        # ── Diagnose-Logging (immer aktiv) ──────────────────────────────
        logger.warning(
            f"[Sunshare] Response: status={json_resp.status_code}, "
            f"content-type='{json_resp.headers.get('content-type', '')}', "
            f"content-encoding='{json_resp.headers.get('content-encoding', '')}', "
            f"content-length={len(json_resp.content)}, "
            f"first_bytes={json_resp.content[:30].hex()!r}"
        )

        # ── PHASE 1: Leere Response prüfen ──────────────────────────────
        if not json_resp.content:
            raise ValueError(
                f"Sunshare: Leere Antwort erhalten (Status {json_resp.status_code}). "
                f"Produkt existiert möglicherweise nicht mehr oder die URL ist ungültig: {json_url}"
            )

        # ── PHASE 2: Content-Type + erste Bytes prüfen ───────────────
        # Nur JSON-Antworten an den JSON-Parser übergeben.
        # HTML-Antworten (Bot-Schutz, 404-Seite, Redirect-Ziel) gesondert behandeln.
        content_type = json_resp.headers.get("content-type", "").lower()
        first_bytes = json_resp.content[:20].lstrip()
        is_html = ("html" in content_type) or first_bytes.startswith(b"<")

        if is_html:
            # HTML-Response: Titel extrahieren für sprechende Fehlermeldung
            try:
                soup = BeautifulSoup(json_resp.content[:5000], "html.parser")
                page_title_tag = soup.find("title")
                page_title = page_title_tag.text.strip() if page_title_tag else "unbekannt"
            except Exception:
                page_title = "unbekannt"

            logger.warning(
                f"[Sunshare] HTML statt JSON erhalten: Status={json_resp.status_code}, "
                f"Content-Type='{content_type}', Seite='{page_title}', URL={json_url}"
            )

            if json_resp.status_code == 404:
                raise ValueError(
                    f"Sunshare: Produkt nicht gefunden (404). "
                    f"Bitte prüfe ob das Produkt noch existiert: {json_url}"
                )
            elif json_resp.status_code == 403:
                raise ValueError(
                    f"Sunshare: Zugriff verweigert (403). "
                    f"Bot-Schutz aktiv oder IP gesperrt. Bitte später erneut versuchen."
                )
            else:
                raise ValueError(
                    f"Sunshare: HTML statt JSON erhalten (Status {json_resp.status_code}, "
                    f"Seite: '{page_title}'). "
                    f"Möglicherweise Bot-Schutz aktiv oder Produkt nicht verfügbar."
                )

        # ── ENCODING-FIX: sunshare.de (Shopify) sendet manchmal 0xAD als einzelnes,
        # ungültiges Byte in UTF-8-Streams (Latin-1 Soft Hyphen Bug).
        #
        # httpx's .json() ruft intern .text → .content.decode('utf-8') auf
        # → UnicodeDecodeError (Subclass von ValueError) → HTTP 422.
        #
        # parse_sunshare_json_safe() löst das präzise:
        # 1. Strict UTF-8 (Normalfall – kein Overhead)
        # 2. Bei Fehler: stray continuation bytes KONTEXTBEWUSST entfernen
        #    (0xAD allein = stray → weg; í=C3AD, Э=D0AD → beibehalten)
        # 3. Erneut strict UTF-8 auf bereinigten Bytes
        # 4. Letzter Fallback mit explizitem Logging
        product_data = parse_sunshare_json_safe(json_resp.content, logger)

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

