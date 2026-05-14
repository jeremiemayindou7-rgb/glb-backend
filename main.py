# ─── GermanLink Business – eBay Import Backend ───────────────────────────────
# Datei: main.py
# Speicherort: C:\Users\jerem\OneDrive\Desktop\germanlinkbusiness\main.py
#
# Starten mit:
#   uvicorn main:app --reload --port 8000

import re
import json
import time
import random
import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="GermanLink Business – eBay Import API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Modelle ───────────────────────────────────────────────────────────────────
class ImportRequest(BaseModel):
    url: str

class SaveRequest(BaseModel):
    source: str
    source_url: str
    base_price: float
    glb_price: float
    currency: str
    category: str
    images: list
    translations: dict

# ── User-Agent Pool ───────────────────────────────────────────────────────────
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
]

def get_headers():
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "max-age=0",
        "DNT": "1",
    }

# ── URL Validierung ───────────────────────────────────────────────────────────
EBAY_PATTERN = re.compile(r"https?://(www\.)?ebay\.[a-z.]{2,6}/itm/\d+", re.IGNORECASE)

def validate_url(url: str) -> str:
    url = url.strip()
    if not EBAY_PATTERN.match(url):
        raise HTTPException(status_code=400, detail=f"Ungültige eBay-URL: {url}")
    return url.split("?")[0]

# ── Preis parsen ──────────────────────────────────────────────────────────────
def parse_price(text: str) -> float:
    cleaned = re.sub(r"[^\d.,]", "", text)
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    return float(cleaned)

def extract_currency(text: str) -> str:
    if "€" in text or "EUR" in text:
        return "EUR"
    if "$" in text or "USD" in text:
        return "USD"
    return "EUR"

# ── Bilder extrahieren ────────────────────────────────────────────────────────
def extract_images(soup: BeautifulSoup) -> list:
    images = []

    for script in soup.find_all("script", {"type": "application/ld+json"}):
        try:
            data = json.loads(script.string or "")
            ld_images = data.get("image", [])
            if isinstance(ld_images, str):
                ld_images = [ld_images]
            for img in ld_images:
                if img.startswith("http") and "ebayimg" in img:
                    images.append(img)
        except Exception:
            continue

    if not images:
        for img in soup.find_all("img", {"data-zoom-src": True}):
            src = img.get("data-zoom-src", "").strip()
            if src and src.startswith("http"):
                images.append(src)

    if not images:
        for img in soup.find_all("img"):
            src = img.get("src", "") or img.get("data-src", "")
            if src and "ebayimg.com" in src and "s-l" in src:
                src = re.sub(r"s-l\d+", "s-l1600", src)
                images.append(src)

    seen = set()
    unique = []
    for url in images:
        if url not in seen:
            seen.add(url)
            unique.append(url)

    return unique[:8]

# ── eBay Seite abrufen ────────────────────────────────────────────────────────
def fetch_ebay_page(url: str):
    session = requests.Session()

    # Erst eBay-Startseite besuchen (setzt Cookies)
    try:
        base_domain = re.match(r"(https?://[^/]+)", url).group(1)
        session.get(base_domain, headers=get_headers(), timeout=10)
        time.sleep(random.uniform(0.8, 2.0))
    except Exception:
        pass

    response = session.get(url, headers=get_headers(), timeout=20, allow_redirects=True)
    return response

# ── HTML parsen ───────────────────────────────────────────────────────────────
def parse_ebay(html: str, source_url: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")

    # Titel
    title = ""
    for script in soup.find_all("script", {"type": "application/ld+json"}):
        try:
            data = json.loads(script.string or "")
            if data.get("@type") == "Product" and data.get("name"):
                title = data["name"].strip()
                break
        except Exception:
            continue

    if not title:
        for selector in ["h1.x-item-title__mainTitle span", "h1[itemprop='name']", "h1"]:
            el = soup.select_one(selector)
            if el:
                title = " ".join(el.get_text().split()).strip()
                if title:
                    break

    if not title:
        page_title = soup.find("title")
        if page_title:
            title = re.sub(r"\s*\|?\s*eBay.*$", "", page_title.get_text(), flags=re.I).strip()

    if not title:
        raise HTTPException(status_code=422, detail="Titel konnte nicht extrahiert werden.")

    # Beschreibung
    description = ""
    meta = soup.find("meta", {"name": "description"})
    if meta:
        description = meta.get("content", "").strip()
    if not description:
        for script in soup.find_all("script", {"type": "application/ld+json"}):
            try:
                data = json.loads(script.string or "")
                if data.get("description"):
                    description = data["description"].strip()
                    break
            except Exception:
                continue
    if not description:
        description = title

    # Preis
    base_price = None
    currency = "EUR"

    for script in soup.find_all("script", {"type": "application/ld+json"}):
        try:
            data = json.loads(script.string or "")
            offers = data.get("offers", {})
            if isinstance(offers, list) and offers:
                offers = offers[0]
            if offers.get("price"):
                base_price = float(str(offers["price"]).replace(",", "."))
                currency = offers.get("priceCurrency", "EUR")
                break
        except Exception:
            continue

    if base_price is None:
        price_tag = soup.find(attrs={"itemprop": "price"})
        if price_tag:
            try:
                base_price = float(str(price_tag.get("content", "")).replace(",", "."))
            except Exception:
                pass

    if base_price is None:
        for selector in ["div.x-price-primary span.ux-textspans", "span.x-price-primary", "span#prcIsum"]:
            el = soup.select_one(selector)
            if el:
                try:
                    text = el.get_text()
                    base_price = parse_price(text)
                    currency = extract_currency(text)
                    break
                except Exception:
                    continue

    if base_price is None:
        raise HTTPException(status_code=422, detail="Preis konnte nicht extrahiert werden.")

    # Kategorie
    category = "Sonstiges"
    breadcrumb = soup.find("nav", {"aria-label": re.compile(r"breadcrumb", re.I)})
    if breadcrumb:
        crumbs = breadcrumb.find_all("a")
        if crumbs:
            category = " ".join(crumbs[-1].get_text().split()).strip()

    return {
        "title": title,
        "description": description,
        "base_price": round(base_price, 2),
        "glb_price": round(base_price * 1.20, 2),
        "currency": currency,
        "category": category,
        "images": extract_images(soup),
        "source_url": source_url,
    }

# ── Übersetzung ───────────────────────────────────────────────────────────────
def translate(text: str, source: str, target: str) -> str:
    if not text.strip():
        return text
    try:
        r = requests.get(
            "https://api.mymemory.translated.net/get",
            params={"q": text[:500], "langpair": f"{source}|{target}"},
            timeout=10,
        )
        data = r.json()
        translated = data.get("responseData", {}).get("translatedText", "")
        if translated and translated.upper() != text.upper():
            return translated
    except Exception:
        pass
    return text

LINGALA_DICT = {
    "tracteur": "masini ya bilanga", "tractor": "masini ya bilanga",
    "traktor": "masini ya bilanga", "voiture": "motuka", "auto": "motuka",
    "téléphone": "telefone", "phone": "telefone", "ordinateur": "ordinatɛrɛ",
    "laptop": "ordinatɛrɛ ya mikolo", "maison": "ndako", "bon": "malamu",
    "nouveau": "ya sika", "new": "ya sika", "utilisé": "esalelaki",
    "prix": "ntalo", "livraison": "kobɔkɔlɔ", "qualité": "bolamu",
    "allemagne": "Alemani", "germany": "Alemani", "diesel": "mazutu",
}

def translate_to_lingala(text: str) -> str:
    result = text.lower()
    for word, ln in LINGALA_DICT.items():
        result = result.replace(word, ln)
    return result if result != text.lower() else f"[LN] {text[:100]}"

# ── Endpunkte ─────────────────────────────────────────────────────────────────
@app.get("/")
def root():
    return {"status": "ok", "service": "GermanLink Business eBay Import API"}

@app.get("/health")
def health():
    return {"status": "healthy"}

@app.post("/api/import-ebay")
def import_ebay(req: ImportRequest):
    clean_url = validate_url(req.url)

    try:
        response = fetch_ebay_page(clean_url)

        if response.status_code == 403:
            raise HTTPException(
                status_code=403,
                detail=(
                    "eBay blockiert den automatischen Zugriff (403 Forbidden). "
                    "Bitte warte auf die Genehmigung deines eBay Developer Accounts. "
                    "Danach wird der Import vollautomatisch funktionieren."
                )
            )

        response.raise_for_status()

    except HTTPException:
        raise
    except requests.exceptions.Timeout:
        raise HTTPException(status_code=504, detail="eBay antwortet nicht (Timeout)")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Verbindungsfehler: {str(e)}")

    data = parse_ebay(response.text, clean_url)

    title_de = data["title"]
    desc_de = data["description"]
    title_fr = translate(title_de, "de", "fr")
    desc_fr = translate(desc_de, "de", "fr")
    title_ln = translate_to_lingala(title_fr)
    desc_ln = translate_to_lingala(desc_fr)

    return {
        "source": "ebay",
        "source_url": clean_url,
        "base_price": data["base_price"],
        "glb_price": data["glb_price"],
        "currency": data["currency"],
        "category": data["category"],
        "images": [f"https://images.weserv.nl/?url={img}&output=jpg" for img in data["images"]],
        "translations": {
            "de": {"title": title_de, "description": desc_de},
            "fr": {"title": title_fr, "description": desc_fr},
            "ln": {"title": title_ln, "description": desc_ln},
        },
    }

@app.post("/api/products")
def save_product(product: SaveRequest):
    print(f"[GLB] Produkt gespeichert: {product.source_url}")
    return {"status": "saved", "source_url": product.source_url}

