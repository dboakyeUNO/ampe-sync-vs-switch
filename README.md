# AMPE: Sync vs Switch

A server-authoritative multiplayer Ampe game. The backend locks secret foot choices, evaluates SYNC / Opapa and SWITCH / Osheshe by team identity, enforces five-second round deadlines, and publishes the same resolved exchange to every player session.

## Run locally

Requires Python 3.10 or newer. The project uses only Python standard-library modules; there are no packages to install.

```powershell
python server.py
```

Open `http://localhost:8000`. For two devices on the same network, open the host computer's LAN address on the other device.

## Automated tests

```powershell
python -m unittest -v test_multiplayer
```

The tests cover both team outcomes from both attacker positions, all four foot pairs, 1v1 transitions and configurable targets, timeouts, late and duplicate choices, fixed team lineups, reversals and elimination, 2v2 through 10v10, session privacy and recovery, host failover, rematches, side swaps, room expiry, and invite links.

## Deploy to Render

`render.yaml` configures one Python web service in Ohio on the Free plan. The build compiles `server.py`; there are no dependencies to install. The app binds to `0.0.0.0`, uses Render's `PORT`, and responds at `/api/health`.

1. Push this project to a Git repository accessible to your Render account.
2. In Render, choose **New + -> Blueprint** and connect that repository.
3. Review the service described by `render.yaml` and apply the Blueprint.
4. Wait for the first deploy to complete. Check the service's `onrender.com` URL and confirm `/api/health` returns `{"ok":true,"service":"ampe-authoritative"}`.

This workspace has no Git remote or Render account connection, so no stable Render deployment has been created. Render Blueprints use the connected repository unless a different `repo` is configured. See the [Render Blueprint reference](https://render.com/docs/blueprint-spec).

## Operational limits

Room state is held in memory and rooms expire after six hours without activity. A process restart, deploy, or instance replacement ends active matches. Keep this service to one instance because instances do not share room memory. Render Free web services may sleep after 15 minutes without traffic, and a restart loses active games. See [Render Free instance limits](https://render.com/docs/free). Durable competition use needs shared persistence and an always-on service.
