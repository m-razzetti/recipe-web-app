# Cloudflare Tunnel

This project can be exposed externally without router port forwarding by running `cloudflared` as part of the Compose stack.

## What changed

- `docker-compose.yml` now includes an optional `cloudflared` service.
- The service is behind the `cloudflare` profile, so it does not start unless you ask for it.
- The tunnel authenticates with `CLOUDFLARE_TUNNEL_TOKEN` from `.env`.

## Cloudflare dashboard setup

1. In Cloudflare Zero Trust, create a new named tunnel.
2. Choose Docker as the connector type.
3. Copy the tunnel token Cloudflare gives you.
4. Add that token to `.env` as `CLOUDFLARE_TUNNEL_TOKEN=...`.
5. In the tunnel's public hostname settings, point your hostname at `http://frontend:80`.

Recommended hostname mapping:

- `recipes.yourdomain.com` -> `http://frontend:80`

If you want to expose the API separately, add another public hostname:

- `api.recipes.yourdomain.com` -> `http://backend:8000`

## Start it

```bash
docker compose --profile cloudflare up -d cloudflared
```

If the rest of the stack is not running yet:

```bash
docker compose --profile cloudflare up -d
```

## Port forwarding

With Cloudflare Tunnel running, you do not need to forward router ports for public access.

You can keep local ports like `9001`, `9002`, and `8081` for LAN/admin use, but they are not required for Cloudflare Tunnel itself.
