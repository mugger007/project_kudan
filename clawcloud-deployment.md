# Kudan Deployment on ClawCloud Run

This guide focuses on run.claw.cloud App Launchpad with a Docker image.

## 1. Sign Up and Claim Free Credit

1. Visit `https://run.claw.cloud`.
2. Sign in with GitHub.
3. Use an account older than 180 days to unlock monthly free credit (typically around $5).

## 2. Build and Push Docker Image

Use GitHub Container Registry (recommended):

```bash
docker login ghcr.io

docker build -t ghcr.io/<your-github-username>/kudan:latest .
docker push ghcr.io/<your-github-username>/kudan:latest
```

## 3. Create App in App Launchpad

1. Open ClawCloud Run -> App Launchpad.
2. Create new app from image:
   - `ghcr.io/<your-github-username>/kudan:latest`
3. Resource recommendation for free credit:
   - 1 to 2 vCPU
   - 2 to 4 GB RAM
   - 10+ GB storage
4. Expose container port `8080` for health checks.

## 4. Environment Variables

Set all variables from `.env.example` in the Launchpad dashboard, especially:

- `POLYMARKET_PRIVATE_KEY`
- `POLYMARKET_WALLET_ADDRESS`
- `POLYGON_RPC_PRIMARY`
- `POLYGON_RPC_FALLBACKS`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `DRY_RUN`
- `HEALTH_PORT=8080`
- `DB_PATH=/data/kudan.db`

## 5. Health Check

Configure health check path as:

- Path: `/health`
- Port: `8080`
- Interval: 30s
- Timeout: 5s

## 6. VPN Inside Container (Optional)

To enable OpenVPN with Proton `.ovpn` config in container mode:

1. Enable privileged/container capabilities in Launchpad (NET_ADMIN and TUN device support).
2. Ensure `openvpn` is available in the image.
3. Mount your Proton `.ovpn` file into the container.
4. Set:
   - `VPN_ENABLED=true`
   - `OPENVPN_CONFIG_FILE=/path/to/your/proton.ovpn`
   - `OPENVPN_EXECUTABLE=openvpn`
   - Optional: `OPENVPN_AUTH_FILE=/path/to/auth.txt`

Kudan runs a reconnect watch loop and entrypoint first-attempt connect.

## 7. Verify Runtime

- Logs should show: `Kudan awakened`.
- Health endpoint should return HTTP 200.
- Telegram should receive startup alert if configured.

## 8. Cost Control Tips

- Keep adaptive polling enabled.
- Start with `DRY_RUN=true`.
- Use low-frequency defaults for medium bucket until stable.
