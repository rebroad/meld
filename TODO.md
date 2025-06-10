# Follow-up ideas from the downstream branch

The rebase of `1ecd0607` keeps upstream's GTK4 implementation and drops the
old logging/debugging code. The following ideas may still be worth revisiting,
but should be implemented against the current APIs rather than restored from
that commit:

- Add targeted, opt-in timing diagnostics for slow directory refreshes,
  comparison loads, and page operations. Avoid unconditional per-widget signal
  logging and duplicate `debug_print` helpers.
- Improve the user experience for genuinely large files: warn before loading,
  show cancellable progress, and make Stop cancel active loads. The queued
  `8acb5673` implementation uses old GTK3 `MsgArea` APIs and needs a GTK4
  version.
- Consider whether special-file handling should cover more than the upstream
  `Gio.FileType.SPECIAL` check. Hidden files, backups, and symlinks are not
  inherently special, so any expansion needs a precise safety/use-case first.
- Evaluate whether minimized-window state should be persisted. The discarded
  implementation uses obsolete GTK3 window-state APIs; do not reintroduce it
  without a GTK4-supported design.

The directory-comparison traversal guard retained during this rebase is the
small, API-independent part of the large-tree optimization. It should gain a
focused regression/performance test if it remains important.
