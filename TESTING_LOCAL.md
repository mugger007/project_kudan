# Local Testing and Development Guide for Kudan

This guide helps you test the Kudan trading bot locally before deploying to ClawCloud.

## 1. Virtual Environment Setup

The project includes a Python 3.13 virtual environment configured at `.venv/` with all dependencies pre-installed.

**Python location:**
```
C:\Users\Jianyang\Documents\Project Kudan\.venv\Scripts\python.exe
```

## 2. Testing Checklist

### Pre-Test
1. **Copy and update `.env`:**
   ```bash
   cp .env.example .env
   ```
   Edit `.env` with:
   - Valid Polymarket wallet address and private key (use **testnet or small funds** first)
   - Free Polygon RPC URLs (Alchemy free tier, 1RPC.io)
   - Telegram bot token and chat ID (optional but recommended for alerts)
   - Set `DRY_RUN=true` for initial testing

2. **Start in dry-run mode first:**
   Make sure `.env` has:
   ```
   DRY_RUN=true
   ```
   This ensures no real trades are executed.

### Run Local Bot

**PowerShell:**
```powershell
& "C:\Users\Jianyang\Documents\Project Kudan\.venv\Scripts\python.exe" main.py
```

**Or simpler (if venv is already active):**
```bash
python main.py
```

### Expected Output
On startup, you should see:
```
2026-04-17 14:32:15 | INFO | kudan | Starting task market_refresh every ~120s
2026-04-17 14:32:15 | INFO | kudan | Starting task event_discovery every ~600s
2026-04-17 14:32:15 | INFO | kudan | Starting task high_prob_5min every ~20s
2026-04-17 14:32:15 | INFO | kudan | Health endpoint listening at http://0.0.0.0:8080/health
2026-04-17 14:32:15 | INFO | kudan | Kudan awakened: guardian of hidden probabilities now watches the oracle realm.
```

### Health Check

In another terminal:
```bash
curl http://127.0.0.1:8080/health
```

Response should be JSON:
```json
{
  "status": "ok",
  "api_ok": true,
  "rpc_ok": true,
  "last_market_scan": "2026-04-17T14:32:30Z",
  "cached_markets": 142,
  "dashboard": {
    "scanned_markets": 150,
    "opportunities_found": 0,
    "trades_sent": 0
  }
}
```

## 3. Local Docker Test

Before pushing to ClawCloud, you can also test the container locally:

```bash
docker compose up -d --build
docker compose logs -f kudan
```

Health check:
```bash
curl http://127.0.0.1:8080/health
```

Stop:
```bash
docker compose down
```

## 4. Database & Logging

### SQLite Database
- Default location (local): `./kudan.db` (from `.env.example`)
- Container location: `/data/kudan.db`

Query logs:
```bash
sqlite3 kudan.db "SELECT * FROM opportunities ORDER BY ts DESC LIMIT 5;"
```

### Log Output
Logs stream to stdout/stderr. For persistent testing, capture to file:
```bash
python main.py > kudan.log 2>&1 &
tail -f kudan.log
```

## 5. Stress Testing

### Dry-Run Coverage
With `DRY_RUN=true`, the bot will:
- Discover and classify candidate events
- Identify high-prob opportunities from shortlisted candidates
- Log them to SQLite
- Print Telegram alerts to console
- NOT place real trades

Run for 30+ minutes to see:
- Market scanning cycles
- Opportunity detection
- Rate-limit behavior
- Health checks

### Risk Settings
Adjust these in `.env` to test different market conditions:
```
HIGH_PROB_THRESHOLD=0.99
MIN_LIQUIDITY_USD=200.0
MAX_SLIPPAGE_PCT=0.0075
```

## 6. Troubleshooting

### Import Errors
If you see import errors, reinstall dependencies:
```bash
& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt
```

### RPC Connection Fails
- Check `POLYGON_RPC_PRIMARY` and `POLYGON_RPC_FALLBACKS` are valid
- Verify outbound HTTPS connectivity
- RPC rotator will auto-fallback if primary is down

### Telegram Not Working
- Leave `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` empty for console-only mode
- Set them for production to get alerts

### Health Endpoint Crashes
If `/health` returns 500:
- Check logs for Python exceptions
- Verify all async tasks are running
- Restart the bot

## 7. Ready for ClawCloud?

Once local testing passes:
1. ✓ Bot starts without errors
2. ✓ Health endpoint responds
3. ✓ Markets are scanned
4. ✓ Opportunities logged (or none found in test markets)
5. ✓ Dry-run trades logged without execution

**Next step:** Follow `clawcloud-deployment.md` to push Docker image and deploy to ClawCloud Run.
