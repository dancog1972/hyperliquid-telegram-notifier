#!/usr/bin/env python3
"""
Hyperliquid -> Telegram fill notifier.

Interroga l'endpoint REST /info di Hyperliquid (userFillsByTime) per un
wallet, confronta i fill ricevuti con l'ultimo stato salvato su disco e
manda un messaggio Telegram per ogni nuova esecuzione (fill) trovata:
acquisto/vendita a limite, stop-loss/take-profit scattati, ecc. -- qualsiasi
ordine che genera un fill sull'account viene notificato.

Pensato per girare periodicamente (es. ogni 5-10 minuti via GitHub Actions
schedulato), non come processo always-on.

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

HL_INFO_URL = "https://api.hyperliquid.xyz/info"
TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"

DEFAULT_STATE_FILE = "state/last_fill.json"
DEFAULT_LOOKBACK_MINUTES = 15


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


def fetch_twap_slice_fills(wallet: str, start_time_ms: int) -> list:
    """Le esecuzioni di ordini TWAP NON compaiono in userFills/userFillsByTime:
    Hyperliquid le espone solo tramite questo endpoint separato, con uno
    schema annidato ({"fill": {...}, "twapId": N}) e senza filtro temporale
    lato server. Filtriamo qui in locale usando start_time_ms."""
    payload = {"type": "userTwapSliceFills", "user": wallet}
    result = http_post_json(HL_INFO_URL, payload)
    if not isinstance(result, list):
        raise RuntimeError(f"Risposta inattesa da Hyperliquid (TWAP): {result!r}")

    fills = []
    for item in result:
        fill = dict(item.get("fill") or {})
        if not fill:
            continue
        fill["twapId"] = item.get("twapId")
        if fill.get("time", 0) >= start_time_ms:
            fills.append(fill)
    return fills


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


def format_message(fill: dict) -> str:
    coin = fill.get("coin", "?")
    side_raw = fill.get("side", "")
    side = "BUY" if side_raw == "B" else "SELL" if side_raw == "A" else side_raw
    px = fill.get("px", "?")
    sz = fill.get("sz", "?")
    direction = fill.get("dir", "")
    closed_pnl = fill.get("closedPnl", "0")
    fee = fill.get("fee", "0")
    fee_token = fill.get("feeToken", "")
    ts_ms = fill.get("time", 0)
    ts = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(ts_ms / 1000)) if ts_ms else "?"

    twap_id = fill.get("twapId")

    lines = [
        f"✅ Eseguito su Hyperliquid: {side} {sz} {coin} @ {px}",
    ]
    if twap_id:
        lines.append(f"Slice di ordine TWAP (id: {twap_id})")
    if direction:
        lines.append(f"Tipo: {direction}")
    try:
        if float(closed_pnl) != 0:
            lines.append(f"PnL realizzato: {closed_pnl} USDC")
    except (TypeError, ValueError):
        pass
    if fee and fee_token:
        lines.append(f"Fee: {fee} {fee_token}")
    lines.append(f"Orario: {ts}")
    return "\n".join(lines)


def send_telegram_message(bot_token: str, chat_id: str, text: str, dry_run: bool = False) -> None:
    if dry_run:
        print("--- [DRY RUN] messaggio che verrebbe inviato ---")
        print(text)
        print("-------------------------------------------------")
        return
    url = TELEGRAM_API_URL.format(token=bot_token)
    payload = {"chat_id": chat_id, "text": text}
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

    if last_time_ms is None:
        start_time_ms = now_ms - lookback_minutes * 60 * 1000
        print(f"Nessuno stato precedente trovato: guardo indietro {lookback_minutes} minuti.")
    else:
        # Piccolo margine all'indietro per non perdere fill sullo stesso ms,
        # deduplicati poi tramite seen_tids.
        start_time_ms = last_time_ms - 1000

    fills = fetch_fills(wallet, start_time_ms, now_ms)
    twap_fills = fetch_twap_slice_fills(wallet, start_time_ms)
    fills = fills + twap_fills
    fills.sort(key=lambda f: (f.get("time", 0), f.get("tid", 0)))

    new_fills = [f for f in fills if f.get("tid") not in seen_tids]

    print(f"Trovati {len(fills)} fill nell'intervallo, {len(new_fills)} nuovi.")

    max_time_seen = last_time_ms or start_time_ms
    latest_tids = list(seen_tids)

    for fill in new_fills:
        text = format_message(fill)
        try:
            send_telegram_message(bot_token, chat_id, text, dry_run=dry_run)
        except Exception as e:
            print(f"ERRORE invio Telegram per tid={fill.get('tid')}: {e}", file=sys.stderr)
            continue
        tid = fill.get("tid")
        if tid is not None:
            latest_tids.append(tid)
        max_time_seen = max(max_time_seen, fill.get("time", max_time_seen))

    # Tieni solo i tid recenti per non far crescere il file all'infinito.
    latest_tids = latest_tids[-500:]

    save_state(state_file, {"last_time_ms": max_time_seen, "seen_tids": latest_tids})
    return 0


if __name__ == "__main__":
    sys.exit(main())
