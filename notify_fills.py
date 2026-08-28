#!/usr/bin/env python3
"""
Hyperliquid -> Telegram fill notifier.

Interroga l'endpoint REST /info di Hyperliquid (userFillsByTime +
userTwapSliceFills) per un wallet, confronta i fill ricevuti con l'ultimo
stato salvato su disco e manda un messaggio Telegram per le nuove
esecuzioni trovate:

- Nuovo TWAP avviato (🆕): non appena un TWAP compare sull'account (anche
  prima che scatti la sua prima slice), un messaggio con coin, lato, size
  target e durata prevista -- se Hyperliquid espone questi dati in modo
  verificabile (best-effort: l'endpoint usato non e' documentato con
  certezza, in caso di dubbio la rilevazione viene semplicemente saltata).
  ECCEZIONE: un TWAP impostato con un trigger di prezzo parte con status
  "untriggered" (vedi is_untriggered_twap_status) -- non e' ancora
  davvero partito, quindi NON genera questa notifica finche' resta in
  quello stato. Quando poi il trigger scatta si manda invece "🎯 TWAP
  triggerato, e' partito" (stesso contenuto, vedi
  format_new_twap_message(triggered=True)) -- vedi main() per come i TWAP
  "in attesa" vengono tenuti d'occhio tra un controllo e l'altro.
- Ordini normali (limite, stop, mercato): notifica IMMEDIATA (🚨) ad ogni
  controllo se c'e' qualcosa di nuovo, con l'eseguito TOTALE dell'ordine
  (media prezzo, size totale) e il dettaglio delle singole esecuzioni sotto
  se sono state piu' di una nello stesso giro di controllo.
- Slice di ordini TWAP gia' avviati: NESSUNA notifica automatica ad ogni
  slice (sarebbero troppe, e cosi' anche i recap periodici che c'erano in
  precedenza). L'eseguito cumulato viene solo accumulato silenziosamente in
  stato; per vederlo si usa il comando Telegram /twap (vedi sotto).
- Riepilogo posizioni (🕓): automatico ogni
  POSITIONS_RECAP_INTERVAL_HOURS ore (variabile d'ambiente, default 2) --
  stesso contenuto della risposta al comando /positions (posizioni perps
  sopra la soglia "polvere" + saldi spot). Il primo riepilogo parte subito
  al primo avvio (nessuno stato precedente), poi ogni N ore da quello.

Comandi Telegram (a richiesta, nessun invio automatico):
  /twap                        PRIMA i TWAP "⏳ In attesa di trigger"
                                (impostati con una condizione di prezzo
                                non ancora scattata -- vedi
                                is_untriggered_twap_status -- quindi senza
                                ancora nessuna esecuzione, altrimenti
                                invisibili qui). POI un riepilogo
                                "▶️ Attivi/eseguiti" PER COIN/TICKER (un
                                blocco per coin, BUY e SELL separati -- non
                                un elenco di ogni singolo ordine TWAP):
                                eseguito cumulato, prezzo medio, % di
                                completamento se disponibile, differenza
                                rispetto al prezzo di mercato attuale. Un
                                TWAP su una coppia SPOT non "canonica"
                                mostra il ticker vero del token invece
                                dell'id posizionale grezzo tipo "@162"
                                (vedi resolve_twap_coin_display). I record
                                grezzi del best-effort endpoint storico
                                TWAP non vengono mai mostrati uno per uno
                                (rischio di superare il limite di
                                lunghezza di Telegram).
  /positions                   In testa, il valore NOZIONALE LORDO
                                stimato di TUTTE le posizioni PERPS
                                aperte in USDC (long e short sommati in
                                valore assoluto, senza compensarsi --
                                anche quelle "polvere" sotto, cosi' il
                                totale riflette l'intero portafoglio) E
                                l'ESPOSIZIONE NETTA (long meno short:
                                positiva se sei net long, negativa se net
                                short, zero se sei delta-neutrale),
                                affiancati dal saldo USDC del conto SPOT
                                per confronto -- sono due conti distinti
                                su Hyperliquid, quindi in generale NON
                                coincidono. Poi le posizioni PERPS aperte, ordinate per valore
                                nozionale USDC DECRESCENTE (sopra i
                                MIN_VALUE_USD_TO_SHOW USDC, default 10 --
                                quelle sotto sono considerate "polvere" e
                                omesse, con un conteggio di quante non sono
                                mostrate): size e direzione, valore
                                nozionale stimato in USDC (quando
                                disponibile), entry vs prezzo attuale, PnL
                                non realizzato, prezzo di liquidazione ed
                                eventuali ordini di
                                stop/take-profit collegati -- seguite da un
                                blocco separato con i saldi SPOT non nulli
                                (conto distinto dai perps su Hyperliquid,
                                anch'essi filtrati sotto i 10 USDC di
                                controvalore stimato, con conteggio di
                                quanti nascosti -- un saldo di cui non si
                                riesce a stimare il controvalore resta
                                sempre visibile).
  /alert <COIN> <sopra|sotto> <VALORE|VALORE%>
                                Imposta un alert di prezzo per QUALSIASI
                                coin quotata su Hyperliquid, sia PERPS che
                                SPOT (non solo quelle con una posizione
                                aperta) -- per i ticker spot non
                                "canonici" la chiave di prezzo viene
                                risolta automaticamente via spotMeta (vedi
                                resolve_spot_mid_key). VALORE puo' essere
                                un prezzo assoluto ("/alert BTC sotto
                                65000") oppure una percentuale ("/alert BTC
                                sotto 5%"), calcolata rispetto al prezzo di
                                mercato attuale nel momento in cui l'alert
                                viene creato (accetta anche "<"/">" al
                                posto di sotto/sopra). La conferma include
                                anche l'elenco aggiornato di tutti gli
                                alert attivi.
  /newalert                     Crea un alert in modo guidato: il bot manda
                                un messaggio-prompt (con "Rispondi"/"Reply"
                                gia' pronto su Telegram) e basta scrivere
                                "<COIN> <sopra|sotto> <VALORE|VALORE%>" in
                                risposta, senza dover ricordare /alert.
  /alerts                       Elenca gli alert di prezzo attivi, col
                                prezzo attuale del token accanto a ognuno
                                quando disponibile (stessa risoluzione
                                spot di /alert).
  /delalert <id>                Rimuove un alert (l'id si vede con /alerts).
                                La conferma include anche l'elenco
                                aggiornato degli alert rimasti attivi.
I comandi vengono letti tramite polling (Telegram getUpdates) alla stessa
frequenza con cui gira lo script: la risposta arriva quindi al PROSSIMO
controllo programmato, non istantaneamente (fino a qualche minuto di
attesa, a seconda di quanto spesso e' schedulato il workflow) -- con
/newalert questo vale per OGNI passaggio (prompt e poi risposta), quindi
con un intervallo lungo puo' richiedere un paio di giri. Per sicurezza
vengono processati solo i messaggi che arrivano dalla chat configurata in
TELEGRAM_CHAT_ID: comandi da altre chat vengono ignorati.

Alert di prezzo (🔔): ad ogni controllo, se ci sono alert attivi, il
prezzo attuale (allMids) viene confrontato con la soglia impostata. Cosa
succede quando scatta dipende dal fatto che tu POSSIEDA o meno quella coin
in quel momento, sia come posizione PERPS aperta sia come saldo SPOT non
nullo (una qualunque delle due basta -- se c'e' una posizione perps viene
mostrata quella nel riepilogo, piu' ricca di dettagli, ma l'alert resta
"sticky" anche solo con lo spot):
- SENZA ne' posizione ne' saldo spot: notifica una volta sola e l'alert
  viene rimosso (va reimpostato con /alert se lo si vuole di nuovo attivo)
  -- come prima.
- CON una posizione perps e/o un saldo spot: la notifica include anche un
  riepilogo (posizione: direzione, size, entry vs prezzo attuale, PnL,
  prezzo di liquidazione; saldo spot: quantita' e controvalore stimato) e
  l'alert NON viene rimosso -- il primo avviso e' sempre immediato, poi si
  ripete come "promemoria" ogni ALERT_REPEAT_EVERY_N_RUNS controlli
  (variabile d'ambiente, default 5 -- non piu' ad ogni singolo giro, per
  non intasare la chat) finche' la condizione resta vera E possiedi ancora
  quella coin, in un modo o nell'altro. QUESTO THROTTLE VALE PERO' SOLO SE
  IL PREZZO RESTA VICINO ALLA SOGLIA: se lo scarto percentuale tra prezzo
  attuale e soglia supera ALERT_URGENT_DEVIATION_PCT (variabile
  d'ambiente, default 8%), la situazione e' considerata urgente e si
  torna a notificare ad OGNI singolo giro finche' resta cosi'. Quando il
  prezzo torna oltre la soglia arriva un ultimo messaggio ("✅ Allarme
  rientrato") ma l'alert RESTA ATTIVO (mantenuto): torna solo "in
  ascolto" (e il contatore si azzera) e puo' scattare di nuovo in futuro
  senza doverlo reimpostare. Si disattiva per davvero (rimosso, "🔕 Alert
  disattivato") solo quando non possiedi piu' quella coin ne' come
  posizione ne' come saldo spot -- a quel punto non c'e' piu' nulla da
  proteggere.

Ogni messaggio del bot porta anche una tastiera Telegram persistente
("/twap", "/positions", "/alerts", "/newalert") cosi' i comandi piu'
comuni si possono richiamare con un tocco invece di scriverli a mano;
scrivere "/start" al bot manda un messaggio di benvenuto che la mostra
anche a chat vuota.

Ogni notifica AUTOMATICA (fill/nuovi TWAP, alert di prezzo, riepilogo
posizioni) e' preceduta dalla propria intestazione, con titolo E colore
distinti per categoria (vedi UPDATE_HEADER_TITLE_BY_KIND /
UPDATE_HEADER_BAR_BY_KIND) cosi' si riconoscono a colpo d'occhio anche
senza leggere il testo: 🟥 "Aggiornamento Ordini Eseguiti" per
ordini/fill/nuovi TWAP, 🟧 "Aggiornamento Alerts" per gli alert di
prezzo, 🟩 "Aggiornamento Generale" per il riepilogo posizioni periodico.
Per gli alert, il titolo include anche il ticker a cui si riferiscono
(es. "🕐 Aggiornamento Alerts su: PURR"), cliccabile come il resto dei
ticker del bot -- utile per distinguerli a colpo d'occhio quando ci sono
piu' alert attivi. Le risposte ai comandi Telegram (es. /twap,
/positions, /alert) restano
invece precedute da una semplice riga separatrice neutra, per restare
visivamente distinte senza per questo sembrare una notifica automatica.

Fascia notturna (QUIET_HOURS_START_HOUR-QUIET_HOURS_END_HOUR, default
22:00-08:00 fuso DISPLAY_TIMEZONE = Europe/Paris -- vedi is_quiet_hours):
le notifiche automatiche "di aggiornamento" (fill/ordini, nuovi TWAP,
riepilogo posizioni) vengono trattenute e mandate al primo giro utile
fuori da questa fascia, invece che nel cuore della notte -- non vengono
perse, solo rimandate. Gli ALERT DI PREZZO fanno eccezione e vengono
sempre mandati subito, a qualunque ora: sono avvisi protettivi e non ha
senso rimandarli. I comandi Telegram (es. /twap, /positions) restano
sempre disponibili a richiesta, in qualunque momento.

Ogni ticker citato (in comandi E notifiche automatiche: /twap, /positions,
/alerts, alert scattati/rientrati, fill, nuovi TWAP) e' un link cliccabile
verso la pagina di trading/grafico del token su Hyperliquid, con il nome
del ticker stesso come testo del link (es. "BTC" diventa un link chiamato
"BTC", non testo semplice) -- vedi ticker_mention/render_ticker_links/
build_hyperliquid_chart_url. Per questo i messaggi vengono inviati con
parse_mode="HTML" (Telegram): il rendering dei link avviene SOLO dentro
send_telegram_message, dopo il troncamento per il limite di lunghezza (cosi'
un troncamento non spezza mai un tag HTML a meta') e con html.escape() di
tutto il resto del testo (compresi eventuali messaggi di errore o altro
testo esterno interpolato), cosi' non e' mai possibile che del testo
imprevisto rompa l'HTML o generi un link non voluto. Le URL usate sono
best-effort: per i PERPS e' confermato il formato
https://app.hyperliquid.xyz/trade/{COIN}; per lo SPOT e' confermato solo
per le coppie "canoniche" (es. PURR/USDC, HYPE/USDC) nella forma
https://app.hyperliquid.xyz/trade/{COIN}/USDC -- per i ticker spot NON
canonici (la maggioranza, es. CAT) lo stesso formato viene usato per
estrapolazione ma NON e' stato possibile verificarlo dal vivo in questo
ambiente di sviluppo (nessun accesso al browser): se un link spot non
canonico apre il mercato sbagliato, va segnalato.

Pensato per girare periodicamente (es. ogni 5-10 minuti via GitHub Actions
schedulato / trigger esterno), non come processo always-on.

Variabili d'ambiente richieste:
  HL_WALLET_ADDRESS     Indirizzo pubblico del wallet Hyperliquid (0x...)
  TELEGRAM_BOT_TOKEN    Token del bot ottenuto da @BotFather
  TELEGRAM_CHAT_ID      Chat id (utente, gruppo o canale) a cui scrivere

Variabili opzionali:
  STATE_FILE            Percorso del file di stato (default: state/last_fill.json)
  LOOKBACK_MINUTES       Alla primissima esecuzione (nessuno stato salvato),
                         quanti minuti indietro guardare (default: 15).
                         Evita di notificare tutta la storia del wallet al
                         primo run.
"""

import html
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
from collections import defaultdict
from datetime import datetime

HL_INFO_URL = "https://api.hyperliquid.xyz/info"
TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
TELEGRAM_GETUPDATES_URL = "https://api.telegram.org/bot{token}/getUpdates"
# Pagina di trading/grafico su Hyperliquid per un ticker (vedi
# build_hyperliquid_chart_url/ticker_mention piu' sotto).
HYPERLIQUID_TRADE_URL_BASE = "https://app.hyperliquid.xyz/trade"

DEFAULT_STATE_FILE = "state/last_fill.json"
DEFAULT_LOOKBACK_MINUTES = 15
# Una volta che un alert su una posizione aperta e' "scattato" (triggered),
# invece di rimandare la notifica ad ogni singolo giro (ogni minuto), la
# ripetiamo solo ogni N giri, per non intasare la chat. Il primo avviso
# resta immediato; solo i "promemoria" successivi vengono diradati.
DEFAULT_ALERT_REPEAT_EVERY_N_RUNS = 10
# Il throttle a N giri sopra vale solo se il prezzo resta "vicino" alla
# soglia (entro questa percentuale, calcolata rispetto alla soglia
# stessa): se invece se ne allontana di piu' (condizione piu' severa),
# consideriamo la situazione urgente e torniamo a notificare ad ogni giro,
# throttle o no.
DEFAULT_ALERT_URGENT_DEVIATION_PCT = 8.0
# Ogni quante ore mandare in automatico un riepilogo delle posizioni
# (stesso contenuto della risposta a /positions, incl. saldi spot e
# filtro "polvere" sulle posizioni perps) senza doverlo chiedere a mano.
DEFAULT_POSITIONS_RECAP_INTERVAL_HOURS = 2.0

# Fuso orario in cui mostrare l'intestazione di aggiornamento e in cui e'
# calcolata la fascia notturna qui sotto. Se il database IANA non e'
# disponibile sul runner, si ricade su UTC senza far fallire lo script
# (vedi format_update_header / is_quiet_hours).
DISPLAY_TIMEZONE = "Europe/Paris"

# Fascia notturna (fuso DISPLAY_TIMEZONE) durante la quale le notifiche
# automatiche "di aggiornamento" -- ordini/fill, nuovi TWAP, riepilogo
# posizioni -- vengono trattenute invece di essere mandate subito (vedi
# is_quiet_hours e il suo uso in main()): non vanno perse, vengono solo
# rimandate al primo giro utile fuori da questa fascia. Gli ALERT di
# prezzo NON sono soggetti a questa restrizione: sono avvisi protettivi e
# vanno mandati a qualunque ora, notte compresa.
QUIET_HOURS_START_HOUR = 22  # incluso: dalle 22:00...
QUIET_HOURS_END_HOUR = 8  # ...escluso: fino alle 8:00 (non incluse)

ITALIAN_MONTHS = [
    "gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno",
    "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre",
]

# Nomi di "type" candidati per recuperare lo stato dei TWAP (size totale,
# eseguito, durata). Non c'e' modo di verificare con certezza quale sia
# quello corretto senza un test dal vivo, quindi li proviamo in ordine e
# usiamo il primo che risponde con dati sensati. Se nessuno funziona, la
# percentuale di completamento viene semplicemente omessa dai messaggi.
TWAP_STATE_TYPE_CANDIDATES = ["userTwapHistory", "twapHistory"]

# Riga divisoria messa in cima ad ogni messaggio Telegram, cosi' notifiche
# ravvicinate restano visivamente distinte invece di confondersi.
MESSAGE_SEPARATOR = "➖➖➖➖➖➖➖➖➖➖➖➖➖➖➖➖➖➖➖➖"

# Tastiera persistente con i due comandi, allegata di default ad ogni
# messaggio mandato dal bot: al tocco Telegram invia il testo del pulsante
# come un normale messaggio, che viene interpretato allo stesso modo di un
# comando digitato a mano (nessuna logica separata da gestire).
COMMAND_KEYBOARD = {
    "keyboard": [["/twap", "/positions"], ["/alerts", "/newalert"]],
    "resize_keyboard": True,
    "is_persistent": True,
}

# Direzioni accettate nel comando /alert, in italiano e come simboli.
ALERT_DIRECTION_ALIASES = {
    "sotto": "below", "under": "below", "below": "below", "<": "below",
    "sopra": "above", "over": "above", "above": "above", ">": "above",
}

# Testo (unico, riconoscibile) usato nel messaggio-prompt mandato dal
# pulsante "/newalert": quando arriva una risposta (reply) a un messaggio
# che lo contiene, main() sa che va interpretata come "<coin> <direzione>
# <valore>" invece che come comando.
ALERT_PROMPT_MARKER = "✏️ Nuovo alert"

# Forza la comparsa della tastiera di risposta rapida di Telegram (con un
# suggerimento di formato), al posto della tastiera fissa COMMAND_KEYBOARD,
# solo per il messaggio-prompt di /newalert.
ALERT_PROMPT_REPLY_MARKUP = {
    "force_reply": True,
    "input_field_placeholder": "es. BTC sotto 65000",
    "selective": True,
}

# Barre piu' vistose (Telegram non supporta colori nel testo dei messaggi
# dei bot, quindi si usano emoji a blocco colorato) usate come intestazione
# delle notifiche AUTOMATICHE (vedi format_update_header), colorate per
# categoria cosi' si riconoscono a colpo d'occhio anche senza leggere il
# testo: rosso per ordini/fill/nuovi TWAP, arancione per gli alert di
# prezzo, verde per il riepilogo posizioni periodico. Le risposte ai
# comandi Telegram (es. /twap, /positions) NON passano da qui: usano il
# separatore neutro MESSAGE_SEPARATOR.
UPDATE_HEADER_BAR_ORDERS = "🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥"
UPDATE_HEADER_BAR_ALERTS = "🟧🟧🟧🟧🟧🟧🟧🟧🟧🟧🟧🟧🟧🟧🟧🟧🟧🟧🟧🟧"
UPDATE_HEADER_BAR_POSITIONS = "🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩"
UPDATE_HEADER_BAR_BY_KIND = {
    "orders": UPDATE_HEADER_BAR_ORDERS,
    "alerts": UPDATE_HEADER_BAR_ALERTS,
    "positions": UPDATE_HEADER_BAR_POSITIONS,
}

# Titolo dell'intestazione delle notifiche AUTOMATICHE, distinto per
# categoria (stessa chiave "kind" usata per UPDATE_HEADER_BAR_BY_KIND --
# vedi format_update_header): cosi' oltre al colore della barra anche il
# testo del titolo distingue a colpo d'occhio ordini/fill/nuovi TWAP,
# alert di prezzo e riepilogo posizioni periodico.
UPDATE_HEADER_TITLE_BY_KIND = {
    "orders": "🕐 Aggiornamento Ordini Eseguiti",
    "alerts": "🕐 Aggiornamento Alerts",
    "positions": "🕐 Aggiornamento Generale",
}


def env_or_die(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        print(f"ERRORE: variabile d'ambiente mancante: {name}", file=sys.stderr)
        sys.exit(1)
    return value


def http_post_json(url: str, payload: dict, headers: dict | None = None) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code} da {url}: {detail}") from e


def fetch_fills(wallet: str, start_time_ms: int, end_time_ms: int) -> list:
    payload = {
        "type": "userFillsByTime",
        "user": wallet,
        "startTime": start_time_ms,
        "endTime": end_time_ms,
        "aggregateByTime": False,
    }
    result = http_post_json(HL_INFO_URL, payload)
    if not isinstance(result, list):
        raise RuntimeError(f"Risposta inattesa da Hyperliquid: {result!r}")
    return result


def fetch_twap_slice_fills(wallet: str, start_time_ms: int, end_time_ms: int) -> list:
    """Le esecuzioni di ordini TWAP NON compaiono in userFills/userFillsByTime:
    Hyperliquid le espone solo tramite questo endpoint separato, con uno
    schema annidato ({"fill": {...}, "twapId": N})."""
    payload = {
        "type": "userTwapSliceFillsByTime",
        "user": wallet,
        "startTime": start_time_ms,
        "endTime": end_time_ms,
    }
    result = http_post_json(HL_INFO_URL, payload)
    if not isinstance(result, list):
        raise RuntimeError(f"Risposta inattesa da Hyperliquid (TWAP): {result!r}")

    fills = []
    for item in result:
        fill = dict(item.get("fill") or {})
        if not fill:
            continue
        fill["twapId"] = item.get("twapId")
        fills.append(fill)
    return fills


def fetch_all_mids() -> dict:
    """Prezzi mid correnti per (quasi) tutte le coin. Best-effort: in caso
    di errore ritorna un dizionario vuoto, cosi' i messaggi funzionano
    comunque senza la riga "prezzo attuale"."""
    try:
        result = http_post_json(HL_INFO_URL, {"type": "allMids"})
        if isinstance(result, dict):
            return result
    except Exception as e:
        print(f"AVVISO: impossibile recuperare i prezzi correnti (allMids): {e}", file=sys.stderr)
    return {}


def fetch_twap_records(wallet: str) -> list:
    """Ritorna la lista (normalizzata, SENZA deduplicare per twap_id --
    vedi build_twap_records_by_id per quello) dei TWAP dell'utente --
    attivi e passati -- con qualunque campo si riesca a interpretare in
    modo affidabile: twap_id, coin, side, total_sz (size target),
    executed_sz/executed_ntl (size/nozionale eseguiti finora -- per il
    record piu' recente di un TWAP concluso, vedi build_twap_records_by_id,
    questi sono gia' i valori FINALI riportati dall'API stessa, piu'
    affidabili di twap_progress se il bot non ha osservato ogni singola
    esecuzione), minutes (durata), status, status_raw, start_ms,
    record_time_ms, trigger (vedi extract_twap_trigger_info -- il prezzo
    soglia per un TWAP con status "waitingForTrigger", None per un TWAP
    senza trigger di prezzo).

    SCHEMA CONFERMATO (non piu' solo ipotizzato -- verificato dall'utente
    interrogando direttamente l'endpoint "twapHistory" con il proprio
    account, il 25/08/2026): ogni voce grezza ha la forma
        {"time": <secondi Unix>, "twapId": <int>,
         "state": {"coin", "side", "sz", "executedSz", "executedNtl",
                    "minutes", "timestamp" (MILLISECONDI!), "randomize",
                    "reduceOnly", "user", ...},
         "status": {"status": <stringa>, "description"?: <stringa>}}
    e per OGNI singolo TWAP (stesso twapId) l'endpoint restituisce PIU' di
    una voce: almeno una per quando e' stato "activated" (executedSz
    ancora a 0) e -- se nel frattempo si e' concluso -- una successiva con
    lo status finale ("finished" o "terminated", confermati entrambi
    realmente in uso). "state.timestamp" resta IDENTICO su tutte le voci
    dello stesso twapId (e' l'istante di attivazione originale, in
    millisecondi) mentre "time" (in SECONDI, non millisecondi -- occhio
    alla scala diversa dagli altri campi!) e' l'istante in cui QUELLA
    specifica voce di storico e' stata registrata, quindi cresce da una
    voce alla successiva per lo stesso twapId. "start_ms" qui sotto usa
    sempre state.timestamp (gia' in ms, costante) quando disponibile;
    "record_time_ms" e' invece time*1000, usato SOLO da
    build_twap_records_by_id per scegliere la voce piu' recente tra i
    duplicati dello stesso twapId (quindi tipicamente quella con lo status
    finale, se esiste).

    "status" e' una stringa gia' interpretata quando possibile: gestisce
    sia il caso confermato -- un oggetto {"status": <stringa>,
    "description"?: ...} -- sia, difensivamente, un oggetto "taggato" con
    una singola chiave stringa (pattern comune in altre API Rust/serde,
    mai osservato qui ma gestito per sicurezza) sia una stringa semplice
    diretta. "status_raw" conserva sempre il valore originale non
    processato, cosi' se "status" risulta None (forma non riconosciuta)
    resta comunque visibile per diagnosi (vedi format_twap_status_message)
    invece di sparire silenziosamente.

    Il nome esatto di questo endpoint (si provano piu' candidati, vedi
    TWAP_STATE_TYPE_CANDIDATES) resta best-effort/non ufficialmente
    documentato da Hyperliquid, ma lo schema sopra e' ora confermato
    empiricamente per il candidato "twapHistory". Ritorna [] se nessun
    candidato risponde in modo utilizzabile -- in tal caso sia il
    rilevamento "nuovo TWAP" sia la % di completamento nei messaggi di
    stato vengono semplicemente omessi, senza far fallire il resto della
    notifica."""
    for type_name in TWAP_STATE_TYPE_CANDIDATES:
        try:
            result = http_post_json(HL_INFO_URL, {"type": type_name, "user": wallet})
        except Exception:
            continue
        raw_records = result if isinstance(result, list) else result.get("records") if isinstance(result, dict) else None
        if not isinstance(raw_records, list):
            continue

        parsed = []
        for record in raw_records:
            if not isinstance(record, dict):
                continue
            state = record.get("state") if isinstance(record.get("state"), dict) else record
            twap_id = record.get("twapId", record.get("id", state.get("twapId")))
            if twap_id is None:
                continue
            status_raw = record.get("status")
            status = None
            if isinstance(status_raw, str):
                status = status_raw
            elif isinstance(status_raw, dict):
                if isinstance(status_raw.get("status"), str):
                    status = status_raw.get("status")
                else:
                    # Difensivo, mai osservato con questo endpoint (vedi
                    # docstring sopra) ma innocuo da gestire: un oggetto
                    # "taggato" con una singola chiave stringa, es.
                    # {"finished": {...}}, usa quella chiave come status.
                    # Mai indovinato se ci sono piu' chiavi o zero chiavi.
                    str_keys = [k for k in status_raw.keys() if isinstance(k, str)]
                    if len(str_keys) == 1:
                        status = str_keys[0]
            # start_ms: SEMPRE da state.timestamp (gia' in millisecondi,
            # costante per tutte le voci dello stesso twapId -- l'istante
            # di attivazione vero) quando disponibile; altrimenti da
            # record["time"] convertito da secondi a millisecondi (*1000
            # -- NON va usato cosi' com'e', e' in una scala diversa).
            state_timestamp_ms = state.get("timestamp")
            record_time_sec = record.get("time")
            if isinstance(state_timestamp_ms, (int, float)):
                start_ms = state_timestamp_ms
            elif isinstance(record_time_sec, (int, float)):
                start_ms = record_time_sec * 1000
            else:
                start_ms = None
            record_time_ms = (record_time_sec * 1000) if isinstance(record_time_sec, (int, float)) else start_ms
            parsed.append(
                {
                    "twap_id": twap_id,
                    "coin": state.get("coin"),
                    "side": state.get("side"),
                    "total_sz": state.get("sz"),
                    "executed_sz": state.get("executedSz"),
                    "executed_ntl": state.get("executedNtl"),
                    "minutes": state.get("minutes"),
                    "status": status,
                    "status_raw": status_raw,
                    "start_ms": start_ms,
                    "record_time_ms": record_time_ms,
                    "trigger": state.get("trigger"),
                }
            )
        if parsed:
            return parsed
    return []


def build_twap_records_by_id(twap_records: list) -> dict:
    """Costruisce il dizionario twap_id (stringa) -> record piu'
    aggiornato, a partire dalla lista grezza (non deduplicata) di
    fetch_twap_records. Necessario perche' l'endpoint "twapHistory"
    restituisce PIU' voci per lo stesso twap_id (una per "activated", una
    per lo status finale se il TWAP si e' gia' concluso -- vedi la
    docstring di fetch_twap_records per i dettagli, confermati
    empiricamente il 25/08/2026): un semplice
    "{r['twap_id']: r for r in twap_records}" prenderebbe l'ULTIMA voce
    nell'ordine restituito dall'API per quel twap_id, che empiricamente e'
    quasi sempre quella "activated" (perche' l'API restituisce le voci
    ordinate per "time" decrescente, quindi lo status finale -- che ha un
    "time" maggiore, essendo successivo -- compare PRIMA, e "activated"
    compare DOPO, sovrascrivendo la voce buona) -- questo era la causa
    reale per cui TWAP gia' completati/terminati continuavano a essere
    mostrati come ancora attivi. Qui invece si tiene esplicitamente, per
    ogni twap_id, la voce con "record_time_ms" piu' alto (la piu' recente
    in assoluto), che quindi e' lo status finale quando esiste."""
    by_id = {}
    for r in twap_records:
        tid = str(r.get("twap_id"))
        existing = by_id.get(tid)
        if existing is None or (r.get("record_time_ms") or 0) >= (existing.get("record_time_ms") or 0):
            by_id[tid] = r
    return by_id


def fetch_clearinghouse_state(wallet: str) -> dict:
    """Stato del conto perps: posizioni aperte, margine, ecc. Best-effort:
    in caso di errore ritorna un dizionario vuoto (il comando /positions
    lo segnala invece di mostrare dati inventati)."""
    try:
        result = http_post_json(HL_INFO_URL, {"type": "clearinghouseState", "user": wallet})
        if isinstance(result, dict):
            return result
    except Exception as e:
        print(f"AVVISO: impossibile recuperare le posizioni (clearinghouseState): {e}", file=sys.stderr)
    return {}


def fetch_spot_state(wallet: str) -> dict:
    """Stato del conto SPOT (saldi token, distinti dalle posizioni perps con
    leva di clearinghouseState). Best-effort: in caso di errore ritorna un
    dizionario vuoto (il blocco spot di /positions viene semplicemente
    omesso, invece di mostrare dati inventati)."""
    try:
        result = http_post_json(HL_INFO_URL, {"type": "spotClearinghouseState", "user": wallet})
        if isinstance(result, dict):
            return result
    except Exception as e:
        print(f"AVVISO: impossibile recuperare i saldi spot (spotClearinghouseState): {e}", file=sys.stderr)
    return {}


def fetch_spot_meta() -> dict:
    """Metadati dei token/coppie spot (spotMeta): serve per risolvere un
    ticker spot (es. "CAT") alla chiave usata per lui in allMids -- vedi
    resolve_spot_mid_key, perche' per lo spot allMids NON e' indicizzato
    per ticker come per i perps. Best-effort: in caso di errore ritorna un
    dizionario vuoto (la risoluzione viene semplicemente saltata)."""
    try:
        result = http_post_json(HL_INFO_URL, {"type": "spotMeta"})
        if isinstance(result, dict):
            return result
    except Exception as e:
        print(f"AVVISO: impossibile recuperare i metadati spot (spotMeta): {e}", file=sys.stderr)
    return {}


def resolve_spot_mid_key(coin: str, spot_meta: dict):
    """Dato un ticker spot (es. "CAT"), ritorna la chiave da usare per
    cercarne il prezzo in allMids (vedi fetch_all_mids/get_price_for_coin).

    Su Hyperliquid allMids NON indicizza le coppie spot per nome del
    token: la coppia "canonica" (al momento solo PURR/USDC) usa il nome
    leggibile "PURR/USDC", tutte le altre usano un id posizionale
    "@<indice della coppia>" (documentato da Hyperliquid, non verificabile
    con accesso di rete da qui). Bisogna quindi passare da spotMeta:
    1. trovare il token per nome nell'array "tokens" -> il suo indice;
    2. trovare in "universe" la coppia che contiene quell'indice -> il suo
       "name" e' la chiave giusta in allMids.
    Ritorna None se il ticker non viene trovato o spot_meta non e'
    interpretabile -- mai una chiave indovinata."""
    if not isinstance(spot_meta, dict):
        return None
    tokens = spot_meta.get("tokens")
    universe = spot_meta.get("universe")
    if not isinstance(tokens, list) or not isinstance(universe, list):
        return None
    token_index = None
    for t in tokens:
        if isinstance(t, dict) and t.get("name") == coin:
            token_index = t.get("index")
            break
    if token_index is None:
        return None
    for pair in universe:
        if not isinstance(pair, dict):
            continue
        pair_tokens = pair.get("tokens")
        if isinstance(pair_tokens, list) and token_index in pair_tokens:
            name = pair.get("name")
            if name:
                return name
    return None


def get_price_for_coin(coin: str, mids: dict, spot_meta: dict | None = None):
    """Prezzo attuale (mid) per un ticker, sia PERPS (chiave diretta in
    allMids, es. "BTC") sia SPOT: se il ticker non si trova direttamente e
    spot_meta e' disponibile, prova a risolverlo con resolve_spot_mid_key
    prima di arrendersi. Ritorna un float, o None se il prezzo non si
    riesce a determinare in nessuno dei due modi (mai un valore
    indovinato)."""
    if not isinstance(mids, dict):
        return None
    raw = mids.get(coin)
    if raw is None and spot_meta:
        resolved_key = resolve_spot_mid_key(coin, spot_meta)
        if resolved_key is not None:
            raw = mids.get(resolved_key)
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def resolve_spot_ticker_name(raw_coin: str, spot_meta: dict):
    """Inverso di resolve_spot_mid_key: dato un identificativo di coppia
    spot come appare gia' nel campo "coin" dei fill/esecuzioni TWAP (es.
    "@162" per una coppia non "canonica", vedi resolve_spot_mid_key),
    risale al ticker leggibile del token BASE (es. "CAT") tramite
    spotMeta. Necessario perche' per lo spot Hyperliquid restituisce i
    fill/TWAP gia' etichettati con l'id posizionale della coppia, non col
    ticker del token -- altrimenti illeggibile per l'utente. Ritorna None
    se non risolvibile (spot_meta assente/malformato, coppia non
    trovata, o il token base non ha un nome) -- mai un nome indovinato;
    vedi resolve_twap_coin_display per come il chiamante gestisce
    l'insuccesso."""
    if not isinstance(spot_meta, dict) or not raw_coin:
        return None
    tokens = spot_meta.get("tokens")
    universe = spot_meta.get("universe")
    if not isinstance(tokens, list) or not isinstance(universe, list):
        return None
    for pair in universe:
        if not isinstance(pair, dict) or pair.get("name") != raw_coin:
            continue
        pair_tokens = pair.get("tokens")
        if not isinstance(pair_tokens, list) or not pair_tokens:
            return None
        base_index = pair_tokens[0]
        for t in tokens:
            if isinstance(t, dict) and t.get("index") == base_index:
                name = t.get("name")
                return name if isinstance(name, str) and name else None
        return None
    return None


def resolve_twap_coin_display(raw_coin, spot_meta: dict | None):
    """Interpreta il campo "coin" grezzo di un fill/TWAP e ritorna
    (nome_da_mostrare, kind, linkabile):
    - ticker perps "normale" (nessun "@" ne' "/" nel valore): ritornato
      cosi' com'e', kind="perp", linkabile=True -- caso comune, nessuna
      risoluzione necessaria.
    - id di coppia spot ("@N", o gia' un nome tipo "TOKEN/USDC") risolto
      con successo via resolve_spot_ticker_name: (nome_token, "spot",
      True).
    - id di coppia spot NON risolto (spot_meta assente o coppia/token non
      trovati): (raw_coin, "spot", False) -- si mostra comunque il
      valore grezzo (mai nasconderlo), ma senza renderlo un link
      cliccabile, perche' un URL costruito sull'id posizionale grezzo
      punterebbe quasi certamente al mercato sbagliato o a un errore.
    Usato da format_twap_status_message e format_new_twap_message per
    evitare di mostrare direttamente identificativi come "@162" senza
    nessun contesto (vedi anche resolve_spot_mid_key per il problema
    analogo sui prezzi)."""
    if not isinstance(raw_coin, str) or not raw_coin:
        return (raw_coin, "perp", False)
    looks_like_spot_id = raw_coin.startswith("@") or "/" in raw_coin
    if not looks_like_spot_id:
        return (raw_coin, "perp", True)
    resolved = resolve_spot_ticker_name(raw_coin, spot_meta) if spot_meta else None
    if resolved:
        return (resolved, "spot", True)
    return (raw_coin, "spot", False)


def fetch_open_orders(wallet: str) -> list:
    """Ordini aperti (incluso stop-loss/take-profit con trigger). Best-effort:
    in caso di errore ritorna una lista vuota."""
    try:
        result = http_post_json(HL_INFO_URL, {"type": "frontendOpenOrders", "user": wallet})
        if isinstance(result, list):
            return result
    except Exception as e:
        print(f"AVVISO: impossibile recuperare gli ordini aperti (frontendOpenOrders): {e}", file=sys.stderr)
    return []


def fetch_telegram_updates(bot_token: str, offset: int) -> list:
    """Legge i messaggi in arrivo al bot (polling, non webhook) a partire da
    `offset` (update_id gia' processati + 1), cosi' Telegram non li
    ri-manda ai giri successivi. Best-effort: in caso di errore ritorna una
    lista vuota, i comandi verranno ripescati al prossimo giro."""
    url = TELEGRAM_GETUPDATES_URL.format(token=bot_token)
    payload = {"offset": offset, "timeout": 0, "allowed_updates": ["message"]}
    try:
        result = http_post_json(url, payload)
    except Exception as e:
        print(f"AVVISO: impossibile leggere i comandi Telegram: {e}", file=sys.stderr)
        return []
    if not isinstance(result, dict) or not result.get("ok"):
        return []
    updates = result.get("result")
    return updates if isinstance(updates, list) else []


def normalize_command(text) -> str:
    """Normalizza il testo di un messaggio Telegram al nome del comando
    (senza "/" iniziale, senza eventuale "@nomebot", minuscolo). Ritorna ""
    se il testo non e' un comando."""
    if not isinstance(text, str) or not text.strip():
        return ""
    first_token = text.strip().split()[0]
    if not first_token.startswith("/"):
        return ""
    first_token = first_token[1:].split("@", 1)[0]
    return first_token.lower()


def command_args(text) -> str:
    """Ritorna il testo di un messaggio Telegram meno il primo token (il
    comando stesso), es. "/alert BTC sotto 65000" -> "BTC sotto 65000"."""
    if not isinstance(text, str):
        return ""
    parts = text.strip().split(maxsplit=1)
    return parts[1] if len(parts) > 1 else ""


def parse_price_alert_command(args_text: str):
    """Interpreta gli argomenti del comando /alert (es. "BTC sotto 65000",
    "ETH > 4000" o "BTC sotto 5%") e ritorna (coin, direction, value,
    is_percent) con direction gia' normalizzata a "below"/"above". Se
    is_percent e' True, value e' una percentuale (da interpretare rispetto
    all'entry price di una posizione aperta -- vedi main()), altrimenti e'
    un prezzo assoluto. Solleva ValueError con un messaggio gia' pronto da
    rimandare all'utente se il formato non e' valido -- mai un valore
    indovinato in caso di dubbio."""
    parts = args_text.split()
    if len(parts) < 3:
        raise ValueError(
            "Formato non valido. Usa: /alert <COIN> <sopra|sotto> <VALORE>\n"
            "VALORE puo' essere un prezzo assoluto o una percentuale (con %) "
            "rispetto all'entry di una tua posizione aperta su quella coin.\n"
            "Esempi: /alert BTC sotto 65000  —  /alert ETH > 4000  —  /alert BTC sotto 5%"
        )
    coin = parts[0].upper()
    direction = ALERT_DIRECTION_ALIASES.get(parts[1].lower())
    if direction is None:
        raise ValueError(f"Direzione '{parts[1]}' non riconosciuta. Usa 'sopra'/'sotto' oppure '>'/'<'.")
    value_raw = parts[2]
    is_percent = value_raw.endswith("%")
    number_part = value_raw[:-1] if is_percent else value_raw
    try:
        value = float(number_part.replace(",", "."))
    except ValueError:
        raise ValueError(f"Valore '{value_raw}' non valido.")
    if value <= 0:
        raise ValueError("Il valore deve essere maggiore di zero.")
    return coin, direction, value, is_percent


def extract_open_positions(clearinghouse_state: dict) -> list:
    """Ritorna la lista di posizioni aperte (size != 0) da clearinghouseState,
    con parsing difensivo per gestire sia il caso in cui i campi della
    posizione siano annidati sotto "position" sia il caso in cui siano
    diretti (schema non verificabile con certezza -- vedi
    format_positions_message). Usata sia dal comando /positions sia per
    calcolare l'entry price quando si imposta un alert in percentuale."""
    asset_positions = clearinghouse_state.get("assetPositions") if isinstance(clearinghouse_state, dict) else None
    if not isinstance(asset_positions, list):
        return []
    positions = []
    for item in asset_positions:
        if not isinstance(item, dict):
            continue
        pos = item.get("position") if isinstance(item.get("position"), dict) else item
        if not isinstance(pos, dict):
            continue
        try:
            szi = float(pos.get("szi", 0))
        except (TypeError, ValueError):
            szi = 0.0
        if szi == 0:
            continue
        positions.append(pos)
    return positions


# Sotto questa soglia (in USDC di controvalore stimato) sia una posizione
# perps sia un saldo spot vengono considerati "polvere" e omessi dai
# rispettivi blocchi ("Posizioni aperte" e "Saldi spot") di /positions e
# del riepilogo automatico, per non intasare il messaggio con residui
# irrilevanti.
MIN_VALUE_USD_TO_SHOW = 10.0


def get_position_value_usd(pos: dict, mids: dict):
    """Valore nozionale approssimativo (in USDC) di una posizione perps,
    usato per il filtro "polvere" di /positions (vedi
    MIN_VALUE_USD_TO_SHOW). Prova, in ordine: il campo
    "positionValue" gia' fornito da clearinghouseState (il piu'
    affidabile); altrimenti size * prezzo attuale (allMids); altrimenti
    size * entry price. Ritorna None se nessuno dei tre e' disponibile --
    mai un valore indovinato."""
    raw = pos.get("positionValue")
    if raw is not None:
        try:
            return abs(float(raw))
        except (TypeError, ValueError):
            pass
    try:
        szi = abs(float(pos.get("szi", 0)))
    except (TypeError, ValueError):
        szi = 0.0
    if szi:
        coin = pos.get("coin")
        for price_raw in (mids.get(coin) if isinstance(mids, dict) else None, pos.get("entryPx")):
            if price_raw is None:
                continue
            try:
                return szi * float(price_raw)
            except (TypeError, ValueError):
                continue
    return None


def get_position_direction_sign(pos: dict):
    """+1 se la posizione e' LONG (szi > 0), -1 se e' SHORT (szi < 0),
    altrimenti None (size zero o "szi" non interpretabile -- caso limite,
    ma il valore nozionale (vedi get_position_value_usd) puo' comunque
    essere noto anche quando "szi" non lo e', es. se arriva gia' un
    "positionValue" affidabile senza "szi" leggibile: in quel caso la
    direzione resta sconosciuta anche se il valore no). Usato da
    format_positions_message per l'esposizione NETTA (long - short),
    separata dal valore nozionale LORDO che invece somma sempre i valori
    assoluti senza compensare long e short tra loro."""
    try:
        szi = float(pos.get("szi", 0))
    except (TypeError, ValueError):
        return None
    if szi > 0:
        return 1
    if szi < 0:
        return -1
    return None


def extract_spot_balances(spot_state: dict) -> list:
    """Ritorna i saldi spot non nulli da spotClearinghouseState (schema
    best-effort, non verificabile con certezza da qui -- vedi
    format_spot_balances_block): ogni voce che non si riesce a
    interpretare in sicurezza viene semplicemente ignorata invece di
    mostrare dati indovinati."""
    if not isinstance(spot_state, dict):
        return []
    raw = spot_state.get("balances")
    if not isinstance(raw, list):
        return []
    balances = []
    for b in raw:
        if not isinstance(b, dict):
            continue
        coin = b.get("coin")
        if not coin:
            continue
        try:
            total = float(b.get("total", 0) or 0)
        except (TypeError, ValueError):
            continue
        if total == 0:
            continue
        balances.append({"coin": coin, "total": total})
    return balances


def get_spot_usdc_balance(spot_state: dict):
    """Ritorna il saldo USDC del conto SPOT (float, 1:1 col suo stesso
    controvalore -- niente prezzo da risolvere) se presente, altrimenti
    None se spot_state non e' interpretabile. Un saldo USDC assente
    dall'elenco (vedi extract_spot_balances, che ignora i saldi nulli)
    e' un genuino zero, non "sconosciuto" -- usato da
    format_positions_message per il confronto tra valore nozionale delle
    posizioni perps e liquidita' USDC disponibile in spot (due conti
    distinti su Hyperliquid, NON la stessa cosa nonostante il secondo
    tenda a muoversi in base a depositi/prelievi legati al trading sul
    primo)."""
    if not isinstance(spot_state, dict):
        return None
    for b in extract_spot_balances(spot_state):
        if b["coin"] == "USDC":
            return b["total"]
    return 0.0


def format_spot_balances_block(spot_state: dict, mids: dict, spot_meta: dict | None = None) -> str | None:
    """Blocco "Saldi spot" per /positions: saldi non nulli dei token nel
    wallet spot (NON posizioni con leva come i perps di
    extract_open_positions -- sono due conti separati su Hyperliquid). Il
    controvalore in USDC viene calcolato con get_price_for_coin (che sa
    risolvere anche i ticker spot tramite spot_meta, se passato -- vedi
    resolve_spot_mid_key); se non si trova alcun prezzo si mostra solo la
    quantita', senza inventare un valore.

    Un saldo il cui controvalore stimato e' sotto MIN_VALUE_USD_TO_SHOW
    (stessa soglia usata per le posizioni perps, vedi
    format_positions_message) viene considerato "polvere" e omesso. Un
    saldo di cui NON si riesce a stimare il controvalore (prezzo non
    risolvibile) viene invece sempre mostrato, per non rischiare di
    nascondere qualcosa di rilevante solo perche' il prezzo non si trova.
    Ritorna None se, dopo il filtro, non resta nessun saldo da mostrare."""
    balances = extract_spot_balances(spot_state)
    if not balances:
        return None
    lines = ["💰 Saldi spot:"]
    hidden_count = 0
    for b in sorted(balances, key=lambda x: x["coin"]):
        coin = b["coin"]
        total = b["total"]
        if coin == "USDC":
            if total < MIN_VALUE_USD_TO_SHOW:
                hidden_count += 1
                continue
            lines.append(f"— {coin}: {total:g} (liquidità)")
            continue
        price = get_price_for_coin(coin, mids, spot_meta)
        if price is not None and total * price < MIN_VALUE_USD_TO_SHOW:
            hidden_count += 1
            continue
        value_txt = f" (~{total * price:g} USDC)" if price is not None else ""
        lines.append(f"— {ticker_mention(coin, kind='spot')}: {total:g}{value_txt}")
    if len(lines) == 1:  # solo l'intestazione "💰 Saldi spot:" -> nulla sopra soglia
        return None
    if hidden_count:
        lines.append(f"({hidden_count} saldo/i sotto i {MIN_VALUE_USD_TO_SHOW:g} USDC non mostrato/i)")
    return "\n".join(lines)


def alert_ticker_kind(alert: dict) -> str:
    """Determina se il ticker di un alert e' PERPS o SPOT in base alla
    "mids_key" salvata su di esso (risolta in try_create_alert -- vedi
    resolve_spot_mid_key): se e' diversa dal ticker stesso, e' stata
    risolta passando per lo spot, quindi e' spot; altrimenti (o se manca,
    come per gli alert creati prima di questa modifica) si assume perps,
    il caso piu' comune -- usato per decidere il link cliccabile del
    ticker (vedi ticker_mention/build_hyperliquid_chart_url)."""
    coin = alert.get("coin")
    mids_key = alert.get("mids_key")
    return "spot" if (mids_key and mids_key != coin) else "perp"


def format_alerts_list_message(alerts: list, mids: dict | None = None, spot_meta: dict | None = None) -> str:
    """Risposta al comando /alerts: elenco degli alert di prezzo attivi.
    Se mids e' passato, ogni riga include anche il prezzo attuale del
    token (usando la "mids_key" gia' risolta salvata sull'alert -- vedi
    try_create_alert/resolve_spot_mid_key -- con fallback su spot_meta per
    gli alert creati prima di questa modifica, che non ce l'hanno
    salvata). Se il prezzo attuale non si riesce a determinare, la riga
    resta senza quell'informazione invece di inventare un valore."""
    if not alerts:
        return (
            "🔔 Nessun alert di prezzo impostato.\n"
            "Per crearne uno: /alert <COIN> <sopra|sotto> <VALORE|VALORE%>\n"
            "Esempi: /alert BTC sotto 65000  —  /alert BTC sotto 5%"
        )
    lines = ["🔔 Alert di prezzo attivi:"]
    for a in sorted(alerts, key=lambda x: x.get("id", 0)):
        verso = "sotto" if a.get("direction") == "below" else "sopra"
        try:
            price_txt = f"{float(a.get('price', 0)):g}"
        except (TypeError, ValueError):
            price_txt = str(a.get("price"))
        pct_txt = ""
        if a.get("pct") is not None:
            try:
                pct_txt = f" ({float(a['pct']):g}% dal prezzo di {float(a.get('ref_price', 0)):g})"
            except (TypeError, ValueError):
                pct_txt = f" ({a['pct']}%)"
        stato_txt = " 🔴 IN ALLARME" if a.get("triggered") else ""
        current_txt = ""
        if mids:
            mids_key = a.get("mids_key") or a.get("coin")
            current_price = get_price_for_coin(mids_key, mids, spot_meta) if mids_key else None
            if current_price is not None:
                current_txt = f" (attuale: {current_price:g})"
        coin_link = ticker_mention(a.get("coin"), kind=alert_ticker_kind(a))
        lines.append(f"— id {a.get('id')}: {coin_link} {verso} {price_txt}{pct_txt}{current_txt}{stato_txt}")
    lines.append("\nPer rimuoverne uno: /delalert <id>")
    return "\n".join(lines)


def format_position_summary(pos: dict, current_price) -> str:
    """Riassunto compatto di una posizione (usato nei messaggi di alert):
    direzione, size, entry vs prezzo attuale, PnL, prezzo di liquidazione.
    Parsing difensivo come nel resto del modulo -- righe omesse quando il
    dato non e' interpretabile con sicurezza."""
    try:
        szi = float(pos.get("szi", 0))
    except (TypeError, ValueError):
        szi = 0.0
    direction = "LONG" if szi > 0 else "SHORT"
    header = f"Posizione: {direction} {abs(szi):g}"

    try:
        entry_px = float(pos.get("entryPx")) if pos.get("entryPx") is not None else None
    except (TypeError, ValueError):
        entry_px = None
    lines = []
    if entry_px is not None:
        header += f" @ entry {entry_px:g}"
        if current_price is not None and entry_px:
            diff_pct = (float(current_price) - entry_px) / entry_px * 100
            if szi < 0:  # short: si guadagna quando il prezzo scende
                diff_pct = -diff_pct
            segno = "+" if diff_pct >= 0 else ""
            lines.append(f"vs entry: {segno}{diff_pct:.2f}%")
    lines.insert(0, header)

    try:
        unrealized_pnl = float(pos.get("unrealizedPnl")) if pos.get("unrealizedPnl") is not None else None
    except (TypeError, ValueError):
        unrealized_pnl = None
    if unrealized_pnl is not None:
        segno = "+" if unrealized_pnl >= 0 else ""
        lines.append(f"PnL non realizzato: {segno}{unrealized_pnl:g} USDC")

    liq_px = pos.get("liquidationPx")
    if liq_px is not None:
        try:
            lines.append(f"Prezzo di liquidazione: {float(liq_px):g}")
        except (TypeError, ValueError):
            pass

    return "\n".join(lines)


def format_spot_holding_summary(balance: dict, current_price) -> str:
    """Riassunto compatto di un saldo SPOT (usato nei messaggi di alert,
    analogo a format_position_summary ma per lo spot: nessuna leva, entry
    price o prezzo di liquidazione -- solo quantita' e, se current_price e'
    disponibile, controvalore stimato in USDC."""
    coin = balance.get("coin", "?")
    coin_link = ticker_mention(coin, kind="spot")
    total = balance.get("total", 0)
    try:
        total_f = float(total)
    except (TypeError, ValueError):
        return f"Saldo spot: {total} {coin_link}"
    line = f"Saldo spot: {total_f:g} {coin_link}"
    if current_price is not None:
        try:
            line += f" (~{total_f * float(current_price):g} USDC)"
        except (TypeError, ValueError):
            pass
    return line


def format_alert_triggered_message(
    alert: dict,
    current_price: float,
    position: dict | None = None,
    repeated: bool = False,
    repeat_every_n_runs: int = DEFAULT_ALERT_REPEAT_EVERY_N_RUNS,
    urgent: bool = False,
    urgent_deviation_pct: float = DEFAULT_ALERT_URGENT_DEVIATION_PCT,
    spot_balance: dict | None = None,
    now_ms: int | None = None,
) -> str:
    """Notifica automatica mandata quando un alert di prezzo scatta (o
    resta attivo). Se hai una posizione PERPS aperta su quella coin (o, in
    mancanza, un saldo SPOT non nullo -- vedi main()), l'alert resta
    attivo e questa notifica si ripete ogni repeat_every_n_runs giri (non
    ad ogni controllo) finche' la condizione persiste E possiedi ancora
    quella coin (in un modo o nell'altro) -- ECCETTO quando il prezzo si
    e' allontanato dalla soglia di piu' di urgent_deviation_pct (vedi
    main()): in quel caso (urgent=True) il throttle viene bypassato e si
    notifica ad ogni giro, perche' la situazione e' considerata piu'
    urgente. Se e' presente sia una posizione perps sia un saldo spot,
    viene mostrata solo la posizione perps (piu' ricca di dettagli:
    leva, PnL, liquidazione), ma l'alert resta "sticky" grazie a
    entrambe. Vedi main() per la logica di stato ("triggered") e
    format_alert_cleared_message per come si ferma.

    now_ms (opzionale) mostra la data/ora in fondo al messaggio (vedi
    format_display_datetime): per gli alert la data non compare piu' nel
    titolo (vedi format_update_header, non essenziale li'), quindi resta
    solo qui nel corpo -- se non passato, la riga viene semplicemente
    omessa."""
    coin = alert.get("coin", "?")
    threshold = alert.get("price", 0)
    verso = "sceso sotto" if alert.get("direction") == "below" else "salito sopra"
    extra = ""
    if alert.get("pct") is not None:
        try:
            extra = f" ({float(alert['pct']):g}% dal prezzo di {float(alert.get('ref_price', 0)):g})"
        except (TypeError, ValueError):
            extra = f" ({alert['pct']}%)"
    header = "🔔 Alert ancora attivo" if repeated else "🔔 Alert scattato"
    coin_link = ticker_mention(coin, kind=alert_ticker_kind(alert))
    lines = [f"{header}: {coin_link} {verso} {threshold:g}{extra}", f"Prezzo attuale: {current_price:g}"]
    holding_line = None
    if position is not None:
        holding_line = format_position_summary(position, current_price)
    elif spot_balance is not None:
        holding_line = format_spot_holding_summary(spot_balance, current_price)
    if holding_line is not None:
        lines.append(holding_line)
        if not repeated:
            cadenza = "questo e' il primo avviso"
        elif urgent:
            cadenza = (
                f"il prezzo si e' allontanato di oltre il {urgent_deviation_pct:g}% dalla soglia: "
                f"ti aggiorno ad ogni controllo"
            )
        else:
            cadenza = f"ti aggiorno ogni {repeat_every_n_runs} controlli"
        lines.append(
            f"({cadenza} finche' resti in questa condizione; quando rientra ricevi un ultimo avviso e l'alert "
            "resta attivo, pronto a scattare di nuovo — si disattiva solo se non possiedi più questa coin, "
            "ne' come posizione ne' come saldo spot)"
        )
    else:
        lines.append("(alert rimosso automaticamente — usa /alert per impostarne uno nuovo)")
    if now_ms is not None:
        lines.append(f"🕐 {format_display_datetime(now_ms)}")
    return "\n".join(lines)


def format_alert_cleared_message(alert: dict, current_price: float, reason: str, now_ms: int | None = None) -> str:
    """Notifica mandata quando un alert "attivo" (con posizione perps e/o
    saldo spot collegato, vedi format_alert_triggered_message) smette di
    ripetersi:
    - reason="recovered": il prezzo e' rientrato oltre la soglia. L'alert
      NON viene rimosso, torna solo "in ascolto" (vedi make_alert_rearm_cb
      in main()) e puo' scattare di nuovo in futuro senza doverlo
      reimpostare -- resta legato alla coin finche' ne possiedi ancora, in
      un modo o nell'altro.
    - reason="holding_closed": non hai piu' ne' una posizione perps ne' un
      saldo spot su quella coin, non c'e' piu' nulla da proteggere --
      l'alert viene rimosso per davvero stavolta.

    now_ms (opzionale) mostra la data/ora in fondo al messaggio (vedi
    format_display_datetime e format_alert_triggered_message, stessa
    logica: la data non compare piu' nel titolo dell'alert)."""
    coin = alert.get("coin", "?")
    threshold = alert.get("price", 0)
    coin_link = ticker_mention(coin, kind=alert_ticker_kind(alert))
    if reason == "holding_closed":
        head = f"🔕 Alert disattivato: non hai più né una posizione né un saldo spot su {coin_link}."
        footer = "(non ricevi più notifiche per questo alert)"
    else:
        verso_rientro = "risalito sopra" if alert.get("direction") == "below" else "ridisceso sotto"
        head = f"✅ Allarme rientrato: {coin_link} e' {verso_rientro} {threshold:g}."
        footer = "(l'alert resta attivo: ti avviso di nuovo se la soglia scatta ancora)"
    lines = [head, f"Prezzo attuale: {current_price:g}", footer]
    if now_ms is not None:
        lines.append(f"🕐 {format_display_datetime(now_ms)}")
    return "\n".join(lines)


def load_state(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(path: str, state: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")


def side_label(side_raw: str) -> str:
    return "BUY" if side_raw == "B" else "SELL" if side_raw == "A" else side_raw


def fmt_ts(ts_ms) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(ts_ms / 1000)) if ts_ms else "?"


def weighted_avg_price(fills: list) -> float:
    total_sz = sum(float(f.get("sz", 0)) for f in fills)
    if total_sz == 0:
        return 0.0
    return sum(float(f.get("px", 0)) * float(f.get("sz", 0)) for f in fills) / total_sz


def format_order_message(fills: list) -> str:
    """Messaggio per un ordine normale (non TWAP): totale eseguito, con il
    dettaglio delle singole esecuzioni se sono state piu' di una."""
    first = fills[0]
    coin = first.get("coin", "?")
    side = side_label(first.get("side", ""))
    direction = first.get("dir", "")

    total_sz = sum(float(f.get("sz", 0)) for f in fills)
    avg_px = weighted_avg_price(fills)
    total_fee = sum(float(f.get("fee", 0) or 0) for f in fills)
    fee_token = first.get("feeToken", "")
    total_closed_pnl = sum(float(f.get("closedPnl", 0) or 0) for f in fills)
    last_ts = max(f.get("time", 0) for f in fills)

    lines = [f"🚨 Eseguito totale: {side} {total_sz:g} {ticker_mention(coin, kind='perp')} @ {avg_px:g} (media)"]
    if direction:
        lines.append(f"Tipo: {direction}")
    if total_closed_pnl != 0:
        lines.append(f"PnL realizzato: {total_closed_pnl:g} USDC")
    if total_fee and fee_token:
        lines.append(f"Fee totale: {total_fee:g} {fee_token}")
    lines.append(f"Orario ultima esecuzione: {fmt_ts(last_ts)}")

    if len(fills) > 1:
        lines.append(f"↳ {len(fills)} esecuzioni:")
        for f in sorted(fills, key=lambda x: x.get("time", 0)):
            lines.append(
                f"   - {float(f.get('sz', 0)):g} @ {float(f.get('px', 0)):g} "
                f"({fmt_ts(f.get('time', 0))})"
            )
    return "\n".join(lines)


def format_display_datetime(now_ms: int) -> str:
    """Formatta now_ms come "<giorno> <mese> <anno>, ore HH:MM" nel fuso
    DISPLAY_TIMEZONE quando disponibile, altrimenti in UTC (mai un crash
    per questo, vedi anche format_update_header/is_quiet_hours che usano
    la stessa logica di fallback). Usata sia per il titolo delle notifiche
    non-alert (vedi format_update_header) sia nel corpo dei messaggi di
    alert, dove la data non compare piu' nel titolo (vedi
    format_alert_triggered_message/format_alert_cleared_message)."""
    try:
        from zoneinfo import ZoneInfo
        dt = datetime.fromtimestamp(now_ms / 1000, tz=ZoneInfo(DISPLAY_TIMEZONE))
        tz_label = ""
    except Exception:
        dt = datetime.utcfromtimestamp(now_ms / 1000)
        tz_label = " UTC"
    mese = ITALIAN_MONTHS[dt.month - 1]
    return f"{dt.day} {mese} {dt.year}, ore {dt.strftime('%H:%M')}{tz_label}"


def format_update_header(
    now_ms: int,
    kind: str = "orders",
    coin: str | None = None,
    coin_kind: str = "perp",
    alert_direction: str | None = None,
    alert_threshold=None,
    alert_current_price=None,
) -> str:
    """Intestazione mandata in cima a ogni notifica AUTOMATICA (fill/nuovi
    TWAP, alert, riepilogo posizioni), sia nel titolo che nel colore della
    barra distinta per categoria in base a `kind` ("orders"/"alerts"/
    "positions" -- vedi UPDATE_HEADER_TITLE_BY_KIND e
    UPDATE_HEADER_BAR_BY_KIND) cosi' si riconoscono a colpo d'occhio anche
    senza leggere il resto del messaggio.

    Per kind="orders"/"positions" il titolo include anche la data/ora
    (vedi format_display_datetime), nel fuso DISPLAY_TIMEZONE quando
    disponibile (altrimenti UTC).

    Per kind="alerts", se `coin` e' passato il titolo mostra invece
    ticker (link cliccabile via ticker_mention, kind=coin_kind), soglia
    dell'alert e prezzo attuale -- es. "🕐 Aggiornamento Alerts: BTC sotto
    60000 (attuale: 59300)" -- per capire subito, senza aprire il
    messaggio, a quale coin si riferisce l'alert e quanto manca/quanto ha
    superato la soglia; utile soprattutto con piu' alert attivi
    contemporaneamente. La data non e' essenziale in questo caso e non
    compare nel titolo (resta comunque nel corpo del messaggio, vedi
    format_alert_triggered_message/format_alert_cleared_message). Se
    `coin` non e' passato (o per gli altri kind) si ricade sul titolo
    semplice con data."""
    title = UPDATE_HEADER_TITLE_BY_KIND.get(kind, UPDATE_HEADER_TITLE_BY_KIND["orders"])
    bar = UPDATE_HEADER_BAR_BY_KIND.get(kind, UPDATE_HEADER_BAR_ORDERS)
    if kind == "alerts" and coin:
        detail_parts = [ticker_mention(coin, kind=coin_kind)]
        verso = {"below": "sotto", "above": "sopra"}.get(alert_direction)
        if verso and alert_threshold is not None:
            try:
                detail_parts.append(f"{verso} {float(alert_threshold):g}")
            except (TypeError, ValueError):
                pass
        if alert_current_price is not None:
            try:
                detail_parts.append(f"(attuale: {float(alert_current_price):g})")
            except (TypeError, ValueError):
                pass
        return f"{title}: {' '.join(detail_parts)}\n{bar}"
    return f"{title} — {format_display_datetime(now_ms)}\n{bar}"


def is_quiet_hours(now_ms: int) -> bool:
    """True se now_ms cade nella fascia notturna QUIET_HOURS_START_HOUR -
    QUIET_HOURS_END_HOUR (fuso DISPLAY_TIMEZONE), durante la quale le
    notifiche automatiche "di aggiornamento" (ordini/fill, nuovi TWAP,
    riepilogo posizioni) vengono trattenute -- vedi il loro uso in
    main(). Gli alert di prezzo NON passano da qui: vengono sempre
    mandati, a qualunque ora. Se il fuso orario non e' disponibile per
    qualche motivo si assume "non e' notte" (fail-open): meglio una
    notifica di troppo che perderne una per un problema d'ambiente."""
    try:
        from zoneinfo import ZoneInfo
        dt = datetime.fromtimestamp(now_ms / 1000, tz=ZoneInfo(DISPLAY_TIMEZONE))
    except Exception:
        return False
    hour = dt.hour
    return hour >= QUIET_HOURS_START_HOUR or hour < QUIET_HOURS_END_HOUR


def is_untriggered_twap_status(status) -> bool:
    """True se lo status (grezzo, da fetch_twap_records) indica un TWAP
    impostato con un trigger di prezzo che NON e' ancora scattato.

    Il valore reale e' stato confermato interrogando direttamente
    l'endpoint "twapHistory" con l'account dell'utente il 25/08/2026:
    "waitingForTrigger" (NON "untriggered" come ipotizzato inizialmente --
    quel nome era un'estrapolazione mai verificata, e questo confronto
    avrebbe quindi sempre fallito silenziosamente contro i dati reali,
    trattando ogni TWAP con trigger di prezzo come gia' partito appena
    creato invece di aspettare che scattasse davvero. E' probabilmente la
    causa di fondo della confusione originale segnalata dall'utente su
    /twap). "untriggered" resta comunque incluso nel confronto come
    fallback difensivo, nel caso comparisse in futuro o per altri
    account. Confronto case-insensitive e con gli spazi tolti per
    robustezza; qualunque altro status (incluso None/sconosciuto) e'
    considerato "gia' attivo", per non rischiare di tenere in sospeso
    all'infinito un TWAP normale solo perche' lo status non e' quello
    atteso -- coerente con main(), che notifica subito i TWAP il cui
    status non e' esplicitamente uno di questi."""
    return isinstance(status, str) and status.strip().lower() in ("waitingfortrigger", "untriggered")


def extract_twap_trigger_info(record: dict | None):
    """Ritorna (prezzo_soglia: float | None, sopra: bool | None) dal
    campo state.trigger di un record TWAP (vedi fetch_twap_records) --
    confermato realmente presente il 25/08/2026 come
    {"trigger": {"px": <stringa>, "above": <bool>}} dentro "state" per un
    TWAP con status "waitingForTrigger" (per i TWAP senza trigger di
    prezzo il campo e' semplicemente null). "above" True significa che
    scatta quando il prezzo SALE sopra "px", False quando SCENDE sotto.
    Ritorna (None, None) se il campo manca o non e' nella forma attesa
    (mai indovinato)."""
    if not isinstance(record, dict):
        return (None, None)
    trigger = record.get("trigger")
    if not isinstance(trigger, dict):
        return (None, None)
    try:
        px = float(trigger.get("px"))
    except (TypeError, ValueError):
        return (None, None)
    above = trigger.get("above")
    if not isinstance(above, bool):
        above = None
    return (px, above)


# Status (grezzi, da fetch_twap_records) che indicano un TWAP NON piu'
# attivo -- confermati via la documentazione pubblica di Hyperliquid
# (due fonti indipendenti concordano su "activated" per un TWAP in corso
# e "terminated" per uno interrotto anzitempo, ma discordano sul nome
# esatto dello stato "eseguito per intero": una lo chiama "completed",
# l'altra "finished" -- entrambi inclusi qui per sicurezza, dato che non
# c'e' modo di verificarlo con accesso di rete diretto da questo
# ambiente). Chiave normalizzata (minuscolo, spazi tolti) -> etichetta
# umana mostrata da format_twap_status_message (vedi
# is_closed_twap_status).
TWAP_CLOSED_STATUS_LABELS = {
    "completed": "✅ Completato",
    "finished": "✅ Completato",
    "terminated": "⛔ Terminato anticipatamente",
    "canceled": "🚫 Annullato",
    "cancelled": "🚫 Annullato",
}


def is_closed_twap_status(status) -> bool:
    """True se lo status (grezzo) indica un TWAP NON piu' attivo (vedi
    TWAP_CLOSED_STATUS_LABELS) -- completato, terminato anzitempo o
    annullato. Qualunque altro status (incluso "activated", None, o uno
    sconosciuto/non documentato) e' considerato "ancora attivo": non
    avendo conferma che sia chiuso, meglio continuare a mostrarlo come in
    corso piuttosto che nasconderlo o etichettarlo erroneamente come
    finito -- coerente con lo stile difensivo del resto del modulo."""
    return isinstance(status, str) and status.strip().lower() in TWAP_CLOSED_STATUS_LABELS


def closed_twap_status_label(status) -> str:
    """Etichetta umana per uno status chiuso (vedi is_closed_twap_status);
    presuppone che is_closed_twap_status(status) sia gia' stato
    verificato True dal chiamante."""
    return TWAP_CLOSED_STATUS_LABELS.get(status.strip().lower(), "✅ Completato")


# Numero massimo di TWAP chiusi (completati/terminati/annullati) mostrati
# nella sezione dedicata di /twap -- solo i piu' recenti (per ultima
# esecuzione nota), per non allungare troppo il messaggio; il conteggio
# di quelli non mostrati viene comunque indicato (niente troncamento
# silenzioso).
CLOSED_TWAP_DISPLAY_LIMIT = 5

# --- Rilevamento TWAP chiusi: strategia a piu' livelli --------------------
# Lo status testuale restituito dall'endpoint best-effort dei TWAP si e'
# rivelato inaffidabile per capire se un TWAP e' concluso (vedi
# is_closed_twap_status/fetch_twap_records): anche dopo aver gestito sia
# stringhe semplici sia oggetti "taggati" stile Rust/serde, TWAP
# confermati conclusi su Hyperliquid stesso (es. un BTC completato il
# 24/08 e un CASHCAT completato il 22/08, segnalati dall'utente)
# continuavano a comparire come "status non riconosciuto" o peggio con
# uno status letto correttamente ma che non corrisponde a nessuna delle
# etichette note (es. potrebbe restare "activated" anche a job concluso,
# se quell'endpoint non aggiorna piu' lo status per TWAP vecchi). Per
# questo il rilevamento NON si basa piu' solo sullo status testuale, ma
# combina tre segnali, dal piu' al meno affidabile:
#
#   1) status esplicito e riconosciuto come chiuso (is_closed_twap_status)
#      -- quando disponibile e interpretabile, e' il segnale piu' diretto.
#   2) durata pianificata scaduta: se il TWAP ha una durata dichiarata
#      (minutes) e un orario di inizio (start_ms), e il tempo attuale ha
#      superato start_ms + minutes (con un margine di sicurezza), il TWAP
#      DEVE essersi concluso per costruzione -- un TWAP su Hyperliquid non
#      puo' restare "in esecuzione" oltre la propria durata pianificata,
#      quindi questo segnale non dipende affatto dal capire il campo
#      status ed e' quindi molto piu' robusto.
#   3) inattivita' prolungata: se nessuna delle due sopra e' disponibile
#      (dati mancanti) ma non arrivano esecuzioni da molto tempo (oltre
#      TWAP_STALE_HOURS), si presume comunque concluso/fermo -- segnale
#      piu' debole, quindi etichettato in modo esplicito come "presunto"
#      e non come "completato con certezza".
#
# I livelli 2 e 3 sono PRESUNZIONI, non conferme: usano un'etichetta
# diversa da quelle di TWAP_CLOSED_STATUS_LABELS per essere oneste sul
# fatto che non sappiamo con certezza l'esito (completato vs terminato
# anticipatamente vs annullato), solo che il TWAP non e' plausibilmente
# ancora "in corso".
TWAP_EXPIRY_GRACE_MINUTES = 15  # margine oltre la durata pianificata
TWAP_STALE_HOURS = 12  # nessuna esecuzione da piu' di N ore -> presunto fermo


def presumed_twap_expiry_label(record: dict | None, prog: dict, now_ms: int | None):
    """Livelli 2 e 3 della strategia di rilevamento TWAP chiusi (vedi il
    blocco di commenti sopra TWAP_EXPIRY_GRACE_MINUTES): ritorna
    un'etichetta se si PRESUME che il TWAP sia concluso, altrimenti None
    (nel qual caso il chiamante deve continuare a considerarlo aperto).
    Richiede now_ms -- se assente ritorna sempre None, disattivando questi
    due livelli (si torna al solo status testuale, vedi
    is_closed_twap_status).

    Livello 2 (durata scaduta): se il record ha sia "start_ms" sia
    "minutes" (durata pianificata in minuti) ed e' trascorso
    start_ms + minutes + un margine di sicurezza (TWAP_EXPIRY_GRACE_MINUTES),
    il TWAP non puo' piu' essere "in esecuzione" per costruzione, a
    prescindere da cosa dica il campo status -- e' il segnale piu'
    affidabile dei due perche' si basa solo su numeri (nessuna stringa da
    interpretare).

    Livello 3 (inattivita' prolungata): se il livello 2 non e' applicabile
    (start_ms o minutes mancanti) ma l'ultima esecuzione nota
    (prog["last_ms"]) risale a piu' di TWAP_STALE_HOURS fa, si presume
    comunque fermo/concluso -- segnale piu' debole (un TWAP molto lungo a
    bassa frequenza potrebbe legittimamente restare inattivo per ore tra
    una slice e l'altra), per questo la soglia e' volutamente larga."""
    if now_ms is None:
        return None
    start_ms = record.get("start_ms") if record else None
    minutes = record.get("minutes") if record else None
    if start_ms is not None and minutes is not None:
        try:
            expected_end_ms = float(start_ms) + float(minutes) * 60_000
            if now_ms > expected_end_ms + TWAP_EXPIRY_GRACE_MINUTES * 60_000:
                return "⌛ Durata pianificata terminata (presumibilmente concluso)"
        except (TypeError, ValueError):
            pass
    last_ms = prog.get("last_ms") if prog else None
    if last_ms is not None:
        try:
            if now_ms - float(last_ms) > TWAP_STALE_HOURS * 3600_000:
                return "🕰️ Nessuna esecuzione recente (presumibilmente fermo)"
        except (TypeError, ValueError):
            pass
    return None


def format_new_twap_message(record: dict, triggered: bool = False, spot_meta: dict | None = None) -> str:
    """Messaggio inviato quando un TWAP inizia davvero a eseguire, prima
    ancora che scatti la sua prima slice. Due casi (vedi
    is_untriggered_twap_status/main per come si distinguono):
    - triggered=False (default): TWAP "normale", senza condizione di
      trigger (o comunque gia' attivo appena rilevato) -- parte subito,
      quindi si notifica appena viene rilevato.
    - triggered=True: TWAP che era stato impostato con un trigger di
      prezzo (status "untriggered" quando e' stato rilevato per la prima
      volta -- niente notifica in quel momento, dato che non e' ancora
      partito) e che ORA ha superato la soglia ed e' effettivamente
      partito.

    spot_meta (opzionale) serve a risolvere il "coin" grezzo quando il
    TWAP e' su una coppia SPOT non "canonica" (vedi
    resolve_twap_coin_display): senza, un TWAP spot verrebbe mostrato con
    un id posizionale illeggibile tipo "@162" invece del ticker."""
    twap_id = record.get("twap_id")
    coin_display, coin_kind, linkable = resolve_twap_coin_display(record.get("coin") or "?", spot_meta)
    side = side_label(record.get("side") or "")

    verb = "TWAP triggerato, e' partito" if triggered else "Nuovo TWAP avviato"
    emoji = "🎯" if triggered else "🆕"
    coin_txt = ticker_mention(coin_display, kind=coin_kind) if linkable else coin_display
    header = f"{emoji} {verb}: {side} {coin_txt}".rstrip()
    total_sz = record.get("total_sz")
    if total_sz is not None:
        try:
            header += f" — size totale {float(total_sz):g}"
        except (TypeError, ValueError):
            pass

    lines = [header]
    if record.get("minutes"):
        lines.append(f"Durata prevista: {record['minutes']} minuti")
    if record.get("status"):
        lines.append(f"Stato: {record['status']}")
    if record.get("start_ms"):
        lines.append(f"Avviato: {fmt_ts(record['start_ms'])}")
    lines.append(f"ID TWAP: {twap_id}")
    return "\n".join(lines)


def format_twap_closed_message(record: dict, spot_meta: dict | None = None) -> str:
    """Messaggio inviato quando un TWAP risulta concluso -- status
    esplicito e riconosciuto come chiuso, vedi is_closed_twap_status
    (SOLO il livello 1 "confermato" della strategia a piu' livelli usata
    da /twap -- vedi il blocco di commenti sopra TWAP_EXPIRY_GRACE_MINUTES
    -- fa scattare questa notifica automatica; le presunzioni via durata
    scaduta/inattivita' prolungata sono abbastanza affidabili da mostrare
    su richiesta in /twap ma non abbastanza da giustificare un push
    automatico che potrebbe rivelarsi sbagliato).

    Usa executed_sz/executed_ntl del record stesso (i valori FINALI
    riportati direttamente dall'endpoint per la voce piu' recente di
    quel twap_id, vedi build_twap_records_by_id) invece di twap_progress,
    cosi' il riepilogo resta corretto anche se il bot non ha osservato
    ogni singola esecuzione (es. era offline, o il TWAP si e' concluso
    tra due controlli senza fill intermedi rilevati)."""
    twap_id = record.get("twap_id")
    coin_display, coin_kind, linkable = resolve_twap_coin_display(record.get("coin") or "?", spot_meta)
    side = side_label(record.get("side") or "")
    coin_txt = ticker_mention(coin_display, kind=coin_kind) if linkable else coin_display
    status_label = closed_twap_status_label(record.get("status"))

    header = f"{status_label}: TWAP {side} {coin_txt}".rstrip()
    lines = [header]

    try:
        executed_sz = float(record.get("executed_sz"))
    except (TypeError, ValueError):
        executed_sz = None
    try:
        total_sz = float(record.get("total_sz"))
    except (TypeError, ValueError):
        total_sz = None
    if executed_sz is not None:
        line = f"Eseguito: {executed_sz:g}"
        if total_sz:
            pct = min(100.0, executed_sz / total_sz * 100)
            line += f" di {total_sz:g} ({pct:.1f}%)"
        lines.append(line)

    try:
        executed_ntl = float(record.get("executed_ntl"))
    except (TypeError, ValueError):
        executed_ntl = None
    if executed_sz and executed_ntl is not None:
        lines.append(f"Prezzo medio: {executed_ntl / executed_sz:g}")

    lines.append(f"ID TWAP: {twap_id}")
    return "\n".join(lines)


def format_twap_status_message(
    twap_progress: dict,
    twap_records_by_id: dict,
    mids: dict,
    pending_twap_ids=None,
    spot_meta: dict | None = None,
    now_ms: int | None = None,
) -> str:
    """Risposta al comando /twap: PRIMA i TWAP "⏳ In attesa di trigger"
    (impostati con una condizione di prezzo, status "untriggered" -- vedi
    is_untriggered_twap_status/main -- che quindi NON hanno ancora
    eseguito nulla e altrimenti sarebbero del tutto invisibili qui, dato
    che non compaiono in twap_progress finche' non partono), POI un
    riepilogo "▶️ Attivi/eseguiti" PER TICKER (coin), non un elenco di
    ogni singolo ordine TWAP: somma l'eseguito cumulato di tutti i TWAP
    con esecuzioni note (twap_progress) sulla stessa coin -- BUY e SELL
    separati, non sommati insieme, per non falsare il totale se ci sono
    TWAP di direzione opposta sulla stessa coin -- MA SOLO per i TWAP che
    NON risultano chiusi (vedi il blocco di commenti sopra
    TWAP_EXPIRY_GRACE_MINUTES per la strategia a piu' livelli usata per
    deciderlo): un TWAP chiuso non viene piu' incluso qui, altrimenti
    resterebbe mostrato per sempre come "in corso" (con tanto di %,
    "mancano circa..." e "vs prezzo attuale") anche molto tempo dopo
    essere finito -- questo era esattamente il bug segnalato dall'utente
    (es. un TWAP BTC completato il 24/08 o un TWAP CASHCAT completato il
    22/08, entrambi ancora mostrati come attivi anche dopo un primo
    tentativo di correzione basato solo sullo status testuale, che si e'
    rivelato insufficiente). I TWAP chiusi vengono invece elencati in una
    sezione separata "✅ Completati/terminati di recente" (etichetta
    esatta a seconda del livello di certezza -- vedi sopra), in forma
    compatta e senza il linguaggio da "ancora in corso" (niente %,
    "mancano circa", "vs prezzo attuale"), limitata alle poche esecuzioni
    piu' recenti (vedi CLOSED_TWAP_DISPLAY_LIMIT) per non allungare
    troppo il messaggio.

    now_ms (opzionale, vedi main) e' l'orario corrente in millisecondi:
    serve ai livelli 2 e 3 della strategia di rilevamento (durata
    pianificata scaduta / inattivita' prolungata). Se omesso, quei due
    livelli sono semplicemente disattivati e si torna al solo status
    testuale (comportamento precedente).

    I record "raw" dell'endpoint TWAP (fetch_twap_records) sono usati
    solo per stimare la % di completamento quando disponibile e per
    determinare lo status aperto/chiuso, MAI mostrati uno per uno: quel
    best-effort endpoint puo' restituire anche moltissimo storico passato
    e un elenco completo rischierebbe di superare il limite di lunghezza
    di Telegram (vedi anche truncate_for_telegram, comunque una rete di
    sicurezza aggiuntiva).

    pending_twap_ids (opzionale, vedi main) e' l'insieme degli id TWAP
    attualmente in attesa di trigger. spot_meta (opzionale) risolve i
    "coin" grezzi delle coppie SPOT non "canoniche" (vedi
    resolve_twap_coin_display) cosi' un TWAP spot non compare con un id
    posizionale illeggibile tipo "@162" invece del ticker."""
    pending_twap_ids = pending_twap_ids or set()

    def coin_display_text(raw_coin):
        name, kind, linkable = resolve_twap_coin_display(raw_coin, spot_meta)
        return ticker_mention(name, kind=kind) if linkable else name

    blocks = []

    if pending_twap_ids:
        pending_lines = ["⏳ In attesa di trigger:"]
        for tid_str in sorted(pending_twap_ids):
            record = twap_records_by_id.get(tid_str)
            if not record:
                # Non piu' nell'elenco best-effort (es. storico troncato
                # dall'endpoint) -- si mostra comunque l'id, senza
                # inventare il resto.
                pending_lines.append(f"— id {tid_str}: dettagli non disponibili")
                continue
            side = side_label(record.get("side") or "")
            coin_txt = coin_display_text(record.get("coin") or "?")
            line = f"— {coin_txt}"
            if side:
                line += f": {side}"
            total_sz = record.get("total_sz")
            if total_sz is not None:
                try:
                    line += f" {float(total_sz):g}"
                except (TypeError, ValueError):
                    pass
            if record.get("minutes"):
                line += f" ({record['minutes']} min)"
            line += f" — id {tid_str}"
            trigger_px, trigger_above = extract_twap_trigger_info(record)
            if trigger_px is not None:
                direzione = "sale sopra" if trigger_above else "scende sotto"
                line += f"\n   scatta quando il prezzo {direzione} {trigger_px:g}"
            pending_lines.append(line)
        blocks.append("\n".join(pending_lines))

    if not twap_progress:
        if not blocks:
            return "📊 Nessuna esecuzione TWAP registrata finora, e nessun TWAP in attesa di trigger."
        blocks.append("▶️ Nessuna esecuzione TWAP registrata finora.")
        return "\n\n".join(blocks)

    # coin grezzo -> side grezzo ("B"/"A") -> aggregato -- SOLO per i TWAP
    # ancora aperti (vedi is_closed_twap_status); quelli chiusi finiscono
    # invece in closed_entries, una lista piatta (non aggregata: si vuole
    # mostrare la singola esecuzione con la sua etichetta di stato, non
    # sommarla ad altre).
    per_coin = defaultdict(lambda: defaultdict(lambda: {
        "executed_sz": 0.0, "notional": 0.0, "last_ms": None, "target_sz": 0.0, "has_target": False, "count": 0,
        "unresolved_status_raws": set(),
    }))
    closed_entries = []
    for key, prog in twap_progress.items():
        raw_coin = prog.get("coin") or "?"
        record = twap_records_by_id.get(key)
        status = record.get("status") if record else None
        if is_closed_twap_status(status):
            # Livello 1: status esplicito e riconosciuto -- il segnale
            # piu' affidabile quando disponibile.
            closed_label = closed_twap_status_label(status)
        else:
            # Livelli 2/3: presunzione via durata scaduta o inattivita'
            # prolungata (vedi presumed_twap_expiry_label) -- necessari
            # perche' lo status testuale da solo si e' rivelato
            # insufficiente per i TWAP realmente conclusi (vedi
            # docstring/commento sopra TWAP_EXPIRY_GRACE_MINUTES).
            closed_label = presumed_twap_expiry_label(record, prog, now_ms)
        if closed_label:
            executed_sz = float(prog.get("executed_sz", 0.0) or 0.0)
            notional = float(prog.get("notional", 0.0) or 0.0)
            avg_px = (notional / executed_sz) if executed_sz else 0.0
            closed_entries.append({
                "raw_coin": raw_coin,
                "side": prog.get("side") or "",
                "executed_sz": executed_sz,
                "avg_px": avg_px,
                "last_ms": prog.get("last_ms"),
                "status_label": closed_label,
            })
            continue
        agg = per_coin[raw_coin][prog.get("side") or ""]
        agg["executed_sz"] += float(prog.get("executed_sz", 0.0) or 0.0)
        agg["notional"] += float(prog.get("notional", 0.0) or 0.0)
        if prog.get("last_ms"):
            agg["last_ms"] = max(agg["last_ms"] or 0, prog["last_ms"])
        agg["count"] += 1
        if record and record.get("total_sz") is not None:
            try:
                agg["target_sz"] += float(record["total_sz"])
                agg["has_target"] = True
            except (TypeError, ValueError):
                pass
        # Diagnostica: se c'e' un record ma lo status non e' stato
        # interpretabile (ne' stringa semplice ne' oggetto "taggato" con
        # una sola chiave -- vedi fetch_twap_records), si mostra il valore
        # grezzo invece di nasconderlo, cosi' se questo TWAP e' in realta'
        # gia' chiuso su Hyperliquid ma non lo rileviamo, si vede subito
        # perche' (utile per segnalarlo e affinare is_closed_twap_status).
        if record and status is None and record.get("status_raw") not in (None, ""):
            agg["unresolved_status_raws"].add(repr(record.get("status_raw")))

    if not per_coin and not closed_entries:
        blocks.append("▶️ Nessuna esecuzione TWAP ancora aperta.")
        return "\n\n".join(blocks)

    active_blocks = ["▶️ Attivi/eseguiti per coin:"]
    if not per_coin:
        active_blocks.append("Nessun TWAP attualmente aperto.")
    # Ordinati per nome mostrato (non per chiave grezza) cosi' un id
    # posizionale come "@162" non finisce fuori posto rispetto ai ticker
    # normali solo per via del carattere "@".
    for raw_coin in sorted(per_coin.keys(), key=lambda c: resolve_twap_coin_display(c, spot_meta)[0]):
        coin_txt = coin_display_text(raw_coin)
        coin_display_name = resolve_twap_coin_display(raw_coin, spot_meta)[0]
        coin_lines = [f"— {coin_txt}:"]
        current_mid = None
        if raw_coin in mids:
            try:
                current_mid = float(mids[raw_coin])
            except (TypeError, ValueError):
                current_mid = None

        for side_raw in sorted(per_coin[raw_coin].keys()):
            agg = per_coin[raw_coin][side_raw]
            executed_sz = agg["executed_sz"]
            avg_px = (agg["notional"] / executed_sz) if executed_sz else 0.0
            plurale = "e" if agg["count"] == 1 else "i"
            coin_lines.append(
                f"   {side_label(side_raw)}: {executed_sz:g} @ media {avg_px:g} ({agg['count']} ordin{plurale})"
            )
            if agg["has_target"] and agg["target_sz"] > 0:
                pct = min(100.0, executed_sz / agg["target_sz"] * 100)
                remaining = max(0.0, agg["target_sz"] - executed_sz)
                coin_lines.append(f"      Completamento: {pct:.1f}% (mancano circa {remaining:g} {coin_display_name})")
            if current_mid is not None and avg_px:
                diff = current_mid - avg_px
                diff_pct = (diff / avg_px * 100) if avg_px else 0
                segno = "+" if diff >= 0 else ""
                coin_lines.append(
                    f"      vs prezzo attuale ({current_mid:g}): {segno}{diff:g} ({segno}{diff_pct:.2f}%)"
                )
            if agg["last_ms"]:
                coin_lines.append(f"      Ultima esecuzione: {fmt_ts(agg['last_ms'])}")
            if agg["unresolved_status_raws"]:
                raws = ", ".join(sorted(agg["unresolved_status_raws"]))
                coin_lines.append(f"      ⚠️ status non riconosciuto (segnalalo): {raws}")

        active_blocks.append("\n".join(coin_lines))

    blocks.append("\n\n".join(active_blocks))

    if closed_entries:
        # Piu' recenti prima (per ultima esecuzione nota); quelli senza
        # last_ms finiscono in coda, non in cima, per non farli sembrare
        # i piu' recenti.
        closed_entries.sort(key=lambda e: e["last_ms"] or 0, reverse=True)
        shown = closed_entries[:CLOSED_TWAP_DISPLAY_LIMIT]
        hidden_count = len(closed_entries) - len(shown)
        closed_lines = ["✅ Completati/terminati di recente:"]
        for entry in shown:
            coin_txt = coin_display_text(entry["raw_coin"])
            side_txt = side_label(entry["side"])
            line = f"— {coin_txt}"
            if side_txt:
                line += f": {side_txt}"
            line += f" {entry['executed_sz']:g} @ media {entry['avg_px']:g}"
            line += f" — {entry['status_label']}"
            if entry["last_ms"]:
                line += f" ({fmt_ts(entry['last_ms'])})"
            closed_lines.append(line)
        if hidden_count > 0:
            closed_lines.append(f"(+{hidden_count} altri completati/terminati, non mostrati)")
        blocks.append("\n".join(closed_lines))

    return "\n\n".join(blocks)


def format_positions_message(
    clearinghouse_state: dict,
    open_orders: list,
    mids: dict,
    spot_state: dict | None = None,
    spot_meta: dict | None = None,
) -> str:
    """Risposta al comando /positions (e contenuto del riepilogo
    automatico ogni POSITIONS_RECAP_INTERVAL_HOURS ore, vedi main): in
    testa, due totali su TUTTE le posizioni aperte (anche quelle
    "polvere" nascoste sotto -- cosi' riflettono l'intero portafoglio
    anche se una singola posizione non compare nell'elenco):
    "💵 Valore nozionale posizioni aperte (lordo)" (somma dei valori
    assoluti via get_position_value_usd -- long e short NON si
    compensano, una long da 50k e una short da 50k danno 100k) e
    "⚖️ Esposizione netta (long − short)" (long positivi, short negativi,
    sommati -- vedi get_position_direction_sign; le stesse due posizioni
    danno 0 qui, essendo delta-neutrali). Se anche una sola posizione non
    ha un valore (o una direzione, per il netto) stimabile, il totale
    interessato lo segnala esplicitamente invece di darlo per buono.
    Affiancato, se spot_state e' passato, da "💰 Saldo USDC spot" (vedi
    get_spot_usdc_balance) per un confronto immediato con il lordo --
    perps e spot sono conti DISTINTI su Hyperliquid (il secondo non e'
    "coperto" dal primo, anche se in pratica tende a muoversi insieme per
    via di depositi/prelievi legati al trading), quindi i due valori NON
    coincidono in generale. Poi le posizioni PERPS aperte (con leva e,
    quando stimabile, il valore nozionale in
    USDC -- stessa stima usata per il filtro "polvere" e per
    l'ordinamento sotto) con entry vs prezzo attuale, PnL, prezzo di
    liquidazione ed eventuali stop/TP collegati (dedotti dagli ordini
    aperti reduce-only con trigger), seguite -- se spot_state e' passato
    -- da un blocco separato
    "Saldi spot" (vedi format_spot_balances_block): su Hyperliquid perps e
    spot sono due conti/saldi distinti, quindi vengono recuperati con due
    chiamate diverse e mostrati in due blocchi diversi. spot_meta
    (spotMeta) e' opzionale e serve solo a valorizzare correttamente i
    ticker spot che non sono coppie "canoniche" (vedi
    resolve_spot_mid_key).

    Le posizioni perps il cui valore nozionale stimato e' sotto
    MIN_VALUE_USD_TO_SHOW (vedi get_position_value_usd) vengono
    considerate "polvere" e omesse -- una posizione di cui NON si riesce a
    stimare il valore viene invece sempre mostrata, per non rischiare di
    nascondere qualcosa di rilevante. Lo stesso filtro (stessa soglia) si
    applica anche ai saldi spot (vedi format_spot_balances_block): un
    saldo sotto soglia viene omesso, uno di cui non si riesce a stimare il
    controvalore resta sempre visibile. Le posizioni mostrate sono
    ordinate per valore nozionale USDC DECRESCENTE (le piu' grandi prima);
    quelle di cui non si riesce a stimare il valore vanno in fondo,
    perche' non confrontabili con le altre.

    Lo schema esatto di clearinghouseState/frontendOpenOrders non e'
    verificabile da qui (nessun accesso di rete nel sandbox di sviluppo),
    quindi il parsing e' difensivo: gestisce sia il caso in cui i campi
    della posizione siano annidati sotto "position" sia il caso in cui
    siano diretti, e omette in silenzio quello che non riesce a
    interpretare con sicurezza invece di mostrare dati indovinati."""
    perps_ok = isinstance(clearinghouse_state, dict) and isinstance(clearinghouse_state.get("assetPositions"), list)
    all_positions = extract_open_positions(clearinghouse_state) if perps_ok else []
    # Valore stimato di TUTTE le posizioni aperte (anche quelle "polvere"
    # che verranno nascoste sotto -- il totale del portafoglio deve
    # restare corretto anche se una singola posizione non viene elencata).
    all_positions_with_value = [(p, get_position_value_usd(p, mids)) for p in all_positions]
    # LORDO: somma dei valori assoluti di ogni posizione (l'esposizione
    # totale "in gioco", senza compensare long e short tra loro -- una
    # posizione long da 50k e una short da 50k valgono 100k qui, non 0).
    total_value_usd = sum(v for _, v in all_positions_with_value if v is not None)
    any_unknown_value = any(v is None for _, v in all_positions_with_value)
    # NETTO: long positivi, short negativi, sommati -- rappresenta la
    # direzione/esposizione netta del portafoglio (le due posizioni
    # dell'esempio sopra si compensano e danno 0 qui). Una posizione
    # esclude dal netto se il valore o la direzione (vedi
    # get_position_direction_sign) non sono determinabili, anche se
    # contribuiva al lordo.
    net_terms = []
    any_unknown_for_net = False
    for pos, value in all_positions_with_value:
        sign = get_position_direction_sign(pos)
        if value is None or sign is None:
            any_unknown_for_net = True
            continue
        net_terms.append(sign * value)
    net_value_usd = sum(net_terms)

    def total_value_line():
        # None se non c'e' nessuna posizione aperta (niente da totalizzare).
        if not all_positions:
            return None
        line = f"💵 Valore nozionale posizioni aperte (lordo): {total_value_usd:g} USDC"
        if any_unknown_value:
            line += " (una o piu' posizioni senza valore stimabile non incluse)"
        net_line = f"⚖️ Esposizione netta (long − short): {net_value_usd:+g} USDC"
        if any_unknown_for_net:
            net_line += " (una o piu' posizioni escluse: valore o direzione non determinabile)"
        line += f"\n{net_line}"
        # Confronto col saldo USDC del conto SPOT (conto distinto dai
        # perps -- vedi get_spot_usdc_balance): mostrato solo se
        # spot_state e' stato passato, altrimenti non sapremmo dire se e'
        # zero o semplicemente non disponibile.
        if spot_state is not None:
            usdc_balance = get_spot_usdc_balance(spot_state)
            line += f"\n💰 Saldo USDC spot: {(usdc_balance or 0.0):g} USDC"
        return line

    positions_with_value = [(p, v) for p, v in all_positions_with_value if v is None or v >= MIN_VALUE_USD_TO_SHOW]
    # Valore nozionale USDC decrescente; le posizioni di cui non si riesce a
    # stimare il valore vanno in fondo (non essendo confrontabili con le
    # altre, ma senza per questo perderle -- restano comunque nell'elenco).
    positions_with_value.sort(key=lambda pv: (pv[1] is None, -(pv[1] or 0)))
    positions = [p for p, _ in positions_with_value]
    hidden_count = len(all_positions) - len(positions)
    spot_block = format_spot_balances_block(spot_state, mids, spot_meta) if spot_state is not None else None

    if not positions:
        if not perps_ok:
            perps_block = "📋 Nessuna posizione trovata (o dati non disponibili al momento)."
        elif hidden_count:
            perps_block = (
                f"📋 Nessuna posizione aperta sopra i {MIN_VALUE_USD_TO_SHOW:g} USDC "
                f"({hidden_count} posizione/i sotto soglia non mostrata/e)."
            )
            tv_line = total_value_line()
            if tv_line:
                perps_block += f"\n{tv_line}"
        else:
            perps_block = "📋 Nessuna posizione aperta al momento."
        return f"{perps_block}\n\n{spot_block}" if spot_block else perps_block

    orders_by_coin = defaultdict(list)
    for o in open_orders:
        if not isinstance(o, dict):
            continue
        coin = o.get("coin")
        if coin:
            orders_by_coin[coin].append(o)

    blocks = ["📋 Posizioni aperte:"]
    tv_line = total_value_line()
    if tv_line:
        blocks[0] += f"\n{tv_line}"
    for pos, value_usd in positions_with_value:
        coin = pos.get("coin", "?")
        try:
            szi = float(pos.get("szi", 0))
        except (TypeError, ValueError):
            szi = 0.0
        direction = "LONG" if szi > 0 else "SHORT"
        abs_sz = abs(szi)

        leverage = pos.get("leverage")
        lev_value = leverage.get("value") if isinstance(leverage, dict) else leverage

        header = f"— {ticker_mention(coin, kind='perp')}: {direction} {abs_sz:g}"
        if lev_value:
            header += f" ({lev_value}x)"
        if value_usd is not None:
            header += f" (~{value_usd:g} USDC)"
        lines = [header]

        try:
            entry_px = float(pos.get("entryPx")) if pos.get("entryPx") is not None else None
        except (TypeError, ValueError):
            entry_px = None

        current_mid = None
        if coin in mids:
            try:
                current_mid = float(mids[coin])
            except (TypeError, ValueError):
                current_mid = None

        if entry_px is not None:
            lines.append(f"   Entry: {entry_px:g}")
            if current_mid is not None and entry_px:
                diff_pct = (current_mid - entry_px) / entry_px * 100
                if szi < 0:  # short: si guadagna quando il prezzo scende
                    diff_pct = -diff_pct
                segno = "+" if diff_pct >= 0 else ""
                lines.append(f"   Attuale: {current_mid:g} ({segno}{diff_pct:.2f}% vs entry)")
        elif current_mid is not None:
            lines.append(f"   Attuale: {current_mid:g}")

        try:
            unrealized_pnl = float(pos.get("unrealizedPnl")) if pos.get("unrealizedPnl") is not None else None
        except (TypeError, ValueError):
            unrealized_pnl = None
        if unrealized_pnl is not None:
            segno = "+" if unrealized_pnl >= 0 else ""
            roe_txt = ""
            roe = pos.get("returnOnEquity")
            if roe is not None:
                try:
                    roe_txt = f" ({segno}{float(roe) * 100:.2f}%)"
                except (TypeError, ValueError):
                    roe_txt = ""
            lines.append(f"   PnL non realizzato: {segno}{unrealized_pnl:g} USDC{roe_txt}")

        liq_px = pos.get("liquidationPx")
        if liq_px is not None:
            try:
                lines.append(f"   Prezzo di liquidazione: {float(liq_px):g}")
            except (TypeError, ValueError):
                pass

        stop_lines = []
        for o in orders_by_coin.get(coin, []):
            is_reduce_only = bool(o.get("reduceOnly"))
            has_trigger = bool(o.get("isTrigger")) or bool(o.get("isPositionTpsl")) or o.get("triggerPx") not in (None, "", "0")
            if not (is_reduce_only and has_trigger):
                continue
            trigger_px = o.get("triggerPx")
            order_type = o.get("orderType", "?")
            sz = o.get("sz", "?")
            try:
                stop_lines.append(f"   Stop/TP: {order_type} trigger @ {float(trigger_px):g} (size {sz})")
            except (TypeError, ValueError):
                stop_lines.append(f"   Stop/TP: {order_type} (size {sz})")

        if stop_lines:
            lines.extend(stop_lines)
        else:
            lines.append("   ⚠️ Nessuno stop/TP trovato per questa posizione")

        blocks.append("\n".join(lines))

    if hidden_count:
        blocks.append(
            f"({hidden_count} posizione/i sotto i {MIN_VALUE_USD_TO_SHOW:g} USDC non mostrata/e)"
        )

    if spot_block:
        blocks.append(spot_block)

    return "\n\n".join(blocks)


# Telegram rifiuta con "Bad Request: text is too long" qualunque messaggio
# oltre questo limite di caratteri -- senza un tetto lato nostro, un
# messaggio troppo lungo (es. troppi elementi elencati) fallisce l'invio,
# e dal punto di vista dell'utente non arriva semplicemente nulla, senza
# nessun indizio del perche'. truncate_for_telegram() e' la rete di
# sicurezza finale, applicata a OGNI messaggio in send_telegram_message.
TELEGRAM_MAX_MESSAGE_LENGTH = 4096


def truncate_for_telegram(text: str, limit: int = TELEGRAM_MAX_MESSAGE_LENGTH) -> str:
    if len(text) <= limit:
        return text
    marker = "\n\n… (messaggio troncato, era troppo lungo per Telegram)"
    cut = max(0, limit - len(marker))
    return text[:cut] + marker


# Segnaposto (caratteri di controllo invisibili, non usati in nessun testo
# normale) creati da ticker_mention() ed espansi in link HTML veri e
# propri da render_ticker_links -- vedi li' per i dettagli di sicurezza.
_TICKER_MARKER_RE = re.compile(r"\x01([^\x01\x02|]{1,30})\|(spot|perp)\x02")


def build_hyperliquid_chart_url(coin: str, kind: str = "perp") -> str:
    """URL della pagina di trading/grafico su Hyperliquid per un ticker.
    Per i PERPS (kind="perp"): app.hyperliquid.xyz/trade/<COIN> --
    formato confermato (es. /trade/BTC). Per lo SPOT (kind="spot"):
    app.hyperliquid.xyz/trade/<COIN>/USDC -- confermato per le coppie
    "canoniche" con nome leggibile (es. PURR/USDC, HYPE/USDC); per i
    token spot NON canonici (la maggior parte, identificati internamente
    da un id posizionale "@N" invece del ticker -- vedi
    resolve_spot_mid_key) questo e' un'ESTRAPOLAZIONE best-effort NON
    verificata in modo diretto (nessun accesso browser dal vivo
    disponibile per testarlo in questo ambiente): se per un token del
    genere il link non apre il mercato giusto, e' qui che va corretto."""
    safe_coin = urllib.parse.quote(coin, safe="")
    if kind == "spot":
        return f"{HYPERLIQUID_TRADE_URL_BASE}/{safe_coin}/USDC"
    return f"{HYPERLIQUID_TRADE_URL_BASE}/{safe_coin}"


def ticker_mention(coin, kind: str = "perp") -> str:
    """Segnaposto per un ticker che, nel messaggio finale, diventa un link
    HTML cliccabile al grafico Hyperliquid del token (vedi
    render_ticker_links, applicato da send_telegram_message subito prima
    dell'invio). Va usato al posto di interpolare `coin` direttamente nel
    testo di un messaggio, ovunque compaia un ticker. kind="spot" per i
    token spot, "perp" (default) per i perps -- vedi
    build_hyperliquid_chart_url per come influenza l'URL generato."""
    if not coin:
        return str(coin)
    return f"\x01{coin}|{kind}\x02"


def render_ticker_links(text: str) -> str:
    """Espande i segnaposto creati da ticker_mention() in veri link HTML
    cliccabili, ed esegue l'escape HTML (&, <, >) di TUTTO il resto del
    testo -- incluso qualunque contenuto esterno imprevedibile finito nel
    messaggio (es. il testo grezzo di un'eccezione riportato all'utente).
    Cosi' un link puo' comparire SOLO dove lo abbiamo messo esplicitamente
    noi via ticker_mention(), mai per errore a partire da testo esterno, e
    nessun carattere imprevisto puo' rompere il parsing HTML di Telegram
    (parse_mode="HTML", vedi send_telegram_message). Un segnaposto tagliato
    a meta' da truncate_for_telegram (troncamento applicato PRIMA di
    questa funzione, apposta) resta semplicemente testo normale invece di
    rompere l'HTML -- degrado sicuro."""
    parts = []
    last_end = 0
    for m in _TICKER_MARKER_RE.finditer(text):
        parts.append(html.escape(text[last_end:m.start()], quote=False))
        coin, kind = m.group(1), m.group(2)
        url = build_hyperliquid_chart_url(coin, kind)
        parts.append(f'<a href="{html.escape(url, quote=True)}">{html.escape(coin, quote=False)}</a>')
        last_end = m.end()
    parts.append(html.escape(text[last_end:], quote=False))
    return "".join(parts)


def send_telegram_message(
    bot_token: str,
    chat_id: str,
    text: str,
    dry_run: bool = False,
    separator: str = MESSAGE_SEPARATOR,
    reply_markup: dict | None = COMMAND_KEYBOARD,
) -> None:
    """Manda un messaggio Telegram. Per default allega la tastiera con i
    pulsanti /twap e /positions (vedi COMMAND_KEYBOARD) cosi' resta sempre
    visibile; passare reply_markup=None per un messaggio senza tastiera.
    Il testo finale viene troncato se supera il limite di Telegram (vedi
    truncate_for_telegram) invece di far fallire l'invio in silenzio, POI
    convertito in HTML (vedi render_ticker_links) per rendere cliccabili i
    ticker inseriti con ticker_mention() -- l'ordine (tronca prima,
    espandi i link dopo) evita di tagliare un tag HTML a meta'."""
    full_text = truncate_for_telegram(f"{separator}\n{text}")
    full_text = render_ticker_links(full_text)
    if dry_run:
        print("--- [DRY RUN] messaggio che verrebbe inviato ---")
        print(full_text)
        if reply_markup:
            print(f"[tastiera: {reply_markup}]")
        print("-------------------------------------------------")
        return
    url = TELEGRAM_API_URL.format(token=bot_token)
    payload = {
        "chat_id": chat_id,
        "text": full_text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    http_post_json(url, payload)


def main() -> int:
    wallet = env_or_die("HL_WALLET_ADDRESS")
    dry_run = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")

    if dry_run:
        bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    else:
        bot_token = env_or_die("TELEGRAM_BOT_TOKEN")
        chat_id = env_or_die("TELEGRAM_CHAT_ID")

    state_file = os.environ.get("STATE_FILE", DEFAULT_STATE_FILE)
    lookback_minutes = int(os.environ.get("LOOKBACK_MINUTES", DEFAULT_LOOKBACK_MINUTES))
    alert_repeat_every_n_runs = max(
        1, int(os.environ.get("ALERT_REPEAT_EVERY_N_RUNS", DEFAULT_ALERT_REPEAT_EVERY_N_RUNS))
    )
    alert_urgent_deviation_pct = float(
        os.environ.get("ALERT_URGENT_DEVIATION_PCT", DEFAULT_ALERT_URGENT_DEVIATION_PCT)
    )
    positions_recap_interval_hours = float(
        os.environ.get("POSITIONS_RECAP_INTERVAL_HOURS", DEFAULT_POSITIONS_RECAP_INTERVAL_HOURS)
    )

    now_ms = int(time.time() * 1000)
    state = load_state(state_file)

    last_time_ms = state.get("last_time_ms")
    seen_tids = set(state.get("seen_tids", []))
    # str(twap_id) -> {"executed_sz","notional" (cumulativi di tutta la vita
    # del TWAP), "last_ms", "coin", "side"}. Consultato a richiesta dal
    # comando /twap (nessun invio automatico periodico).
    twap_progress = state.get("twap_progress", {})
    is_very_first_run = "known_twap_ids" not in state
    known_twap_ids_list = [str(x) for x in state.get("known_twap_ids", [])]
    known_twap_ids = set(known_twap_ids_list)
    # TWAP visti con status "untriggered" (vedi is_untriggered_twap_status)
    # ancora in attesa che il trigger di prezzo scatti: nessuna notifica
    # "Nuovo TWAP avviato" e' stata mandata per questi (non sono ancora
    # davvero partiti) -- quando lo status smette di essere "untriggered"
    # si manda la notifica "TWAP triggerato" (vedi piu' sotto) e vengono
    # tolti da qui.
    pending_twap_ids = {str(x) for x in state.get("pending_twap_ids", [])}
    # TWAP il cui status finale (vedi is_closed_twap_status) e' gia' stato
    # notificato con "🏁 TWAP concluso" (vedi piu' sotto): evita di
    # rimandare la stessa notifica ad ogni giro finche' quel record resta
    # nello storico best-effort. is_closed_notified_first_run distingue il
    # primissimo avvio di QUESTA funzionalita' (potrebbe attivarsi dopo
    # che l'utente ha gia' molti TWAP chiusi in storico) da un normale
    # nuovo TWAP che si chiude da qui in avanti -- stesso principio di
    # is_very_first_run/known_twap_ids sopra, per non mandare una raffica
    # di notifiche per tutta la storia passata dell'account alla prima
    # attivazione.
    is_closed_notified_first_run = "notified_closed_twap_ids" not in state
    notified_closed_twap_ids = {str(x) for x in state.get("notified_closed_twap_ids", [])}
    last_update_id = int(state.get("last_update_id", 0) or 0)
    # Lista di alert di prezzo attivi: {"id","coin","direction" ("below"/
    # "above"),"price","created_ms"}. Gestita a richiesta con /alert,
    # /alerts, /delalert; controllata automaticamente ad ogni giro (vedi
    # sotto) e rimossa (via on_success) non appena la notifica scatta.
    price_alerts = state.get("price_alerts", [])
    next_alert_id = int(state.get("next_alert_id", 1) or 1)
    # Timestamp (ms) dell'ultimo riepilogo posizioni automatico mandato
    # (vedi POSITIONS_RECAP_INTERVAL_HOURS piu' sotto). None -> nessuno
    # ancora mandato, se ne manda uno subito per stabilire la base.
    last_positions_recap_ms = state.get("last_positions_recap_ms")

    # Cache dei prezzi mid: recuperati dalla rete solo se effettivamente
    # serve (un comando /twap o /positions, o un alert attivo), mai in
    # anticipo.
    mids_cache = None

    def get_mids() -> dict:
        nonlocal mids_cache
        if mids_cache is None:
            mids_cache = fetch_all_mids()
        return mids_cache

    # Cache dello stato posizioni (clearinghouseState): recuperato dalla
    # rete solo se serve (comando /positions).
    positions_state_cache = None

    def get_positions_state() -> dict:
        nonlocal positions_state_cache
        if positions_state_cache is None:
            positions_state_cache = fetch_clearinghouse_state(wallet)
        return positions_state_cache

    # Cache dello stato spot (spotClearinghouseState): conto separato dai
    # perps su Hyperliquid, recuperato dalla rete solo se serve (comando
    # /positions).
    spot_state_cache = None

    def get_spot_state() -> dict:
        nonlocal spot_state_cache
        if spot_state_cache is None:
            spot_state_cache = fetch_spot_state(wallet)
        return spot_state_cache

    # Cache dei metadati spot (spotMeta): serve solo per risolvere un
    # ticker spot alla chiave usata per lui in allMids (vedi
    # resolve_spot_mid_key), quindi si recupera dalla rete solo se un
    # ticker non si trova direttamente in allMids (es. /alert su un ticker
    # spot non "canonico").
    spot_meta_cache = None

    def get_spot_meta() -> dict:
        nonlocal spot_meta_cache
        if spot_meta_cache is None:
            spot_meta_cache = fetch_spot_meta()
        return spot_meta_cache

    def find_open_position(coin):
        return next((p for p in extract_open_positions(get_positions_state()) if p.get("coin") == coin), None)

    def find_spot_balance(coin):
        return next((b for b in extract_spot_balances(get_spot_state()) if b.get("coin") == coin), None)

    def try_create_alert(args_text: str) -> str:
        """Interpreta args_text come "<COIN> <sopra|sotto> <VALORE|VALORE%>"
        e, se valido, aggiunge l'alert a price_alerts. Funziona per QUALSIASI
        coin con un prezzo su Hyperliquid, sia PERPS che SPOT (non solo
        quelle con una posizione aperta): la percentuale e' calcolata
        rispetto al prezzo attuale (allMids) nel momento in cui l'alert
        viene creato. Per i ticker spot non "canonici" allMids non e'
        indicizzato per ticker (vedi resolve_spot_mid_key): se il ticker
        non si trova direttamente si prova a risolverlo via spotMeta, e la
        chiave risolta ("mids_key") viene salvata sull'alert cosi' anche i
        controlli successivi (vedi il loop degli alert in main()) sanno
        dove cercare il prezzo. Ritorna il testo di conferma o dell'errore
        da rimandare all'utente."""
        nonlocal next_alert_id
        try:
            coin, direction, value, is_percent = parse_price_alert_command(args_text)
        except ValueError as e:
            return f"⚠️ {e}"

        mids_now = get_mids()
        mids_key = coin
        if coin not in mids_now:
            resolved = resolve_spot_mid_key(coin, get_spot_meta())
            if resolved is not None and resolved in mids_now:
                mids_key = resolved

        pct = None
        ref_price = None
        if is_percent:
            base_raw = mids_now.get(mids_key)
            if base_raw is None:
                return (
                    f"⚠️ Coin '{coin}' non trovata tra i prezzi correnti Hyperliquid "
                    f"(ne' perps ne' spot). Controlla il ticker."
                )
            try:
                ref_price = float(base_raw)
            except (TypeError, ValueError):
                return f"⚠️ Prezzo attuale di {coin} non disponibile al momento, riprova più tardi."
            pct = value
            price = ref_price * (1 - value / 100) if direction == "below" else ref_price * (1 + value / 100)
        else:
            if mids_now and mids_key not in mids_now:
                return (
                    f"⚠️ Coin '{coin}' non trovata tra i prezzi correnti Hyperliquid "
                    f"(ne' perps ne' spot). Controlla il ticker."
                )
            price = value

        alert = {
            "id": next_alert_id,
            "coin": coin,
            "mids_key": mids_key,
            "direction": direction,
            "price": price,
            "created_ms": now_ms,
            "triggered": False,
        }
        if pct is not None:
            alert["pct"] = pct
            alert["ref_price"] = ref_price
        price_alerts.append(alert)
        next_alert_id += 1
        verso = "scende sotto" if direction == "below" else "sale sopra"
        extra = f" ({pct:g}% dal prezzo di {ref_price:g})" if pct is not None else ""
        coin_link = ticker_mention(coin, kind="spot" if mids_key != coin else "perp")
        confirmation = f"🔔 Alert impostato: ti avviso quando {coin_link} {verso} {price:g}{extra} (id {alert['id']})."
        # L'utente vuole rivedere subito l'elenco aggiornato ogni volta che
        # un alert viene aggiunto o modificato, non solo con /alerts.
        return f"{confirmation}\n\n{format_alerts_list_message(price_alerts, mids_now, get_spot_meta())}"

    # Un'unica chiamata di rete (best-effort) usata sia per rilevare TWAP
    # appena avviati sia, piu' sotto, come sorgente per la % di
    # completamento nei messaggi di stato -- evita di richiamare l'endpoint
    # TWAP una volta per ogni TWAP attivo.
    try:
        twap_records = fetch_twap_records(wallet)
    except Exception as e:
        print(f"AVVISO: impossibile recuperare l'elenco dei TWAP: {e}", file=sys.stderr)
        twap_records = []
    twap_records_by_id = build_twap_records_by_id(twap_records)

    # Coda di tutti i messaggi AUTOMATICI da mandare in questo giro (nuovi
    # TWAP, ordini eseguiti): si costruisce prima di inviare qualunque cosa,
    # cosi' possiamo far precedere tutto da un'unica intestazione "nuovo
    # aggiornamento" e mandare i messaggi in ordine. Le risposte ai comandi
    # Telegram NON passano da questa coda: sono risposte dirette, inviate
    # subito, senza intestazione. Ogni elemento ha "text" e "on_success"
    # (eseguito solo se l'invio va a buon fine, per mantenere la stessa
    # logica di retry-al-prossimo-giro di prima in caso di errore Telegram).
    outgoing = []

    def make_new_twap_cb(tid_str):
        def cb():
            known_twap_ids.add(tid_str)
            known_twap_ids_list.append(tid_str)
        return cb

    def make_twap_triggered_cb(tid_str):
        def cb():
            pending_twap_ids.discard(tid_str)
        return cb

    if is_very_first_run:
        # Primissimo run in assoluto per questa funzionalita': non
        # notificare come "nuovi" i TWAP gia' esistenti/passati, altrimenti
        # si riceve una raffica di notifiche per tutta la storia
        # dell'account. Si "conoscono" silenziosamente e si notificano solo
        # i TWAP che compariranno (o che scatteranno, se gia' in attesa di
        # un trigger di prezzo -- vedi is_untriggered_twap_status) da qui
        # in avanti.
        for tid_str, record in twap_records_by_id.items():
            if tid_str not in known_twap_ids:
                known_twap_ids.add(tid_str)
                known_twap_ids_list.append(tid_str)
                if is_untriggered_twap_status(record.get("status")):
                    pending_twap_ids.add(tid_str)
        if twap_records_by_id:
            print(f"Primo run: {len(twap_records_by_id)} TWAP esistenti registrati senza notifica.")
    else:
        # --- Nuovi TWAP e TWAP con trigger di prezzo che scatta: alcuni
        # TWAP su Hyperliquid vengono impostati con un trigger di prezzo e
        # partono solo quando la soglia viene superata -- fino ad allora
        # il loro status (best-effort, vedi fetch_twap_records) e'
        # "untriggered" e NON sono ancora davvero partiti. Percio': un
        # TWAP nuovo con status "untriggered" viene solo tenuto d'occhio
        # (pending_twap_ids), SENZA notifica di creazione (non ha ancora
        # eseguito nulla); quando poi il suo status smette di essere
        # "untriggered" si manda la notifica "🎯 TWAP triggerato, e'
        # partito" (vedi format_new_twap_message(triggered=True)). Un
        # TWAP nuovo senza trigger (o gia' attivo appena rilevato) parte
        # invece subito: notifica immediata "🆕 Nuovo TWAP avviato", come
        # sempre. ---
        for tid_str, record in twap_records_by_id.items():
            untriggered_now = is_untriggered_twap_status(record.get("status"))
            if tid_str not in known_twap_ids:
                if untriggered_now:
                    # Nessun invio Telegram qui, quindi nessun retry da
                    # gestire: la mutazione e' sicura anche fuori da
                    # on_success.
                    known_twap_ids.add(tid_str)
                    known_twap_ids_list.append(tid_str)
                    pending_twap_ids.add(tid_str)
                else:
                    outgoing.append(
                        {
                            "text": format_new_twap_message(record, spot_meta=get_spot_meta()),
                            "on_success": make_new_twap_cb(tid_str),
                            "kind": "orders",
                        }
                    )
            elif tid_str in pending_twap_ids and not untriggered_now:
                outgoing.append(
                    {
                        "text": format_new_twap_message(record, triggered=True, spot_meta=get_spot_meta()),
                        "on_success": make_twap_triggered_cb(tid_str),
                        "kind": "orders",
                    }
                )
            elif untriggered_now and tid_str not in pending_twap_ids:
                # Riconciliazione: un TWAP gia' "known" da prima (quindi
                # non rientra nel ramo "nuovo" sopra) ma mai finito in
                # pending_twap_ids -- tipicamente perche' scoperto quando
                # is_untriggered_twap_status confrontava ancora solo con
                # "untriggered" invece del valore reale
                # "waitingForTrigger" (bug corretto il 25/08/2026), quindi
                # era stato scambiato per gia' partito. Lo si aggiunge ora
                # silenziosamente (nessun invio Telegram, quindi nessuna
                # gestione di retry necessaria) cosi' torna visibile nella
                # sezione "In attesa di trigger" di /twap invece di
                # restare bloccato in uno stato invisibile per sempre.
                pending_twap_ids.add(tid_str)

    # --- TWAP conclusi: notifica automatica "🏁 TWAP concluso" -------------
    # Passaggio separato da is_very_first_run sopra perche' questa
    # funzionalita' e' stata aggiunta in un secondo momento: un account
    # che usa gia' il bot da tempo ha gia' "known_twap_ids" popolato (quindi
    # is_very_first_run e' False) ma non ha ancora "notified_closed_twap_ids"
    # -- serve quindi un proprio primo-avvio silenzioso indipendente,
    # altrimenti si manderebbe una notifica per OGNI TWAP gia' concluso in
    # passato in un colpo solo. Notifica solo lo status "confermato"
    # (is_closed_twap_status, livello 1 -- vedi format_twap_closed_message),
    # mai una presunzione via durata scaduta/inattivita': un push automatico
    # sbagliato sarebbe peggio di uno assente, mentre su /twap (a richiesta)
    # le presunzioni restano comunque visibili.
    def make_twap_closed_cb(tid_str):
        def cb():
            notified_closed_twap_ids.add(tid_str)
        return cb

    if is_closed_notified_first_run:
        for tid_str, record in twap_records_by_id.items():
            if is_closed_twap_status(record.get("status")):
                notified_closed_twap_ids.add(tid_str)
        if twap_records_by_id:
            print(
                f"Primo run per la notifica TWAP conclusi: "
                f"{len(notified_closed_twap_ids)} TWAP gia' chiusi registrati senza notifica."
            )
    else:
        for tid_str, record in twap_records_by_id.items():
            if tid_str in notified_closed_twap_ids:
                continue
            if is_closed_twap_status(record.get("status")):
                outgoing.append(
                    {
                        "text": format_twap_closed_message(record, spot_meta=get_spot_meta()),
                        "on_success": make_twap_closed_cb(tid_str),
                        "kind": "orders",
                    }
                )

    if last_time_ms is None:
        start_time_ms = now_ms - lookback_minutes * 60 * 1000
        print(f"Nessuno stato precedente trovato: guardo indietro {lookback_minutes} minuti.")
    else:
        # Piccolo margine all'indietro per non perdere fill sullo stesso ms,
        # deduplicati poi tramite seen_tids.
        start_time_ms = last_time_ms - 1000

    # Stato di default (nessun fill nuovo): se la chiamata sotto fallisce
    # (es. errore transitorio dell'API di Hyperliquid -- piu' probabile con
    # un controllo molto frequente) NON deve far crashare tutto lo script,
    # altrimenti anche la gestione dei comandi Telegram piu' sotto (/twap,
    # /positions, ecc.) non verrebbe mai raggiunta in quel giro. Si salta
    # solo il rilevamento fill di questo giro (si riprova al prossimo,
    # nessun dato perso grazie alla finestra continua) mentre il resto
    # (alert di prezzo, comandi) va avanti comunque con lo stato esistente.
    max_time_seen = last_time_ms or start_time_ms
    latest_tids = list(seen_tids)
    regular_new = []
    twap_new = []
    try:
        fills = fetch_fills(wallet, start_time_ms, now_ms)
        twap_fills = fetch_twap_slice_fills(wallet, start_time_ms, now_ms)
        all_fills = fills + twap_fills
        all_fills.sort(key=lambda f: (f.get("time", 0), f.get("tid", 0)))

        new_fills = [f for f in all_fills if f.get("tid") not in seen_tids]
        print(f"Trovati {len(all_fills)} fill nell'intervallo, {len(new_fills)} nuovi.")

        regular_new = [f for f in new_fills if not f.get("twapId")]
        twap_new = [f for f in new_fills if f.get("twapId")]
    except Exception as e:
        print(f"AVVISO: impossibile recuperare i fill in questo giro (salto, riprovo al prossimo): {e}", file=sys.stderr)

    def make_regular_cb(group):
        def cb():
            nonlocal max_time_seen
            for f in group:
                tid = f.get("tid")
                if tid is not None:
                    latest_tids.append(tid)
                max_time_seen = max(max_time_seen, f.get("time", max_time_seen))
        return cb

    # --- Ordini normali: notifica ad ogni controllo se c'e' qualcosa di nuovo ---
    if regular_new:
        by_oid = defaultdict(list)
        for f in regular_new:
            by_oid[f.get("oid")].append(f)
        for oid, group in by_oid.items():
            outgoing.append(
                {"text": format_order_message(group), "on_success": make_regular_cb(group), "kind": "orders"}
            )

    # --- Slice TWAP: accumulo silenzioso ad ogni controllo (nessun invio
    # automatico -- lo stato cumulato si consulta a richiesta con /twap) ---
    if twap_new:
        by_twap = defaultdict(list)
        for f in twap_new:
            by_twap[f.get("twapId")].append(f)
        for twap_id, group in by_twap.items():
            key = str(twap_id)
            prog = twap_progress.get(key, {"executed_sz": 0.0, "notional": 0.0, "last_ms": None})
            for f in group:
                sz = float(f.get("sz", 0))
                px = float(f.get("px", 0))
                prog["executed_sz"] = prog.get("executed_sz", 0.0) + sz
                prog["notional"] = prog.get("notional", 0.0) + sz * px
                prog["last_ms"] = f.get("time")
            prog["coin"] = group[0].get("coin", "?")
            prog["side"] = group[0].get("side", "")
            twap_progress[key] = prog

            for f in group:
                tid = f.get("tid")
                if tid is not None:
                    latest_tids.append(tid)
                max_time_seen = max(max_time_seen, f.get("time", max_time_seen))

    def make_alert_remove_cb(alert_id):
        def cb():
            price_alerts[:] = [a for a in price_alerts if a.get("id") != alert_id]
        return cb

    def make_alert_mark_active_cb(alert):
        def cb():
            alert["triggered"] = True
            alert["runs_since_notify"] = 0
        return cb

    def make_alert_rearm_cb(alert):
        def cb():
            # Il prezzo e' rientrato oltre la soglia: l'alert NON viene
            # rimosso, torna solo "in ascolto" (triggered=False) cosi' puo'
            # scattare di nuovo se la soglia viene superata un'altra volta.
            alert["triggered"] = False
            alert["runs_since_notify"] = 0
        return cb

    def make_alert_reset_counter_cb(alert):
        def cb():
            alert["runs_since_notify"] = 0
        return cb

    def make_positions_recap_cb():
        def cb():
            nonlocal last_positions_recap_ms
            last_positions_recap_ms = now_ms
        return cb

    # --- Alert di prezzo: confronto col prezzo attuale (allMids, una sola
    # chiamata per tutti gli alert) solo se ce n'e' almeno uno attivo.
    # Comportamento:
    # - Alert non ancora scattato la cui condizione si avvera: se c'e' una
    #   posizione aperta su quella coin, l'alert diventa "attivo"
    #   (triggered=True) e NON viene rimosso -- restera' attivo finche' la
    #   condizione persiste E la posizione resta aperta. Senza posizione,
    #   si comporta come prima: notifica una volta e viene rimosso.
    # - Alert gia' "attivo": ripete la notifica ogni ALERT_REPEAT_EVERY_N_RUNS
    #   giri (di default 5) finche' la condizione resta vera E la posizione
    #   resta aperta -- non ad ogni singolo giro, per non intasare la chat;
    #   il conteggio (runs_since_notify) viene azzerato appena viene inviato
    #   un promemoria, e anche quando l'alert scatta la prima volta o rientra.
    #   - Se il prezzo rientra oltre la soglia: notifica "allarme
    #     rientrato" e l'alert torna "in ascolto" (NON viene rimosso), cosi'
    #     puo' scattare di nuovo in futuro senza doverlo reimpostare.
    #   - Se invece e' la posizione a chiudersi: notifica "alert
    #     disattivato" e stavolta l'alert viene rimosso davvero (non c'e'
    #     piu' nulla da proteggere).
    # Le mutazioni avvengono solo via on_success, cosi' un invio fallito
    # viene ritentato al giro successivo (stessa logica del resto). ---
    if price_alerts:
        mids_for_alerts = get_mids()
        for alert in price_alerts:
            coin = alert.get("coin")
            # "mids_key" e' la chiave risolta in try_create_alert (puo' differire
            # da "coin" per i ticker spot non "canonici" -- vedi
            # resolve_spot_mid_key); gli alert creati prima di questa modifica non
            # ce l'hanno salvata, quindi si ricade su "coin" per compatibilita'.
            mids_key = alert.get("mids_key") or coin
            price_raw = mids_for_alerts.get(mids_key)
            if price_raw is None:
                continue  # coin non presente in allMids: si riprova al prossimo giro
            try:
                current_price = float(price_raw)
                threshold = float(alert.get("price"))
            except (TypeError, ValueError):
                continue
            direction = alert.get("direction")
            condition_met = (direction == "below" and current_price <= threshold) or (
                direction == "above" and current_price >= threshold
            )

            if alert.get("triggered"):
                # Una posizione perps aperta OPPURE un saldo spot non nullo
                # su questa coin bastano a tenere l'alert "sticky" -- si
                # controlla lo spot solo se non c'e' gia' una posizione
                # perps (che e' comunque piu' ricca da mostrare), per non
                # sprecare una chiamata di rete quando non serve.
                position = find_open_position(coin)
                spot_balance = None if position is not None else find_spot_balance(coin)
                if position is None and spot_balance is None:
                    outgoing.append(
                        {
                            "text": format_alert_cleared_message(alert, current_price, "holding_closed", now_ms=now_ms),
                            "on_success": make_alert_remove_cb(alert.get("id")),
                            "kind": "alerts",
                            "coin": coin,
                            "coin_kind": alert_ticker_kind(alert),
                            "alert_direction": alert.get("direction"),
                            "alert_threshold": threshold,
                            "alert_current_price": current_price,
                        }
                    )
                elif not condition_met:
                    outgoing.append(
                        {
                            "text": format_alert_cleared_message(alert, current_price, "recovered", now_ms=now_ms),
                            "on_success": make_alert_rearm_cb(alert),
                            "kind": "alerts",
                            "coin": coin,
                            "coin_kind": alert_ticker_kind(alert),
                            "alert_direction": alert.get("direction"),
                            "alert_threshold": threshold,
                            "alert_current_price": current_price,
                        }
                    )
                else:
                    # Se il prezzo si e' allontanato dalla soglia di piu' di
                    # alert_urgent_deviation_pct, la situazione e' considerata
                    # urgente e il throttle a N giri viene bypassato del
                    # tutto: si notifica ad ogni giro finche' resta cosi'.
                    deviation_pct = (abs(current_price - threshold) / abs(threshold) * 100) if threshold else None
                    urgent = deviation_pct is not None and deviation_pct > alert_urgent_deviation_pct

                    runs_since_notify = alert.get("runs_since_notify", 0) + 1
                    if urgent or runs_since_notify >= alert_repeat_every_n_runs:
                        outgoing.append(
                            {
                                "text": format_alert_triggered_message(
                                    alert,
                                    current_price,
                                    position,
                                    repeated=True,
                                    repeat_every_n_runs=alert_repeat_every_n_runs,
                                    urgent=urgent,
                                    urgent_deviation_pct=alert_urgent_deviation_pct,
                                    spot_balance=spot_balance,
                                    now_ms=now_ms,
                                ),
                                "on_success": make_alert_reset_counter_cb(alert),
                                "kind": "alerts",
                                "coin": coin,
                                "coin_kind": alert_ticker_kind(alert),
                                "alert_direction": alert.get("direction"),
                                "alert_threshold": threshold,
                                "alert_current_price": current_price,
                            }
                        )
                    else:
                        # Non ancora il momento di ripetere l'avviso: si
                        # aggiorna solo il contatore (nessun invio Telegram,
                        # quindi la mutazione e' sicura anche fuori da
                        # on_success -- non c'e' nulla da ritentare).
                        alert["runs_since_notify"] = runs_since_notify
            elif condition_met:
                position = find_open_position(coin)
                spot_balance = None if position is not None else find_spot_balance(coin)
                if position is not None or spot_balance is not None:
                    outgoing.append(
                        {
                            "text": format_alert_triggered_message(
                                alert, current_price, position, repeated=False, spot_balance=spot_balance,
                                now_ms=now_ms,
                            ),
                            "on_success": make_alert_mark_active_cb(alert),
                            "kind": "alerts",
                            "coin": coin,
                            "coin_kind": alert_ticker_kind(alert),
                            "alert_direction": alert.get("direction"),
                            "alert_threshold": threshold,
                            "alert_current_price": current_price,
                        }
                    )
                else:
                    outgoing.append(
                        {
                            "text": format_alert_triggered_message(
                                alert, current_price, None, repeated=False, now_ms=now_ms
                            ),
                            "on_success": make_alert_remove_cb(alert.get("id")),
                            "kind": "alerts",
                            "coin": coin,
                            "coin_kind": alert_ticker_kind(alert),
                            "alert_direction": alert.get("direction"),
                            "alert_threshold": threshold,
                            "alert_current_price": current_price,
                        }
                    )

    # --- Riepilogo posizioni automatico: stesso contenuto della risposta a
    # /positions (posizioni perps sopra la soglia "polvere" + saldi spot),
    # mandato da solo ogni POSITIONS_RECAP_INTERVAL_HOURS ore (variabile
    # d'ambiente, default 2) senza doverlo chiedere a mano. Le chiamate di
    # rete necessarie avvengono solo quando e' davvero il momento di
    # mandarlo. Se non e' mai stato mandato (prima volta che lo stato ha
    # questo campo) se ne manda uno subito, per stabilire la base. La
    # mutazione dell'ultimo timestamp avviene solo via on_success, stessa
    # logica di retry-al-prossimo-giro del resto. ---
    positions_recap_interval_ms = positions_recap_interval_hours * 3600 * 1000
    if last_positions_recap_ms is None or (now_ms - last_positions_recap_ms) >= positions_recap_interval_ms:
        positions_recap_text = format_positions_message(
            get_positions_state(),
            fetch_open_orders(wallet),
            get_mids(),
            spot_state=get_spot_state(),
            spot_meta=get_spot_meta(),
        )
        outgoing.append(
            {
                "text": (
                    f"🕓 Riepilogo posizioni automatico (ogni {positions_recap_interval_hours:g}h):\n\n"
                    f"{positions_recap_text}"
                ),
                "on_success": make_positions_recap_cb(),
                "kind": "positions",
            }
        )

    # --- Invio delle notifiche automatiche: ognuna con la propria
    # intestazione, titolo E colore distinti per categoria (vedi
    # UPDATE_HEADER_TITLE_BY_KIND / UPDATE_HEADER_BAR_BY_KIND) cosi'
    # ordini/fill/nuovi TWAP ("Aggiornamento Ordini Eseguiti", rosso),
    # alert di prezzo ("Aggiornamento Alerts", arancione) e riepilogo
    # posizioni ("Aggiornamento Generale", verde) si riconoscono a colpo
    # d'occhio anche senza leggere il testo.
    #
    # Durante la fascia notturna (vedi is_quiet_hours/QUIET_HOURS_*) tutto
    # cio' che non e' un alert di prezzo viene trattenuto: NON si chiama
    # on_success, quindi lo stato non avanza e la stessa notifica verra'
    # ritentata (e stavolta mandata) al primo giro utile fuori dalla
    # fascia notturna -- nessuna notifica viene persa, solo rimandata. Gli
    # alert restano sempre immediati, a qualunque ora. ---
    if outgoing:
        quiet = is_quiet_hours(now_ms)
        held_count = 0
        for item in outgoing:
            if quiet and item.get("kind") != "alerts":
                held_count += 1
                continue
            separator = format_update_header(
                now_ms,
                item.get("kind", "orders"),
                coin=item.get("coin"),
                coin_kind=item.get("coin_kind", "perp"),
                alert_direction=item.get("alert_direction"),
                alert_threshold=item.get("alert_threshold"),
                alert_current_price=item.get("alert_current_price"),
            )
            try:
                send_telegram_message(bot_token, chat_id, item["text"], dry_run=dry_run, separator=separator)
            except Exception as e:
                print(f"ERRORE invio Telegram: {e}", file=sys.stderr)
                continue
            item["on_success"]()
        if held_count:
            print(
                f"Fascia notturna ({QUIET_HOURS_START_HOUR}-{QUIET_HOURS_END_HOUR} {DISPLAY_TIMEZONE}): "
                f"{held_count} notifica/e non-alert trattenuta/e, verranno mandate al prossimo giro utile."
            )

    # --- Comandi Telegram in arrivo: risposte dirette a /twap e /positions,
    # inviate subito (non accodate/con intestazione), solo se il messaggio
    # arriva dalla chat configurata (sicurezza: nessun altro puo' pilotare
    # il bot). L'offset avanza comunque, anche se la risposta fallisce: i
    # comandi Telegram non hanno bisogno di retry come le notifiche. ---
    new_last_update_id = last_update_id
    if bot_token:
        updates = fetch_telegram_updates(bot_token, last_update_id + 1)
        print(f"Comandi Telegram: {len(updates)} update ricevuti (offset richiesto: {last_update_id + 1}).")
        for update in updates:
            update_id = update.get("update_id")
            if isinstance(update_id, int) and update_id > new_last_update_id:
                new_last_update_id = update_id

            message = update.get("message") or {}
            msg_chat_id = (message.get("chat") or {}).get("id")
            if msg_chat_id is None or str(msg_chat_id) != str(chat_id):
                print(f"  update {update_id}: ignorato (chat {msg_chat_id} diversa da quella configurata).")
                continue  # ignora comandi da chat diverse da quella configurata

            text = message.get("text")
            reply_to = message.get("reply_to_message") or {}
            reply_markup_override = None
            reply_text = None

            # Tutta la logica che determina la risposta e' avvolta in un
            # try/except: un eventuale bug in uno dei comandi non deve far
            # crashare l'intero giro (che lascerebbe l'offset non salvato e
            # farebbe ripetere lo stesso errore ad ogni run) ne' lasciare
            # l'utente senza nessuna risposta -- meglio un messaggio di
            # errore visibile (e nei log) che silenzio totale.
            try:
                if isinstance(reply_to, dict) and ALERT_PROMPT_MARKER in (reply_to.get("text") or ""):
                    # Risposta (Telegram "reply") al messaggio-prompt mandato
                    # dal pulsante /newalert: il testo e' direttamente
                    # "<coin> <direzione> <valore>", senza comando iniziale.
                    print(f"  update {update_id}: risposta al prompt /newalert -> '{text}'")
                    reply_text = try_create_alert((text or "").strip())
                else:
                    command = normalize_command(text)
                    print(f"  update {update_id}: testo='{text}' comando riconosciuto='{command or '(nessuno)'}'")
                    if command == "twap":
                        reply_text = format_twap_status_message(
                            twap_progress, twap_records_by_id, get_mids(),
                            pending_twap_ids=pending_twap_ids, spot_meta=get_spot_meta(),
                            now_ms=now_ms,
                        )
                    elif command == "positions":
                        ch_state = get_positions_state()
                        open_orders = fetch_open_orders(wallet)
                        reply_text = format_positions_message(
                            ch_state, open_orders, get_mids(), spot_state=get_spot_state(), spot_meta=get_spot_meta()
                        )
                    elif command == "alert":
                        reply_text = try_create_alert(command_args(text))
                    elif command == "newalert":
                        reply_text = (
                            f"{ALERT_PROMPT_MARKER}: rispondi a questo messaggio (usa 'Rispondi'/'Reply' su "
                            f"Telegram) con <COIN> <sopra|sotto> <VALORE|VALORE%>\n"
                            f"Esempi: BTC sotto 65000  —  BTC sotto 5%"
                        )
                        reply_markup_override = ALERT_PROMPT_REPLY_MARKUP
                    elif command == "alerts":
                        reply_text = format_alerts_list_message(price_alerts, get_mids(), get_spot_meta())
                    elif command in ("delalert", "rmalert"):
                        args_text = command_args(text).strip()
                        try:
                            alert_id = int(args_text)
                        except ValueError:
                            reply_text = "⚠️ Usa: /delalert <id> (vedi gli id con /alerts)"
                        else:
                            before = len(price_alerts)
                            price_alerts[:] = [a for a in price_alerts if a.get("id") != alert_id]
                            if len(price_alerts) < before:
                                reply_text = (
                                    f"🗑️ Alert {alert_id} rimosso.\n\n"
                                    f"{format_alerts_list_message(price_alerts, get_mids(), get_spot_meta())}"
                                )
                            else:
                                reply_text = f"⚠️ Nessun alert trovato con id {alert_id}."
                    elif command == "start":
                        # Solo per mostrare/ripristinare la tastiera con i
                        # pulsanti su una chat senza ancora nessun messaggio.
                        reply_text = (
                            "👋 Bot Hyperliquid attivo. Usa i pulsanti qui sotto (o scrivi i comandi):\n"
                            "/twap — stato dei TWAP\n"
                            "/positions — posizioni aperte e stop\n"
                            "/alerts — alert di prezzo attivi\n"
                            "/newalert — crea un alert in modo guidato\n"
                            "/alert <COIN> <sopra|sotto> <VALORE|VALORE%> — imposta un alert direttamente\n"
                            "/delalert <id> — rimuove un alert"
                        )
                    else:
                        continue
            except Exception as e:
                import traceback
                traceback.print_exc(file=sys.stderr)
                reply_text = f"⚠️ Errore interno gestendo il comando (controlla i log del workflow): {e}"
                reply_markup_override = None

            try:
                if reply_markup_override is not None:
                    send_telegram_message(bot_token, chat_id, reply_text, dry_run=dry_run, reply_markup=reply_markup_override)
                else:
                    send_telegram_message(bot_token, chat_id, reply_text, dry_run=dry_run)
            except Exception as e:
                print(f"ERRORE invio risposta comando Telegram: {e}", file=sys.stderr)

    # Tieni solo i piu' recenti per non far crescere il file all'infinito.
    # notified_closed_twap_ids e' un set (non una lista in ordine di
    # scoperta come known_twap_ids_list), ma gli id TWAP di Hyperliquid
    # sono numerici crescenti (osservato empiricamente), quindi ordinare
    # per valore numerico e tenere gli ultimi N approssima comunque "i
    # piu' recenti" invece di un troncamento arbitrario.
    latest_tids = latest_tids[-500:]
    known_twap_ids_list = known_twap_ids_list[-500:]
    notified_closed_twap_ids_list = sorted(
        notified_closed_twap_ids, key=lambda x: int(x) if x.isdigit() else -1
    )[-1000:]

    save_state(
        state_file,
        {
            "last_time_ms": max_time_seen,
            "seen_tids": latest_tids,
            "twap_progress": twap_progress,
            "known_twap_ids": known_twap_ids_list,
            "pending_twap_ids": sorted(pending_twap_ids),
            "notified_closed_twap_ids": notified_closed_twap_ids_list,
            "last_update_id": new_last_update_id,
            "price_alerts": price_alerts,
            "next_alert_id": next_alert_id,
            "last_positions_recap_ms": last_positions_recap_ms,
        },
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
