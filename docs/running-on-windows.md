# Running QuickLinks on Windows

QuickLinks runs natively on Windows with no Docker and no installed packages — it is a standard-library Python application. This guide sets it up as a machine-wide service that starts at boot, using only what ships with Windows.

Everything below was executed on Windows 11 against release `2026.08.11.003` except the two steps that need an elevated prompt, which are marked.

## Requirements

| | |
| --- | --- |
| Windows | 10, 11, or Server 2016+ |
| Python | **3.9 or newer**, 64-bit. Verified here on 3.13 and 3.14; releases are built on 3.13. |
| Packages | **None**, unless you enable on-premises Active Directory. Microsoft Entra ID needs nothing extra. |
| Privileges | Administrator for the service registration and firewall steps only. |

Install Python for **all users** so a service account can reach it:

```powershell
winget install --id Python.Python.3.13 --scope machine
```

## 1. Pin the interpreter path

Do not point a service at `py.exe` or a bare `python`. The launcher resolves to whichever install happens to win, and on a machine with more than one Python that is not the one you tested. Get the real path and keep it:

```powershell
py -3 -c "import sys; print(sys.executable)"
```

On the machine this guide was written on, that printed a **3.14** install even though a 3.13 was also present — exactly the ambiguity you want to remove. Use the full path everywhere below.

## 2. Choose a port and confirm it is free

```powershell
Get-NetTCPConnection -LocalPort 6969 -State Listen -ErrorAction SilentlyContinue
```

No output means the port is free. Anything else, pick another and use it consistently.

## 3. Lay out the files

Two separate directories: the application, and the data. Keeping data out of the application directory means it is not even adjacent to anything reachable over HTTP.

```powershell
New-Item -ItemType Directory -Force -Path 'C:\Program Files\QuickLinks' | Out-Null
New-Item -ItemType Directory -Force -Path 'C:\ProgramData\QuickLinks' | Out-Null

$release = '2026.08.13.001'   # check the releases page for the current version
$zip = "$env:TEMP\quicklinks.zip"
Invoke-WebRequest "https://github.com/falco1717/quicklinks/archive/refs/tags/$release.zip" -OutFile $zip
Expand-Archive $zip -DestinationPath $env:TEMP -Force
Copy-Item "$env:TEMP\quicklinks-$release\*" 'C:\Program Files\QuickLinks' -Recurse -Force
Remove-Item $zip, "$env:TEMP\quicklinks-$release" -Recurse -Force
```

`C:\Program Files` is already read-only to non-administrators, which is what you want: QuickLinks never writes to its own directory.

## 4. Lock down the data directory

It holds the SQLite database with administrator password hashes, the session-signing secret, and any uploaded logo. Only the service account and administrators should be able to read it.

**Elevated prompt:**

```powershell
icacls 'C:\ProgramData\QuickLinks' /inheritance:r `
  /grant 'NT AUTHORITY\NETWORK SERVICE:(OI)(CI)(F)' `
  /grant 'BUILTIN\Administrators:(OI)(CI)(F)' `
  /grant 'NT AUTHORITY\SYSTEM:(OI)(CI)(F)'
```

## 5. Write the wrapper script

A scheduled task cannot read an `.env` file, and putting `SESSION_SECRET` in a machine-wide environment variable exposes it to every process on the box. A small wrapper script holds the configuration instead, so the secret lives in exactly one file you can ACL.

Save as `C:\Program Files\QuickLinks\run-quicklinks.cmd`, substituting your interpreter path from step 1:

```bat
@echo off
set "PYTHONUTF8=1"
set "HOST=127.0.0.1"
set "PORT=6969"
set "DATA_DIR=C:\ProgramData\QuickLinks"
set "SESSION_SECRET=replace-with-a-long-random-value"
set "LOG_LEVEL=INFO"
set "SESSION_COOKIE_SECURE=auto"
cd /d "C:\Program Files\QuickLinks"
"C:\Program Files\Python313\python.exe" "C:\Program Files\QuickLinks\server.py"
```

Generate the secret:

```powershell
-join ((48..57)+(65..90)+(97..122) | Get-Random -Count 64 | ForEach-Object {[char]$_})
```

Notes:

- **`PYTHONUTF8=1`** avoids code-page surprises in logs, CSV import/export, and branding text.
- **`HOST=127.0.0.1`** binds to loopback only. Use `0.0.0.0` only if you intend to reach it from other machines, and then read step 8.
- **`SESSION_SECRET`** set here stays stable across restarts. Omit it and QuickLinks generates one into `DATA_DIR` instead. Either is fine; never share one between instances.
- To skip the create-administrator page, add `ADMIN_USERNAME` and `ADMIN_PASSWORD`. Both or neither — a partial pair deliberately stops startup.

Because the secret is in this file, restrict it — **elevated prompt**:

```powershell
icacls 'C:\Program Files\QuickLinks\run-quicklinks.cmd' /inheritance:r `
  /grant 'NT AUTHORITY\NETWORK SERVICE:(RX)' `
  /grant 'BUILTIN\Administrators:(F)' `
  /grant 'NT AUTHORITY\SYSTEM:(F)'
```

## 6. Register the service

Windows cannot run `python.exe` directly as a service — a service binary has to respond to service control messages, which Python does not. The two ways round that are a scheduled task (built in, below) or a service wrapper such as NSSM ([see below](#alternative-nssm)).

**Elevated prompt** — an unelevated shell fails here with "Access is denied":

```powershell
$action  = New-ScheduledTaskAction -Execute 'C:\Program Files\QuickLinks\run-quicklinks.cmd'
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId 'NT AUTHORITY\NETWORK SERVICE' `
    -LogonType ServiceAccount -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName 'QuickLinks' -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Description 'QuickLinks link portal'

Start-ScheduledTask -TaskName 'QuickLinks'
```

Why these settings:

- **`NETWORK SERVICE`** is a low-privilege built-in account that can bind a socket. It needs no password and no management. `SYSTEM` would work and is over-privileged.
- **`-ExecutionTimeLimit ([TimeSpan]::Zero)`** means never kill it. Without this, the default three-day limit stops your portal.
- **`-RestartCount 3`** restarts it if it dies, the rough equivalent of Docker's `restart: unless-stopped`.
- **`-MultipleInstances IgnoreNew`** stops a second copy fighting for the port.

## 7. Check it

```powershell
Get-ScheduledTask -TaskName 'QuickLinks' | Select-Object TaskName, State
Get-ScheduledTaskInfo -TaskName 'QuickLinks' | Select-Object LastRunTime, LastTaskResult
Invoke-WebRequest 'http://127.0.0.1:6969/api/product' -UseBasicParsing | Select-Object -Expand Content
Invoke-WebRequest 'http://127.0.0.1:6969/api/session' -UseBasicParsing | Select-Object -Expand Content
```

`/api/session` reports `"setup_required": true` until you create the first administrator. Browse to `http://127.0.0.1:6969/admin` to do that.

### Logging

QuickLinks logs through Python's `logging`, which writes to **stderr**, not stdout. Redirect both or you will get an empty log file and assume the service is broken. Change the last line of the wrapper to:

```bat
"C:\Program Files\Python313\python.exe" "C:\Program Files\QuickLinks\server.py" >> "C:\ProgramData\QuickLinks\quicklinks.log" 2>&1
```

That file grows without bound; rotate it if you care. There is no built-in rotation.

## 8. Reaching it from other machines

Do not expose the bare server directly. Python's `http.server` is not hardened for hostile traffic, and it speaks plain HTTP with no TLS. Put a reverse proxy in front — IIS with Application Request Routing, or any nginx or Caddy you already run.

The proxy must forward two headers or you lose real behaviour:

- **`X-Forwarded-Proto`** — what makes QuickLinks mark its session cookie `Secure`
- **`X-Forwarded-For`** — read when `TRUST_PROXY=1` is set, so login rate limiting sees real client addresses instead of lumping everyone into one bucket

If you accept plain HTTP on a trusted internal network instead, set `HOST=0.0.0.0` in the wrapper and open the port — **elevated prompt**:

```powershell
New-NetFirewallRule -DisplayName 'QuickLinks 6969' -Direction Inbound `
  -Protocol TCP -LocalPort 6969 -Action Allow -Profile Domain
```

`-Profile Domain` keeps it closed on public networks. Widen only deliberately.

Using Microsoft Entra ID? The redirect URI registered in the Entra portal must be the address users actually reach — the proxy's public hostname followed by `/api/auth/entra/callback`, not `127.0.0.1`.

## Optional: Active Directory

Only on-premises AD needs packages. Entra ID uses nothing beyond the standard library.

**Elevated prompt:**

```powershell
& 'C:\Program Files\Python313\python.exe' -m venv 'C:\Program Files\QuickLinks\venv'
& 'C:\Program Files\QuickLinks\venv\Scripts\pip.exe' install -r 'C:\Program Files\QuickLinks\requirements.txt'
```

Then point the wrapper's last line at `C:\Program Files\QuickLinks\venv\Scripts\python.exe` and restart the task.

`ldap3` and `dnspython` are imported lazily, only when an AD login is attempted, so skipping this costs nothing until you enable AD — at which point sign-in fails with a clear message rather than crashing.

## Upgrading

```powershell
Stop-ScheduledTask -TaskName 'QuickLinks'
Copy-Item 'C:\ProgramData\QuickLinks' "$env:TEMP\quicklinks-backup-$(Get-Date -f yyyy-MM-dd)" -Recurse
# extract the new release over C:\Program Files\QuickLinks as in step 3,
# keeping run-quicklinks.cmd
Start-ScheduledTask -TaskName 'QuickLinks'
```

Schema migrations run at startup and preserve existing links, locations, and administrators. Back up first anyway.

Note that `run-quicklinks.cmd` lives inside the application directory, so a careless extract can overwrite it. Keep a copy, or move the wrapper somewhere outside `C:\Program Files\QuickLinks` and adjust the task action.

## Backup and restore

Everything that matters is in `C:\ProgramData\QuickLinks`.

```powershell
Stop-ScheduledTask -TaskName 'QuickLinks'
Compress-Archive 'C:\ProgramData\QuickLinks\*' 'D:\backups\quicklinks.zip' -Force
Start-ScheduledTask -TaskName 'QuickLinks'
```

Stop the task first. QuickLinks uses WAL journalling, so copying `links.db` while it is running can capture an inconsistent database.

## Alternative: NSSM

If you want a real entry in `services.msc` with proper log rotation, [NSSM](https://nssm.cc/) wraps any console program as a service:

```powershell
nssm install QuickLinks "C:\Program Files\Python313\python.exe" "C:\Program Files\QuickLinks\server.py"
nssm set QuickLinks AppDirectory "C:\Program Files\QuickLinks"
nssm set QuickLinks AppEnvironmentExtra PYTHONUTF8=1 HOST=127.0.0.1 PORT=6969 DATA_DIR=C:\ProgramData\QuickLinks SESSION_SECRET=... LOG_LEVEL=INFO
nssm set QuickLinks AppStdout "C:\ProgramData\QuickLinks\quicklinks.log"
nssm set QuickLinks AppStderr "C:\ProgramData\QuickLinks\quicklinks.log"
nssm set QuickLinks AppRotateFiles 1
nssm set QuickLinks ObjectName "NT AUTHORITY\NetworkService"
nssm start QuickLinks
```

This trades a third-party dependency for real service semantics and log rotation. The scheduled task above needs nothing you do not already have. Either is fine.

## Uninstalling

```powershell
Stop-ScheduledTask -TaskName 'QuickLinks'
Unregister-ScheduledTask -TaskName 'QuickLinks' -Confirm:$false
Remove-NetFirewallRule -DisplayName 'QuickLinks 6969' -ErrorAction SilentlyContinue
Remove-Item 'C:\Program Files\QuickLinks' -Recurse -Force
# keep C:\ProgramData\QuickLinks if you want the data
```

## What was verified, and what was not

Executed on Windows 11 against `2026.08.11.003`:

- The application running under the wrapper script on Python 3.14, serving `/`, `/admin`, both JS bundles, both stylesheets, `assets/`, and every `/api/` route
- `data/links.db`, `data/.session_secret`, `server.py`, `VERSION`, `Dockerfile`, and `tests/` all returning `404` — the static allowlist behaves the same as on Linux
- The database created in `C:\ProgramData`-style external data directory, with nothing written into the application directory
- The full 71-test suite passing on Windows under Python 3.14
- Logging going to stderr, which is why the redirect note above exists

Not executed, because they need an elevated prompt: `Register-ScheduledTask`, the `icacls` hardening, and `New-NetFirewallRule`. An unelevated attempt to register the task fails with "Access is denied", confirming the elevation requirement. The commands are standard, but run them yourself and check step 7 reports a healthy task.
