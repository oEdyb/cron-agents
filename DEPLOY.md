# Deploy

This setup keeps the public checkout separate from private state, model credentials, and generated briefings. It uses systemd timers for scheduling and can sync only the output folder to another machine.

## Deployment contract

- Audit the host before changing it. Check its identity, resources, running services, firewall, and existing copies of the app.
- Run the app as a dedicated non-login user.
- Keep `/opt/cron-agents` root-owned and read-only to the runtime user.
- Keep config, credentials, SQLite state, locks, and output below mode-`0700` `/var/lib/cron-agents`.
- Install model CLIs outside the Git checkout. Record their versions and use absolute paths.
- Give every job the same `flock` file so two model processes cannot overlap.
- Sync the briefing directory, not an entire notes vault.
- Prove a systemd service run and one automatic timer run before calling the deployment complete.

If SSH or administrative access is unstable, stop. Do not reboot, reset passwords, or use provider rescue mode as an unplanned workaround.

## 1. Inspect the host

Run read-only checks first:

```bash
hostnamectl
uname -a
timedatectl
free -h
df -h /
systemctl --failed
systemctl list-timers --all
command -v python3 git flock npm
```

Look for root-run services that could read colocated credentials. A dedicated user prevents accidental environment and permission leaks. It cannot protect credentials from a compromised root process.

On Ubuntu 24.04, install and verify the base packages:

```bash
sudo apt update
sudo apt install --yes \
  ca-certificates \
  git \
  python3.12 \
  python3.12-venv \
  util-linux

python3.12 --version
python3.12 -m venv --help >/dev/null
flock --version
```

## 2. Install the app

The commands below target Ubuntu and the public repository. Adjust package installation for another distribution.

```bash
sudo useradd \
  --system \
  --home /var/lib/cron-agents \
  --create-home \
  --shell /usr/sbin/nologin \
  cronagents

sudo chmod 0700 /var/lib/cron-agents
sudo install -d -o cronagents -g cronagents -m 0700 \
  /var/lib/cron-agents/data \
  /var/lib/cron-agents/inbox

sudo git clone https://github.com/oEdyb/cron-agents.git /opt/cron-agents
sudo python3.12 -m venv /opt/cron-agents/.venv
sudo /opt/cron-agents/.venv/bin/pip install /opt/cron-agents

sudo install -o cronagents -g cronagents -m 0600 \
  /opt/cron-agents/config.example.yaml \
  /var/lib/cron-agents/config.yaml
```

Install Codex or Kimi outside `/opt/cron-agents`. Installing a CLI inside the checkout makes deployment audits report a dirty tree.

Install Node.js 22.19 or newer from the official Node.js distribution before using the pinned npm installs below. Kimi requires that version. Verify the Node version before installing either CLI:

```bash
node --version
npm --version
node -e 'const [a,b]=process.versions.node.split(".").map(Number);process.exit(a>22||(a===22&&b>=19)?0:1)'
```

Choose one model CLI. These versions passed the repository's real model runs on 2026-08-01:

```bash
sudo install -d -o root -g root -m 0755 /opt/cron-agents-tools
sudo npm install --prefix /opt/cron-agents-tools "@openai/codex@0.146.0"
# Or install Kimi:
sudo npm install --prefix /opt/cron-agents-tools "@moonshot-ai/kimi-code@0.29.1"
```

Edit `/var/lib/cron-agents/config.yaml` and make these paths absolute:

| Setting | Value |
| --- | --- |
| `state_dir` | `/var/lib/cron-agents/data` |
| Codex command executable | `/opt/cron-agents-tools/node_modules/.bin/codex` |
| Kimi command executable | `/opt/cron-agents-tools/node_modules/.bin/kimi` |
| Kimi `--agent-file` argument | `/opt/cron-agents/agents/text.md` |
| Curator prompt | `/opt/cron-agents/prompts/curator.md` |
| Writer prompt | `/opt/cron-agents/prompts/writer.md` |
| Briefing `output_dir` | `/var/lib/cron-agents/inbox` |

Apply only the rows for your selected model. For Kimi, set both `agents.curator.model` and `agents.writer.model` to `kimi`.

Keep the model environment list narrow. Codex needs `PATH` and `HOME`. Kimi also needs `KIMI_CODE_EXPERIMENTAL_FLAG` because the tool-free agent file uses its v2 engine. Leave `CODEX_HOME` and `KIMI_CODE_HOME` unset on a dedicated user; both CLIs already store credentials below that user's private home.

## 3. Authenticate the model CLI

Authenticate as `cronagents`, not as root:

```bash
sudo install -d -o cronagents -g cronagents -m 0700 \
  /var/lib/cron-agents/.codex

sudo -u cronagents env \
  HOME=/var/lib/cron-agents \
  /opt/cron-agents-tools/node_modules/.bin/codex login --device-auth

sudo -u cronagents env \
  HOME=/var/lib/cron-agents \
  /opt/cron-agents-tools/node_modules/.bin/codex login status
```

OpenAI also documents copying `~/.codex/auth.json` to a headless host. Treat that file as a password. Transfer it over an encrypted connection, install it with mode `0600`, remove any staging copy, and never print it to a terminal or log.

Kimi uses its own device-code login:

```bash
sudo install -d -o cronagents -g cronagents -m 0700 \
  /var/lib/cron-agents/.kimi-code

sudo -u cronagents env \
  HOME=/var/lib/cron-agents \
  /opt/cron-agents-tools/node_modules/.bin/kimi login

sudo -u cronagents env \
  HOME=/var/lib/cron-agents \
  /opt/cron-agents-tools/node_modules/.bin/kimi doctor
```

Kimi `0.29.1` has no login-status command. The model-backed briefing run in the next section proves that the saved login works.

## 4. Run before scheduling

Use the same identity, paths, and environment that systemd will use:

```bash
for CRON_JOB in rss hn briefing; do
  sudo -u cronagents env \
    HOME=/var/lib/cron-agents \
    PATH=/opt/cron-agents/.venv/bin:/opt/cron-agents-tools/node_modules/.bin:/usr/local/bin:/usr/bin:/bin \
    /opt/cron-agents/.venv/bin/cron-agents \
    --config /var/lib/cron-agents/config.yaml run "$CRON_JOB" || break
done
```

The command above uses Codex. Kimi deployments add `KIMI_CODE_EXPERIMENTAL_FLAG=1` to the `env` arguments.

Check that the briefing exists, contains the selected source IDs, and lists a link for every cited source. A retry for the same UTC date must reuse `data/selections/YYYY-MM-DD.json`.

Choose the ledger policy before scheduling. Stop the old writer and copy its final state once, or start with a documented fresh ledger. Running two production writers with separate state can reuse the same source.

## 5. Add systemd scheduling

Create `/etc/systemd/system/cron-agents@.service`:

```ini
[Unit]
Description=Cron Agents job: %i
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
User=cronagents
Group=cronagents
WorkingDirectory=/opt/cron-agents
Environment="HOME=/var/lib/cron-agents"
Environment="PATH=/opt/cron-agents/.venv/bin:/opt/cron-agents-tools/node_modules/.bin:/usr/local/bin:/usr/bin:/bin"
ExecStart=/usr/bin/flock /var/lib/cron-agents/run.lock /opt/cron-agents/.venv/bin/cron-agents --config /var/lib/cron-agents/config.yaml run %i
TimeoutStartSec=25min
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
ReadWritePaths=/var/lib/cron-agents
```

The unit above uses Codex. Kimi deployments add one line: `Environment="KIMI_CODE_EXPERIMENTAL_FLAG=1"`.

The service timeout must exceed the curator timeout plus the writer timeout and leave time for validation and disk writes. The example config allows 600 seconds for each model stage, so the unit allows 25 minutes.

Create one timer per job with this shape:

```ini
[Unit]
Description=Run JOB on schedule

[Timer]
OnCalendar=CALENDAR
Persistent=true
AccuracySec=1min
Unit=cron-agents@JOB.service

[Install]
WantedBy=timers.target
```

Use these values or choose your own:

| Timer file | `JOB` | `CALENDAR` |
| --- | --- | --- |
| `cron-agents-hn.timer` | `hn` | `*-*-* 00/3:00:00` |
| `cron-agents-rss.timer` | `rss` | `*-*-* 00/6:30:00` |
| `cron-agents-briefing.timer` | `briefing` | `*-*-* 21:00:00 Etc/UTC` |

The first two expressions use the server's timezone. The briefing expression names its timezone. Replace `Etc/UTC` with your IANA timezone when you want a local delivery hour. Do not assume that `CRON_TZ` works in the cron implementation on the host.

Test the service boundary before enabling timers:

```bash
sudo systemctl daemon-reload
sudo systemctl start cron-agents@rss.service
systemctl show cron-agents@rss.service -p Result -p ExecMainStatus
journalctl -u cron-agents@rss.service --since today

sudo systemctl enable --now \
  cron-agents-hn.timer \
  cron-agents-rss.timer \
  cron-agents-briefing.timer

systemctl list-timers 'cron-agents-*'
```

After the first scheduled fire, check `LastTriggerUSec`, `Result=success`, and `ExecMainStatus=0`. A successful manual command does not prove the timer environment.

## 6. Sync one output folder

If the briefing belongs in an Obsidian vault on another computer, share only `/var/lib/cron-agents/inbox`.

One option is a dedicated Syncthing identity:

```bash
sudo apt install syncthing
sudo install -d -o cronagents -g cronagents -m 0700 \
  /var/lib/cron-agents/syncthing
sudo -u cronagents syncthing generate \
  --home=/var/lib/cron-agents/syncthing
sudo -u cronagents syncthing device-id \
  --home=/var/lib/cron-agents/syncthing
```

Create `/etc/systemd/system/cron-agents-sync.service`:

```ini
[Unit]
Description=Cron Agents output sync
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=cronagents
Group=cronagents
ExecStart=/usr/bin/syncthing serve --no-browser --no-restart --no-upgrade --home=/var/lib/cron-agents/syncthing
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
CapabilityBoundingSet=
RestrictNamespaces=true
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
ReadWritePaths=/var/lib/cron-agents/inbox /var/lib/cron-agents/syncthing
InaccessiblePaths=-/var/lib/cron-agents/.codex
InaccessiblePaths=-/var/lib/cron-agents/.kimi-code
InaccessiblePaths=-/var/lib/cron-agents/config.yaml
InaccessiblePaths=-/var/lib/cron-agents/data
InaccessiblePaths=-/var/lib/cron-agents/run.lock

[Install]
WantedBy=multi-user.target
```

Start the service, then pair the devices and folder:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now cron-agents-sync.service
systemctl is-active cron-agents-sync.service
```

Keep its web interface on loopback. Pair it with the other computer using the running Syncthing GUI or `syncthing cli`, then use one folder ID for these paths:

```text
server: /var/lib/cron-agents/inbox
client: <vault>/<your inbox folder>/Cron Agents
```

Set the server folder to **Send Only** and the client folder to **Receive Only**. This prevents edits in the notes app from changing the server's generated output.

On macOS, `brew services start syncthing` starts the client at login. Syncthing transfers changes when both devices are online, so a sleeping Mac catches up later. Compare a test file's SHA-256 on both machines before relying on the share.

Obsidian Headless Sync is another option when you want a vault on the server and have an Obsidian Sync subscription. Obsidian warns against running its desktop and headless sync clients on the same device. Do not put a full vault on a shared VPS when the job needs only one output folder.

## Common failures

| Symptom | Check |
| --- | --- |
| Login works as root but the job fails | Authenticate with the runtime user's `HOME`. |
| Manual run works but systemd fails | Compare `HOME`, `PATH`, model-home variables, absolute paths, and file ownership. |
| `runuser` disappears after narrowing `PATH` | Use `/usr/sbin/runuser`, or run the command through systemd as `User=cronagents`. |
| The Git checkout becomes dirty | Move model CLI files and writable state outside `/opt/cron-agents`. |
| The briefing runs at the wrong hour | Name the timezone in `OnCalendar` and inspect `systemctl list-timers`. |
| A writer retry chooses new sources | Restore the saved selection and confirm every run uses the same state directory. |
| Two briefings reuse a source | Check for two writers, two databases, or a deleted selection file. |
| Sync says connected but no note appears | Check the folder ID, exact paths, permissions, ignore rules, and hashes. |
| The full vault appears on the VPS | Stop sync and correct the shared folder root before continuing. |

## Agent completion checks

An agent should report the deployment complete only after it has evidence for each item:

- The local, public, and server commit SHAs match.
- The server checkout is clean.
- Model login succeeds as the runtime user without printing credentials.
- Collectors and one curator-to-writer briefing pass as the runtime user.
- The systemd service passes with its production environment.
- All timers are active and enabled.
- At least one timer has fired from its schedule with exit status `0`.
- The saved selection contains unique IDs and every selected source is marked published.
- The server and client briefing hashes match.
- Only the intended output folder is shared.
- Syncthing reports Send Only on the server and Receive Only on the client.
- No secret, private config, state database, or generated briefing is tracked by Git.

## References

- [Codex authentication](https://learn.chatgpt.com/docs/auth)
- [Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode)
- [Kimi installation](https://moonshotai.github.io/kimi-code/en/guides/getting-started.html)
- [Kimi command and device login](https://moonshotai.github.io/kimi-code/en/reference/kimi-command)
- [Node.js downloads](https://nodejs.org/en/download)
- [systemd timers](https://www.freedesktop.org/software/systemd/man/latest/systemd.timer.html) and [calendar events](https://www.freedesktop.org/software/systemd/man/latest/systemd.time.html)
- [Syncthing command-line operation](https://docs.syncthing.net/users/syncthing.html)
- [Syncthing folder types](https://docs.syncthing.net/users/foldertypes.html)
- [Obsidian Headless Sync](https://obsidian.md/help/sync/headless)
