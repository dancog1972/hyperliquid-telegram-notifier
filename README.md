# Hyperliquid → Telegram notifier

Notifica su Telegram ogni volta che un ordine sul tuo account Hyperliquid
viene **eseguito** (fill): acquisto/vendita a limite, stop-loss o
take-profit che scattano, ecc. Gira gratuitamente su GitHub Actions, con un
controllo ogni ~10 minuti — non è notifica istantanea, ma non richiede
nessun server sempre acceso.

## Come funziona

1. Un workflow schedulato (`.github/workflows/notify.yml`) esegue
   `notify_fills.py` ogni 10 minuti.
2. Lo script chiama l'endpoint pubblico `POST /info` di Hyperliquid
   (`userFillsByTime`) usando solo il tuo **indirizzo wallet pubblico** —
   non serve mai la chiave privata.
3. Confronta i fill ricevuti con l'ultimo stato salvato
   (`state/last_fill.json`, committato nel repo) per capire quali sono
   nuovi.
4. Per ogni fill nuovo manda un messaggio al bot Telegram configurato.
5. Alla fine il workflow fa commit del nuovo stato, così il run successivo
   sa da dove ripartire.

## Setup

### 1. Crea il bot Telegram

1. Apri una chat con [@BotFather](https://t.me/BotFather) su Telegram.
2. `/newbot`, segui le istruzioni, salva il **token** che ti dà (tipo
   `123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`).
3. Scrivi un messaggio qualsiasi al tuo nuovo bot (o aggiungilo a un
   gruppo/canale).
4. Recupera il tuo **chat id**:
   apri nel browser
   `https://api.telegram.org/bot<IL_TUO_TOKEN>/getUpdates`
   dopo aver scritto al bot, e cerca il campo `"chat":{"id": ...}`.

### 2. Crea il repository su GitHub

1. Crea un nuovo repository (può essere privato — anzi, meglio).
2. Carica questi file mantenendo la struttura:
   ```
   notify_fills.py
   .github/workflows/notify.yml
   state/last_fill.json
   ```

### 3. Configura i secrets del repo

Nel repository: **Settings → Secrets and variables → Actions → New
repository secret**. Aggiungi:

| Nome secret          | Valore                                              |
|-----------------------|------------------------------------------------------|
| `HL_WALLET_ADDRESS`   | Il tuo indirizzo wallet Hyperliquid (`0x...`), pubblico |
| `TELEGRAM_BOT_TOKEN`  | Il token ottenuto da BotFather                       |
| `TELEGRAM_CHAT_ID`    | Il chat id ottenuto al punto precedente              |

### 4. Abilita le Actions e testa

1. Vai nella tab **Actions** del repo, abilita i workflow se richiesto.
2. Apri il workflow "Hyperliquid -> Telegram notifier" e lancialo a mano
   con **Run workflow** (grazie al trigger `workflow_dispatch`) per un
   primo test, invece di aspettare il cron.
3. Se tutto va bene, da quel momento riceverai un messaggio Telegram per
   ogni fill futuro. La primissima esecuzione guarda indietro solo
   `LOOKBACK_MINUTES` (default 15) minuti, per non spammarti con tutta la
   storia del wallet.

## Personalizzazioni utili

- **Frequenza del controllo**: modifica la riga `cron` in
  `.github/workflows/notify.yml` (minimo consigliato da GitHub: 5 minuti;
  sotto carico le esecuzioni schedulate possono comunque ritardare).
- **Finestra di lookback al primo avvio**: variabile d'ambiente
  `LOOKBACK_MINUTES` nel workflow.
- **Test senza inviare messaggi veri**: esegui localmente con
  `DRY_RUN=1 HL_WALLET_ADDRESS=0x... python notify_fills.py` — stampa i
  messaggi a schermo invece di mandarli su Telegram.

## Limiti da conoscere

- Non è vero real-time: la latenza massima è pari all'intervallo del
  cron (di default fino a ~10 minuti, più eventuali code di GitHub).
- L'endpoint `userFillsByTime` restituisce al massimo gli ultimi 10.000
  fill e 2.000 per chiamata: per un account con volumi altissimi in
  quella finestra andrebbe gestita la paginazione (non necessaria per un
  uso normale).
- Il file di stato viene committato nel repo dal bot stesso: se lavori in
  team su questo repo, tienine conto per evitare conflitti.
