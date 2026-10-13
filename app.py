import pandas as pd
import streamlit as st

import core

st.set_page_config(page_title="UPC → dati videogioco", page_icon="🎮", layout="wide")
st.title("🎮 Da barcode UPC/EAN a dati del videogioco")
st.caption("UPCitemdb per il titolo, GameTDB per seriale, regione e lingue.")

# ---------- 1. GameTDB ----------
with st.sidebar:
    st.header("1. Database GameTDB")
    st.write("Carica l'XML (o lo ZIP) della piattaforma, es. `ps3tdb.xml`, "
             "scaricabile dalla pagina download di gametdb.com.")
    up = st.file_uploader("File XML o ZIP", type=["xml", "zip"])
    url = st.text_input("…oppure URL diretto del file (opzionale)")
    if st.button("Scarica da URL") and url:
        try:
            st.session_state["tdb_bytes"] = core.fetch_gametdb(url)
            st.success("Scaricato.")
        except Exception as e:
            st.error(f"Download non riuscito: {e}")
    if up is not None:
        st.session_state["tdb_bytes"] = up.getvalue()

    games = []
    if "tdb_bytes" in st.session_state:
        try:
            games = core.parse_gametdb(st.session_state["tdb_bytes"])
            st.success(f"{len(games)} titoli caricati")
        except Exception as e:
            st.error(f"File non leggibile: {e}")

    st.header("Filtri")
    regions = sorted({g["region"] for g in games if g["region"]})
    region = st.selectbox("Regione", [""] + regions,
                          format_func=lambda x: x or "tutte")
    lang = st.text_input("Lingua richiesta (es. IT)", value="").strip().upper()

# ---------- 2. Barcode ----------
st.header("2. Barcode")
raw = st.text_input("UPC o EAN", value="711719158165")
code = core.normalize_code(raw)

if not code:
    st.stop()

ok = core.check_digit_ok(code)
c1, c2 = st.columns(2)
with c1:
    st.metric("Codice normalizzato", code)
with c2:
    st.metric("Cifra di controllo", "valida ✅" if ok else "NON valida ❌")

parts = core.split_upc(code)
if parts:
    st.write(f"**Divisione:** `{parts['sistema']}` sistema · `{parts['produttore']}` "
             f"produttore · `{parts['prodotto']}` prodotto · `{parts['controllo']}` "
             f"controllo · EAN-13: `{parts['ean13']}`")

# ---------- 3. Lookup ----------
if st.button("Cerca", type="primary"):
    with st.spinner("Interrogo UPCitemdb…"):
        st.session_state["upc_res"] = core.upc_lookup(code)
        st.session_state["upc_code"] = code

res = st.session_state.get("upc_res")
if res and st.session_state.get("upc_code") == code:
    st.header("3. Risultato UPCitemdb")
    if res["status"] != "ok":
        st.warning(f"Esito: {res['status']}")
    else:
        st.write({k: res.get(k) for k in ("title", "brand", "model", "category")})
        if res.get("from_cache"):
            st.caption("(dal cache locale)")

    # ---------- 4. Abbinamento ----------
    st.header("4. Abbinamento con GameTDB")
    default_title = core.clean_title(res.get("title") or "")
    query = st.text_input("Titolo da cercare (modificabile)", value=default_title)

    if not games:
        st.info("Carica prima il file GameTDB dalla barra laterale.")
    elif query:
        hits = core.match_games(query, games, region=region, lang=lang)
        if not hits:
            st.warning("Nessun abbinamento. Prova a cambiare titolo o togliere i filtri.")
        else:
            df = pd.DataFrame(hits)[["title", "serial", "id", "region",
                                     "languages", "score"]]
            df.columns = ["Titolo", "Seriale", "ID GameTDB", "Regione",
                          "Lingue", "Punteggio"]
            st.dataframe(df, use_container_width=True, hide_index=True)

            best = hits[0]
            record = {"upc": code, "ean13": "0" + code if len(code) == 12 else code,
                      "titolo_upcitemdb": res.get("title"),
                      "titolo": best["title"], "seriale": best["serial"],
                      "regione": best["region"], "lingue": best["languages"],
                      "fonte": "UPCitemdb + GameTDB"}
            st.subheader("Record proposto (migliore abbinamento)")
            st.json(record)
            st.download_button("Scarica CSV",
                               pd.DataFrame([record]).to_csv(index=False),
                               file_name=f"{code}.csv", mime="text/csv")
