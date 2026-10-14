# Muse Link

Talk or type to your Muse from a HomeGlow wall display. Muse's answers show on the HomeGlow card and can be read out loud.

Muse Link keeps a connection to your Muse open using Meta's Muse Gadgets SDK, and passes what you say on the wall to Muse and Muse's answers back. Everything sent from the wall goes to its own side chat in the Muse app.

## What you need

- A Muse account, and the **Muse app** on your phone.
- A **Muse SDK token**. Get one at [gadgets.muse.ai](https://gadgets.muse.ai/settings/sdk-tokens) (Account, then SDK tokens). It starts with `mgst_`.
- **Bluetooth on your Home Assistant machine, for pairing once.** A Raspberry Pi 3, 4 or 5 has it built in. Other machines, and Home Assistant running in a virtual machine, need a USB Bluetooth adapter.
- **With an Android phone, a second Linux machine for pairing once.** The Muse Android app can't pair over Home Assistant OS's Bluetooth (see [Android phones](#android-phones)). Pair any Debian, Ubuntu or Raspberry Pi OS machine with Bluetooth instead, then move that pairing to Muse Link.
- **HomeGlow**, with the Muse card (`muse-wall.html`) from this add-on's GitHub page.

## Set up

1. **Add your token.** Open the **Configuration** tab, paste your token into **Muse SDK token**, and click **Save**.
2. **Start the add-on** from the **Info** tab. Turning on **Show in sidebar** puts Muse Link in Home Assistant's sidebar.
3. **Pair.** Open **Muse Link** from the sidebar, or click **Open Web UI**.
   1. In the Muse app, turn on **Settings → Devices → Developer mode**.
   2. Click **Start pairing**. The page shows a device name like `MuseGadget1A2B3C`.
   3. In the Muse app, tap **Settings → Devices → Add Device** (the **+**). Pick that name, tap **Continue** on the community-device notice, and when it asks for Wi-Fi, pick the network it shows. No password is needed.
   4. The page changes to **Connected to your Muse**. This can take up to a minute.

   Pairing is a one-time step. After it, Muse Link reconnects on its own, including after restarts and updates.

   If the Muse app hangs up right after you tap **Continue**, you're likely using an Android phone. Move a pairing here instead (below).
4. **Connect the HomeGlow card.**
   1. In HomeGlow, go to **Admin → Plugins** and upload `muse-wall.html`.
   2. Add the **Muse** widget to your dashboard.
   3. Copy the **Bridge address** and **Wall key** from the Muse Link page into the widget's **Plugin Options**.

### Move a pairing from another machine

Use this with an Android phone, or if you already run Muse on another Linux machine, such as the [bridge](https://github.com/jasonstantler/homeglow-muse/tree/main/bridge) on a Raspberry Pi or virtual machine. Muse Link takes over that machine's pairing and becomes the same device in your Muse app.

1. **Pair the other machine, if it isn't already.** On any Debian, Ubuntu or Raspberry Pi OS machine with Bluetooth, run Meta's installer with your SDK token and pair it with the Muse app. Step 2 of the [bridge guide](https://github.com/jasonstantler/homeglow-muse/tree/main/bridge#2-install-metas-kit-and-pair-it) shows how. The installer sets Bluetooth up the way the Android app needs.
2. **Make a pairing code.** On that machine, run:

   ```bash
   curl -fsSL https://raw.githubusercontent.com/jasonstantler/homeglow-muse/main/bridge/move-pairing.sh | sudo bash
   ```

   It turns Muse off on that machine, because one pairing can only run in one place, and prints a code between `BEGIN MUSE PAIRING` and `END MUSE PAIRING`.
3. **Paste it into Muse Link.** Open **Muse Link** from the sidebar, open **Move a pairing here from another machine**, paste everything from `BEGIN` to `END`, and click **Move it here**. Muse Link restarts as the moved device and shows **Connected to your Muse** within a minute.
4. Run `clear` on the other machine to take the code off the screen. The code works like a password for your Muse, so don't share it or post screenshots of it.

You can then turn the other machine off, or uninstall Muse from it. To move the pairing back, stop Muse Link first, then run `sudo systemctl enable --now musegadget` on that machine.

### Android phones

The Muse Android app fails when Bluetooth sends packets larger than 512 bytes. Home Assistant OS's Bluetooth allows up to 517 bytes, and add-ons can't change that setting. With an Android phone, pairing gets as far as **Continue**, then the app hangs up. Muse Link notices this and points you to moving a pairing instead. Meta's installer turns this setting down on ordinary Linux machines, so pairing works there.

### Turn on the microphone (to talk instead of type)

Browsers only allow the microphone on secure addresses. If the HomeGlow screen opens HomeGlow at `http://localhost…`, it works as is. If it opens a number address such as `http://192.168.1.25:3000`:

1. In that screen's browser, open `chrome://flags/#unsafely-treat-insecure-origin-as-secure` (in Edge, `edge://flags/#unsafely-treat-insecure-origin-as-secure`).
2. Set it to **Enabled**, type HomeGlow's address exactly as it appears, and click **Relaunch**.
3. Tap the orb on the card and choose **Allow** when the browser asks.

## Using it

- **Talk:** tap the orb and speak. It sends when you pause.
- **Type:** use the text box, or **Type** on smaller tiles.
- **Quick buttons:** set up to six in the card's Plugin Options, as `Label = what to send`. End a label with `…` to have it fill the text box so you can finish the sentence.
- **Questions:** when Muse ends with a question, **Yes** and **No** buttons appear.
- **Purchases and other approvals** still happen in the Muse app.
- **Voice:** answers are read with the browser's voice. Edge's natural voices sound best. Pick one in the card's Plugin Options.

## Safety

- **Muse can't run commands or change files on your Home Assistant machine.** The SDK normally lets Muse run commands on the device it's installed on. Muse Link turns that off: it answers Muse's health check and refuses everything else.
- **The add-on can't see your Home Assistant setup.** It has no access to your configuration or your devices. It uses Home Assistant's D-Bus only to reach Bluetooth for pairing.
- **Keep the wall key private.** Anyone on your home network with it can send requests to your Muse, and whatever you've connected to Muse (email, calendar) can be read out on the wall. If the key gets out, click **Make a new key** and paste the new one into the card.
- **Home network only.** Don't forward the add-on's port on your router.

## Settings

| Setting | What it does |
|---|---|
| Muse SDK token | Your token from gadgets.muse.ai. Restart the add-on after changing it. |
| Network → 8791/tcp | The port the HomeGlow card connects to. Change it here if something else uses 8791; the Muse Link page shows the new address. |

## Troubleshooting

| What you see | What to do |
|---|---|
| **This Home Assistant machine has no Bluetooth** | Plug in a USB Bluetooth adapter, restart the add-on, and pair again. You only need it for pairing. |
| **The add-on couldn't reach Bluetooth** | Restart the add-on. If it keeps happening, turn off Home Assistant's own Bluetooth integration while you pair (Settings → Devices & services → Bluetooth → ⋮ → Disable), then turn it back on afterwards. |
| **Pairing closed before the Muse app finished** | Click **Try again**. Make sure Developer mode is on in the Muse app. |
| **Your phone hung up right after you tapped Continue** | This happens with Android phones on Home Assistant OS. [Move a pairing from another machine](#move-a-pairing-from-another-machine) instead. |
| **That isn't a whole pairing code** | Copy the code again, from the `-----BEGIN` line to the `-----END` line, and paste all of it. |
| **Paired, connecting** for more than a minute | Check the add-on's **Log** tab. If it says the pairing was removed, pair again. |
| The card says **Can't reach Muse Link** | Use your Home Assistant's address on your home network in the bridge address, and check the add-on is running. |
| The card says **Wrong wall key** | Copy the key again from the Muse Link page. |

## Removing it

To unpair, remove the device in the Muse app (**Settings → Devices**). To remove the add-on, uninstall it from the **Info** tab.

## Credits

Built on Meta's [Muse Gadgets SDK](https://github.com/facebookincubator/muse-gadget-sdk) (Apache 2.0), which the add-on downloads when it's installed. Not made by or affiliated with Meta.
