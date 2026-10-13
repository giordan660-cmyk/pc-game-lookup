import pandas as pd
import streamlit as st

import core

st.set_page_config(page_title="UPC → dati videogioco", page_icon="🎮", layout="wide")
st.title("🎮 Da barcode UPC/EAN a dati del videogioco")
st.caption("UPCitemdb per il titolo, GameTDB per seriale, regione e lingue. "
           "Filtri predefiniti: PAL + italiano.")

# ---------- barra laterale: GameTDB e filtri ----------
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
    options = [""] + regions
    default_idx = options.index("PAL") if "PAL" in options else 0
    region = st.selectbox("Regione", options, index=default_idx,
                          format_func=lambda x: x or "tutte")
    lang = st.text_input("Lingua richiesta (vuoto = qualsiasi)",
                         value="IT").strip().upper()

tab_search, tab_mine = st.tabs(["Cerca barcode", "I miei barcode"])

# ---------- scheda: cerca ----------
with tab_search:
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
        st.write(f"**Divisione:** `{parts['sistema']}` sistema · "
                 f"`{parts['produttore']}` produttore · `{parts['prodotto']}` "
                 f"prodotto · `{parts['controllo']}` controllo · "
                 f"EAN-13: `{parts['ean13']}`")

    # già confermato in precedenza?
    saved = core.get_confirmed(code)
    if saved:
        st.success("Barcode già confermato nella tua tabella: nessuna ricerca necessaria.")
        st.json(saved)
        if st.button("Rimuovi dalla mia tabella"):
            core.delete_confirmed(code)
            st.rerun()

    if st.button("Cerca" if not saved else "Cerca di nuovo", type="primary",
                 disabled=not ok):
        with st.spinner("Interrogo UPCitemdb…"):
            st.session_state["upc_res"] = core.upc_lookup(code)
            st.session_state["upc_code"] = code
    if not ok:
        st.warning("Cifra di controllo errata: controlla il codice.")

    res = st.session_state.get("upc_res")
    if res and st.session_state.get("upc_code") == code:
        st.header("2. Risultato UPCitemdb")
        if res["status"] != "ok":
            st.warning(f"Esito: {res['status']}")
        else:
            st.write({k: res.get(k) for k in ("title", "brand", "model", "category")})
            if res.get("from_cache"):
                st.caption("(dal cache locale)")

        st.header("3. Abbinamento con GameTDB")
        query = st.text_input("Titolo da cercare (modificabile)",
                              value=core.clean_title(res.get("title") or ""))

        if not games:
            st.info("Carica prima il file GameTDB dalla barra laterale.")
        elif query:
            hits = core.match_games(query, games, region=region, lang=lang)
            if not hits:
                st.warning("Nessun abbinamento. Prova a cambiare il titolo o a "
                           "togliere il filtro lingua (alcuni giochi PAL venduti "
                           "in Italia hanno solo l'inglese).")
            else:
                df = pd.DataFrame(hits)[["title", "serial", "id", "region",
                                         "languages", "score"]]
                df.columns = ["Titolo", "Seriale", "ID GameTDB", "Regione",
                              "Lingue", "Punteggio"]
                st.dataframe(df, use_container_width=True, hide_index=True)

                labels = [f"{h['title']} · {h['serial']} · {h['region']} "
                          f"· {h['languages']}" for h in hits]
                pick = st.selectbox("Qual è quello giusto?", range(len(hits)),
                                    format_func=lambda i: labels[i])
                if st.button("✅ Conferma e salva nella mia tabella"):
                    core.confirm_match(code, hits[pick], res.get("title"))
                    st.success("Salvato.")
                    st.rerun()

# ---------- scheda: i miei barcode ----------
with tab_mine:
    rows = core.list_confirmed()
    st.write(f"**{len(rows)}** barcode confermati.")
    if rows:
        df = pd.DataFrame(rows)
        st.dataframe(df, use_container_width=True, hide_index=True)
        st.download_button("Scarica CSV", df.to_csv(index=False),
                           file_name="miei_barcode.csv", mime="text/csv")
    st.info("Su Streamlit Community Cloud la tabella si azzera al riavvio "
            "dell'app: scarica il CSV ogni tanto e ricaricalo qui sotto.")
    csv_up = st.file_uploader("Ripristina da CSV", type=["csv"], key="csv_restore")
    if csv_up is not None and st.button("Importa"):
        n = core.import_confirmed(pd.read_csv(csv_up, dtype=str).fillna("")
                                  .to_dict("records"))
        st.success(f"{n} righe importate.")
        st.rerun()
