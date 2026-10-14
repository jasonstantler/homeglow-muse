"""Bluetooth pairing for Muse Link, kept apart from the rest of the add-on.

Two modes:

  pair_helper.py --broker   Started once by muse_link.py while the add-on still
                            has the network-admin permission, which borrowing
                            the Bluetooth adapter needs. muse_link.py then gives
                            that permission up. The broker only reads "start"
                            and "cancel" lines and runs one pairing at a time,
                            passing its events back.
  pair_helper.py            One pairing window. Prints JSON lines: "open" when
                            setup opens, "borrowed"/"returned" around taking
                            the adapter from Home Assistant, "dropped" if the
                            app hangs up right after confirming, and "done".

Pairing uses Muse Link's own Bluetooth stack (ble_direct.py) so the Muse
Android app works; if the adapter can't be borrowed it falls back to the
SDK's BlueZ server.
"""

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import types

logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("muse-link.pairing")
_print_lock = threading.Lock()


def say(**event):
    with _print_lock:
        print(json.dumps(event), flush=True)


class WatchSetup(logging.Handler):
    """Turns the setup log into events for the Muse Link page.

    Notices the Muse app hanging up right after it confirmed pairing (what the
    Android app does when the ATT MTU is above 515), and the adapter being
    borrowed from Home Assistant and given back.
    """

    def __init__(self):
        super().__init__(logging.INFO)
        self.mtu = 0
        self.confirmed = False
        self.moved_on = False

    def emit(self, record):
        try:
            text = record.getMessage()
        except Exception:
            return
        if text.startswith("ATT MTU "):
            try:
                self.mtu = int(text.split()[-1])
            except ValueError:
                pass
        elif text.startswith("pairing confirmed"):
            self.confirmed, self.moved_on = True, False
        elif self.confirmed and text.startswith("RX action:"):
            self.moved_on = True
        elif text.startswith("BLE client disconnected"):
            if self.confirmed and not self.moved_on:
                say(event="dropped", mtu=self.mtu)
            self.confirmed = False
        elif text.startswith("borrowed Bluetooth adapter"):
            say(event="borrowed")
        elif text.startswith("gave the Bluetooth adapter back"):
            say(event="returned")


def _use_direct_bluetooth(mac):
    """Have the SDK's pair() use ble_direct.DirectBleServer instead of BlueZ."""
    import ble_direct

    servers = []

    class Server(ble_direct.DirectBleServer):
        def __init__(self, local_name, on_write, on_disconnect):
            super().__init__(local_name, on_write, on_disconnect,
                             address=ble_direct.random_static_address(mac))
            servers.append(self)

    def stop(_signum, _frame):
        # Cancel: close the window cleanly, so the adapter goes straight back.
        if servers:
            servers[-1].stop()
        else:
            sys.exit(1)

    signal.signal(signal.SIGTERM, stop)
    shim = types.ModuleType("musegadget.ble_server")
    shim.BleServer = Server
    sys.modules["musegadget.ble_server"] = shim


def _use_bluez():
    sys.modules.pop("musegadget.ble_server", None)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)


def pair_once():
    from musegadget import identity
    from musegadget.pair import pair

    watch = WatchSetup()
    for name in ("musegadget", "muse-link.bluetooth"):
        logging.getLogger(name).addHandler(watch)

    def opened(window):
        say(event="open", name=window.ble_name, timeout=window.timeout_s, token=window.has_sdk_token)

    try:
        import ble_direct
        _use_direct_bluetooth(identity.load_or_create().mac)
    except Exception as exc:  # Bumble missing or broken: use BlueZ
        log.warning("Muse Link's own Bluetooth isn't available (%s); using Home Assistant's", exc)
        ble_direct = None
        _use_bluez()

    try:
        try:
            result = pair(on_open=opened)
        except Exception as exc:
            if ble_direct is None or not isinstance(exc, ble_direct.AdapterUnavailable):
                raise
            if exc.reason == "no_adapter":
                raise RuntimeError("no Bluetooth adapter found") from exc
            log.warning("couldn't borrow the Bluetooth adapter (%s); pairing through Home Assistant's Bluetooth", exc)
            say(event="fallback", reason=exc.reason)
            _use_bluez()
            result = pair(on_open=opened)
    except Exception as exc:  # no adapter, no D-Bus, Bluetooth refused ...
        say(event="done", outcome="failed", problem="%s: %s" % (type(exc).__name__, exc))
        return 1
    say(event="done", outcome=result.outcome.value, problem=result.sdk_token_problem)
    return 0


def broker():
    """Run pairings on request, one at a time, keeping this process's permissions."""
    child = None

    def relay(proc):
        for line in proc.stdout:
            with _print_lock:
                sys.stdout.write(line)
                sys.stdout.flush()
        proc.wait()
        say(event="exit", code=proc.returncode)

    for line in sys.stdin:
        command = line.strip()
        running = child is not None and child.poll() is None
        if command == "start" and not running:
            child = subprocess.Popen([sys.executable, os.path.abspath(__file__)],
                                     stdout=subprocess.PIPE, text=True, env=dict(os.environ))
            threading.Thread(target=relay, args=(child,), daemon=True).start()
        elif command == "cancel" and running:
            child.terminate()
    # muse_link.py went away: close any pairing window, which gives the adapter back.
    if child is not None and child.poll() is None:
        child.terminate()
        child.wait(10)
    return 0


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    sys.exit(broker() if "--broker" in sys.argv[1:] else pair_once())
