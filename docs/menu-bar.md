# The menu-bar app

`sellbird tray` puts the Sellbird bird in the macOS menu bar (or the Windows
tray). It shows whether Sellbird is running, and lets you start, stop or
pause it and open the dashboard. **Start at login** in its menu starts it when
you log in.

## What's waiting on you

When replies need you or emails wait for your approval, a small bubble next to
the bird shows how many, in the state's color (green running, amber paused).
The menu's top row says which: "2 replies need you · 8 emails to approve".

## Notifications

A new reply pops up a notification naming who wrote and quoting them, or
"said yes to your offer" when they accepted it. Clicking it opens that
conversation in the dashboard. Only replies that arrive while the menu-bar
app runs notify, and five or more at once become one summary.

**On a Mac**, notifications come from a tiny app named Sellbird, with the bird
as its icon. macOS shows a notification with the icon of the app that sent it,
and only lets a real app send one. The menu-bar app sets this up the first
time it runs, in `~/Library/Application Support/sellbird/Sellbird.app`, and
macOS asks once whether Sellbird may send notifications. It uses the
prebuilt `sellbird/desktop/notifier/sellbird-notify` (Apple silicon and Intel),
or builds one with `swiftc` when that's missing. Without either, notifications
still work but show Script Editor's icon.

**On Windows**, notifications come from the tray icon itself, so they show the
bird. Windows may name the sender "Python".

## Changing the notifier

After editing `sellbird/desktop/notifier/main.swift`, rebuild the shipped
binary on a Mac and commit both files:

```bash
cd sellbird/desktop/notifier
swiftc -O -target arm64-apple-macos12 -o /tmp/arm main.swift
swiftc -O -target x86_64-apple-macos12 -o /tmp/x86 main.swift
lipo -create /tmp/arm /tmp/x86 -output sellbird-notify && strip -x sellbird-notify
shasum -a 256 main.swift | cut -d' ' -f1 > sellbird-notify.source-sha256
```

The test suite and the `macOS notifier` workflow fail if the binary is stale.
