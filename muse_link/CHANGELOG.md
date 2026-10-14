# Changelog

## 1.2.0

- **Pairing works with Android phones.** While the pairing window is open, Muse Link borrows the Bluetooth adapter and runs its own Bluetooth (Google's Bumble) with the packet size the Muse Android app needs. Home Assistant OS's own Bluetooth uses larger packets, which made the Android app hang up after Continue. The adapter goes back to Home Assistant as soon as pairing ends.
- The add-on now uses the host network and the network-admin permission, which borrowing the adapter needs. Only a small pairing helper keeps that permission; the rest of the add-on drops it (and raw-socket access) when it starts.
- The setup page only listens on Home Assistant's internal network.
- New **Wall card port** option, in place of the Network setting.

## 1.1.0

- **Move a pairing here:** take over the pairing of another Linux machine that runs Muse, such as the bridge on a Raspberry Pi or a virtual machine. Run `move-pairing.sh` there and paste the code it prints into the Muse Link page.
- The Muse Android app can't pair over Home Assistant OS's Bluetooth. Muse Link now notices when the app hangs up right after you tap Continue, and points you to moving a pairing instead.
- The Muse Link page shows which device Muse Link is in your Muse app.

## 1.0.0

- First release: pairs with the Muse app from a page in Home Assistant's sidebar, and connects the HomeGlow Muse card to your Muse.
- Muse can't run commands or change files on the Home Assistant machine.
