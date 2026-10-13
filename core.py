"""Logica: validazione barcode, UPCitemdb, GameTDB, abbinamento."""
import difflib
import io
import re
import sqlite3
import time
import zipfile
import xml.etree.ElementTree as ET

import requests

UPCITEMDB_URL = "https://api.upcitemdb.com/prod/trial/lookup"
DB_PATH = "barcodes.db"

# ---------- barcode ----------

def normalize_code(raw: str) -> str:
    """Tiene solo le cifre. Un EAN-13 che inizia per 0 diventa UPC-A (12 cifre)."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 13 and digits.startswith("0"):
        digits = digits[1:]
    return digits


def check_digit_ok(code: str) -> bool:
    """Valida la cifra di controllo GS1 (UPC-A 12 cifre o EAN-13 13 cifre)."""
    if len(code) not in (12, 13) or not code.isdigit():
        return False
    body, check = code[:-1], int(code[-1])
    # si pesa partendo da destra: 3,1,3,1...
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return (10 - total % 10) % 10 == check


def split_upc(code: str) -> dict:
    if len(code) != 12:
        return {}
    return {
        "sistema": code[0],
        "produttore": code[1:6],
        "prodotto": code[6:11],
        "controllo": code[11],
        "prefisso_gs1_6": code[:6],
        "ean13": "0" + code,
    }

# ---------- cache SQLite ----------

def _db():
    db = sqlite3.connect(DB_PATH)
    db.execute(
        """CREATE TABLE IF NOT EXISTS upc_cache
           (upc TEXT PRIMARY KEY, title TEXT, brand TEXT, model TEXT,
            category TEXT, raw_status TEXT, fetched INTEGER)"""
    )
    return db


def upc_lookup(upc: str, use_cache: bool = True) -> dict:
    """Interroga UPCitemdb (endpoint trial). Ritorna dict con 'status' e i campi."""
    db = _db()
    if use_cache:
        row = db.execute(
            "SELECT title, brand, model, category, raw_status FROM upc_cache WHERE upc=?",
            (upc,),
        ).fetchone()
        if row:
            return {"status": row[4], "title": row[0], "brand": row[1],
                    "model": row[2], "category": row[3], "from_cache": True}

    try:
        r = requests.get(UPCITEMDB_URL, params={"upc": upc}, timeout=15)
    except requests.RequestException as e:
        return {"status": f"errore di rete: {e}"}

    if r.status_code == 429:
        return {"status": "limite giornaliero UPCitemdb raggiunto (429)"}
    if r.status_code != 200:
        return {"status": f"errore HTTP {r.status_code}"}

    data = r.json()
    items = data.get("items") or []
    if not items:
        res = {"status": "non trovato", "title": None, "brand": None,
               "model": None, "category": None}
    else:
        it = items[0]
        res = {"status": "ok", "title": it.get("title"), "brand": it.get("brand"),
               "model": it.get("model"), "category": it.get("category")}
    db.execute(
        "INSERT OR REPLACE INTO upc_cache VALUES (?,?,?,?,?,?,?)",
        (upc, res["title"], res["brand"], res["model"], res["category"],
         res["status"], int(time.time())),
    )
    db.commit()
    res["from_cache"] = False
    return res

# ---------- titoli ----------

NOISE = (r"\b(ps3|ps2|ps4|ps5|psp|playstation ?\d?|sony|wii u?|nintendo|switch|"
         r"xbox( 360| one)?|pal|ntsc|italian[oa]?|ita|eur?|usa|platinum|essentials?|"
         r"game|videogame|video game)\b")
ROMAN = {"ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7",
         "viii": "8", "ix": "9", "x": "10"}


def clean_title(t: str) -> str:
    t = re.sub(NOISE, " ", t or "", flags=re.I)
    t = re.sub(r"[\-–—:()\[\]/,]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def norm_for_match(t: str) -> str:
    t = clean_title(t).lower()
    t = re.sub(r"[^\w\s]", "", t)
    words = [ROMAN.get(w, w) for w in t.split()]
    return " ".join(words)

# ---------- GameTDB ----------

def format_serial(gid: str) -> str:
    """BCES00510 -> BCES-00510 (solo se ha la forma 4 lettere + 5 cifre)."""
    m = re.fullmatch(r"([A-Z]{4})(\d{5})", gid or "")
    return f"{m.group(1)}-{m.group(2)}" if m else gid


def read_xml_bytes(data: bytes) -> bytes:
    """Accetta XML oppure ZIP contenente un XML."""
    if data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".xml")]
            if not names:
                raise ValueError("nessun file .xml nello zip")
            return z.read(names[0])
    return data


def parse_gametdb(data: bytes) -> list[dict]:
    root = ET.fromstring(read_xml_bytes(data))
    games = []
    for g in root.iter("game"):
        gid = (g.findtext("id") or "").strip()
        if not gid:
            continue
        region = (g.findtext("region") or "").strip()
        langs = (g.findtext("languages") or "").strip()
        titles = []
        for loc in g.findall("locale"):
            tt = (loc.findtext("title") or "").strip()
            if tt:
                titles.append((loc.get("lang", ""), tt))
        if not titles and g.get("name"):
            titles.append(("", g.get("name")))
        for lang, tt in titles:
            games.append({"title": tt, "lang": lang, "id": gid,
                          "serial": format_serial(gid), "region": region,
                          "languages": langs})
    return games


def fetch_gametdb(url: str) -> bytes:
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    return r.content


def match_games(query: str, games: list[dict], region: str = "", lang: str = "",
                limit: int = 10) -> list[dict]:
    q = norm_for_match(query)
    if not q:
        return []
    out = {}
    for g in games:
        if region and g["region"] != region:
            continue
        if lang and lang not in [x.strip() for x in g["languages"].split(",")]:
            continue
        n = norm_for_match(g["title"])
        score = difflib.SequenceMatcher(None, q, n).ratio()
        if q and n and (q in n or n in q):
            score = max(score, 0.9)
        if score >= 0.6:
            key = g["id"]
            if key not in out or score > out[key]["score"]:
                out[key] = {**g, "score": round(score, 2)}
    return sorted(out.values(), key=lambda x: -x["score"])[:limit]
