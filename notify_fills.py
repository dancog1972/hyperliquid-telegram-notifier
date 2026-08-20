#!/usr/bin/env python3
"""
Hyperliquid -> Telegram fill notifier.

Interroga l'endpoint REST /info di Hyperliquid (userFillsByTime +
userTwapSliceFills) per un wallet, confronta i fill ricevuti con l'ultimo
stato salvato su disco e manda un messaggio Telegram per le nuove
esecuzioni trovate:

- Ordini normali (limite, stop, mercato): notifica IMMEDIATA (🚨) ad ogni
  controllo se c'e' qualcosa di nuovo, con l'eseguito TOTALE dell'ordine
  (media prezzo, size totale) e il dettaglio delle singole esecuzioni sotto
  se sono state piu' di una nello stesso giro di controllo.
- Slice di ordini TWAP: NESSuna notifica ad ogni slice (sarebbero troppe).
  Le slice vengono accumulate silenziosamente, e una volta ogni
  TWAP_RECAP_MINUTES (default 60) viene mandato un unico messaggio di
  recap per ciascun TWAP attivo con esecuzioni in quel periodo: quante
  slice, size totale ed eseguito cumulato del TWAP, differenza rispetto al
  prezzo di mercato attuale. La percentuale di completamento rispetto alla
  size totale del TWAP viene mostrata solo se Hyperliquid espone quel dato
  in modo verificabile; altrimenti quella riga viene omessa (mai mostrato
  un numero indovinato).

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

HL_INFO_URL = "https://api.hyperliquid.xyz/info"
TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"

DEFAULT_STATE_FILE = "state/last_fill.json"
DEFAULT_LOOKBACK_MINUTES = 15

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


def fetch_twap_target(wallet: str, twap_id) -> dict | None:
    """Prova a recuperare la size totale target e la durata di un TWAP
    specifico, per calcolare quanto manca al completamento. Best-effort:
    lo schema esatto della risposta non e' verificabile senza un test dal
    vivo, quindi qualunque cosa non torni chiaramente interpretabile viene
    scartata silenziosamente (nessun numero indovinato)."""
    for type_name in TWAP_STATE_TYPE_CANDIDATES:
        try:
            result = http_post_json(HL_INFO_URL, {"type": type_name, "user": wallet})
        except Exception:
            continue
        records = result if isinstance(result, list) else result.get("records") if isinstance(result, dict) else None
        if not isinstance(records, list):
            continue
        for record in records:
            if not isinstance(record, dict):
                continue
            rec_id = record.get("twapId", record.get("id"))
            state = record.get("state") if isinstance(record.get("state"), dict) else record
            if rec_id != twap_id and state.get("twapId") != twap_id:
                continue
            total_sz = state.get("sz")
            executed_sz = state.get("executedSz")
            minutes = state.get("minutes")
            if total_sz is None:
                continue
            return {"total_sz": total_sz, "executed_sz": executed_sz, "minutes": minutes}
    return None


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

    # --- Ordini normali: notifica immediata ad ogni controllo, come prima ---
    if regular_new:
        by_oid = defaultdict(list)
        for f in regular_new:
            by_oid[f.get("oid")].append(f)
        for oid, group in by_oid.items():
            text = format_order_message(group)
            try:
                send_telegram_message(bot_token, chat_id, text, dry_run=dry_run)
            except Exception as e:
                print(f"ERRORE invio Telegram (ordine oid={oid}): {e}", file=sys.stderr)
                continue
            for f in group:
                tid = f.get("tid")
                if tid is not None:
                    latest_tids.append(tid)
                max_time_seen = max(max_time_seen, f.get("time", max_time_seen))

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

    # --- Recap TWAP: solo quelli il cui periodo di accumulo ha superato
    # TWAP_RECAP_MINUTES, indipendentemente dal fatto che questo giro abbia
    # trovato nuove slice o meno (un TWAP puo' aver accumulato slice in giri
    # precedenti e diventare "scaduto" anche in un giro senza fill nuovi). ---
    due_recaps = [
        (key, prog)
        for key, prog in twap_progress.items()
        if prog.get("pending_count", 0) > 0
        and prog.get("period_start_ms") is not None
        and (now_ms - prog["period_start_ms"]) >= recap_interval_ms
    ]

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
            try:
                target = fetch_twap_target(wallet, key)
            except Exception as e:
                # Best-effort: questo dato non deve mai far fallire il
                # recap, nel dubbio si omette la % di completamento.
                print(f"AVVISO: impossibile recuperare il target del TWAP {key}: {e}", file=sys.stderr)
                target = None

            text = format_twap_recap_message(key, prog, current_mid, target)
            try:
                send_telegram_message(bot_token, chat_id, text, dry_run=dry_run)
            except Exception as e:
                print(f"ERRORE invio Telegram (recap TWAP {key}): {e}", file=sys.stderr)
                continue  # riprova al prossimo giro, l'accumulo resta intatto

            # Azzera l'accumulo del periodo solo se l'invio e' andato a
            # buon fine, altrimenti si riprova al giro successivo.
            prog["pending_sz"] = 0.0
            prog["pending_notional"] = 0.0
            prog["pending_count"] = 0
            prog["period_start_ms"] = None
            twap_progress[key] = prog

    # Tieni solo i tid recenti per non far crescere il file all'infinito.
    latest_tids = latest_tids[-500:]

    save_state(
        state_file,
        {
            "last_time_ms": max_time_seen,
            "seen_tids": latest_tids,
            "twap_progress": twap_progress,
        },
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
