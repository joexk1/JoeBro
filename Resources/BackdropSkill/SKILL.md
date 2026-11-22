---
name: joebro-backdrop
description: Build an animated backdrop for the JoeBro macOS app. Use when the user asks for a JoeBro background, wallpaper, animated backdrop or ASCII scene.
---

# JoeBro backdrop builder

A backdrop is one self-contained HTML file that JoeBro renders full-window behind its panels, non-interactive, in a WKWebView.

## Install path

Write the file to `~/Library/Application Support/JoeBro/Backdrops/<name>.html` (create the folder if missing). JoeBro lists it in Settings > Appearance > Backdrop > Animated. Start from `template.html` next to this file.

## Contract

The page must define:

```js
window.jbBackdrop = {
  pause()  { /* stop the animation loop */ },
  resume() { /* restart it */ },
  setScheme(s) { /* 's' is 'dark' or 'light'; adapt colours */ }
};
```

JoeBro calls `setScheme` once after load and whenever the system appearance changes, and calls `pause`/`resume` when the window is hidden, the app is inactive, or Reduce Motion is on. The page starts running on its own; `pause` must really stop work (cancel `requestAnimationFrame`).

## Rules

- Single file. Inline all CSS and JS. No network, no external fonts, scripts, images or `fetch`. Data URIs are fine.
- Canvas 2D or ASCII text rendered to a canvas. Fill the viewport (`100vw x 100vh`), resize on `resize`.
- Low CPU: cap at 30 fps, honour `devicePixelRatio` (cap it at 2), avoid allocations per frame.
- The page sits behind translucent panels, so keep it calm: low contrast, slow motion, no flashing, no text that must be read.
- Transparent or flat background is fine; the app draws no ground behind it, so paint your own (dark: near black, light: warm paper).
- Respect `prefers-reduced-motion`: render one still frame.
- Never touch `window.top`, cookies or storage.

## Steps

1. Copy `template.html` to the install path under a short kebab-case name.
2. Replace the `draw(t)` function with the scene. Keep the loop, `pause`, `resume` and `setScheme`.
3. Open the file in a browser to check it renders and idles near 0% CPU when paused.
4. Tell the user to pick it in JoeBro Settings > Appearance.
