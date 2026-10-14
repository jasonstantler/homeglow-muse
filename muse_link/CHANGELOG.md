# Changelog

## 1.1.0

- **Move a pairing here:** take over the pairing of another Linux machine that runs Muse, such as the bridge on a Raspberry Pi or a virtual machine. Run `move-pairing.sh` there and paste the code it prints into the Muse Link page.
- The Muse Android app can't pair over Home Assistant OS's Bluetooth. Muse Link now notices when the app hangs up right after you tap Continue, and points you to moving a pairing instead.
- The Muse Link page shows which device Muse Link is in your Muse app.

## 1.0.0

- First release: pairs with the Muse app from a page in Home Assistant's sidebar, and connects the HomeGlow Muse card to your Muse.
- Muse can't run commands or change files on the Home Assistant machine.
