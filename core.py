"""Logica: validazione barcode, UPCitemdb, GameTDB, abbinamento."""
import difflib
import io
import json
import base64
import re
import sqlite3
import time
import zipfile
import xml.etree.ElementTree as ET

import requests
from urllib.parse import quote

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
        """CREATE TABLE IF NOT EXISTS upc_cache3
           (upc TEXT, provider TEXT, title TEXT, brand TEXT, model TEXT,
            category TEXT, raw_status TEXT, fetched INTEGER, alt TEXT,
            PRIMARY KEY (upc, provider))"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS confirmed
           (upc TEXT PRIMARY KEY, title TEXT, serial TEXT, gametdb_id TEXT,
            region TEXT, languages TEXT, upc_title TEXT, confirmed INTEGER)"""
    )
    return db


# ---------- barcode confermati (tabella tua) ----------

CONFIRMED_COLS = ["upc", "title", "serial", "gametdb_id", "region",
                  "languages", "upc_title"]


def confirm_match(upc: str, game: dict, upc_title: str | None = None) -> None:
    db = _db()
    db.execute(
        "INSERT OR REPLACE INTO confirmed VALUES (?,?,?,?,?,?,?,?)",
        (upc, game["title"], game["serial"], game["id"], game["region"],
         game["languages"], upc_title, int(time.time())),
    )
    db.commit()


def get_confirmed(upc: str) -> dict | None:
    row = _db().execute(
        f"SELECT {','.join(CONFIRMED_COLS)} FROM confirmed WHERE upc=?", (upc,)
    ).fetchone()
    return dict(zip(CONFIRMED_COLS, row)) if row else None


def list_confirmed() -> list[dict]:
    rows = _db().execute(
        f"SELECT {','.join(CONFIRMED_COLS)} FROM confirmed ORDER BY confirmed DESC"
    ).fetchall()
    return [dict(zip(CONFIRMED_COLS, r)) for r in rows]


def delete_confirmed(upc: str) -> None:
    db = _db()
    db.execute("DELETE FROM confirmed WHERE upc=?", (upc,))
    db.commit()


def import_confirmed(rows: list[dict]) -> int:
    """Ripristina da CSV esportato. Ritorna il numero di righe importate."""
    db = _db()
    n = 0
    for r in rows:
        upc = normalize_code(str(r.get("upc", "")))
        if not upc or not r.get("serial"):
            continue
        db.execute(
            "INSERT OR REPLACE INTO confirmed VALUES (?,?,?,?,?,?,?,?)",
            (upc, r.get("title"), r.get("serial"), r.get("gametdb_id"),
             r.get("region"), r.get("languages"), r.get("upc_title"),
             int(time.time())),
        )
        n += 1
    db.commit()
    return n


BARCODELOOKUP_URL = "https://api.barcodelookup.com/v3/products"
EBAY_TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
EBAY_SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
EBAY_MARKETS = {"EBAY_IT": "Italia", "EBAY_DE": "Germania", "EBAY_FR": "Francia",
                "EBAY_ES": "Spagna", "EBAY_GB": "Regno Unito"}
_ebay_token = {"value": None, "exp": 0.0, "key": None}


def _ebay_get_token(client_id: str, client_secret: str) -> str:
    key = f"{client_id}:{client_secret}"
    if _ebay_token["value"] and _ebay_token["key"] == key and time.time() < _ebay_token["exp"]:
        return _ebay_token["value"]
    basic = base64.b64encode(key.encode()).decode()
    r = requests.post(
        EBAY_TOKEN_URL, timeout=15,
        headers={"Authorization": f"Basic {basic}",
                 "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "client_credentials",
              "scope": "https://api.ebay.com/oauth/api_scope"})
    r.raise_for_status()
    j = r.json()
    _ebay_token.update(value=j["access_token"], key=key,
                       exp=time.time() + int(j.get("expires_in", 7200)) - 60)
    return _ebay_token["value"]


def _ebay_lookup(upc: str, api_key: str | None, markets=None) -> dict:
    empty = {"title": None, "brand": None, "model": None, "category": None,
             "alternatives": []}
    markets = list(markets or EBAY_MARKETS)
    if not api_key or ":" not in api_key:
        return {"status": "mancano Client ID e Client Secret di eBay", **empty}
    cid, secret = api_key.split(":", 1)
    try:
        token = _ebay_get_token(cid, secret)
    except requests.HTTPError as e:
        return {"status": f"autenticazione eBay fallita (HTTP "
                          f"{e.response.status_code}): controlla le chiavi di produzione",
                **empty}
    forms = [upc] + (["0" + upc] if len(upc) == 12 else [])
    items, found_in, last_err = [], None, None
    for market in markets:
        headers = {"Authorization": f"Bearer {token}",
                   "X-EBAY-C-MARKETPLACE-ID": market}
        for gtin in forms:
            r = requests.get(EBAY_SEARCH_URL, headers=headers, timeout=15,
                             params={"gtin": gtin, "limit": 20})
            if r.status_code == 429:
                return {"status": "limite di richieste eBay raggiunto (429)", **empty}
            if r.status_code in (401, 403):
                return {"status": f"accesso negato da eBay (HTTP {r.status_code})",
                        **empty}
            if r.status_code != 200:
                last_err = f"errore HTTP {r.status_code} su {market}"
                continue
            items = r.json().get("itemSummaries") or []
            if items:
                found_in = market
                break
        if items:
            break
    if not items:
        return {"status": last_err or "non trovato", **empty}
    # titolo più frequente tra gli annunci (dopo la pulizia)
    groups: dict[str, list[str]] = {}
    for it in items:
        t = it.get("title") or ""
        groups.setdefault(norm_for_match(t), []).append(t)
    best_key = max(groups, key=lambda k: len(groups[k]))
    best = groups[best_key][0]
    cat = (items[0].get("categories") or [{}])[0].get("categoryName") or ""
    alts = [v[0] for k, v in groups.items() if k != best_key][:5]
    return {"status": "ok", "title": best, "brand": None, "model": None,
            "category": f"{cat} [eBay {EBAY_MARKETS.get(found_in, found_in)}]".strip(),
            "alternatives": alts}


SCANDEX_URL = "https://scandex.gamery.app/api/v2/lookup"


def _scandex_lookup(upc: str, api_key: str | None) -> dict:
    empty = {"title": None, "brand": None, "model": None, "category": None,
             "alternatives": []}
    if not api_key:
        return {"status": "manca il token API di ScanDex", **empty}
    r = requests.get(SCANDEX_URL, params={"value": upc}, timeout=15,
                     headers={"Authorization": api_key})
    if r.status_code == 404:
        return {"status": "non trovato", **empty}
    if r.status_code in (401, 403):
        return {"status": f"token ScanDex non valido (HTTP {r.status_code})", **empty}
    if r.status_code == 429:
        return {"status": "troppe richieste a ScanDex (429)", **empty}
    if r.status_code != 200:
        return {"status": _http_err(r, api_key), **empty}
    j = r.json()
    meta = j.get("igdb_metadata")
    if not meta:
        return {"status": "barcode noto a ScanDex ma senza gioco abbinato", **empty}
    plat = (meta.get("platform") or {}).get("name")
    return {"status": "ok", "title": meta.get("name"), "brand": None,
            "model": f"IGDB {meta.get('id')}" if meta.get("id") else None,
            "category": plat, "alternatives": []}


PROVIDERS = {"retroplace": "Retroplace (chiave API venditore, dà anche il product ID)",
             "scandex": "ScanDex (database barcode di videogiochi)",
             "ebay": "eBay Browse API (chiavi sviluppatore gratuite)",
             "barcodelookup": "Barcode Lookup (serve chiave API)",
             "upcitemdb": "UPCitemdb (gratuito, con limite giornaliero)"}


HEADERS = {"User-Agent": "upc-game-lookup/1.0 (personal Streamlit app)"}


def _http_err(r, api_key=None) -> str:
    """Messaggio d'errore HTTP con un frammento della risposta, senza chiavi."""
    txt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", r.text or "")).strip()[:160]
    for secret in filter(None, [api_key] + (api_key or "").split(":")):
        txt = txt.replace(secret, "***")
    return f"errore HTTP {r.status_code}" + (f": {txt}" if txt else "")


def _unpack_alt(raw) -> dict:
    data = json.loads(raw or "[]")
    if isinstance(data, list):
        return {"alternatives": data, "releases": []}
    return {"alternatives": data.get("alternatives", []),
            "releases": data.get("releases", [])}


RETROPLACE_URL = "https://www.retroplace.com/en/api/{key}/getItemByBarcode"
RETRO_REGION_RANK = {"it": 0, "eu": 1}


def _retroplace_lookup(upc: str, api_key: str | None) -> dict:
    empty = {"title": None, "brand": None, "model": None, "category": None,
             "alternatives": [], "releases": []}
    if not api_key:
        return {"status": "manca la chiave API di Retroplace", **empty}
    url = RETROPLACE_URL.format(key=quote(api_key, safe=""))
    rels, msg = [], ""
    for code in dict.fromkeys([upc] + (["0" + upc] if len(upc) == 12 else [])):
        r = requests.get(url, params={"barcode": code}, headers=HEADERS,
                         timeout=15)
        if r.status_code != 200:
            return {"status": _http_err(r, api_key), **empty}
        try:
            j = r.json()
        except ValueError:
            return {"status": "risposta non valida da Retroplace", **empty}
        if j.get("result") == "success":
            rels = j.get("releases") or []
            if rels:
                break
        else:
            msg = j.get("message", "")
            if msg != "Not found.":
                return {"status": f"Retroplace: {msg}", **empty}
    if not rels:
        return {"status": "non trovato", **empty}
    norm = []
    for x in rels:
        regs = x.get("region") or []
        norm.append({"rp_id": x.get("rp_id"), "title": x.get("title"),
                     "system": x.get("system"), "barcode": x.get("barcode"),
                     "productid": ", ".join(p for p in (x.get("productid_medium") or []) if p),
                     "region": ",".join(regs),
                     "_rank": min([RETRO_REGION_RANK.get(g, 2) for g in regs] or [2])})
    norm.sort(key=lambda d: d["_rank"])
    for d in norm:
        d.pop("_rank")
    best = norm[0]
    alts = [f"{d['title']} ({d['system']}, {d['region']})" for d in norm[1:6]]
    return {"status": "ok", "title": best["title"], "brand": None,
            "model": best["productid"] or None, "category": best["system"],
            "alternatives": alts, "releases": norm}


def _fetch_provider(upc: str, provider: str, api_key: str | None,
                    markets=None) -> dict:
    """Chiamata di rete. Ritorna dict con status e campi normalizzati."""
    empty = {"title": None, "brand": None, "model": None, "category": None}
    try:
        if provider == "retroplace":
            return _retroplace_lookup(upc, api_key)
        if provider == "scandex":
            return _scandex_lookup(upc, api_key)
        if provider == "ebay":
            return _ebay_lookup(upc, api_key, markets)
        if provider == "barcodelookup":
            if not api_key:
                return {"status": "manca la chiave API di Barcode Lookup", **empty}
            r = requests.get(BARCODELOOKUP_URL, timeout=15,
                             params={"barcode": upc, "formatted": "y", "key": api_key})
            if r.status_code == 404:
                return {"status": "non trovato", **empty}
            if r.status_code in (401, 403):
                return {"status": "chiave API non valida o piano esaurito "
                                  f"(HTTP {r.status_code})", **empty}
            if r.status_code == 429:
                return {"status": "troppe richieste (429)", **empty}
            if r.status_code != 200:
                return {"status": _http_err(r, api_key), **empty}
            items = r.json().get("products") or []
            if not items:
                return {"status": "non trovato", **empty}
            it = items[0]
            return {"status": "ok", "title": it.get("title"),
                    "brand": it.get("brand") or it.get("manufacturer"),
                    "model": it.get("model") or it.get("mpn"),
                    "category": it.get("category")}
        # upcitemdb
        r = requests.get(UPCITEMDB_URL, params={"upc": upc}, headers=HEADERS,
                         timeout=15)
        if r.status_code == 429:
            return {"status": "limite giornaliero UPCitemdb raggiunto (429)", **empty}
        if r.status_code != 200:
            return {"status": _http_err(r, api_key), **empty}
        items = r.json().get("items") or []
        if not items:
            return {"status": "non trovato", **empty}
        it = items[0]
        return {"status": "ok", "title": it.get("title"), "brand": it.get("brand"),
                "model": it.get("model"), "category": it.get("category")}
    except requests.RequestException as e:
        msg = str(e)
        for secret in filter(None, [api_key] + (api_key or "").split(":")):
            msg = msg.replace(secret, "***").replace(quote(secret, safe=""), "***")
        return {"status": f"errore di rete: {msg}", **empty}


def upc_lookup(upc: str, provider: str = "upcitemdb", api_key: str | None = None,
               use_cache: bool = True, markets=None) -> dict:
    """Cerca il barcode nel provider scelto, con cache SQLite per provider."""
    db = _db()
    if use_cache:
        row = db.execute(
            "SELECT title, brand, model, category, raw_status, alt FROM upc_cache3 "
            "WHERE upc=? AND provider=?", (upc, provider)).fetchone()
        if row:
            return {"status": row[4], "title": row[0], "brand": row[1],
                    "model": row[2], "category": row[3],
                    **_unpack_alt(row[5]), "from_cache": True}

    res = _fetch_provider(upc, provider, api_key, markets)
    # si salva solo l'esito "ok" o "non trovato", non gli errori temporanei
    cacheable = res["status"] == "ok" or (res["status"] == "non trovato"
                                          and provider not in ("ebay", "scandex", "retroplace"))
    if cacheable:
        db.execute(
            "INSERT OR REPLACE INTO upc_cache3 VALUES (?,?,?,?,?,?,?,?,?)",
            (upc, provider, res["title"], res["brand"], res["model"],
             res["category"], res["status"], int(time.time()),
             json.dumps({"alternatives": res.get("alternatives") or [],
                         "releases": res.get("releases") or []})))
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
    t = re.sub(r"\([^)]*\b(?:19|20)\d{2}\b[^)]*\)", " ", t or "")  # (Sony PlayStation 3, 2010)
    t = re.sub(r"\b(usato|nuovo|sigillato|completo|gioco|videogioco|ottime condizioni)\b",
               " ", t, flags=re.I)
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
