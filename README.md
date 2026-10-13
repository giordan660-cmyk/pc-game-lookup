# UPC → dati videogioco

App Streamlit: barcode → UPCitemdb (titolo) → GameTDB (seriale, regione, lingue).

## Avvio locale
    pip install -r requirements.txt
    streamlit run app.py

## Online (gratis)
Streamlit Community Cloud: carica questi file su un repository GitHub, poi
share.streamlit.io → New app → scegli repo e `app.py`.
Alternativa: Hugging Face Spaces (SDK Streamlit).
Nota: il cache SQLite su questi servizi è temporaneo e si azzera al riavvio.
