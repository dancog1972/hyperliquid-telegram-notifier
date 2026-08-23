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
- Ordini normali (limite, stop, mercato): notifica IMMEDIATA (🚨) ad ogni
  controllo se c'e' qualcosa di nuovo, con l'eseguito TOTALE dell'ordine
  (media prezzo, size totale) e il dettaglio delle singole esecuzioni sotto
  se sono state piu' di una nello stesso giro di controllo.
- Slice di ordini TWAP gia' avviati: NESSUNA notifica automatica ad ogni
  slice (sarebbero troppe, e cosi' anche i recap periodici che c'erano in
  precedenza). L'eseguito cumulato viene solo accumulato silenziosamente in
  stato; per vederlo si usa il comando Telegram /twap (vedi sotto).

Comandi Telegram (a richiesta, nessun invio automatico):
  /twap                        Stato di tutti i TWAP con esecuzioni
                                registrate o record noti: eseguito
                                cumulato, % di completamento se
                                disponibile, differenza rispetto al
                                prezzo di mercato attuale.
  /positions                   Posizioni perps aperte: size e direzione,
                                entry vs prezzo attuale, PnL non
                                realizzato, prezzo di liquidazione ed
                                eventuali ordini di stop/take-profit
                                collegati.
  /alert <COIN> <sopra|sotto> <VALORE|VALORE%>
                                Imposta un alert di prezzo per QUALSIASI
                                coin quotata su Hyperliquid (non solo
                                quelle con una posizione aperta). VALORE
                                puo' essere un prezzo assoluto ("/alert
                                BTC sotto 65000") oppure una percentuale
                                ("/alert BTC sotto 5%"), calcolata rispetto
                                al prezzo di mercato attuale nel momento in
                                cui l'alert viene creato (accetta anche
                                "<"/">" al posto di sotto/sopra).
  /newalert                     Crea un alert in modo guidato: il bot manda
                                un messaggio-prompt (con "Rispondi"/"Reply"
                                gia' pronto su Telegram) e basta scrivere
                                "<COIN> <sopra|sotto> <VALORE|VALORE%>" in
                                risposta, senza dover ricordare /alert.
  /alerts                       Elenca gli alert di prezzo attivi.
  /delalert <id>                Rimuove un alert (l'id si vede con /alerts).
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
succede quando scatta dipende dal fatto che tu abbia o meno una posizione
APERTA su quella coin in quel momento:
- SENZA posizione aperta: notifica una volta sola e l'alert viene rimosso
  (va reimpostato con /alert se lo si vuole di nuovo attivo) -- come
  prima.
- CON una posizione aperta: la notifica include anche un riepilogo della
  posizione (direzione, size, entry vs prezzo attuale, PnL, prezzo di
  liquidazione) e l'alert NON viene rimosso -- continua a ripetersi ad
  ogni controllo finche' la condizione resta vera E la posizione resta
  aperta. Si ferma da solo, con un ultimo messaggio dedicato, alla prima
  delle due condizioni che viene meno: il prezzo torna oltre la soglia
  ("✅ Allarme rientrato") oppure la posizione viene chiusa ("🔕 Alert
  disattivato"). Con un intervallo di controllo breve questo puo'
  generare piu' notifiche ravvicinate finche' la condizione persiste --
  e' il comportamento voluto per un alert legato a una posizione aperta.

Ogni messaggio del bot porta anche una tastiera Telegram persistente
("/twap", "/positions", "/alerts", "/newalert") cosi' i comandi piu'
comuni si possono richiamare con un tocco invece di scriverli a mano;
scrivere "/start" al bot manda un messaggio di benvenuto che la mostra
anche a chat vuota.

Ogni messaggio Telegram e' preceduto da una riga separatrice, per restare
visivamente distinto anche quando le notifiche arrivano ravvicinate. Ogni
giro di controllo in cui c'e' almeno una notifica automatica da mandare e'
preceduto da un'unica intestazione "nuovo aggiornamento" ben visibile
(nessuna intestazione se non c'e' nulla di nuovo).

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

import json
import os
import sys
import time
import urllib.request
import urllib.error
from collections import defaultdict
from datetime import datetime

HL_INFO_URL = "https://api.hyperliquid.xyz/info"
TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
TELEGRAM_GETUPDATES_URL = "https://api.telegram.org/bot{token}/getUpdates"

DEFAULT_STATE_FILE = "state/last_fill.json"
DEFAULT_LOOKBACK_MINUTES = 15

# Fuso orario in cui mostrare l'intestazione di aggiornamento. Se il
# database IANA non e' disponibile sul runner, si ricade su UTC senza far
# fallire lo script (vedi format_update_header).
DISPLAY_TIMEZONE = "Europe/Paris"

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

# Barra piu' vistosa (Telegram non supporta colori nel testo dei messaggi
# dei bot, quindi si usa un emoji a blocco colorato ben visibile) usata solo
# per l'intestazione "nuovo aggiornamento", cosi' risalta rispetto alle
# notifiche vere e proprie che la seguono.
UPDATE_HEADER_BAR = "🟧🟧🟧🟧🟧🟧🟧🟧🟧🟧🟧🟧🟧🟧🟧🟧🟧🟧🟧🟧"


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
    """Ritorna la lista (normalizzata) dei TWAP dell'utente -- attivi e
    passati -- con qualunque campo si riesca a interpretare in modo
    affidabile: twap_id, coin, side, total_sz (size target), minutes
    (durata), status, start_ms.

    Best-effort e best-guess: il nome esatto di questo endpoint e il suo
    schema non sono documentati in modo verificabile, quindi si provano piu'
    candidati e si scartano silenziosamente i campi che non si riescono a
    interpretare con sicurezza (mai un dato indovinato). Ritorna [] se
    nessun candidato risponde in modo utilizzabile -- in tal caso sia il
    rilevamento "nuovo TWAP" sia la % di completamento nei messaggi di stato
    vengono semplicemente omessi, senza far fallire il resto della
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
            status = record.get("status")
            if isinstance(status, dict):
                status = status.get("status")
            elif not isinstance(status, str):
                status = None
            parsed.append(
                {
                    "twap_id": twap_id,
                    "coin": state.get("coin"),
                    "side": state.get("side"),
                    "total_sz": state.get("sz"),
                    "executed_sz": state.get("executedSz"),
                    "minutes": state.get("minutes"),
                    "status": status,
                    "start_ms": record.get("time", state.get("timestamp")),
                }
            )
        if parsed:
            return parsed
    return []


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


def format_alerts_list_message(alerts: list) -> str:
    """Risposta al comando /alerts: elenco degli alert di prezzo attivi."""
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
        lines.append(f"— id {a.get('id')}: {a.get('coin')} {verso} {price_txt}{pct_txt}{stato_txt}")
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


def format_alert_triggered_message(alert: dict, current_price: float, position: dict | None = None, repeated: bool = False) -> str:
    """Notifica automatica mandata quando un alert di prezzo scatta (o
    resta attivo). Se c'e' una posizione aperta su quella coin, l'alert
    resta attivo e questa notifica si ripete ad ogni controllo finche' la
    condizione persiste E la posizione resta aperta -- vedi main() per la
    logica di stato ("triggered") e format_alert_cleared_message per come
    si ferma."""
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
    lines = [f"{header}: {coin} {verso} {threshold:g}{extra}", f"Prezzo attuale: {current_price:g}"]
    if position is not None:
        lines.append(format_position_summary(position, current_price))
        lines.append(
            "(ti aggiorno ad ogni controllo finche' resti in questa condizione o chiudi la posizione — "
            "arriva un ultimo messaggio quando l'allarme rientra)"
        )
    else:
        lines.append("(alert rimosso automaticamente — usa /alert per impostarne uno nuovo)")
    return "\n".join(lines)


def format_alert_cleared_message(alert: dict, current_price: float, reason: str) -> str:
    """Notifica mandata quando un alert "attivo" (con posizione collegata,
    vedi format_alert_triggered_message) smette di ripetersi: perche' il
    prezzo e' rientrato oltre la soglia (reason="recovered") o perche' la
    posizione e' stata chiusa (reason="position_closed"). L'alert viene
    rimosso subito dopo (vedi main())."""
    coin = alert.get("coin", "?")
    threshold = alert.get("price", 0)
    if reason == "position_closed":
        head = f"🔕 Alert disattivato: la posizione su {coin} e' stata chiusa."
    else:
        verso_rientro = "risalito sopra" if alert.get("direction") == "below" else "ridisceso sotto"
        head = f"✅ Allarme rientrato: {coin} e' {verso_rientro} {threshold:g}."
    return f"{head}\nPrezzo attuale: {current_price:g}\n(non ricevi più notifiche per questo alert)"


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

    lines = [f"🚨 Eseguito totale: {side} {total_sz:g} {coin} @ {avg_px:g} (media)"]
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


def format_update_header(now_ms: int) -> str:
    """Intestazione mandata una sola volta all'inizio di ogni giro in cui
    c'e' almeno un messaggio automatico da inviare, per separare
    visivamente un controllo dall'altro. Mostrata nel fuso orario
    DISPLAY_TIMEZONE quando disponibile, altrimenti in UTC (mai un crash
    per questo)."""
    try:
        from zoneinfo import ZoneInfo
        dt = datetime.fromtimestamp(now_ms / 1000, tz=ZoneInfo(DISPLAY_TIMEZONE))
        tz_label = ""
    except Exception:
        dt = datetime.utcfromtimestamp(now_ms / 1000)
        tz_label = " UTC"
    mese = ITALIAN_MONTHS[dt.month - 1]
    return (
        f"🕐 NUOVO AGGIORNAMENTO — {dt.day} {mese} {dt.year}, ore {dt.strftime('%H:%M')}{tz_label}\n"
        f"{UPDATE_HEADER_BAR}"
    )


def format_new_twap_message(record: dict) -> str:
    """Messaggio inviato non appena un nuovo TWAP viene rilevato (avviato),
    prima ancora che scatti la sua prima slice."""
    twap_id = record.get("twap_id")
    coin = record.get("coin") or "?"
    side = side_label(record.get("side") or "")

    header = f"🆕 Nuovo TWAP avviato: {side} {coin}".rstrip()
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


MAX_EXTRA_TWAP_RECORDS = 15


def format_twap_status_message(twap_progress: dict, twap_records_by_id: dict, mids: dict) -> str:
    """Risposta al comando /twap: stato di ogni TWAP di cui abbiamo
    esecuzioni accumulate, piu' (se ce ne sono ancora spazio) i record
    noti piu' recenti senza esecuzioni. L'endpoint usato per i record e'
    best-effort e su alcuni account restituisce anche moltissimo storico
    passato: senza un limite il messaggio puo' superare il limite di
    lunghezza di Telegram (~4096 caratteri) e l'invio fallisce in
    silenzio dal punto di vista dell'utente -- da qui il tetto su quanti
    record "extra" (senza esecuzioni note) vengono mostrati."""
    progress_keys = list(twap_progress.keys())
    extra_items = [(key, r) for key, r in twap_records_by_id.items() if key not in twap_progress]
    extra_items.sort(key=lambda kv: kv[1].get("start_ms") or 0, reverse=True)
    omitted_count = max(0, len(extra_items) - MAX_EXTRA_TWAP_RECORDS)
    extra_keys = [key for key, _ in extra_items[:MAX_EXTRA_TWAP_RECORDS]]

    keys = sorted(progress_keys) + extra_keys
    if not keys:
        return "📊 Nessun TWAP trovato (ne' esecuzioni registrate ne' record noti)."

    blocks = ["📊 Stato TWAP:"]
    for key in keys:
        prog = twap_progress.get(key, {})
        record = twap_records_by_id.get(key)
        coin = prog.get("coin") or (record.get("coin") if record else None) or "?"
        side_raw = prog.get("side") or ((record.get("side") if record else "") or "")
        side = side_label(side_raw)

        lines = [f"— TWAP {key}: {side} {coin}".rstrip()]

        executed_sz = float(prog.get("executed_sz", 0.0) or 0.0)
        notional = float(prog.get("notional", 0.0) or 0.0)
        avg_px = (notional / executed_sz) if executed_sz else 0.0
        if executed_sz:
            lines.append(f"   Eseguito: {executed_sz:g} {coin} @ media {avg_px:g}")
        else:
            lines.append("   Eseguito: nessuna esecuzione registrata finora")

        if record and record.get("total_sz") is not None:
            try:
                total_sz = float(record["total_sz"])
                if total_sz > 0:
                    pct = min(100.0, executed_sz / total_sz * 100)
                    remaining = max(0.0, total_sz - executed_sz)
                    lines.append(f"   Completamento: {pct:.1f}% (mancano circa {remaining:g} {coin})")
            except (TypeError, ValueError, ZeroDivisionError):
                pass

        if record and record.get("status"):
            lines.append(f"   Stato: {record['status']}")

        current_mid = None
        if coin in mids:
            try:
                current_mid = float(mids[coin])
            except (TypeError, ValueError):
                current_mid = None
        if current_mid is not None and avg_px:
            diff = current_mid - avg_px
            diff_pct = (diff / avg_px * 100) if avg_px else 0
            segno = "+" if diff >= 0 else ""
            lines.append(
                f"   Prezzo attuale {coin}: {current_mid:g} (differenza vs media: {segno}{diff:g}, {segno}{diff_pct:.2f}%)"
            )

        if prog.get("last_ms"):
            lines.append(f"   Ultima esecuzione: {fmt_ts(prog['last_ms'])}")

        blocks.append("\n".join(lines))

    if omitted_count:
        blocks.append(f"… e altri {omitted_count} TWAP piu' vecchi omessi (nessuna esecuzione registrata per questi).")

    return "\n\n".join(blocks)


def format_positions_message(clearinghouse_state: dict, open_orders: list, mids: dict) -> str:
    """Risposta al comando /positions: posizioni perps aperte con entry vs
    prezzo attuale, PnL, prezzo di liquidazione ed eventuali stop/TP
    collegati (dedotti dagli ordini aperti reduce-only con trigger).

    Lo schema esatto di clearinghouseState/frontendOpenOrders non e'
    verificabile da qui (nessun accesso di rete nel sandbox di sviluppo),
    quindi il parsing e' difensivo: gestisce sia il caso in cui i campi
    della posizione siano annidati sotto "position" sia il caso in cui
    siano diretti, e omette in silenzio quello che non riesce a
    interpretare con sicurezza invece di mostrare dati indovinati."""
    if not isinstance(clearinghouse_state, dict) or not isinstance(clearinghouse_state.get("assetPositions"), list):
        return "📋 Nessuna posizione trovata (o dati non disponibili al momento)."

    positions = extract_open_positions(clearinghouse_state)
    if not positions:
        return "📋 Nessuna posizione aperta al momento."

    orders_by_coin = defaultdict(list)
    for o in open_orders:
        if not isinstance(o, dict):
            continue
        coin = o.get("coin")
        if coin:
            orders_by_coin[coin].append(o)

    blocks = ["📋 Posizioni aperte:"]
    for pos in positions:
        coin = pos.get("coin", "?")
        try:
            szi = float(pos.get("szi", 0))
        except (TypeError, ValueError):
            szi = 0.0
        direction = "LONG" if szi > 0 else "SHORT"
        abs_sz = abs(szi)

        leverage = pos.get("leverage")
        lev_value = leverage.get("value") if isinstance(leverage, dict) else leverage

        header = f"— {coin}: {direction} {abs_sz:g}"
        if lev_value:
            header += f" ({lev_value}x)"
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
    truncate_for_telegram) invece di far fallire l'invio in silenzio."""
    full_text = truncate_for_telegram(f"{separator}\n{text}")
    if dry_run:
        print("--- [DRY RUN] messaggio che verrebbe inviato ---")
        print(full_text)
        if reply_markup:
            print(f"[tastiera: {reply_markup}]")
        print("-------------------------------------------------")
        return
    url = TELEGRAM_API_URL.format(token=bot_token)
    payload = {"chat_id": chat_id, "text": full_text}
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
    last_update_id = int(state.get("last_update_id", 0) or 0)
    # Lista di alert di prezzo attivi: {"id","coin","direction" ("below"/
    # "above"),"price","created_ms"}. Gestita a richiesta con /alert,
    # /alerts, /delalert; controllata automaticamente ad ogni giro (vedi
    # sotto) e rimossa (via on_success) non appena la notifica scatta.
    price_alerts = state.get("price_alerts", [])
    next_alert_id = int(state.get("next_alert_id", 1) or 1)

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

    def try_create_alert(args_text: str) -> str:
        """Interpreta args_text come "<COIN> <sopra|sotto> <VALORE|VALORE%>"
        e, se valido, aggiunge l'alert a price_alerts. Funziona per QUALSIASI
        coin con un prezzo su Hyperliquid (non solo quelle con una posizione
        aperta): la percentuale e' calcolata rispetto al prezzo attuale
        (allMids) nel momento in cui l'alert viene creato. Ritorna il testo
        di conferma o dell'errore da rimandare all'utente."""
        nonlocal next_alert_id
        try:
            coin, direction, value, is_percent = parse_price_alert_command(args_text)
        except ValueError as e:
            return f"⚠️ {e}"

        mids_now = get_mids()
        pct = None
        ref_price = None
        if is_percent:
            base_raw = mids_now.get(coin)
            if base_raw is None:
                return f"⚠️ Coin '{coin}' non trovata tra i prezzi correnti Hyperliquid. Controlla il ticker."
            try:
                ref_price = float(base_raw)
            except (TypeError, ValueError):
                return f"⚠️ Prezzo attuale di {coin} non disponibile al momento, riprova più tardi."
            pct = value
            price = ref_price * (1 - value / 100) if direction == "below" else ref_price * (1 + value / 100)
        else:
            if mids_now and coin not in mids_now:
                return f"⚠️ Coin '{coin}' non trovata tra i prezzi correnti Hyperliquid. Controlla il ticker."
            price = value

        alert = {
            "id": next_alert_id,
            "coin": coin,
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
        return f"🔔 Alert impostato: ti avviso quando {coin} {verso} {price:g}{extra} (id {alert['id']})."

    # Un'unica chiamata di rete (best-effort) usata sia per rilevare TWAP
    # appena avviati sia, piu' sotto, come sorgente per la % di
    # completamento nei messaggi di stato -- evita di richiamare l'endpoint
    # TWAP una volta per ogni TWAP attivo.
    try:
        twap_records = fetch_twap_records(wallet)
    except Exception as e:
        print(f"AVVISO: impossibile recuperare l'elenco dei TWAP: {e}", file=sys.stderr)
        twap_records = []
    twap_records_by_id = {str(r["twap_id"]): r for r in twap_records}

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

    if is_very_first_run:
        # Primissimo run in assoluto per questa funzionalita': non
        # notificare come "nuovi" i TWAP gia' esistenti/passati, altrimenti
        # si riceve una raffica di notifiche per tutta la storia
        # dell'account. Si "conoscono" silenziosamente e si notificano solo
        # i TWAP che compariranno da qui in avanti.
        for tid_str in twap_records_by_id:
            if tid_str not in known_twap_ids:
                known_twap_ids.add(tid_str)
                known_twap_ids_list.append(tid_str)
        if twap_records_by_id:
            print(f"Primo run: {len(twap_records_by_id)} TWAP esistenti registrati senza notifica.")
    else:
        for tid_str, record in twap_records_by_id.items():
            if tid_str in known_twap_ids:
                continue
            outgoing.append({"text": format_new_twap_message(record), "on_success": make_new_twap_cb(tid_str)})

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
            outgoing.append({"text": format_order_message(group), "on_success": make_regular_cb(group)})

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
        return cb

    def noop_cb():
        pass

    # --- Alert di prezzo: confronto col prezzo attuale (allMids, una sola
    # chiamata per tutti gli alert) solo se ce n'e' almeno uno attivo.
    # Comportamento:
    # - Alert non ancora scattato la cui condizione si avvera: se c'e' una
    #   posizione aperta su quella coin, l'alert diventa "attivo"
    #   (triggered=True) e NON viene rimosso -- restera' attivo finche' la
    #   condizione persiste E la posizione resta aperta. Senza posizione,
    #   si comporta come prima: notifica una volta e viene rimosso.
    # - Alert gia' "attivo": ripete la notifica ad ogni controllo finche'
    #   la condizione resta vera E la posizione resta aperta; si ferma (con
    #   un messaggio dedicato) alla prima delle due condizioni che viene
    #   meno -- prezzo rientrato oltre la soglia, o posizione chiusa.
    # Le mutazioni avvengono solo via on_success, cosi' un invio fallito
    # viene ritentato al giro successivo (stessa logica del resto). ---
    if price_alerts:
        mids_for_alerts = get_mids()
        for alert in price_alerts:
            coin = alert.get("coin")
            price_raw = mids_for_alerts.get(coin)
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
                position = next(
                    (p for p in extract_open_positions(get_positions_state()) if p.get("coin") == coin), None
                )
                if position is None:
                    outgoing.append(
                        {
                            "text": format_alert_cleared_message(alert, current_price, "position_closed"),
                            "on_success": make_alert_remove_cb(alert.get("id")),
                        }
                    )
                elif not condition_met:
                    outgoing.append(
                        {
                            "text": format_alert_cleared_message(alert, current_price, "recovered"),
                            "on_success": make_alert_remove_cb(alert.get("id")),
                        }
                    )
                else:
                    outgoing.append(
                        {
                            "text": format_alert_triggered_message(alert, current_price, position, repeated=True),
                            "on_success": noop_cb,
                        }
                    )
            elif condition_met:
                position = next(
                    (p for p in extract_open_positions(get_positions_state()) if p.get("coin") == coin), None
                )
                if position is not None:
                    outgoing.append(
                        {
                            "text": format_alert_triggered_message(alert, current_price, position, repeated=False),
                            "on_success": make_alert_mark_active_cb(alert),
                        }
                    )
                else:
                    outgoing.append(
                        {
                            "text": format_alert_triggered_message(alert, current_price, None, repeated=False),
                            "on_success": make_alert_remove_cb(alert.get("id")),
                        }
                    )

    # --- Invio delle notifiche automatiche: intestazione di aggiornamento
    # (solo se c'e' davvero qualcosa da mandare in questo giro) seguita da
    # tutti i messaggi accodati, in ordine. ---
    if outgoing:
        try:
            send_telegram_message(
                bot_token, chat_id, format_update_header(now_ms), dry_run=dry_run, separator=UPDATE_HEADER_BAR
            )
        except Exception as e:
            # L'intestazione e' solo cosmetica: un suo eventuale fallimento
            # non deve bloccare l'invio delle notifiche vere e proprie.
            print(f"AVVISO: impossibile inviare l'intestazione di aggiornamento: {e}", file=sys.stderr)

        for item in outgoing:
            try:
                send_telegram_message(bot_token, chat_id, item["text"], dry_run=dry_run)
            except Exception as e:
                print(f"ERRORE invio Telegram: {e}", file=sys.stderr)
                continue
            item["on_success"]()

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
                        reply_text = format_twap_status_message(twap_progress, twap_records_by_id, get_mids())
                    elif command == "positions":
                        ch_state = get_positions_state()
                        open_orders = fetch_open_orders(wallet)
                        reply_text = format_positions_message(ch_state, open_orders, get_mids())
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
                        reply_text = format_alerts_list_message(price_alerts)
                    elif command in ("delalert", "rmalert"):
                        args_text = command_args(text).strip()
                        try:
                            alert_id = int(args_text)
                        except ValueError:
                            reply_text = "⚠️ Usa: /delalert <id> (vedi gli id con /alerts)"
                        else:
                            before = len(price_alerts)
                            price_alerts[:] = [a for a in price_alerts if a.get("id") != alert_id]
                            reply_text = (
                                f"🗑️ Alert {alert_id} rimosso."
                                if len(price_alerts) < before
                                else f"⚠️ Nessun alert trovato con id {alert_id}."
                            )
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
    latest_tids = latest_tids[-500:]
    known_twap_ids_list = known_twap_ids_list[-500:]

    save_state(
        state_file,
        {
            "last_time_ms": max_time_seen,
            "seen_tids": latest_tids,
            "twap_progress": twap_progress,
            "known_twap_ids": known_twap_ids_list,
            "last_update_id": new_last_update_id,
            "price_alerts": price_alerts,
            "next_alert_id": next_alert_id,
        },
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
