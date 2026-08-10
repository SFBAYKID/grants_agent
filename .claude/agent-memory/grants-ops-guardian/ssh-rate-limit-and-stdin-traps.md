---
name: ssh-rate-limit-and-stdin-traps
description: Two transport traps hit during the d664548 deploy — `ssh -n` silently uploads an EMPTY file, and a burst of separate SSH sessions gets port 22 REJECTED; multiplex onto one connection
metadata:
  type: project
---

## 1. `ssh -n host 'cat > file' < local` writes an EMPTY file, and reports success

`-n` redirects ssh's stdin from `/dev/null`, and it **wins over an explicit `< file`
redirect** on the same command. The remote `cat` then reads EOF immediately, creates a
0-byte file, and exits 0. Nothing anywhere says the transfer was empty.

This is the mirror image of the already-recorded "ssh eats the heredoc" gotcha in
[[deploy-mechanism]] (where the fix was to ADD `-n`). The rule that covers both:

> `-n` on every ssh that does **not** need stdin; **never** on the ssh that *is* the
> consumer of a pipe, heredoc, or `< file`.

**Re-bit 2026-08-09 during the a718066 deploy, in its third guise:** `ssh -n host
'bash -s' <<REMOTE` — a *heredoc* rather than a `< file`. `-n` won again, `bash -s`
read EOF, and the whole verification block (remote size + sha of the just-uploaded
archive) printed NOTHING and exited 0. Silence from a verification step is not a pass.
The tell is generic: **if a step produces no output at all, re-run it standalone
before trusting anything downstream** — which is what confirmed the upload was in fact
byte-exact. A heredoc is a stdin consumer exactly like `< file`; the rule has no
carve-out for it.

Cost here: three uploaded manifests were silently 0 bytes. It was caught only because the
next step hashed them. **Always `wc -c` the remote file after an stdin upload** — the
pre-image/target hash manifests are exactly the files whose emptiness would turn a
"verified" deploy into an unchecked one.

## 1b. Hand-escaped quoting inside an `ssh host "…"` string produces FALSE readings

2026-08-09 (cadfefe deploy): a cleanup command embedded `pgrep -cf \x27grant_watch[.]slack[.]grant\x27`
inside a double-quoted ssh argument. `\x27` is not a shell escape — the remote shell received a
corrupted pattern, and the check printed **`PID_COUNT=0`** on a bot that was alive and healthy, plus
`cut: the delimiter must be a single character` for the sha lines. A momentary "the bot is dead"
scare, caused entirely by the transport.

**Rule: any remote command containing quotes goes in a FILE and is run with `ssh host 'bash -s' <
script.sh`.** Reserve inline `ssh host '…'` for single-quoted commands with no nested quoting. And
when a check reports a zero, print the pattern it used alongside it (`pgrep -af` rather than bare
`pgrep -cf`) so the zero can be judged rather than believed — the same "never trust a zero from a
pattern you have not seen match" rule as the rsync itemize direction in [[deploy-mechanism]].

## 2. A burst of separate SSH sessions gets port 22 REJECTED (not timed out)

After ~6 short-lived connections in ~5 minutes the droplet began answering with
`Connection refused` — a TCP RST, intermittent, then clearing on its own. Diagnosis that
distinguishes it from a dead host:

- `nc -z host 22` alternates refused / succeeded within seconds (a down sshd is refused
  *every* time; a rebooting host is usually a timeout, not a refusal).
- `uptime` afterwards showed **14 h** — no reboot had happened.
- ICMP is filtered on this droplet, so `ping` failing proves nothing. Do not read it as
  "host is down".

This is a connection-**rate** limiter (ufw's `limit`-style REJECT), not fail2ban: every one
of our authentications had succeeded, and fail2ban keys on auth failures.

**The remedy, and it is also just better manners on a shared box:** multiplex. One master,
every later command riding it, same key and same tenant:

```bash
CS=/tmp/claude-501/gwcm.sock          # MUST be short - see below
ssh -i ~/.ssh/grants_droplet -o IdentitiesOnly=yes \
    -o ControlMaster=yes -o ControlPath="$CS" -o ControlPersist=20m \
    -f -N "$GRANTS_DROPLET_USER@$GRANTS_DROPLET_HOST"
# then every command adds only:  -o ControlPath="$CS"
ssh -o ControlPath="$CS" -O check "$GRANTS_DROPLET_USER@$GRANTS_DROPLET_HOST"
ssh -o ControlPath="$CS" -O exit  "$GRANTS_DROPLET_USER@$GRANTS_DROPLET_HOST"   # when done
```

This stays inside the canonical grants-only door: same `-i ~/.ssh/grants_droplet`, same
`IdentitiesOnly=yes`, same tenant, no `~/.ssh/config` alias, and the socket is local to the
laptop so nothing on the droplet or in any global config changes.

**`ControlPath` has a ~104-byte limit.** The session scratchpad path
(`/private/tmp/claude-501/-Users-…/<uuid>/scratchpad/…`) is far too long and fails with
`unix_listener: path … too long for Unix domain socket`. Use a short path such as
`/tmp/claude-501/gwcm.sock` and delete it at the end.

**Also:** launching a droplet-side job with `nohup … &` inside an ssh command still holds
the channel open until the child's inherited stdout closes, so the ssh hangs until the
client times out even though the job started fine. Redirect the child's stdout/stderr to a
file **and** re-check with a separate short command rather than trusting the hung session;
the job itself is unaffected.
