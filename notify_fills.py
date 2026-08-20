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
- Slice di ordini TWAP gia' avviati: NESSuna notifica ad ogni slice
  (sarebbero troppe). Le slice vengono accumulate silenziosamente, e una
  volta ogni TWAP_RECAP_MINUTES (default 60) viene mandato un unico
  messaggio di recap (📊) per ciascun TWAP attivo con esecuzioni in quel
  periodo: quante slice, size totale ed eseguito cumulato del TWAP,
  differenza rispetto al prezzo di mercato attuale. La percentuale di
  completamento rispetto alla size totale del TWAP viene mostrata solo se
  disponibile; altrimenti quella riga viene omessa (mai mostrato un numero
  indovinato).

Ogni messaggio Telegram e' preceduto da una riga separatrice, per restare
visivamente distinto anche quando le notifiche arrivano ravvicinate.

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
  TWAP_RECAP_MINUTES     Ogni quanti minuti mandare il recap accumulato
                         delle slice TWAP (default: 60).
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

DEFAULT_TWAP_RECAP_MINUTES = 60

# Riga divisoria messa in cima ad ogni messaggio Telegram, cosi' notifiche
# ravvicinate restano visivamente distinte invece di confondersi.
MESSAGE_SEPARATOR = "➖➖➖➖➖➖➖➖➖➖➖➖➖➖➖➖➖➖➖➖"


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
    rilevamento "nuovo TWAP" sia la % di completamento nei recap vengono
    semplicemente omessi, senza far fallire il resto della notifica."""
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


def format_twap_recap_message(twap_id, prog: dict, current_mid: float | None, target: dict | None) -> str:
    """Recap periodico (non per-slice) di un TWAP: quanto eseguito nel
    periodo appena chiuso, quanto eseguito in totale sul TWAP, differenza
    col prezzo di mercato attuale ed eventuale % di completamento."""
    coin = prog.get("coin", "?")
    side = side_label(prog.get("side", ""))

    pending_sz = prog.get("pending_sz", 0.0)
    pending_count = prog.get("pending_count", 0)
    pending_avg = (prog.get("pending_notional", 0.0) / pending_sz) if pending_sz else 0.0

    cum_executed_sz = prog.get("executed_sz", 0.0)
    cum_avg_px = (prog.get("notional", 0.0) / cum_executed_sz) if cum_executed_sz else 0.0

    period_start = prog.get("period_start_ms")
    period_end = prog.get("last_ms")

    plurale = "e" if pending_count == 1 else "i"
    lines = [
        f"📊 Recap TWAP {coin} (id {twap_id})",
        f"{pending_count} esecuzion{plurale} in questo periodo: {side} {pending_sz:g} {coin} @ media {pending_avg:g}",
        f"Eseguito totale sul TWAP finora: {cum_executed_sz:g} {coin} @ media {cum_avg_px:g}",
    ]

    if target and target.get("total_sz"):
        try:
            total_sz = float(target["total_sz"])
            if total_sz > 0:
                pct = min(100.0, cum_executed_sz / total_sz * 100)
                remaining = max(0.0, total_sz - cum_executed_sz)
                lines.append(f"Completamento TWAP: {pct:.1f}% (mancano circa {remaining:g} {coin})")
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    if current_mid is not None and cum_avg_px:
        diff = current_mid - cum_avg_px
        diff_pct = (diff / cum_avg_px * 100) if cum_avg_px else 0
        segno = "+" if diff >= 0 else ""
        lines.append(
            f"Prezzo attuale {coin}: {current_mid:g} (differenza vs media: {segno}{diff:g}, {segno}{diff_pct:.2f}%)"
        )

    if period_start:
        lines.append(f"Periodo: {fmt_ts(period_start)} → {fmt_ts(period_end)}")
    return "\n".join(lines)


def format_update_header(now_ms: int) -> str:
    """Intestazione mandata una sola volta all'inizio di ogni giro in cui
    c'e' almeno un messaggio da inviare, per separare visivamente un
    controllo dall'altro. Mostrata nel fuso orario DISPLAY_TIMEZONE quando
    disponibile, altrimenti in UTC (mai un crash per questo)."""
    try:
        from zoneinfo import ZoneInfo
        dt = datetime.fromtimestamp(now_ms / 1000, tz=ZoneInfo(DISPLAY_TIMEZONE))
        tz_label = ""
    except Exception:
        dt = datetime.utcfromtimestamp(now_ms / 1000)
        tz_label = " UTC"
    mese = ITALIAN_MONTHS[dt.month - 1]
    return f"🕐 Nuovo aggiornamento — {dt.day} {mese} {dt.year}, ore {dt.strftime('%H:%M')}{tz_label}"


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


def send_telegram_message(bot_token: str, chat_id: str, text: str, dry_run: bool = False) -> None:
    full_text = f"{MESSAGE_SEPARATOR}\n{text}"
    if dry_run:
        print("--- [DRY RUN] messaggio che verrebbe inviato ---")
        print(full_text)
        print("-------------------------------------------------")
        return
    url = TELEGRAM_API_URL.format(token=bot_token)
    payload = {"chat_id": chat_id, "text": full_text}
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
    recap_interval_ms = int(os.environ.get("TWAP_RECAP_MINUTES", DEFAULT_TWAP_RECAP_MINUTES)) * 60 * 1000

    now_ms = int(time.time() * 1000)
    state = load_state(state_file)

    last_time_ms = state.get("last_time_ms")
    seen_tids = set(state.get("seen_tids", []))
    # str(twap_id) -> {"executed_sz","notional" (cumulativi di tutta la vita
    # del TWAP), "pending_sz","pending_notional","pending_count",
    # "period_start_ms","last_ms" (accumulo del periodo di recap corrente,
    # azzerato dopo ogni recap inviato con successo), "coin", "side"}.
    twap_progress = state.get("twap_progress", {})
    is_very_first_run = "known_twap_ids" not in state
    known_twap_ids_list = [str(x) for x in state.get("known_twap_ids", [])]
    known_twap_ids = set(known_twap_ids_list)

    # Un'unica chiamata di rete (best-effort) usata sia per rilevare TWAP
    # appena avviati sia, piu' sotto, come sorgente per la % di
    # completamento nei recap -- evita di richiamare l'endpoint TWAP una
    # volta per ogni TWAP attivo.
    try:
        twap_records = fetch_twap_records(wallet)
    except Exception as e:
        print(f"AVVISO: impossibile recuperare l'elenco dei TWAP: {e}", file=sys.stderr)
        twap_records = []
    twap_records_by_id = {str(r["twap_id"]): r for r in twap_records}

    # Coda di tutti i messaggi da mandare in questo giro: si costruisce
    # prima di inviare qualunque cosa, cosi' possiamo far precedere tutto da
    # un'unica intestazione "nuovo aggiornamento" e mandare i messaggi in
    # ordine, invece di intestazioni ripetute sparse nel codice. Ogni
    # elemento ha "text" e "on_success" (eseguito solo se l'invio va a buon
    # fine, per mantenere la stessa logica di retry-al-prossimo-giro di
    # prima in caso di errore Telegram).
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

    fills = fetch_fills(wallet, start_time_ms, now_ms)
    twap_fills = fetch_twap_slice_fills(wallet, start_time_ms, now_ms)
    all_fills = fills + twap_fills
    all_fills.sort(key=lambda f: (f.get("time", 0), f.get("tid", 0)))

    new_fills = [f for f in all_fills if f.get("tid") not in seen_tids]
    print(f"Trovati {len(all_fills)} fill nell'intervallo, {len(new_fills)} nuovi.")

    max_time_seen = last_time_ms or start_time_ms
    latest_tids = list(seen_tids)

    regular_new = [f for f in new_fills if not f.get("twapId")]
    twap_new = [f for f in new_fills if f.get("twapId")]

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

    # --- Slice TWAP: accumulo silenzioso ad ogni controllo (nessun invio) ---
    if twap_new:
        by_twap = defaultdict(list)
        for f in twap_new:
            by_twap[f.get("twapId")].append(f)
        for twap_id, group in by_twap.items():
            key = str(twap_id)
            prog = twap_progress.get(
                key,
                {
                    "executed_sz": 0.0,
                    "notional": 0.0,
                    "pending_sz": 0.0,
                    "pending_notional": 0.0,
                    "pending_count": 0,
                    "period_start_ms": None,
                    "last_ms": None,
                },
            )
            for f in group:
                sz = float(f.get("sz", 0))
                px = float(f.get("px", 0))
                prog["executed_sz"] = prog.get("executed_sz", 0.0) + sz
                prog["notional"] = prog.get("notional", 0.0) + sz * px
                prog["pending_sz"] = prog.get("pending_sz", 0.0) + sz
                prog["pending_notional"] = prog.get("pending_notional", 0.0) + sz * px
                prog["pending_count"] = prog.get("pending_count", 0) + 1
                if prog.get("period_start_ms") is None:
                    prog["period_start_ms"] = f.get("time")
                prog["last_ms"] = f.get("time")
            prog["coin"] = group[0].get("coin", "?")
            prog["side"] = group[0].get("side", "")
            twap_progress[key] = prog

            # Le slice TWAP sono "viste" (niente ri-conteggio al prossimo
            # giro) anche se non mandiamo ancora nessun messaggio: sono gia'
            # state incorporate nell'accumulo cumulativo.
            for f in group:
                tid = f.get("tid")
                if tid is not None:
                    latest_tids.append(tid)
                max_time_seen = max(max_time_seen, f.get("time", max_time_seen))

    # --- Recap TWAP: un unico orario "prossimo recap" condiviso da TUTTI i
    # TWAP, cosi' arrivano tutti insieme nello stesso batch invece che ognuno
    # secondo il proprio orologio individuale (che partirebbe in momenti
    # diversi a seconda di quando si e' vista la prima slice di ciascuno). ---
    next_recap_due_ms = state.get("next_recap_due_ms")
    if next_recap_due_ms is None:
        next_recap_due_ms = now_ms + recap_interval_ms

    recap_due_now = now_ms >= next_recap_due_ms
    while now_ms >= next_recap_due_ms:
        next_recap_due_ms += recap_interval_ms  # riallinea senza "andare alla deriva" anche se un giro salta

    due_recaps = (
        [(key, prog) for key, prog in twap_progress.items() if prog.get("pending_count", 0) > 0]
        if recap_due_now
        else []
    )

    def make_recap_cb(key, prog):
        def cb():
            # Azzera l'accumulo del periodo solo se l'invio e' andato a
            # buon fine, altrimenti si riprova al giro successivo.
            prog["pending_sz"] = 0.0
            prog["pending_notional"] = 0.0
            prog["pending_count"] = 0
            prog["period_start_ms"] = None
            twap_progress[key] = prog
        return cb

    if due_recaps:
        mids = fetch_all_mids()
        for key, prog in due_recaps:
            coin = prog.get("coin", "?")
            current_mid = None
            if coin in mids:
                try:
                    current_mid = float(mids[coin])
                except (TypeError, ValueError):
                    current_mid = None
            # Riusa i record TWAP gia' scaricati a inizio run (nessuna
            # chiamata di rete aggiuntiva per ogni recap).
            record = twap_records_by_id.get(key)
            target = {"total_sz": record["total_sz"]} if record and record.get("total_sz") is not None else None

            text = format_twap_recap_message(key, prog, current_mid, target)
            outgoing.append({"text": text, "on_success": make_recap_cb(key, prog)})

    # --- Invio effettivo: intestazione di aggiornamento (solo se c'e'
    # davvero qualcosa da mandare in questo giro) seguita da tutti i
    # messaggi accodati, in ordine. ---
    if outgoing:
        try:
            send_telegram_message(bot_token, chat_id, format_update_header(now_ms), dry_run=dry_run)
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
            "next_recap_due_ms": next_recap_due_ms,
        },
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
