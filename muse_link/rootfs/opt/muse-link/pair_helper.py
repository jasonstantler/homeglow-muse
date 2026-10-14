"""Open Bluetooth setup once, for the Muse app to pair this add-on.

Runs as its own process because the SDK's Bluetooth code takes over signal
handling while it runs. Prints one JSON line when setup opens and one when it
ends, plus one if the app hangs up right after confirming; everything else
goes to the log.
"""

import json
import logging
import sys

logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def say(**event):
    print(json.dumps(event), flush=True)


class WatchSetup(logging.Handler):
    """Notices the Muse app hanging up right after it confirmed pairing.

    The Muse Android app fails when the negotiated ATT MTU is above 515, and
    Home Assistant OS's BlueZ negotiates 517. The SDK only logs it, so watch
    the log: consent, then a disconnect with no further request.
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


def main():
    from musegadget.pair import pair

    logging.getLogger("musegadget").addHandler(WatchSetup())

    def opened(window):
        say(event="open", name=window.ble_name, timeout=window.timeout_s, token=window.has_sdk_token)

    try:
        result = pair(on_open=opened)
    except Exception as exc:  # no adapter, no D-Bus, Bluetooth refused ...
        say(event="done", outcome="failed", problem="%s: %s" % (type(exc).__name__, exc))
        return 1
    say(event="done", outcome=result.outcome.value, problem=result.sdk_token_problem)
    return 0


if __name__ == "__main__":
    sys.exit(main())
