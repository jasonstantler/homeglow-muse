# Muse for HomeGlow

Talk or type to your [Muse](https://muse.ai) from a [HomeGlow](https://github.com/jherforth/HomeGlow) wall display. Tap the orb and speak, or type, and Muse's answer shows on the card and is read out loud.

This repository has two parts:

| Part | Where it goes |
|---|---|
| **Muse Link** (`muse_link`) | A Home Assistant add-on. It keeps the connection to your Muse open and passes requests from the wall to Muse. |
| **The Muse card** (`homeglow/muse-wall.html`) | A HomeGlow plugin. The orb, the text box, quick buttons and Muse's answers. |

## Install

1. **Add this repository to Home Assistant.**

   [![Add this repository to Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fjasonstantler%2Fhomeglow-muse)

   Or go to **Settings → Add-ons → Add-on Store**, open the **⋮** menu, choose **Repositories**, and paste this page's address.
2. **Install Muse Link** from the Add-on Store, then follow its **Documentation** tab: add your Muse SDK token, start it, and pair it with the Muse app from the Muse Link page.

   With an **Android phone**, pair another Linux machine first, then move that pairing to Muse Link. The Muse Android app can't pair over Home Assistant OS's Bluetooth. The add-on's Documentation tab shows how.
3. **Add the card to HomeGlow.** Download [`homeglow/muse-wall.html`](homeglow/muse-wall.html), upload it in HomeGlow under **Admin → Plugins**, and paste the bridge address and wall key from the Muse Link page into its Plugin Options.

You need a Muse account, a Muse SDK token from [gadgets.muse.ai](https://gadgets.muse.ai/settings/sdk-tokens), and Bluetooth on the Home Assistant machine for the one-time pairing (built into a Raspberry Pi; otherwise a USB Bluetooth adapter).

## Without Home Assistant OS

If you don't run Home Assistant with add-ons, the `bridge` folder runs the same thing on any Debian or Raspberry Pi OS machine. See [bridge/README.md](bridge/README.md).

## Safety

Muse can't run commands or change files on the machine Muse Link runs on. Muse Link refuses everything except Muse's health check. The wall key keeps other devices on your network from using it, so keep the key private.

## Credits

Built on Meta's [Muse Gadgets SDK](https://github.com/facebookincubator/muse-gadget-sdk) (Apache 2.0). Not made by or affiliated with Meta.
