import json, os, time, urllib.error, urllib.request
from enum import Enum
import qbittorrentapi

# qBittorrent connection settings
URL = os.getenv("QBT_URL", "http://localhost")
PORT = int(os.getenv("QBT_PORT", "8080"))
API_TOKEN = os.getenv("QBT_API_TOKEN")
# Empty or the .env.example placeholder means no real token has been set yet
HAS_API_TOKEN = bool(API_TOKEN) and API_TOKEN != "qbt_CHANGE_ME"

# gluetun control server settings
GLUETUN_URL = os.getenv("GLUETUN_URL", "http://localhost:8000").rstrip("/")
GLUETUN_API_KEY = os.getenv("GLUETUN_API_KEY")
VPN_INTERFACE = os.getenv("QBT_VPN_INTERFACE", "tun0")

# Interface qBittorrent is parked on while gluetun has no forwarded port
PARKED_INTERFACE = "lo"

# How often to check gluetun and qBittorrent (in seconds)
CHECK_INTERVAL = int(os.getenv("QBT_CHECK_INTERVAL", "60"))

# Port watchdog
FIREWALLED_THRESHOLD = int(os.getenv("QBT_FIREWALLED_THRESHOLD", "3"))
REBIND_COOLDOWN = int(os.getenv("QBT_REBIND_COOLDOWN", "600"))

# Torrent Management
CLEANUP_EVERY = int(os.getenv("QBT_CLEANUP_EVERY", "0"))
MAX_RATIO = float(os.getenv("QBT_MAX_RATIO", "2.0"))

class ManageMethod(Enum):
    IGNORE = 0
    STOP = 1
    REMOVE = 2
    DELETE = 3

    @staticmethod
    def from_str(label: str):
        label = label.strip().upper()
        if label in ManageMethod.__members__:
            return ManageMethod[label]
        return ManageMethod.IGNORE

NO_CATEGORY_METHOD = ManageMethod.from_str(os.getenv("QBT_NO_CATEGORY_METHOD", "ignore"))
STOP_CATEGORIES = [c.strip() for c in os.getenv("QBT_STOP_CATEGORIES", "").split(",") if c.strip()]
REMOVE_CATEGORIES = [c.strip() for c in os.getenv("QBT_REMOVE_CATEGORIES", "").split(",") if c.strip()]
DELETE_CATEGORIES = [c.strip() for c in os.getenv("QBT_DELETE_CATEGORIES", "").split(",") if c.strip()]
DEFAULT_METHOD = ManageMethod.from_str(os.getenv("QBT_DEFAULT_METHOD", "ignore"))


# Get gluetun's forwarded port, 0 is down.
def get_forwarded_port():
    request = urllib.request.Request(f"{GLUETUN_URL}/v1/portforward")
    if GLUETUN_API_KEY:
        request.add_header("X-API-Key", GLUETUN_API_KEY)
    with urllib.request.urlopen(request, timeout=10) as response:
        data = json.load(response)
    ports = data.get("ports") or []
    return int(ports[0]) if ports else 0


# qBittorrent ignores setting the port it already has, so bounce the port up and back
def rebind_port(client, port):
    temp = port + 1 if port < 65535 else port - 1
    client.app.preferences = {"listen_port": temp}
    time.sleep(2)
    client.app.preferences = {"listen_port": port}


def make_client():
    client = qbittorrentapi.Client(host=URL, port=PORT, api_key=API_TOKEN)
    client.auth_log_in()
    return client


# Docker HEALTHCHECK: healthy only when qBittorrent's API accepts our API key.
def healthcheck():
    if not HAS_API_TOKEN:
        raise SystemExit("QBT_API_TOKEN is not set")
    base = URL.rstrip("/")
    if "://" not in base:
        base = f"http://{base}"
    request = urllib.request.Request(f"{base}:{PORT}/api/v2/app/version")
    request.add_header("Authorization", f"Bearer {API_TOKEN}")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            print(f"qBittorrent {response.read().decode().strip()}")
    except urllib.error.HTTPError as e:
        hint = " (check QBT_API_TOKEN)" if e.code in (401, 403) else ""
        raise SystemExit(f"qBittorrent API returned HTTP {e.code}{hint}")
    except (urllib.error.URLError, TimeoutError) as e:
        raise SystemExit(f"qBittorrent API unreachable: {e}")


def sync_port(client, forwarded_port, state):
    prefs = client.app.preferences
    current_port = prefs["listen_port"]
    current_interface = prefs.get("current_network_interface", "")

    if forwarded_port == 0:
        # The VPN is down or reconnecting. Park qBittorrent on loopback so it is guaranteed to
        # rebind once a port comes back, even if Proton hands out the same port again.
        if current_interface != PARKED_INTERFACE:
            print("gluetun has no forwarded port; parking qBittorrent on loopback.")
            client.app.preferences = {"listen_port": 0, "current_network_interface": PARKED_INTERFACE}
        else:
            print("Waiting for gluetun to forward a port.")
        state["firewalled_count"] = 0
        return

    if current_port != forwarded_port or current_interface != VPN_INTERFACE:
        print(f"Applying forwarded port {forwarded_port} on {VPN_INTERFACE} "
              f"(was {current_port} on {current_interface or 'any interface'}).")
        client.app.preferences = {
            "listen_port": forwarded_port,
            "current_network_interface": VPN_INTERFACE,
            "random_port": False,
            "upnp": False,
        }
        state["last_rebind"] = time.monotonic()
        state["firewalled_count"] = 0
        return

    status = client.transfer.info.get("connection_status", "")

    if FIREWALLED_THRESHOLD > 0:
        if status == "firewalled":
            state["firewalled_count"] += 1
        else:
            state["firewalled_count"] = 0

        if (state["firewalled_count"] >= FIREWALLED_THRESHOLD
                and time.monotonic() - state["last_rebind"] > REBIND_COOLDOWN):
            print(f"Firewalled for {state['firewalled_count']} checks on port {forwarded_port}; forcing rebind.")
            rebind_port(client, forwarded_port)
            state["last_rebind"] = time.monotonic()
            state["firewalled_count"] = 0
            return

    print(f"Port {forwarded_port} OK (status: {status or 'unknown'}).")


def cleanup_torrents(client):
    stopped_count = removed_count = deleted_count = 0

    torrents = client.torrents_info()
    print(f"Checking {len(torrents)} torrent(s) for ratio > {MAX_RATIO}.")

    for torrent in torrents:
        method = DEFAULT_METHOD

        if torrent.category:
            if torrent.category in DELETE_CATEGORIES:
                method = ManageMethod.DELETE
            elif torrent.category in REMOVE_CATEGORIES:
                method = ManageMethod.REMOVE
            elif torrent.category in STOP_CATEGORIES:
                method = ManageMethod.STOP
        else:
            method = NO_CATEGORY_METHOD

        if torrent.ratio <= MAX_RATIO:
            continue

        is_seeding = torrent.state_enum.is_uploading
        is_stopped = torrent.state in ("pausedUP", "stoppedUP")

        # Ignore torrents that are not seeding
        if not is_seeding and not is_stopped:
            continue

        if method == ManageMethod.STOP:
            # Don't stop torrents that are already stopped
            if not is_stopped:
                print(f"Stopping: {torrent.name} (ratio={torrent.ratio:.2f})")
                torrent.stop()
                stopped_count += 1
        elif method == ManageMethod.REMOVE:
            print(f"Removing: {torrent.name} (ratio={torrent.ratio:.2f})")
            torrent.delete(delete_files=False)
            removed_count += 1
        elif method == ManageMethod.DELETE:
            print(f"Deleting: {torrent.name} (ratio={torrent.ratio:.2f})")
            torrent.delete(delete_files=True)
            deleted_count += 1

    print(f"Stopped {stopped_count}, removed {removed_count}, and deleted {deleted_count}.")


def main():
    # Logging in without a key would get us banned by qBittorrent, so wait to be restarted with one.
    if not HAS_API_TOKEN:
        print("QBT_API_TOKEN is not set. Create an API key in qBittorrent, set it, and restart qbtman.")
        while True:
            time.sleep(3600)

    state = {"firewalled_count": 0, "last_rebind": float("-inf")}
    client = None
    loop = 0

    while True:
        started = time.monotonic()
        try:
            if client is None:
                client = make_client()

            # A gluetun problem shouldn't stop torrent cleanup, so handle it separately.
            forwarded_port = None
            try:
                forwarded_port = get_forwarded_port()
            except urllib.error.HTTPError as e:
                hint = " (check GLUETUN_API_KEY)" if e.code in (401, 403) else ""
                print(f"gluetun control server returned HTTP {e.code}{hint}.")
            except (urllib.error.URLError, TimeoutError, ValueError) as e:
                print(f"gluetun control server unreachable: {e}")

            if forwarded_port is not None:
                sync_port(client, forwarded_port, state)

            if CLEANUP_EVERY > 0 and loop % CLEANUP_EVERY == 0:
                cleanup_torrents(client)

        except qbittorrentapi.LoginFailed as e:
            print(f"Login failed: {e}")
            client = None
        except qbittorrentapi.APIConnectionError as e:
            print(f"qBittorrent unreachable: {e}")
            client = None
        except qbittorrentapi.APIError as e:
            print(f"qBittorrent API error: {e}")
            client = None
        except Exception as e:
            print(f"Error: {e}")

        loop += 1
        time.sleep(max(1, CHECK_INTERVAL - (time.monotonic() - started)))
