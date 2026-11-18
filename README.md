# qbtman

A small companion container for qBittorrent running behind [gluetun](https://github.com/qdm12/gluetun) with ProtonVPN or another VPN provider that supports P2P. It does three things:

- **Port sync** – reads the forwarded port from gluetun's control server and applies it to qBittorrent, bound to the VPN interface. While the VPN is down it parks qBittorrent on loopback, so qBittorrent always rebinds when a port comes back.
- **Firewalled watchdog** – if qBittorrent keeps reporting "firewalled" on the correct port, it forces a rebind.
- **Ratio management** (optional) – stops, removes, or deletes seeding torrents above a ratio, based on their category.

gluetun handles the VPN tunnel, the kill switch and the NAT-PMP renewal. qbtman keeps qBittorrent in sync with it.

## Requirements

- Docker with the Compose plugin
- A paid ProtonVPN plan (port forwarding is only available on P2P servers) or another way to port forward with gluetun
- qBittorrent **5.2 or newer**, for API key authentication. The `lscr.io/linuxserver/qbittorrent:latest` image used here meets this.

## Quick Start

### Docker Compose

```yaml
services:
  gluetun:
    image: qmcgaw/gluetun:v3.41.3
    container_name: gluetun
    restart: unless-stopped
    cap_add:
      - NET_ADMIN
    devices:
      - /dev/net/tun:/dev/net/tun
    volumes:
      - ./docker/gluetun:/gluetun
    ports:
      # qBittorrent WebUI (qbittorrent shares gluetun's network, so its ports are published here)
      - "8080:8080"
    environment:
      TZ: America/New_York
      ## Wireguard:
      VPN_TYPE: wireguard
      WIREGUARD_PRIVATE_KEY: ${WIREGUARD_PRIVATE_KEY}
      WIREGUARD_ADDRESSES: ${WIREGUARD_ADDRESSES}
      VPN_DNS_ADDRESS: ${VPN_DNS_ADDRESS}

      VPN_PORT_FORWARDING: "on"
      VPN_PORT_FORWARDING_PROVIDER: protonvpn

      ## Enable if using a specific server:
      # VPN_SERVICE_PROVIDER: custom
      # WIREGUARD_PUBLIC_KEY: ${WIREGUARD_PUBLIC_KEY}
      # WIREGUARD_ENDPOINT_IP: ${WIREGUARD_ENDPOINT_IP}
      # WIREGUARD_ENDPOINT_PORT: ${WIREGUARD_ENDPOINT_PORT}

      ## Enable if allowing gluetun to find servers:
      VPN_SERVICE_PROVIDER: protonvpn
      SERVER_COUNTRIES: "United States,Canada" # What countries to find servers in
      PORT_FORWARD_ONLY: "on" # Only use P2P servers, and let gluetun do the NAT-PMP renewal
      ## Needed when finding servers to refresh the server list
      UPDATER_PERIOD: 24h
      UPDATER_PROTONVPN_EMAIL: ${PROTON_EMAIL} # Proton Username, ends with @protonmail.com
      UPDATER_PROTONVPN_PASSWORD: ${PROTON_PASSWORD}

      # Control server (port 8000, not published) requires auth; qbtman reads the forwarded port from it
      # Generate a key with: docker run --rm qmcgaw/gluetun genkey
      HTTP_CONTROL_SERVER_AUTH_DEFAULT_ROLE: '{"auth":"apikey","apikey":"${GLUETUN_API_KEY}"}'

  qbittorrent:
    image: lscr.io/linuxserver/qbittorrent:latest
    container_name: qbittorrent
    restart: unless-stopped
    network_mode: "service:gluetun"
    depends_on:
      gluetun:
        condition: service_healthy
    environment:
      PUID: 1000
      PGID: 1000
      TZ: America/New_York
      WEBUI_PORT: 8080
    volumes:
      - ./docker/config:/config
      - ./docker/downloads:/downloads

  # qbtman's image healthcheck also covers qBittorrent: it passes only when qBittorrent accepts QBT_API_TOKEN
  qbtman:
    image: ghcr.io/masonwp123/qbtman:latest
    container_name: qbtman
    restart: unless-stopped
    init: true
    network_mode: "service:gluetun"
    depends_on:
      gluetun:
        condition: service_healthy
      qbittorrent:
        condition: service_started
    environment:
      # qBittorrent is reachable on localhost since everything shares gluetun's network
      QBT_URL: http://localhost
      QBT_PORT: 8080
      QBT_API_TOKEN: ${QBT_API_TOKEN}
      # gluetun's control server, where the forwarded port is read from
      GLUETUN_URL: http://localhost:8000
      GLUETUN_API_KEY: ${GLUETUN_API_KEY}
      # Interface qbittorrent is bound to while the VPN is up (gluetun's VPN_INTERFACE)
      QBT_VPN_INTERFACE: tun0
      # How often to check gluetun and qbittorrent (in seconds)
      QBT_CHECK_INTERVAL: 60
      # How many checks qbittorrent needs to report being firewalled before rebinding its port, 0 disables the watchdog
      QBT_FIREWALLED_THRESHOLD: 3
      # How often are we allowed to rebind the qbittorrent port (in seconds)
      QBT_REBIND_COOLDOWN: 600
      # Do the cleanup every n checks, 0 disables torrent management
      QBT_CLEANUP_EVERY: 0
      # What ratio to stop/remove/delete torrents at (please be generous)
      QBT_MAX_RATIO: 2.0
      # Default method for all categories
      QBT_DEFAULT_METHOD: "ignore"
      # Which categories should be explicitly stopped
      QBT_IGNORE_CATEGORIES: "ignore0,ignore1"
      # Which categories should have their torrents stopped
      QBT_STOP_CATEGORIES: "stop0,stop1"
      # Which categories should have their torrents removed (overrides stopped)
      QBT_REMOVE_CATEGORIES: "remove0,remove1"
      # Which categories should have their torrents removed and files deleted (ex: if they auto move)
      QBT_DELETE_CATEGORIES: "delete0,delete1"
      # [ignore, stop, remove, delete] What action to do with torrents that have no category
      QBT_NO_CATEGORY_METHOD: "ignore"
```

Private environment variables should be stored in the .env alongside the docker-compose.yaml. See [.env.example](.env.example)

### 2. Generate a WireGuard config

Source a WireGuard config file for use with port forwarding and that supports P2P connections. It should be in the format of a .conf and will look like this:

```conf
[Interface]         
PrivateKey = MyPrivateKey
Address = IP/Subnet
DNS = IP

[Peer]
PublicKey = MyPublicKey
Endpoint = IP:PORT
```

Copy these values from the `.conf` into `.env`:

| `.env` variable | From the `.conf` |
|---|---|
| `WIREGUARD_PRIVATE_KEY` | `PrivateKey` |
| `WIREGUARD_ADDRESSES` | `Address` |
| `VPN_DNS_ADDRESS` | `DNS` |

### 3. Choose how gluetun picks a server

`docker-compose.yaml` supports two modes. **Comment out whichever one you don't use.**

**A. Let gluetun pick servers (default, recommended).** \
gluetun chooses a random P2P server in `SERVER_COUNTRIES`, and picks another one if a server fails.

- (*Optional*) `PROTON_EMAIL` / `PROTON_PASSWORD` – used to keep gluetun's Proton server list up to date. Use your **Proton address** (`@protonmail.com` / `@proton.me`). gluetun v3.41.3 can't log in with an external email address.

Edit `SERVER_COUNTRIES` in `docker-compose.yaml` to suit you. Pick close servers to prioritize low latency.

**B. Use one specific server.**

| `.env` variable | From the `.conf` |
|---|---|
| `WIREGUARD_PUBLIC_KEY` | `PublicKey` |
| `WIREGUARD_ENDPOINT_IP` | IP part of `Endpoint` |
| `WIREGUARD_ENDPOINT_PORT` | Port part of `Endpoint` |

In this mode there is no failover. If Proton retires that server, you'll need to update `.env`.

### 4. Generate a gluetun API key

qbtman authenticates to gluetun's control server with this key. Port 8000 is not published, so only containers inside gluetun's network can reach it.

```
docker run --rm qmcgaw/gluetun genkey
```

Put the output in `GLUETUN_API_KEY`.

### 5. Start the stack and set up qBittorrent

qbtman needs a qBittorrent API key, and you can only create one once qBittorrent is running. Start gluetun and qBittorrent first:

```
docker compose up -d gluetun qbittorrent
docker logs qbittorrent   # the linuxserver image prints a temporary WebUI password
```

1. Open `http://<docker-host>:8080` and log in as `admin` with the temporary password, then set a permanent password.
2. In the Web UI settings, generate an **API key** and put it in `QBT_API_TOKEN` in `.env`.

You don't need to set qBittorrent's listening port, network interface or UPnP options. qbtman manages them.

qBittorrent doesn't need the token and has no healthcheck of its own. If qbtman is already running without a valid `QBT_API_TOKEN`, it shows as `unhealthy`, which is expected during setup. With no token set, it logs that the token is missing and waits without trying to log in, because repeated failed logins get it banned by qBittorrent.

### 6. Start qbtman

```
docker compose up -d
```

This starts qbtman with the new token, or recreates it if it was already running. It turns `healthy` once qBittorrent accepts the key.

## Usage

### Checking that it works

```
docker logs -f qbtman
```

A healthy start looks like this:

```
Applying forwarded port 46464 on tun0 (was 6881 on any interface).
Port 46464 OK (status: connected).
```

`docker ps` should show gluetun and qbtman as `(healthy)`. qBittorrent has no healthcheck of its own. qbtman's healthcheck calls qBittorrent's `/api/v2/app/version` with `QBT_API_TOKEN`, so it passes only when qBittorrent is up and the key is valid.

From then on there is one line per check, every `QBT_CHECK_INTERVAL` seconds. gluetun logs its own side with `docker logs gluetun`. Look for `port forwarded is …`.

In the qBittorrent Web UI, the connection icon in the status bar should turn green once peers start connecting to you. On a quiet instance this can take a few minutes.

### Updating

```
docker compose pull
docker compose up -d
```

`latest` follows the newest build of `main`.  These should be stable as new updates will only get pushed into `dev` and tested there.

### Restarting gluetun

qBittorrent and qbtman share gluetun's network namespace. If gluetun restarts or is recreated, restart them too:

```
docker compose restart qbittorrent qbtman
```

### gluetun version

The compose file pins `qmcgaw/gluetun:v3.41.3` on purpose. `:latest` is built from gluetun's development branch, which may break features. Check gluetun's release notes before you raise the pin.

## Configuration

All qbtman settings are environment variables on the `qbtman` service in `docker-compose.yaml`.

### Connection

| Variable | Default | Description |
|---|---|---|
| `QBT_URL` | `http://localhost` | qBittorrent Web UI URL. qBittorrent shares gluetun's network, so `localhost` is correct. |
| `QBT_PORT` | `8080` | qBittorrent Web UI port. It must match `WEBUI_PORT` on the qbittorrent service. |
| `QBT_API_TOKEN` | – | qBittorrent API key. |
| `GLUETUN_URL` | `http://localhost:8000` | gluetun control server. |
| `GLUETUN_API_KEY` | – | Must match the key in gluetun's `HTTP_CONTROL_SERVER_AUTH_DEFAULT_ROLE`. |
| `QBT_VPN_INTERFACE` | `tun0` | Interface qBittorrent is bound to. Must match gluetun's `VPN_INTERFACE`. |
| `QBT_CHECK_INTERVAL` | `60` | Seconds between checks. |

### Firewalled watchdog

| Variable | Default | Description |
|---|---|---|
| `QBT_FIREWALLED_THRESHOLD` | `3` | Number of consecutive "firewalled" checks before qbtman forces a rebind. `0` disables the watchdog; port sync keeps running. |
| `QBT_REBIND_COOLDOWN` | `600` | Minimum seconds between forced rebinds, and the grace period after a port change. |

qBittorrent reports "firewalled" until it sees an incoming connection. An idle instance with nothing seeding may trigger a harmless rebind every cooldown period.

### Ratio management

| Variable | Default | Description |
|---|---|---|
| `QBT_CLEANUP_EVERY` | `0` | Run cleanup every *n* checks. `0` disables it. |
| `QBT_MAX_RATIO` | `2.0` | Torrents with a ratio **above** this are acted on. |
| `QBT_STOP_CATEGORIES` | *(empty)* | Comma-separated categories whose torrents are **stopped**. |
| `QBT_REMOVE_CATEGORIES` | *(empty)* | Categories whose torrents are **removed** (files kept). |
| `QBT_DELETE_CATEGORIES` | *(empty)* | Categories whose torrents are **removed and their files deleted**. |
| `QBT_NO_CATEGORY_METHOD` | `ignore` | What to do with uncategorised torrents: `ignore`, `stop`, `remove` or `delete`. |

How cleanup decides:

- Only completed torrents that are seeding or already stopped are considered. Downloading torrents are never touched.
- If a category appears in more than one list, **delete** wins over **remove**, and **remove** wins over **stop**.
- Categories not listed anywhere are ignored.

The category names in `docker-compose.yaml` (`stop0`, `remove0`, …) are placeholders. Replace them with your own before you set `QBT_CLEANUP_EVERY`. **`delete` removes data from disk.** Start with `stop` until you've confirmed it matches the right torrents.

## Troubleshooting

| qbtman log message | Cause |
|---|---|
| `QBT_API_TOKEN is not set` | Expected during setup. Set the key and recreate the container with `docker compose up -d`. |
| `Login failed` / `qBittorrent API error` | `QBT_API_TOKEN` is wrong, or qBittorrent is older than 5.2. |
| `qBittorrent unreachable` | qBittorrent is still starting, or `QBT_PORT` doesn't match `WEBUI_PORT`. |
| `gluetun control server returned HTTP 401 (check GLUETUN_API_KEY)` | The key differs between the gluetun and qbtman services. |
| `gluetun control server unreachable` | gluetun was restarted. Run `docker compose restart qbittorrent qbtman`. |
| `Waiting for gluetun to forward a port.` (keeps repeating) | Check `docker logs gluetun`. The usual causes are a WireGuard config generated without NAT-PMP, or (in custom mode) a server that isn't P2P. |
| Port changes every minute or two | gluetun isn't on `v3.41.3`. See [gluetun version](#gluetun-version). |

If qbtman shows as `unhealthy`, its latest healthcheck output says why:

```
docker inspect --format '{{json .State.Health.Log}}' qbtman
```

| Healthcheck output | Cause |
|---|---|
| `QBT_API_TOKEN is not set` | The token is empty or still the `.env.example` placeholder. Set it and run `docker compose up -d`. |
| `HTTP 403 (check QBT_API_TOKEN)` | `QBT_API_TOKEN` is wrong. After fixing `.env`, run `docker compose up -d` so qbtman picks it up. |
| `qBittorrent API unreachable` | qBittorrent isn't running or isn't listening yet, or `WEBUI_PORT` and `QBT_PORT` don't match. |
