import os, re, subprocess, time
from enum import Enum
import qbittorrentapi

# qBittorrent connection settings
URL = os.getenv("QBT_URL", "localhost")
PORT = int(os.getenv("QBT_PORT", "8081"))
API_TOKEN = os.getenv("QBT_API_TOKEN")

# Natpmp settings
NATPMP_GATEWAY = os.getenv("QBT_NATPMP_GATEWAY", "10.2.0.1")
RENEW_INTERVAL = int(os.getenv("QBT_RENEW_INTERVAL", "45"))
FIREWALLED_THRESHOLD = int(os.getenv("QBT_FIREWALLED_THRESHOLD", "3"))
REBIND_COOLDOWN = int(os.getenv("QBT_REBIND_COOLDOWN", "600"))

# Torrent Management
CLEANUP_EVERY = int(os.getenv("QBT_CLEANUP_EVERY", "0"))
MAX_RATIO = float(os.getenv("QBT_MAX_RATIO", "2.0"))

STOP_CATEGORIES = [c.strip() for c in os.getenv("QBT_STOP_CATEGORIES", "").split(",") if c.strip()]
REMOVE_CATEGORIES = [c.strip() for c in os.getenv("QBT_REMOVE_CATEGORIES", "").split(",") if c.strip()]
DELETE_CATEGORIES = [c.strip() for c in os.getenv("QBT_DELETE_CATEGORIES", "").split(",") if c.strip()]

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

def get_forwarded_port(protocol):
    result = subprocess.run(
        ["natpmpc", "-a", "1", "0", protocol, "60", "-g", NATPMP_GATEWAY],
        capture_output=True,
        text=True,
        timeout=10,
    )
    output = result.stdout + "\n" + result.stderr

    print(output)

    if result.returncode != 0:
        raise RuntimeError(f"natpmpc failed: {output}")

    match = re.search(r"Mapped public port (\d+) protocol", output, re.IGNORECASE)
    if not match:
        raise RuntimeError(f"Could not find assigned port:\n{output}")

    return int(match.group(1))


def get_ports():
    udp_port = get_forwarded_port("udp")
    tcp_port = get_forwarded_port("tcp")
    if udp_port != tcp_port:
        raise RuntimeError(f"UDP Port {udp_port} does not equal TCP Port {tcp_port}")
    return udp_port


def rebind_port(client, port):
    """qBittorrent ignores setting the port it already has, so bounce it
    through a temporary value to force the listening socket to be rebuilt."""
    temp = port + 1 if port < 65535 else port - 1
    client.app.preferences = {"listen_port": temp}
    time.sleep(2)
    client.app.preferences = {"listen_port": port}


def make_client():
    client = qbittorrentapi.Client(host=URL, port=PORT, api_key=API_TOKEN)
    client.auth_log_in()
    return client


def sync_port(client, new_port, state):
    current_port = client.app.preferences["listen_port"]
    status = client.transfer.info.get("connection_status", "")

    if status == "firewalled":
        state["firewalled_count"] += 1
    else:
        state["firewalled_count"] = 0

    if current_port != new_port:
        print(f"Port changed {current_port} -> {new_port}, applying.")
        client.app.preferences = {"listen_port": new_port}
        state["last_rebind"] = time.monotonic()
        state["firewalled_count"] = 0
        return

    if (state["firewalled_count"] >= FIREWALLED_THRESHOLD
            and time.monotonic() - state["last_rebind"] > REBIND_COOLDOWN):
        print(f"Firewalled for {state['firewalled_count']} checks on port {new_port}; forcing rebind.")
        rebind_port(client, new_port)
        state["last_rebind"] = time.monotonic()
        state["firewalled_count"] = 0
        return

    print(f"Port {current_port} OK (status: {status or 'unknown'}).")


def cleanup_torrents(client):
    stopped_count = removed_count = deleted_count = 0

    torrents = client.torrents_info()
    print(f"Checking {len(torrents)} torrent(s) for ratio > {MAX_RATIO}.")

    for torrent in torrents:
        method = ManageMethod.IGNORE

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
    state = {"firewalled_count": 0, "last_rebind": float("-inf")}
    client = None
    loop = 0

    while True:
        started = time.monotonic()
        try:
            # Renew the mapping first, every loop, no matter what else fails.
            new_port = get_ports()

            if client is None:
                client = make_client()

            sync_port(client, new_port, state)

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
        time.sleep(max(1, RENEW_INTERVAL - (time.monotonic() - started)))