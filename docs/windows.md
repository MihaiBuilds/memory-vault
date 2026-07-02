# Memory Vault on Windows

Memory Vault runs on Windows via Docker Desktop. This page covers the two issues Windows users have hit most often: line-ending breakage on `scripts/start.sh`, and slow search latency when the repo lives on the Windows filesystem instead of inside WSL2.

## Line-ending error: `exec ./scripts/start.sh: no such file or directory`

If you see this error when starting the container (and the container exits with code 255), it's a line-ending issue, not a missing file. Windows git installs default to `core.autocrlf=true`, which can rewrite `scripts/start.sh` with `\r\n` line endings. Linux then reads the shebang as `#!/bin/sh\r` and tries to run an interpreter literally named `sh\r`.

Memory Vault ships with defensive `.gitattributes` rules and strips carriage returns inside the Docker image, so fresh clones should just work. If you still hit the error, run this **inside the repo** — don't change your global git config, since that can affect your other Windows projects:

```bash
cd memory-vault
git config core.autocrlf false
git rm --cached -r .
git reset --hard
docker compose build --no-cache
docker compose up -d
```

`--no-cache` matters because Docker caches the broken version in a build layer — rebuilding without it won't fix the issue.

## Performance: clone into WSL2, not a Windows path

Clone Memory Vault into your WSL2 filesystem (for example `~/memory-vault`), not a Windows path (`C:\Users\...`). Docker Desktop on Windows pays a significant I/O cost crossing between the Windows filesystem and the Linux VM, and search latency can drop from seconds to milliseconds when the repo lives inside WSL2.

If you're unsure where the repo lives, in WSL2 run `pwd` — paths starting with `/mnt/c/...` are on the Windows filesystem (slow). Paths starting with `/home/...` are inside the Linux VM (fast).
