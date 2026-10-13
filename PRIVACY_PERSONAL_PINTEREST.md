# Joebro Personal Pinterest Integration — Privacy

Effective date: 13 October 2026

Joebro Personal is a self-hosted personal assistant operated by Joe Robinson. This page covers its optional Pinterest integration for the account owner's own use. It is not a hosted service offered to other Pinterest users.

## Access and purpose

The account owner connects Pinterest using Pinterest's OAuth authorization screen. The integration requests read access to the account profile, boards and Pins (`user_accounts:read`, `boards:read`, and `pins:read`). It does not request advertising, posting, board-writing or Pin-writing permissions.

The integration reads available account and Pin analytics, including impressions, saves and clicks, to produce account-health reports. When explicitly requested, it also reads owned Pin metadata and downloads selected Pin images into the owner's shared file store. The owner may request reuse of a downloaded image as a Spotify playlist cover.

## Storage

OAuth app credentials and tokens are stored in restricted-permission files on the owner's Mac and self-hosted Raspberry Pi. Daily analytics monitoring retains up to 90 snapshots on the Pi. Exported images and assistant conversations remain in the owner's storage until removed by the owner; they are not automatically deleted after 90 days.

## Data flows

API requests and token refreshes go to Pinterest over HTTPS. Selected images may be uploaded to Spotify when the owner asks for a playlist cover. Pinterest tool results may be sent to the owner's configured AI model provider to answer the owner's questions; those providers' own privacy and retention terms apply. Daily analytics collection itself makes no AI model calls.

The integration does not publish account analytics, sell Pinterest data, use it for advertising, or train a model on it. Its source and configuration are under the account owner's control.

## Control and contact

The owner can revoke the app's Pinterest access in Pinterest's connected-app settings, disable the Joebro integration and daily monitor, and remove locally stored credentials, snapshots, images or conversations. Revoking access prevents new Pinterest API reads but does not itself remove existing local files.

Questions about this personal integration can be raised through the [Joebro project](https://github.com/joexk1/JoeBro).
