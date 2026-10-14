"""Open Bluetooth setup once, for the Muse app to pair this add-on.

Runs as its own process because the SDK's Bluetooth code takes over signal
handling while it runs. Prints one JSON line when setup opens and one when it
ends; everything else goes to the log.
"""

import json
import logging
import sys

logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def say(**event):
    print(json.dumps(event), flush=True)


def main():
    from musegadget.pair import pair

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
