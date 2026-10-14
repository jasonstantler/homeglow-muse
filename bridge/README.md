# Muse bridge without Home Assistant OS

The same bridge as the Muse Link add-on, for a Debian, Ubuntu or Raspberry Pi OS machine that stays on (a Pi, a mini PC, a virtual machine). It needs Bluetooth once, for pairing.

## 1. Make an account for Muse

Meta's kit runs commands as an account you pick. Give it one with no admin rights:

```bash
sudo adduser --disabled-password --gecos "" musewall
```

## 2. Install Meta's kit and pair it

In the Muse app, turn on **Settings → Devices → Developer mode**. Then, with your SDK token from [gadgets.muse.ai](https://gadgets.muse.ai/settings/sdk-tokens):

```bash
curl -fsSL https://raw.githubusercontent.com/facebookincubator/muse-gadget-sdk/main/linux/install.sh -o install.sh
bash install.sh --run-as musewall --sdk-token mgst_YOUR-TOKEN
history -c
```

When it says pairing is open, add the `MuseGadget…` device in the Muse app (**Settings → Devices → Add Device**), tap **Continue**, and pick the Wi-Fi network it shows.

## 3. Install the bridge

Copy this `bridge` folder to the machine, then:

```bash
cd bridge && sudo bash install-bridge.sh
```

It prints the **bridge address** and **wall key** to paste into the HomeGlow card's Plugin Options. Check it from another computer: `http://MACHINE-ADDRESS:8791/status` should say **Wrong wall key**.

## Moving to the Home Assistant add-on

To run Muse from Home Assistant instead, move this machine's pairing to the Muse Link add-on rather than pairing again:

```bash
curl -fsSL https://raw.githubusercontent.com/jasonstantler/homeglow-muse/main/bridge/move-pairing.sh | sudo bash
```

It turns Muse off here and prints a code. Paste it into **Move a pairing here from another machine** on the Muse Link page. Then point the HomeGlow card at the address and key Muse Link shows. Run `clear` afterwards: the code works like a password for your Muse.

## Managing it

```bash
sudo bash install-bridge.sh --show-key    # the address and key again
sudo bash install-bridge.sh --new-key     # a new key (paste it into the card)
sudo bash install-bridge.sh --uninstall   # back to Meta's plain gadget
sudo journalctl -u musegadget -n 50       # the log
```

Muse can't run commands or change files on this machine: the bridge refuses everything but Muse's health check. Answers are written; the card reads them out with the browser's voice.
