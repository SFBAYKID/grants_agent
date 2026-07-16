---
name: macos-archive-safety
description: Fail-closed file-list handling for surgical deploys from the macOS laptop
metadata:
  type: project
---

The laptop's system Bash is 3.2 and does not provide `mapfile`. Do not use a Bash `mapfile` pipeline
to assemble the changed-file list for `git archive`. On 2026-07-15 that helper failed and an empty
path array caused `git archive <target> --` to emit the complete tracked tree. The full tracked tree
was still tenant-scoped and secret-safe, and all 108 remote files were hash-verified against the
target before restart, but it exceeded the intended surgical delta.

Use zsh's newline-array expansion from the repository working tree, and fail closed on an empty list:

```zsh
files=("${(@f)$(git diff --name-only "$deployed..$target")}")
(( ${#files[@]} > 0 )) || { print -u2 "empty deploy delta"; return 1 }
git archive --format=tar "$target" -- "${files[@]}" |
  ssh -i ~/.ssh/grants_droplet -o IdentitiesOnly=yes \
    "$GRANTS_DROPLET_USER@$GRANTS_DROPLET_HOST" \
    'tar -xf - -C "$HOME/grants_agent"'
```

Before restart, verify every intended remote file using the target commit's blob hash. For a
full-tree copy, verify every tracked blob and executable mode. Never infer success from `tar` alone.
