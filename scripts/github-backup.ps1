# new-project-setup:managed-helper:v1
#requires -Version 5.1

[CmdletBinding()]
param(
    [string]$ProjectRoot = ".",
    [string]$ConfigPath = ".github-backup.json",
    [string]$Repository,
    [string]$RemoteName = "github-backup",
    [string]$SourceCommit,
    [string]$CandidateCommit,
    [string]$HistoryBaseCommit,
    [switch]$FullSourceHistory,
    [switch]$PrivateSourceSync,
    [switch]$ScanOnly,
    [switch]$AuditSourceHistory
)

$ErrorActionPreference = "Stop"
if ($PSVersionTable.PSEdition -eq 'Core' -and $PSVersionTable.PSVersion.Major -lt 7) {
    throw 'PowerShell 7 or Windows PowerShell 5.1 is required.'
}
$BackupFormat = 2
$BackupAuthorName = "Codex Sanitized Backup"
$BackupAuthorEmail = "codex-sanitized-backup@users.noreply.github.com"
$MaxTextBytes = 5MB
$RegexTimeout = [TimeSpan]::FromSeconds(2)
$StagingOwnerFormat = 1
$PathComparison = if ([Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT) {
    [StringComparison]::OrdinalIgnoreCase
} else {
    [StringComparison]::Ordinal
}

function Invoke-External {
    param(
        [Parameter(Mandatory = $true)][string]$Command,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [switch]$AllowFailure
    )

    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & $Command @Arguments 2>&1
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousPreference
    }
    $normalizedOutput = [string[]]@($output | ForEach-Object { [string]$_ })

    if (-not $AllowFailure -and $exitCode -ne 0) {
        $operation = if ($Arguments.Count -ge 3 -and $Arguments[0] -ceq '-C') { $Arguments[2] } else { $Arguments[0] }
        throw "$Command $operation failed with exit code ${exitCode}: $($normalizedOutput -join [Environment]::NewLine)"
    }

    return [pscustomobject]@{ ExitCode = $exitCode; Output = $normalizedOutput }
}

function Get-NormalizedFullPath {
    param([Parameter(Mandatory = $true)][string]$Path)

    $full = [IO.Path]::GetFullPath($Path)
    $root = [IO.Path]::GetPathRoot($full)
    if ($full.Length -gt $root.Length) {
        $full = $full.TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
    }
    return $full
}

function Get-DescendantPathPrefix {
    param([Parameter(Mandatory = $true)][string]$Path)
    $normalized = Get-NormalizedFullPath $Path
    $separator = [string][IO.Path]::DirectorySeparatorChar
    $alternate = [string][IO.Path]::AltDirectorySeparatorChar
    if ($normalized.EndsWith($separator) -or $normalized.EndsWith($alternate)) { return $normalized }
    return $normalized + $separator
}

function Get-LocalStateRoot {
    $candidates = New-Object Collections.Generic.List[string]
    $runningOnWindows = [Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT
    if ($runningOnWindows) {
        if (-not [string]::IsNullOrWhiteSpace([string]$env:LOCALAPPDATA)) { $candidates.Add([string]$env:LOCALAPPDATA) }
    }
    else {
        if (-not [string]::IsNullOrWhiteSpace([string]$env:XDG_DATA_HOME)) { $candidates.Add([string]$env:XDG_DATA_HOME) }
    }
    $specialFolder = [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData)
    if (-not [string]::IsNullOrWhiteSpace($specialFolder)) { $candidates.Add($specialFolder) }
    $userProfile = if (-not $runningOnWindows -and -not [string]::IsNullOrWhiteSpace([string]$env:HOME)) {
        [string]$env:HOME
    } else {
        [Environment]::GetFolderPath([Environment+SpecialFolder]::UserProfile)
    }
    if (-not [string]::IsNullOrWhiteSpace($userProfile)) { $candidates.Add((Join-Path $userProfile '.local/share')) }

    foreach ($candidate in $candidates) {
        if ([IO.Path]::IsPathRooted($candidate)) { return Get-NormalizedFullPath $candidate }
    }
    throw 'Unable to resolve persistent per-user application data for GitHub audit state.'
}

function Test-SamePath {
    param([string]$Left, [string]$Right)
    $leftFull = Get-NormalizedFullPath $Left
    $rightFull = Get-NormalizedFullPath $Right
    if ([string]::Equals($leftFull, $rightFull, $PathComparison)) { return $true }
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT -and
        (Test-Path -LiteralPath $leftFull) -and (Test-Path -LiteralPath $rightFull)) {
        $testCommand = if (Test-Path -LiteralPath '/usr/bin/test' -PathType Leaf) { '/usr/bin/test' } else { '/bin/test' }
        & $testCommand $leftFull '-ef' $rightFull
        return $LASTEXITCODE -eq 0
    }
    return $false
}

function Assert-ContainedPath {
    param([string]$Path, [string]$Parent, [string]$Label)

    $fullPath = Get-NormalizedFullPath $Path
    $fullParent = Get-NormalizedFullPath $Parent
    $prefix = Get-DescendantPathPrefix $fullParent
    if (-not $fullPath.StartsWith($prefix, $PathComparison)) {
        throw "$Label must remain inside its expected parent."
    }
    return $fullPath
}

function Test-RedirectedItem {
    param([object]$Item)

    $linkType = if ($Item.PSObject.Properties['LinkType']) { [string]$Item.LinkType } else { '' }
    $targets = if ($Item.PSObject.Properties['Target']) { @($Item.Target) } else { @() }
    return -not [string]::IsNullOrWhiteSpace($linkType) -or $targets.Count -gt 0
}

function Resolve-PhysicalExistingPath {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [int]$Depth = 0
    )

    if ($Depth -gt 32) { throw 'Unable to resolve the physical path without a redirect cycle.' }
    $full = Get-NormalizedFullPath $Path
    if (-not (Test-Path -LiteralPath $full)) { throw 'Physical path resolution requires an existing path.' }
    $pathRoot = [IO.Path]::GetPathRoot($full)
    $cursor = $pathRoot
    $relative = $full.Substring($pathRoot.Length)
    $separators = [char[]]@([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
    foreach ($segment in $relative.Split($separators, [StringSplitOptions]::RemoveEmptyEntries)) {
        $candidate = Join-Path $cursor $segment
        $item = Get-Item -Force -LiteralPath $candidate -ErrorAction Stop
        if (-not (Test-RedirectedItem $item)) {
            $cursor = $candidate
            continue
        }

        $targetPath = $null
        if ($item.PSObject.Methods.Name -contains 'ResolveLinkTarget') {
            try {
                $targetItem = $item.ResolveLinkTarget($true)
                if ($targetItem) { $targetPath = [string]$targetItem.FullName }
            } catch {}
        }
        if ([string]::IsNullOrWhiteSpace($targetPath)) {
            $targets = @($item.Target | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) })
            if ($targets.Count -ne 1) { throw 'Redirected path has no single resolvable target.' }
            $targetPath = [string]$targets[0]
            if (-not [IO.Path]::IsPathRooted($targetPath)) {
                $targetPath = Join-Path (Split-Path -Parent $candidate) $targetPath
            }
        }
        $cursor = Resolve-PhysicalExistingPath $targetPath ($Depth + 1)
    }
    return Get-NormalizedFullPath $cursor
}

function Test-FileSystemCaseInsensitive {
    param([string]$Path)
    $probe = Get-NormalizedFullPath $Path
    while (-not (Test-Path -LiteralPath $probe)) {
        $parent = Split-Path -Parent $probe
        if ([string]::IsNullOrWhiteSpace($parent) -or $parent -ceq $probe) { return $false }
        $probe = $parent
    }
    for ($index = $probe.Length - 1; $index -ge 0; $index--) {
        $character = $probe[$index]
        if (-not [char]::IsLetter($character)) { continue }
        $replacement = if ([char]::IsUpper($character)) { [char]::ToLowerInvariant($character) } else { [char]::ToUpperInvariant($character) }
        $variant = $probe.Substring(0, $index) + $replacement + $probe.Substring($index + 1)
        return (Test-Path -LiteralPath $variant) -and (Test-SamePath $probe $variant)
    }
    return $false
}

function Get-PathIdentityKey {
    param([string]$Path)
    $identity = if (Test-Path -LiteralPath $Path) { Resolve-PhysicalExistingPath $Path } else { Get-NormalizedFullPath $Path }
    if ([Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT -or (Test-FileSystemCaseInsensitive $identity)) {
        return $identity.ToUpperInvariant()
    }
    return $identity
}

function Assert-NoRedirectedPath {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$Label)

    $full = Get-NormalizedFullPath $Path
    $pathRoot = [IO.Path]::GetPathRoot($full)
    $cursor = $pathRoot
    $relative = $full.Substring($pathRoot.Length)
    $separators = [char[]]@([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
    foreach ($segment in $relative.Split($separators, [StringSplitOptions]::RemoveEmptyEntries)) {
        $cursor = Join-Path $cursor $segment
        if (-not (Test-Path -LiteralPath $cursor)) { break }
        $item = Get-Item -Force -LiteralPath $cursor
        if (Test-RedirectedItem $item) {
            throw "$Label crosses a redirected path component."
        }
    }
    return $full
}

function Resolve-SafeProjectRoot {
    param([string]$Path)

    $candidate = Assert-NoRedirectedPath (Get-NormalizedFullPath $Path) 'ProjectRoot'
    if (-not (Test-Path -LiteralPath $candidate -PathType Container)) {
        throw "ProjectRoot must be an existing directory."
    }
    $resolved = (Resolve-Path -LiteralPath $candidate).Path
    if (-not (Test-SamePath $candidate $resolved)) { throw "ProjectRoot must not be a redirected link." }
    $gitRoot = ((Invoke-External 'git' @('-C', $resolved, 'rev-parse', '--show-toplevel')).Output | Select-Object -First 1).Trim()
    if (-not (Test-SamePath $resolved $gitRoot)) { throw "ProjectRoot must be the Git repository root." }
    Assert-NoRedirectedPath $resolved 'ProjectRoot' | Out-Null
    return Get-NormalizedFullPath $resolved
}

function Invoke-TestFault {
    param([string]$Name, [string]$ContextPath)

    if ([string]$env:NEW_PROJECT_SETUP_TEST_FAULT -cne $Name) { return }
    $action = [string]$env:NEW_PROJECT_SETUP_TEST_ACTION
    if ([string]::IsNullOrWhiteSpace($action)) { throw "Injected test fault at $Name." }
    if (-not [IO.Path]::IsPathRooted($action) -or [IO.Path]::GetExtension($action) -ine '.ps1') {
        throw "The injected test action must be an absolute local PowerShell script path."
    }
    $actionPath = Assert-NoRedirectedPath $action 'Injected test action'
    if (-not (Test-Path -LiteralPath $actionPath -PathType Leaf)) { throw "The injected test action is unavailable." }
    & $actionPath $Name $ContextPath | Out-Null
}

function Write-Utf8Atomically {
    param([string]$Path, [string]$Content, [string]$ExpectedParent, [string]$Label)

    Assert-ContainedPath $Path $ExpectedParent $Label | Out-Null
    $directory = Split-Path -Parent $Path
    Assert-NoRedirectedPath $directory "$Label parent" | Out-Null
    if (Test-Path -LiteralPath $Path) { Assert-NoRedirectedPath $Path $Label | Out-Null }
    $operationId = [Guid]::NewGuid().ToString('N')
    $tempPath = Join-Path $directory ('.' + [IO.Path]::GetFileName($Path) + '.codex-' + $operationId + '.tmp')
    $backupPath = Join-Path $directory ('.' + [IO.Path]::GetFileName($Path) + '.codex-' + $operationId + '.bak')
    Assert-ContainedPath $tempPath $ExpectedParent "$Label temporary path" | Out-Null
    Assert-ContainedPath $backupPath $ExpectedParent "$Label rollback path" | Out-Null
    try {
        [IO.File]::WriteAllText($tempPath, $Content, [Text.UTF8Encoding]::new($false))
        Assert-NoRedirectedPath $directory "$Label parent" | Out-Null
        Assert-NoRedirectedPath $tempPath "$Label temporary path" | Out-Null
        Assert-ContainedPath $Path $ExpectedParent $Label | Out-Null
        if (Test-Path -LiteralPath $Path) {
            [IO.File]::Replace($tempPath, $Path, $backupPath)
            Assert-NoRedirectedPath $backupPath "$Label rollback path" | Out-Null
            Assert-ContainedPath $backupPath $ExpectedParent "$Label rollback path" | Out-Null
            Remove-Item -LiteralPath $backupPath -Force
        }
        else { [IO.File]::Move($tempPath, $Path) }
    }
    finally {
        if (Test-Path -LiteralPath $tempPath) {
            Assert-NoRedirectedPath $tempPath "$Label temporary path" | Out-Null
            Assert-ContainedPath $tempPath $ExpectedParent "$Label temporary path" | Out-Null
            Remove-Item -LiteralPath $tempPath -Force
        }
        if (Test-Path -LiteralPath $backupPath) {
            Assert-NoRedirectedPath $backupPath "$Label rollback path" | Out-Null
            Assert-ContainedPath $backupPath $ExpectedParent "$Label rollback path" | Out-Null
            Remove-Item -LiteralPath $backupPath -Force
        }
    }
}

function Convert-GlobToRegex {
    param([Parameter(Mandatory = $true)][string]$Glob)

    $normalized = $Glob.Replace('\', '/').TrimStart('/')
    $escaped = [Regex]::Escape($normalized)
    $escaped = $escaped.Replace('\*\*/', '(?:.*/)?').Replace('\*\*', '.*')
    $escaped = $escaped.Replace('\*', '[^/]*').Replace('\?', '[^/]')
    return "^${escaped}$"
}

function Test-GlobMatch {
    param([string]$Path, [string]$Glob)
    return $Path -match (Convert-GlobToRegex -Glob $Glob)
}

function Get-Sha256Bytes {
    param([Parameter(Mandatory = $true)][byte[]]$Bytes)

    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($sha.ComputeHash($Bytes))).Replace('-', '').ToLowerInvariant()
    }
    finally {
        $sha.Dispose()
    }
}

function Get-Sha256Text {
    param([Parameter(Mandatory = $true)][string]$Value)
    return Get-Sha256Bytes -Bytes ([Text.Encoding]::UTF8.GetBytes($Value))
}

function Get-RepositoryFromUrl {
    param([Parameter(Mandatory = $true)][string]$Url)

    $trimmed = $Url.Trim()
    if ($trimmed -match '^(?i:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([^/\s]+/[^/\s]+?)(?:\.git)?/?$') {
        return $Matches[1]
    }
    return $null
}

function Test-SameRepository {
    param([string]$Left, [string]$Right)
    return -not [string]::IsNullOrWhiteSpace($Left) -and
        -not [string]::IsNullOrWhiteSpace($Right) -and
        [string]::Equals($Left.TrimEnd('/'), $Right.TrimEnd('/'), [StringComparison]::OrdinalIgnoreCase)
}

function Resolve-GitHubRepository {
    param([Parameter(Mandatory = $true)][string]$RepositoryValue, [switch]$AllowMissing)

    $view = Invoke-External 'gh' @('repo', 'view', $RepositoryValue, '--json', 'nameWithOwner,url,visibility,isEmpty') -AllowFailure
    if ($view.ExitCode -ne 0) {
        if ($AllowMissing) { return $null }
        throw "Unable to resolve the configured GitHub repository."
    }
    try { $data = ($view.Output -join [Environment]::NewLine) | ConvertFrom-Json }
    catch { throw "GitHub returned invalid repository metadata." }
    $canonical = [string]$data.nameWithOwner
    $url = [string]$data.url
    if ($canonical -notmatch '^[^/\s]+/[^/\s]+$' -or -not (Test-SameRepository $canonical (Get-RepositoryFromUrl $url))) {
        throw "GitHub returned inconsistent repository identity metadata."
    }
    if ([string]$data.visibility -cne 'PRIVATE') { throw "Backup repository must be private: $canonical" }
    return [pscustomobject]@{
        Repository = $canonical
        Url = $url
        IsEmpty = [bool]$data.isEmpty
    }
}

function Get-RemoteUrlState {
    param([string]$RepoRoot, [string]$Name)

    $fetch = Invoke-External 'git' @('-C', $RepoRoot, 'remote', 'get-url', '--all', $Name)
    $push = Invoke-External 'git' @('-C', $RepoRoot, 'remote', 'get-url', '--push', '--all', $Name)
    return [pscustomobject]@{
        Fetch = [string[]]@($fetch.Output | ForEach-Object { ([string]$_).Trim() } | Where-Object { $_ })
        Push = [string[]]@($push.Output | ForEach-Object { ([string]$_).Trim() } | Where-Object { $_ })
    }
}

function Test-RemoteMatchesRepository {
    param([object]$UrlState, [string]$ExpectedRepository)

    if ($UrlState.Fetch.Count -eq 0 -or $UrlState.Push.Count -eq 0) { return $false }
    foreach ($url in @($UrlState.Fetch) + @($UrlState.Push)) {
        if (-not (Test-SameRepository (Get-RepositoryFromUrl ([string]$url)) $ExpectedRepository)) { return $false }
    }
    return $true
}

function Disable-RemotePush {
    param([string]$RepoRoot, [string]$Name)

    Invoke-External 'git' @('-C', $RepoRoot, 'config', '--unset-all', "remote.${Name}.pushurl") -AllowFailure | Out-Null
    Invoke-External 'git' @('-C', $RepoRoot, 'config', '--add', "remote.${Name}.pushurl", 'DISABLED') | Out-Null
    $pushUrls = [string[]]@((Invoke-External 'git' @('-C', $RepoRoot, 'remote', 'get-url', '--push', '--all', $Name)).Output)
    if ($pushUrls.Count -ne 1 -or $pushUrls[0].Trim() -cne 'DISABLED') {
        throw "Unable to disable the unverified legacy remote: $Name"
    }

    $branch = ((Invoke-External 'git' @('-C', $RepoRoot, 'branch', '--show-current')).Output | Select-Object -First 1).Trim()
    if ($branch) {
        $upstreamRemote = Invoke-External 'git' @('-C', $RepoRoot, 'config', '--get', "branch.${branch}.remote") -AllowFailure
        if ($upstreamRemote.ExitCode -eq 0 -and (($upstreamRemote.Output | Select-Object -First 1).Trim()) -ceq $Name) {
            Invoke-External 'git' @('-C', $RepoRoot, 'branch', '--unset-upstream') | Out-Null
        }
    }
    Write-Warning "Disabled push and upstream tracking for unverified legacy remote: $Name"
}

function Get-RemoteBranchTip {
    param([string]$Url, [string]$Branch)

    $ref = "refs/heads/$Branch"
    $result = Invoke-External 'git' @('ls-remote', '--exit-code', '--heads', $Url, $ref) -AllowFailure
    if ($result.ExitCode -eq 2) { return [pscustomobject]@{ Exists = $false; Sha = $null; Ref = $ref } }
    if ($result.ExitCode -ne 0) { throw "Unable to inspect the exact backup branch." }
    $lines = @($result.Output | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) })
    if ($lines.Count -ne 1 -or [string]$lines[0] -notmatch '^([0-9a-fA-F]{40,64})\s+(.+)$' -or $Matches[2] -cne $ref) {
        throw "Backup branch inspection returned an ambiguous result."
    }
    return [pscustomobject]@{ Exists = $true; Sha = $Matches[1].ToLowerInvariant(); Ref = $ref }
}

function New-CompiledRegex {
    param([string]$Id, [string]$Pattern, [string]$Category = 'secret')

    try {
        $compiled = [Regex]::new(
            $Pattern,
            [Text.RegularExpressions.RegexOptions]::CultureInvariant,
            $RegexTimeout
        )
    }
    catch {
        throw "Invalid confidential scan rule ${Id}: $($_.Exception.Message)"
    }
    return [pscustomobject]@{ Id = $Id; Regex = $compiled; Pattern = $Pattern; Category = $Category }
}

function Get-ScanRules {
    param([object[]]$CustomRules)

    $rules = @(
        (New-CompiledRegex 'private-key' '-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----'),
        (New-CompiledRegex 'bearer-token' '(?i)authorization\s*[:=]\s*["'']?bearer\s+[A-Za-z0-9._~+/=-]{8,}'),
        (New-CompiledRegex 'credential-assignment' '(?i)(?:api[_-]?key|client[_-]?secret|password|passwd|token|secret)\s*[:=]\s*(?:"(?!example|placeholder|replace|changeme)(?=[^"\r\n]*[0-9+/=_-])[^"\r\n]{10,}"|''(?!example|placeholder|replace|changeme)(?=[^''\r\n]*[0-9+/=_-])[^''\r\n]{10,}'')'),
        (New-CompiledRegex 'known-token-format' '(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16})'),
        (New-CompiledRegex 'connection-string' '(?i)(?:mongodb(?:\+srv)?|postgres(?:ql)?|mysql|redis|amqp)://[^\s"'']+:[^\s"'']+@'),
        (New-CompiledRegex 'private-network' '(?<!\d)(?!(?:100\.64\.0\.0|100\.100\.100\.100|100\.127\.255\.255)(?!\d))(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2}|100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])(?:\.\d{1,3}){2})(?!\d)' 'operational-metadata'),
        (New-CompiledRegex 'machine-user-path' '(?i)(?:[A-Z]:\\Users\\[^\\\r\n]+|/home/[^/\s]+|/Users/[^/\s]+)' 'operational-metadata'),
        (New-CompiledRegex 'operational-endpoint' '(?i)(?:root@[A-Za-z0-9._-]+|/srv/[A-Za-z0-9._/-]+)' 'operational-metadata'),
        (New-CompiledRegex 'git-lfs-pointer' '(?m)^version https://git-lfs\.github\.com/spec/v1\s*$')
    )

    foreach ($custom in @($CustomRules)) {
        if (-not $custom.id -or -not $custom.regex) {
            throw "Each confidential_patterns entry requires id and regex."
        }
        $rules += New-CompiledRegex -Id ([string]$custom.id) -Pattern ([string]$custom.regex)
    }
    return $rules
}

function Get-TextContent {
    param([Parameter(Mandatory = $true)][byte[]]$Bytes)

    if ($Bytes.Length -gt $MaxTextBytes) {
        return [pscustomobject]@{ Kind = 'oversize'; Content = $null }
    }
    if ($Bytes.Length -eq 0) {
        return [pscustomobject]@{ Kind = 'text'; Content = '' }
    }

    try {
        if ($Bytes.Length -ge 2 -and $Bytes[0] -eq 0xff -and $Bytes[1] -eq 0xfe) {
            return [pscustomobject]@{ Kind = 'text'; Content = [Text.Encoding]::Unicode.GetString($Bytes, 2, $Bytes.Length - 2) }
        }
        if ($Bytes.Length -ge 2 -and $Bytes[0] -eq 0xfe -and $Bytes[1] -eq 0xff) {
            return [pscustomobject]@{ Kind = 'text'; Content = [Text.Encoding]::BigEndianUnicode.GetString($Bytes, 2, $Bytes.Length - 2) }
        }
        if ($Bytes.Length -ge 3 -and $Bytes[0] -eq 0xef -and $Bytes[1] -eq 0xbb -and $Bytes[2] -eq 0xbf) {
            return [pscustomobject]@{ Kind = 'text'; Content = [Text.UTF8Encoding]::new($false, $true).GetString($Bytes, 3, $Bytes.Length - 3) }
        }

        $sampleLength = [Math]::Min($Bytes.Length, 8192)
        $evenNull = 0
        $oddNull = 0
        for ($index = 0; $index -lt $sampleLength; $index++) {
            if ($Bytes[$index] -eq 0) {
                if ($index % 2 -eq 0) { $evenNull++ } else { $oddNull++ }
            }
        }
        if ($oddNull -gt ($sampleLength / 8) -and $evenNull -eq 0) {
            return [pscustomobject]@{ Kind = 'text'; Content = [Text.Encoding]::Unicode.GetString($Bytes) }
        }
        if ($evenNull -gt ($sampleLength / 8) -and $oddNull -eq 0) {
            return [pscustomobject]@{ Kind = 'text'; Content = [Text.Encoding]::BigEndianUnicode.GetString($Bytes) }
        }
        if ($evenNull + $oddNull -gt 0) {
            return [pscustomobject]@{ Kind = 'binary'; Content = $null }
        }

        return [pscustomobject]@{ Kind = 'text'; Content = [Text.UTF8Encoding]::new($false, $true).GetString($Bytes) }
    }
    catch {
        return [pscustomobject]@{ Kind = 'binary'; Content = $null }
    }
}

function Get-LineNumberForIndex {
    param([string]$Content, [int]$Index)

    if ($Index -le 0) { return 1 }
    $line = 1
    for ($cursor = 0; $cursor -lt $Index -and $cursor -lt $Content.Length; $cursor++) {
        if ($Content[$cursor] -eq [char]"`n") { $line++ }
    }
    return $line
}

function Test-FingerprintedAllowance {
    param([object[]]$Entries, [string]$Rule, [string]$Path, [string]$Sha256)

    foreach ($entry in @($Entries)) {
        if ($entry.rule -eq $Rule -and $entry.sha256 -eq $Sha256 -and (Test-GlobMatch $Path ([string]$entry.path))) {
            return $true
        }
    }
    return $false
}

function Get-DefaultConfig {
    return [pscustomobject]@{
        repository = $null
        exclude = @()
        allow_findings = @()
        allow_binary = @()
        confidential_patterns = @()
    }
}

function Get-CommittedConfig {
    param([string]$RepoRoot, [string]$RelativePath, [string]$Commit)

    if ([string]::IsNullOrWhiteSpace($RelativePath) -or [IO.Path]::IsPathRooted($RelativePath) -or
        $RelativePath.Replace('\', '/') -match '(^|/)\.\.(/|$)' -or $RelativePath.IndexOf([char]0) -ge 0) {
        throw "ConfigPath must be a project-relative committed path."
    }
    if ([Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT -and $RelativePath -match ':') {
        throw "ConfigPath must not use an alternate data stream."
    }
    $path = $RelativePath.Replace('\', '/').TrimStart('/')
    $defaults = Get-DefaultConfig

    $show = Invoke-External 'git' @('-C', $RepoRoot, 'show', "${Commit}:$path") -AllowFailure
    if ($show.ExitCode -eq 0) {
        $loaded = ($show.Output -join [Environment]::NewLine) | ConvertFrom-Json
        foreach ($property in @('repository', 'exclude', 'allow_findings', 'allow_binary', 'confidential_patterns')) {
            if ($null -ne $loaded.$property) { $defaults.$property = $loaded.$property }
        }
    }

    $worktreePath = Get-NormalizedFullPath (Join-Path $RepoRoot $path.Replace('/', [IO.Path]::DirectorySeparatorChar))
    Assert-ContainedPath $worktreePath $RepoRoot 'ConfigPath' | Out-Null
    if (Test-Path -LiteralPath $worktreePath) {
        Assert-NoRedirectedPath $worktreePath 'ConfigPath' | Out-Null
        $worktreeText = Get-Content -Raw -LiteralPath $worktreePath
        $committedText = if ($show.ExitCode -eq 0) { $show.Output -join [Environment]::NewLine } else { $null }
        if ($worktreeText.TrimEnd() -ne ([string]$committedText).TrimEnd()) {
            Write-Warning "Ignoring dirty or untracked $path; backup policy comes only from committed $Commit."
        }
    }
    return $defaults
}

function Get-TreeEntries {
    param([string]$RepoRoot, [string]$Commit)

    if ($Commit -notmatch '^[0-9a-fA-F]{40,64}$') {
        throw 'Git tree enumeration requires an exact commit object ID.'
    }
    $gitExecutable = if (-not [string]::IsNullOrWhiteSpace([string]$env:NPS_TEST_REAL_GIT) -and
        (Test-Path -LiteralPath ([string]$env:NPS_TEST_REAL_GIT) -PathType Leaf)) {
        [IO.Path]::GetFullPath([string]$env:NPS_TEST_REAL_GIT)
    } elseif ([Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT) {
        (Get-Command git.exe -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
    } else {
        (Get-Command git -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
    }
    $startInfo = New-Object Diagnostics.ProcessStartInfo
    $startInfo.FileName = $gitExecutable
    $startInfo.Arguments = "ls-tree -rz --full-tree $Commit"
    $startInfo.WorkingDirectory = $RepoRoot
    $startInfo.UseShellExecute = $false
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.CreateNoWindow = $true
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $startInfo
    $stream = New-Object IO.MemoryStream
    try {
        if (-not $process.Start()) { throw 'Unable to start Git tree enumeration.' }
        $process.StandardOutput.BaseStream.CopyTo($stream)
        $errorText = $process.StandardError.ReadToEnd()
        $process.WaitForExit()
        if ($process.ExitCode -ne 0) {
            throw "git ls-tree failed with exit code $($process.ExitCode): $errorText"
        }
        try {
            $text = [Text.UTF8Encoding]::new($false, $true).GetString($stream.ToArray())
        }
        catch {
            throw 'Git tree contains a pathname that is not valid UTF-8 and cannot be materialized safely.'
        }
    }
    finally {
        $stream.Dispose()
        $process.Dispose()
    }

    $entries = New-Object Collections.Generic.List[object]
    foreach ($record in $text.Split([char[]]@([char]0), [StringSplitOptions]::RemoveEmptyEntries)) {
        if ([string]$record -notmatch '(?s)^(\d+)\s+(blob|commit)\s+([0-9a-f]+)\t(.+)$') {
            throw "Unable to parse Git tree entry without risking an incomplete snapshot."
        }
        $entries.Add([pscustomobject]@{
            Mode = $Matches[1]
            Type = $Matches[2]
            ObjectId = $Matches[3]
            Path = $Matches[4]
        })
    }
    return $entries
}

function Get-ConfidentialPathFindings {
    param([string]$Path, [object[]]$Rules)

    $matches = New-Object Collections.Generic.List[string]
    foreach ($rule in $Rules) {
        try { $matched = $rule.Regex.IsMatch($Path) }
        catch [Text.RegularExpressions.RegexMatchTimeoutException] {
            $matches.Add('regex-timeout')
            continue
        }
        if ($matched) { $matches.Add([string]$rule.Id) }
    }
    return [string[]]@($matches | Sort-Object -Unique)
}

function Get-RedactedPathLabel {
    param([string]$Path)
    return "<confidential-path:$((Get-Sha256Text $Path).Substring(0, 12))>"
}

function Test-CommitMetadata {
    param([string]$RepoRoot, [string]$Commit, [object[]]$Rules)

    $findings = New-Object Collections.Generic.List[object]
    $metadata = (Invoke-External 'git' @('-C', $RepoRoot, 'cat-file', 'commit', $Commit)).Output -join [Environment]::NewLine
    foreach ($rule in $Rules) {
        try { $matched = $rule.Regex.IsMatch($metadata) }
        catch [Text.RegularExpressions.RegexMatchTimeoutException] {
            $findings.Add([pscustomobject]@{ Rule = 'regex-timeout'; Path = "<commit-metadata:$Commit>" })
            continue
        }
        if ($matched) {
            $findings.Add([pscustomobject]@{ Rule = $rule.Id; Path = "<commit-metadata:$Commit>" })
        }
    }
    return [object[]]@($findings | Sort-Object Rule, Path -Unique)
}

function Test-CommitSnapshot {
    param(
        [string]$RepoRoot,
        [string]$Commit,
        [object]$Config,
        [object[]]$Rules,
        [string]$TempParent,
        [switch]$HistoryAudit,
        [switch]$SourceAudit,
        [string]$InheritedFromCommit,
        [switch]$PrivateSourceSync,
        [switch]$KeepSnapshot
    )

    $defaultExcludes = @(
        [pscustomobject]@{ Id = 'secret-or-runtime-path'; Regex = '(^|/)(\.env($|\.)|[^/]+\.(key|pem|p12|pfx|crt|cer|cookie|db|sqlite|sqlite3)$)' },
        [pscustomobject]@{ Id = 'generated-path'; Regex = '(^|/)(logs?|uploads?|screenshots?|videos?|test-artifacts|playwright-report|node_modules|dist|build|out|\.next|coverage|\.cache|cache)(/|$)' },
        [pscustomobject]@{ Id = 'private-work-log'; Regex = '(^|/)docs/work-log\.md$' },
        [pscustomobject]@{ Id = 'local-private-doc'; Regex = '(^|/)[^/]+\.local\.md$' }
    )

    $entries = Get-TreeEntries $RepoRoot $Commit
    $inheritedPaths = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    if ($InheritedFromCommit) {
        $parentEntries = [Collections.Generic.Dictionary[string,string]]::new([StringComparer]::Ordinal)
        foreach ($parentEntry in @(Get-TreeEntries $RepoRoot $InheritedFromCommit)) {
            $parentEntries.Add($parentEntry.Path, "$($parentEntry.Mode)|$($parentEntry.ObjectId)")
        }
        foreach ($entry in $entries) {
            $parentIdentity = $null
            if ($parentEntries.TryGetValue($entry.Path, [ref]$parentIdentity) -and
                $parentIdentity -ceq "$($entry.Mode)|$($entry.ObjectId)") {
                $inheritedPaths.Add($entry.Path) | Out-Null
            }
        }
    }
    $included = New-Object Collections.Generic.List[object]
    $excluded = New-Object Collections.Generic.List[object]
    $findings = New-Object Collections.Generic.List[object]

    foreach ($entry in $entries) {
        $isInherited = $inheritedPaths.Contains($entry.Path)
        $pathFindings = @(Get-ConfidentialPathFindings $entry.Path $Rules)
        $reportPath = if ($pathFindings.Count -gt 0) { Get-RedactedPathLabel $entry.Path } else { $entry.Path }
        if ($entry.Type -ne 'blob' -or $entry.Mode -notin @('100644', '100755')) {
            $findings.Add([pscustomobject]@{ Rule = 'unsupported-git-object'; Path = $reportPath })
            continue
        }

        $reason = $null
        foreach ($rule in $defaultExcludes) {
            if ($entry.Path -match $rule.Regex) { $reason = $rule.Id; break }
        }
        if (-not $reason -and (-not $HistoryAudit -or $SourceAudit)) {
            foreach ($glob in @($Config.exclude)) {
                if (Test-GlobMatch $entry.Path ([string]$glob)) { $reason = 'project-exclude'; break }
            }
        }

        if ($reason) {
            if ($HistoryAudit) {
                $privateOnlyExclude = $reason -in @('generated-path', 'private-work-log', 'local-private-doc', 'project-exclude')
                if (-not $isInherited -and (-not $PrivateSourceSync -or -not $privateOnlyExclude)) {
                    $findings.Add([pscustomobject]@{ Rule = 'forbidden-history-path'; Path = $reportPath })
                    foreach ($pathRule in $pathFindings) {
                        $findings.Add([pscustomobject]@{ Rule = $pathRule; Path = $reportPath })
                    }
                }
            } else {
                $excluded.Add([pscustomobject]@{ Path = $entry.Path; ReportPath = $reportPath; Reason = $reason })
            }
            continue
        }
        if (-not $isInherited) {
            foreach ($pathRule in $pathFindings) {
                $findings.Add([pscustomobject]@{ Rule = $pathRule; Path = $reportPath })
            }
        }
        $entry | Add-Member -NotePropertyName ReportPath -NotePropertyValue $reportPath
        $entry | Add-Member -NotePropertyName Inherited -NotePropertyValue $isInherited
        $included.Add($entry)
    }

    if ($included.Count -eq 0) {
        $findings.Add([pscustomobject]@{ Rule = 'empty-snapshot'; Path = '.' })
    }

    $snapshotRoot = Join-Path $TempParent ("snapshot-" + [Guid]::NewGuid().ToString('N'))
    $archivePath = "${snapshotRoot}.tar"
    New-Item -ItemType Directory -Force -Path $snapshotRoot | Out-Null
    if (@($findings | Where-Object Rule -eq 'unsupported-git-object').Count -gt 0) {
        $result = [pscustomobject]@{
            Included = [string[]]@($included | ForEach-Object { $_.ReportPath })
            Excluded = [object[]]@($excluded | ForEach-Object { [pscustomobject]@{ Path = $_.ReportPath; Reason = $_.Reason } })
            Findings = [object[]]@($findings | Sort-Object Rule, Path -Unique)
            SnapshotRoot = $snapshotRoot
        }
        if (-not $KeepSnapshot) { Remove-SafeDirectory $snapshotRoot $TempParent 'Snapshot path' }
        return $result
    }
    Invoke-External 'git' @('-C', $RepoRoot, 'archive', '--format=tar', "--output=$archivePath", $Commit) | Out-Null
    $tarArguments = if ([Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT) {
        @('--options', 'hdrcharset=UTF-8', '-xf', $archivePath, '-C', $snapshotRoot)
    } else {
        @('-xf', $archivePath, '-C', $snapshotRoot)
    }
    Invoke-External 'tar' $tarArguments | Out-Null
    Remove-Item -LiteralPath $archivePath -Force

    foreach ($entry in $excluded) {
        $excludedPath = Get-NormalizedFullPath (Join-Path $snapshotRoot $entry.Path.Replace('/', [IO.Path]::DirectorySeparatorChar))
        Assert-ContainedPath $excludedPath $snapshotRoot 'Excluded snapshot path' | Out-Null
        if (Test-Path -LiteralPath $excludedPath) {
            if (Test-Path -LiteralPath $excludedPath -PathType Container) { Assert-NoRedirectedTree $excludedPath 'Excluded snapshot path' }
            else { Assert-NoRedirectedPath $excludedPath 'Excluded snapshot path' | Out-Null }
            Assert-ContainedPath $excludedPath $snapshotRoot 'Excluded snapshot path' | Out-Null
            Remove-Item -LiteralPath $excludedPath -Force -Recurse
        }
    }

    foreach ($entry in $included) {
        $localPath = Get-NormalizedFullPath (Join-Path $snapshotRoot $entry.Path.Replace('/', [IO.Path]::DirectorySeparatorChar))
        Assert-ContainedPath $localPath $snapshotRoot 'Snapshot entry' | Out-Null
        if (Test-Path -LiteralPath $localPath) { Assert-NoRedirectedPath $localPath 'Snapshot entry' | Out-Null }
        if (-not (Test-Path -LiteralPath $localPath -PathType Leaf)) {
            $findings.Add([pscustomobject]@{ Rule = 'missing-snapshot-entry'; Path = $entry.ReportPath })
            continue
        }
        $bytes = [IO.File]::ReadAllBytes($localPath)
        $sha256 = Get-Sha256Bytes $bytes
        $decoded = Get-TextContent $bytes
        if ($decoded.Kind -eq 'oversize') {
            if (-not $entry.Inherited) { $findings.Add([pscustomobject]@{ Rule = 'oversize-file'; Path = $entry.ReportPath }) }
            continue
        }
        if ($decoded.Kind -eq 'binary') {
            if (-not $entry.Inherited -and -not (Test-FingerprintedAllowance $Config.allow_binary 'binary-file' $entry.Path $sha256)) {
                $findings.Add([pscustomobject]@{ Rule = 'unreviewed-binary'; Path = $entry.ReportPath })
            }
            continue
        }

        foreach ($rule in $Rules) {
            $content = if ($entry.Path -eq 'scripts/github-backup.ps1') {
                $decoded.Content.Replace([string]$rule.Pattern, '')
            } else {
                $decoded.Content
            }
            try { $match = $rule.Regex.Match($content) }
            catch [Text.RegularExpressions.RegexMatchTimeoutException] {
                $findings.Add([pscustomobject]@{ Rule = 'regex-timeout'; Path = $entry.ReportPath })
                continue
            }
            if ($match.Success -and -not $entry.Inherited -and
                -not (Test-FingerprintedAllowance $Config.allow_findings $rule.Id $entry.Path $sha256)) {
                $findings.Add([pscustomobject]@{
                    Rule = $rule.Id
                    Path = $entry.ReportPath
                    Line = Get-LineNumberForIndex $content $match.Index
                })
            }
        }
    }

    $includedPaths = [string[]]($included | ForEach-Object { $_.ReportPath })
    $excludedItems = [object[]]($excluded | ForEach-Object {
        [pscustomobject]@{ Path = $_.ReportPath; Reason = $_.Reason }
    })
    $findingItems = [object[]]($findings | Sort-Object Rule, Path -Unique)
    $result = [pscustomobject]@{
        Included = $includedPaths
        Excluded = $excludedItems
        Findings = $findingItems
        SnapshotRoot = $snapshotRoot
    }
    if (-not $KeepSnapshot) { Remove-SafeDirectory $snapshotRoot $TempParent 'Snapshot path' }
    return $result
}

function Write-AuditManifest {
    param([string]$AuditPath, [string]$AuditParent, [string]$SourceHead, [object]$Result)

    $manifest = [ordered]@{
        format = 1
        source_head = $SourceHead
        included = @($Result.Included)
        excluded = @($Result.Excluded)
        findings = @($Result.Findings)
    } | ConvertTo-Json -Depth 6
    Write-Utf8Atomically $AuditPath ($manifest + "`n") $AuditParent 'Audit manifest'
}

function Resolve-ExactCommit {
    param([string]$RepoRoot, [string]$Value, [string]$Label)

    if ([string]::IsNullOrWhiteSpace($Value) -or $Value -notmatch '^[0-9a-fA-F]{40,64}$') {
        throw "$Label must be an exact commit object ID."
    }
    $normalized = $Value.ToLowerInvariant()
    $resolved = Invoke-External 'git' @('-C', $RepoRoot, 'rev-parse', '--verify', "${normalized}^{commit}") -AllowFailure
    $lines = @($resolved.Output | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) })
    if ($resolved.ExitCode -ne 0 -or $lines.Count -ne 1 -or ([string]$lines[0]).Trim() -cne $normalized) {
        throw "$Label must identify an existing exact immutable commit."
    }
    return $normalized
}

function Get-SourceHeadState {
    param([string]$RepoRoot, [switch]$AllowUnborn)

    $symbolic = Invoke-External 'git' @('-C', $RepoRoot, 'symbolic-ref', '--quiet', 'HEAD') -AllowFailure
    $symbolicLines = @($symbolic.Output | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) })
    $symbolicRef = if ($symbolic.ExitCode -eq 0 -and $symbolicLines.Count -eq 1 -and
        ([string]$symbolicLines[0]).Trim() -match '^refs/heads/.+$') {
        ([string]$symbolicLines[0]).Trim()
    } else { $null }
    $head = Invoke-External 'git' @('-C', $RepoRoot, 'rev-parse', '--verify', 'HEAD^{commit}') -AllowFailure
    if ($head.ExitCode -eq 0) {
        $lines = @($head.Output | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) })
        if ($lines.Count -ne 1 -or ([string]$lines[0]).Trim() -notmatch '^[0-9a-fA-F]{40,64}$') {
            throw 'Unable to resolve the exact source HEAD.'
        }
        return [pscustomobject]@{ Commit = ([string]$lines[0]).Trim().ToLowerInvariant(); SymbolicRef = $symbolicRef }
    }
    if (-not $AllowUnborn) { throw 'A committed source HEAD is required.' }

    if (-not $symbolicRef) {
        throw 'Unable to verify a valid unborn source HEAD.'
    }
    $existingRef = Invoke-External 'git' @('-C', $RepoRoot, 'show-ref', '--verify', '--quiet', $symbolicRef) -AllowFailure
    if ($existingRef.ExitCode -eq 0) { throw 'The symbolic source HEAD does not resolve to a valid commit.' }
    if ($existingRef.ExitCode -ne 1) { throw 'Unable to verify the unborn source HEAD.' }
    return [pscustomobject]@{ Commit = $null; SymbolicRef = $symbolicRef }
}

function Assert-CandidateSourceState {
    param([string]$RepoRoot, [object]$ExpectedHead, [string]$ExpectedIndexTree)

    $currentHead = Get-SourceHeadState $RepoRoot -AllowUnborn
    if ([string]$currentHead.Commit -cne [string]$ExpectedHead.Commit -or
        [string]$currentHead.SymbolicRef -cne [string]$ExpectedHead.SymbolicRef) {
        throw 'Source HEAD changed during candidate audit.'
    }
    $currentIndexTree = ((Invoke-External 'git' @('-C', $RepoRoot, 'write-tree')).Output | Select-Object -First 1).Trim()
    if ($currentIndexTree -cne $ExpectedIndexTree) {
        throw 'The staged source tree changed during candidate audit.'
    }
}

function Assert-SourceHead {
    param([string]$RepoRoot, [string]$Expected)
    $current = ((Invoke-External 'git' @('-C', $RepoRoot, 'rev-parse', '--verify', 'HEAD^{commit}')).Output | Select-Object -First 1).Trim()
    if ($current -cne $Expected) { throw "Source HEAD changed during backup. No push was made." }
}

function Test-BackupHistory {
    param([string]$RepoRoot, [string]$TipCommit, [object]$Config, [object[]]$Rules, [string]$TempParent)

    $findings = New-Object Collections.Generic.List[object]
    $roots = @((Invoke-External 'git' @('-C', $RepoRoot, 'rev-list', '--max-parents=0', $TipCommit)).Output | Where-Object { $_ })
    if ($roots.Count -ne 1) { $findings.Add([pscustomobject]@{ Rule = 'invalid-history-roots'; Path = '.' }) }
    $merges = @((Invoke-External 'git' @('-C', $RepoRoot, 'rev-list', '--min-parents=2', $TipCommit)).Output | Where-Object { $_ })
    if ($merges.Count -gt 0) { $findings.Add([pscustomobject]@{ Rule = 'merge-history'; Path = '.' }) }

    if ($roots.Count -eq 1) {
        $marker = Invoke-External 'git' @('-C', $RepoRoot, 'show', "$($roots[0]):.codex-sanitized-backup.json") -AllowFailure
        if ($marker.ExitCode -ne 0) {
            $findings.Add([pscustomobject]@{ Rule = 'missing-root-marker'; Path = '.codex-sanitized-backup.json' })
        } else {
            try { $markerData = ($marker.Output -join [Environment]::NewLine) | ConvertFrom-Json }
            catch { $markerData = $null }
            if (-not $markerData -or [int]$markerData.format -ne $BackupFormat) {
                $findings.Add([pscustomobject]@{ Rule = 'invalid-root-marker'; Path = '.codex-sanitized-backup.json' })
            }
        }
    }

    $expectedIdentity = "${BackupAuthorName}|${BackupAuthorEmail}|${BackupAuthorName}|${BackupAuthorEmail}"
    foreach ($identity in (Invoke-External 'git' @('-C', $RepoRoot, 'log', $TipCommit, '--format=%an|%ae|%cn|%ce')).Output) {
        if ([string]$identity -cne $expectedIdentity) {
            $findings.Add([pscustomobject]@{ Rule = 'unsafe-commit-identity'; Path = '.' })
            break
        }
    }

    foreach ($commit in (Invoke-External 'git' @('-C', $RepoRoot, 'rev-list', '--reverse', $TipCommit)).Output) {
        foreach ($finding in @(Test-CommitMetadata $RepoRoot ([string]$commit) $Rules)) { $findings.Add($finding) }
        $scan = Test-CommitSnapshot $RepoRoot ([string]$commit) $Config $Rules $TempParent -HistoryAudit
        foreach ($finding in $scan.Findings) { $findings.Add($finding) }
    }
    return @($findings | Sort-Object Rule, Path -Unique)
}

function Test-SourceHistory {
    param([string]$RepoRoot, [string]$TipCommit, [object]$Config, [object[]]$Rules, [string]$TempParent, [string]$BaseCommit, [switch]$PrivateSourceSync)

    $findings = New-Object Collections.Generic.List[object]
    $revision = if ($BaseCommit) { "${BaseCommit}..${TipCommit}" } else { $TipCommit }
    foreach ($commit in (Invoke-External 'git' @('-C', $RepoRoot, 'rev-list', '--reverse', $revision)).Output) {
        foreach ($finding in @(Test-CommitMetadata $RepoRoot ([string]$commit) $Rules)) { $findings.Add($finding) }
        $commitLine = ((Invoke-External 'git' @('-C', $RepoRoot, 'rev-list', '--parents', '--max-count=1', [string]$commit)).Output | Select-Object -First 1).Trim()
        $commitParts = @($commitLine.Split([char[]]@(' '), [StringSplitOptions]::RemoveEmptyEntries))
        $parentCommit = if ($commitParts.Count -gt 1) { [string]$commitParts[1] } else { $null }
        $scan = Test-CommitSnapshot $RepoRoot ([string]$commit) $Config $Rules $TempParent -HistoryAudit -SourceAudit -InheritedFromCommit $parentCommit -PrivateSourceSync:$PrivateSourceSync
        foreach ($finding in $scan.Findings) { $findings.Add($finding) }
    }
    return @($findings | Sort-Object Rule, Path -Unique)
}

function Assert-NoRedirectedTree {
    param([string]$Root, [string]$Label)

    Assert-NoRedirectedPath $Root $Label | Out-Null
    $pending = New-Object Collections.Generic.Stack[string]
    $pending.Push((Get-NormalizedFullPath $Root))
    while ($pending.Count -gt 0) {
        $directory = $pending.Pop()
        foreach ($item in @(Get-ChildItem -Force -LiteralPath $directory)) {
            if (Test-RedirectedItem $item) {
                throw "$Label contains a redirected path component."
            }
            if ($item.PSIsContainer) { $pending.Push($item.FullName) }
        }
    }
}

function Remove-SafeDirectory {
    param([string]$Path, [string]$ExpectedParent, [string]$Label)

    Assert-ContainedPath $Path $ExpectedParent $Label | Out-Null
    Assert-NoRedirectedTree $Path $Label
    Assert-ContainedPath $Path $ExpectedParent $Label | Out-Null
    Remove-Item -LiteralPath $Path -Force -Recurse
}

function Get-StagingOwnerPath {
    param([string]$StagingRoot)
    return Join-Path $StagingRoot '.git/codex-staging-owner.json'
}

function Write-StagingOwner {
    param([string]$StagingRoot, [string]$BoundStagingRoot, [string]$SourceKey, [string]$Repository)

    $gitDirectory = Join-Path $StagingRoot '.git'
    $ownerPath = Get-StagingOwnerPath $StagingRoot
    $content = ([ordered]@{
        format = $StagingOwnerFormat
        source_key = $SourceKey
        repository = $Repository
        staging_path = (Get-NormalizedFullPath $BoundStagingRoot)
    } | ConvertTo-Json) + "`n"
    Write-Utf8Atomically $ownerPath $content $gitDirectory 'Staging ownership record'
}

function Assert-StagingRepositoryShape {
    param([string]$StagingRoot, [string]$StagingParent, [string]$Repository)

    Assert-ContainedPath $StagingRoot $StagingParent 'Backup staging path' | Out-Null
    Assert-NoRedirectedPath $StagingRoot 'Backup staging path' | Out-Null
    if (-not (Test-Path -LiteralPath $StagingRoot -PathType Container)) { throw "Backup staging path is missing." }
    $gitDirectory = Join-Path $StagingRoot '.git'
    if (-not (Test-Path -LiteralPath $gitDirectory -PathType Container)) {
        throw "Backup staging must use its own Git directory."
    }
    Assert-NoRedirectedPath $gitDirectory 'Backup staging Git directory' | Out-Null
    $topLevel = ((Invoke-External 'git' @('-C', $StagingRoot, 'rev-parse', '--show-toplevel')).Output | Select-Object -First 1).Trim()
    $absoluteGit = ((Invoke-External 'git' @('-C', $StagingRoot, 'rev-parse', '--absolute-git-dir')).Output | Select-Object -First 1).Trim()
    if (-not (Test-SamePath $topLevel $StagingRoot) -or -not (Test-SamePath $absoluteGit $gitDirectory)) {
        throw "Backup staging repository escapes its owned path."
    }
    $originState = Get-RemoteUrlState $StagingRoot 'origin'
    if (-not (Test-RemoteMatchesRepository $originState $Repository)) {
        throw "Backup staging origin does not match the canonical backup repository."
    }
}

function Assert-OwnedStaging {
    param([string]$StagingRoot, [string]$StagingParent, [string]$SourceKey, [string]$Repository)

    Assert-StagingRepositoryShape $StagingRoot $StagingParent $Repository
    $ownerPath = Get-StagingOwnerPath $StagingRoot
    if (-not (Test-Path -LiteralPath $ownerPath -PathType Leaf)) { throw "Backup staging ownership is missing." }
    Assert-NoRedirectedPath $ownerPath 'Staging ownership record' | Out-Null
    try { $owner = Get-Content -Raw -LiteralPath $ownerPath | ConvertFrom-Json }
    catch { throw "Backup staging ownership is invalid." }
    if ([int]$owner.format -ne $StagingOwnerFormat -or [string]$owner.source_key -cne $SourceKey -or
        -not (Test-SameRepository ([string]$owner.repository) $Repository) -or
        -not (Test-SamePath ([string]$owner.staging_path) $StagingRoot)) {
        throw "Backup staging ownership does not match this source and repository."
    }
}

function Initialize-StagingOwnership {
    param([string]$StagingRoot, [string]$StagingParent, [string]$SourceKey, [string]$Repository)

    Assert-StagingRepositoryShape $StagingRoot $StagingParent $Repository
    $ownerPath = Get-StagingOwnerPath $StagingRoot
    if (Test-Path -LiteralPath $ownerPath) {
        Assert-OwnedStaging $StagingRoot $StagingParent $SourceKey $Repository
        return
    }
    $status = (Invoke-External 'git' @('-C', $StagingRoot, 'status', '--porcelain')).Output
    if ($status.Count -gt 0) {
        throw "Unowned backup staging contains changes and will not be adopted or cleaned."
    }
    Write-StagingOwner $StagingRoot $StagingRoot $SourceKey $Repository
    Assert-OwnedStaging $StagingRoot $StagingParent $SourceKey $Repository
}

function Assert-SafeStagingWorktree {
    param([string]$StagingRoot)

    foreach ($item in @(Get-ChildItem -Force -LiteralPath $StagingRoot | Where-Object { $_.Name -ne '.git' })) {
        Assert-ContainedPath $item.FullName $StagingRoot 'Backup staging child' | Out-Null
        if ($item.PSIsContainer) { Assert-NoRedirectedTree $item.FullName 'Backup staging child' }
        else { Assert-NoRedirectedPath $item.FullName 'Backup staging child' | Out-Null }
    }
}

function Clear-OwnedStagingWorktree {
    param([string]$StagingRoot, [string]$StagingParent, [string]$SourceKey, [string]$Repository)

    Assert-OwnedStaging $StagingRoot $StagingParent $SourceKey $Repository
    Invoke-TestFault 'backup-before-staging-cleanup' $StagingRoot
    Assert-OwnedStaging $StagingRoot $StagingParent $SourceKey $Repository
    Assert-SafeStagingWorktree $StagingRoot
    $head = Invoke-External 'git' @('-C', $StagingRoot, 'rev-parse', '--verify', 'HEAD^{commit}') -AllowFailure
    if ($head.ExitCode -eq 0) {
        Invoke-External 'git' @('-C', $StagingRoot, 'restore', '--staged', '--worktree', '--source=HEAD', '--', '.') | Out-Null
    } else {
        Invoke-External 'git' @('-C', $StagingRoot, 'read-tree', '--empty') | Out-Null
    }
    foreach ($item in @(Get-ChildItem -Force -LiteralPath $StagingRoot | Where-Object { $_.Name -ne '.git' })) {
        Assert-OwnedStaging $StagingRoot $StagingParent $SourceKey $Repository
        Assert-ContainedPath $item.FullName $StagingRoot 'Backup staging child' | Out-Null
        if ($item.PSIsContainer) { Assert-NoRedirectedTree $item.FullName 'Backup staging child' }
        else { Assert-NoRedirectedPath $item.FullName 'Backup staging child' | Out-Null }
        Assert-ContainedPath $item.FullName $StagingRoot 'Backup staging child' | Out-Null
        Remove-Item -LiteralPath $item.FullName -Force -Recurse
    }
}

function New-OwnedStaging {
    param(
        [string]$StagingRoot,
        [string]$StagingParent,
        [string]$SourceKey,
        [string]$Repository,
        [string]$TargetUrl,
        [object]$RemoteMain
    )

    $constructionRoot = Join-Path $StagingParent ('.codex-backup-new-' + [Guid]::NewGuid().ToString('N'))
    Assert-ContainedPath $constructionRoot $StagingParent 'Backup construction path' | Out-Null
    New-Item -ItemType Directory -Path $constructionRoot | Out-Null
    try {
        Assert-NoRedirectedPath $constructionRoot 'Backup construction path' | Out-Null
        Invoke-External 'git' @('-C', $constructionRoot, 'init', '-b', 'main') | Out-Null
        Invoke-External 'git' @('-C', $constructionRoot, 'remote', 'add', 'origin', $TargetUrl) | Out-Null
        Write-StagingOwner $constructionRoot $constructionRoot $SourceKey $Repository
        if ($RemoteMain.Exists) {
            $captureRef = "refs/codex/github-backup/$([Guid]::NewGuid().ToString('N'))"
            Invoke-External 'git' @('-C', $constructionRoot, 'fetch', '--no-tags', '--no-write-fetch-head', $TargetUrl, "$($RemoteMain.Ref):$captureRef") | Out-Null
            $captured = ((Invoke-External 'git' @('-C', $constructionRoot, 'rev-parse', '--verify', "${captureRef}^{commit}")).Output | Select-Object -First 1).Trim()
            if ($captured -cne $RemoteMain.Sha) { throw "Backup branch changed while staging was initialized." }
            Invoke-External 'git' @('-C', $constructionRoot, 'reset', '--hard', $captured) | Out-Null
            Invoke-External 'git' @('-C', $constructionRoot, 'update-ref', '-d', $captureRef) | Out-Null
        }
        Assert-StagingRepositoryShape $constructionRoot $StagingParent $Repository
        Write-StagingOwner $constructionRoot $StagingRoot $SourceKey $Repository
        Assert-NoRedirectedPath $StagingParent 'Backup staging parent' | Out-Null
        if (Test-Path -LiteralPath $StagingRoot) { throw "Backup staging path appeared during initialization." }
        Move-Item -LiteralPath $constructionRoot -Destination $StagingRoot
        Assert-OwnedStaging $StagingRoot $StagingParent $SourceKey $Repository
    }
    catch {
        if (Test-Path -LiteralPath $constructionRoot) {
            try {
                $ownerPath = Get-StagingOwnerPath $constructionRoot
                if (Test-Path -LiteralPath $ownerPath) {
                    Write-StagingOwner $constructionRoot $constructionRoot $SourceKey $Repository
                    Assert-OwnedStaging $constructionRoot $StagingParent $SourceKey $Repository
                    Remove-SafeDirectory $constructionRoot $StagingParent 'Backup construction path'
                }
            } catch { }
        }
        throw
    }
}

foreach ($required in @('git', 'tar')) {
    if (-not (Get-Command $required -ErrorAction SilentlyContinue)) { throw "Required command is unavailable: $required" }
}
$resolvedProjectRoot = Resolve-SafeProjectRoot $ProjectRoot
if ($resolvedProjectRoot -match '(?i)[\\/](OneDrive|Dropbox|Google Drive)[\\/]') {
    Write-Warning "The Git root is cloud-synchronized. Use GitHub, not folder synchronization, between computers."
}

if ($CandidateCommit) {
    if (-not $ScanOnly) { throw 'CandidateCommit is available only with ScanOnly.' }
    if ($SourceCommit -or $AuditSourceHistory -or $Repository -or $HistoryBaseCommit -or $FullSourceHistory) {
        throw 'CandidateCommit cannot be combined with SourceCommit, AuditSourceHistory, Repository, HistoryBaseCommit, or FullSourceHistory.'
    }
}
elseif ($HistoryBaseCommit) {
    if (-not $AuditSourceHistory -or -not $SourceCommit -or $FullSourceHistory) {
        throw 'HistoryBaseCommit requires AuditSourceHistory and an exact SourceCommit, and cannot use FullSourceHistory.'
    }
}
elseif ($FullSourceHistory -and -not $AuditSourceHistory) {
    throw 'FullSourceHistory requires AuditSourceHistory.'
}

$sourceHeadState = if ($CandidateCommit) {
    Get-SourceHeadState $resolvedProjectRoot -AllowUnborn
} else {
    Get-SourceHeadState $resolvedProjectRoot
}
$sourceHead = [string]$sourceHeadState.Commit
$candidateCommitId = $null
$candidateIndexTree = $null
$validatedHistoryBase = $null
if ($CandidateCommit) {
    $candidateCommitId = Resolve-ExactCommit $resolvedProjectRoot $CandidateCommit 'CandidateCommit'
    $candidateIndexTree = ((Invoke-External 'git' @('-C', $resolvedProjectRoot, 'write-tree')).Output | Select-Object -First 1).Trim()
    if ($candidateIndexTree -notmatch '^[0-9a-fA-F]{40,64}$') { throw 'Unable to resolve the exact staged source tree.' }
    $candidateTree = ((Invoke-External 'git' @('-C', $resolvedProjectRoot, 'rev-parse', '--verify', "${candidateCommitId}^{tree}")).Output | Select-Object -First 1).Trim()
    if ($candidateTree -cne $candidateIndexTree) {
        throw 'CandidateCommit must contain the exact current staged source tree.'
    }
    $candidateLine = ((Invoke-External 'git' @('-C', $resolvedProjectRoot, 'rev-list', '--parents', '--max-count=1', $candidateCommitId)).Output | Select-Object -First 1).Trim()
    $candidateParts = @($candidateLine.Split([char[]]@(' '), [StringSplitOptions]::RemoveEmptyEntries))
    $candidateParents = @(if ($candidateParts.Count -gt 1) { $candidateParts[1..($candidateParts.Count - 1)] })
    if ($sourceHead) {
        if ($candidateParents.Count -ne 1 -or $candidateParents[0] -cne $sourceHead) {
            throw 'CandidateCommit must have the exact current source HEAD as its sole parent.'
        }
    }
    elseif ($candidateParents.Count -ne 0) {
        throw 'CandidateCommit for an unborn source HEAD must have no parent.'
    }
}
else {
    if ($SourceCommit) {
        $resolvedSourceCommit = Resolve-ExactCommit $resolvedProjectRoot $SourceCommit 'SourceCommit'
        if ($sourceHead -cne $resolvedSourceCommit) {
            throw 'SourceCommit must be the exact current immutable source HEAD.'
        }
    }
    if ($HistoryBaseCommit) {
        $validatedHistoryBase = Resolve-ExactCommit $resolvedProjectRoot $HistoryBaseCommit 'HistoryBaseCommit'
        $ancestor = Invoke-External 'git' @('-C', $resolvedProjectRoot, 'merge-base', '--is-ancestor', $validatedHistoryBase, $sourceHead) -AllowFailure
        if ($ancestor.ExitCode -ne 0) {
            throw 'HistoryBaseCommit must be an ancestor of the current SourceCommit.'
        }
    }
}
$projectName = Split-Path -Leaf $resolvedProjectRoot
$projectIdentity = Get-PathIdentityKey $resolvedProjectRoot
$projectKey = (Get-Sha256Text $projectIdentity).Substring(0, 16)
$localData = Get-LocalStateRoot
$auditRoot = Get-NormalizedFullPath (Join-Path $localData 'Codex/github-backup-audits')
Assert-ContainedPath $auditRoot $localData 'Audit path' | Out-Null
Assert-NoRedirectedPath $localData 'Local application data path' | Out-Null
New-Item -ItemType Directory -Force -Path $auditRoot | Out-Null
Assert-NoRedirectedPath $auditRoot 'Audit path' | Out-Null
$auditPath = Get-NormalizedFullPath (Join-Path $auditRoot "${projectName}-${projectKey}.json")
$sourceHistoryCachePath = Get-NormalizedFullPath (Join-Path $auditRoot "${projectName}-${projectKey}-source-history.json")
Assert-ContainedPath $auditPath $auditRoot 'Audit manifest' | Out-Null
Assert-ContainedPath $sourceHistoryCachePath $auditRoot 'Source history cache' | Out-Null

$config = if ($CandidateCommit -and -not $sourceHead) {
    Get-DefaultConfig
} else {
    Get-CommittedConfig $resolvedProjectRoot $ConfigPath $sourceHead
}
$rules = Get-ScanRules $config.confidential_patterns
$effectiveRules = if ($PrivateSourceSync) {
    [object[]]@($rules | Where-Object { [string]$_.Category -ne 'operational-metadata' })
} else {
    $rules
}
$scriptHash = (Get-FileHash -Algorithm SHA256 $MyInvocation.MyCommand.Path).Hash
$policyHash = Get-Sha256Text (($config | ConvertTo-Json -Depth 8 -Compress) + '|' + $scriptHash)
$tempCandidate = Get-NormalizedFullPath ([IO.Path]::GetTempPath())
$tempParent = Resolve-PhysicalExistingPath $tempCandidate
Assert-NoRedirectedPath $tempParent 'Temporary path' | Out-Null
$tempRoot = Get-NormalizedFullPath (Join-Path $tempParent ("codex-github-backup-" + [Guid]::NewGuid().ToString('N')))
Assert-ContainedPath $tempRoot $tempParent 'Temporary backup path' | Out-Null
New-Item -ItemType Directory -Force -Path $tempRoot | Out-Null
Assert-NoRedirectedPath $tempRoot 'Temporary backup path' | Out-Null
$lockStream = $null
$lockPath = $null

try {
    if ($candidateCommitId) {
        $candidateSnapshot = Test-CommitSnapshot $resolvedProjectRoot $candidateCommitId $config $effectiveRules $tempRoot -HistoryAudit -SourceAudit -InheritedFromCommit $sourceHead -PrivateSourceSync:$PrivateSourceSync
        $candidateFindings = New-Object Collections.Generic.List[object]
        foreach ($finding in @(Test-CommitMetadata $resolvedProjectRoot $candidateCommitId $effectiveRules)) { $candidateFindings.Add($finding) }
        foreach ($finding in @($candidateSnapshot.Findings)) { $candidateFindings.Add($finding) }
        $candidateScan = [pscustomobject]@{
            Included = $candidateSnapshot.Included
            Excluded = $candidateSnapshot.Excluded
            Findings = [object[]]@($candidateFindings | Sort-Object Rule, Path -Unique)
            SnapshotRoot = $candidateSnapshot.SnapshotRoot
        }
        Invoke-TestFault 'backup-after-source-audit' $resolvedProjectRoot
        Assert-CandidateSourceState $resolvedProjectRoot $sourceHeadState $candidateIndexTree
        Write-AuditManifest $auditPath $auditRoot $candidateCommitId $candidateScan
        if ($candidateScan.Findings.Count -gt 0) {
            Write-Host 'Candidate source audit blocked. Matched values are not displayed:'
            $candidateScan.Findings | ForEach-Object { Write-Host "  [$($_.Rule)] $($_.Path)" }
            throw "Candidate source commit is not safe to record. Local audit: $auditPath"
        }
        Write-Host "Candidate source audit passed: $($candidateScan.Included.Count) included."
        return
    }

    $sourceScan = if ($validatedHistoryBase) {
        Test-CommitSnapshot $resolvedProjectRoot $sourceHead $config $effectiveRules $tempRoot -InheritedFromCommit $validatedHistoryBase -PrivateSourceSync:$PrivateSourceSync -KeepSnapshot
    } else {
        Test-CommitSnapshot $resolvedProjectRoot $sourceHead $config $effectiveRules $tempRoot -PrivateSourceSync:$PrivateSourceSync -KeepSnapshot
    }
    if ($validatedHistoryBase -or $FullSourceHistory) { Assert-SourceHead $resolvedProjectRoot $sourceHead }
    Write-AuditManifest $auditPath $auditRoot $sourceHead $sourceScan
    if ($sourceScan.Findings.Count -gt 0) {
        Write-Host "GitHub backup blocked. Matched values are not displayed:"
        $sourceScan.Findings | ForEach-Object { Write-Host "  [$($_.Rule)] $($_.Path)" }
        throw "Sanitized snapshot verification failed. Local audit: $auditPath"
    }
    Write-Host "Sanitized snapshot passed: $($sourceScan.Included.Count) included, $($sourceScan.Excluded.Count) excluded."
    if ($AuditSourceHistory) {
        $historyBase = $validatedHistoryBase
        if (-not $historyBase -and -not $FullSourceHistory) {
            $cachedSourceAudit = if (Test-Path -LiteralPath $sourceHistoryCachePath) {
                Assert-NoRedirectedPath $sourceHistoryCachePath 'Source history cache' | Out-Null
                try { Get-Content -Raw -LiteralPath $sourceHistoryCachePath | ConvertFrom-Json } catch { $null }
            } else { $null }
            if ($cachedSourceAudit -and $cachedSourceAudit.policy_hash -ceq $policyHash -and
                [string]$cachedSourceAudit.tip -match '^[0-9a-fA-F]{40,64}$') {
                $tipExists = Invoke-External 'git' @('-C', $resolvedProjectRoot, 'cat-file', '-e', "$($cachedSourceAudit.tip)^{commit}") -AllowFailure
                $tipIsAncestor = if ($tipExists.ExitCode -eq 0) {
                    Invoke-External 'git' @('-C', $resolvedProjectRoot, 'merge-base', '--is-ancestor', [string]$cachedSourceAudit.tip, $sourceHead) -AllowFailure
                } else { $null }
                if ($tipIsAncestor -and $tipIsAncestor.ExitCode -eq 0) { $historyBase = [string]$cachedSourceAudit.tip }
            }
        }
        $historyFindings = @(Test-SourceHistory $resolvedProjectRoot $sourceHead $config $effectiveRules $tempRoot $historyBase -PrivateSourceSync:$PrivateSourceSync)
        if ($validatedHistoryBase -or $FullSourceHistory) { Assert-SourceHead $resolvedProjectRoot $sourceHead }
        if ($historyFindings.Count -gt 0) {
            Write-Host "Source history audit blocked. Matched values are not displayed:"
            $historyFindings | ForEach-Object { Write-Host "  [$($_.Rule)] $($_.Path)" }
            throw "Source history is not safe to push. Local audit: $auditPath"
        }
        Invoke-TestFault 'backup-after-source-history-audit' $resolvedProjectRoot
        Assert-SourceHead $resolvedProjectRoot $sourceHead
        if (-not $validatedHistoryBase) {
            $sourceCache = (([ordered]@{ tip = $sourceHead; policy_hash = $policyHash } | ConvertTo-Json) + "`n")
            Write-Utf8Atomically $sourceHistoryCachePath $sourceCache $auditRoot 'Source history cache'
        }
        Assert-SourceHead $resolvedProjectRoot $sourceHead
        if ($historyBase) { Write-Host "Incremental source history audit passed from $historyBase." }
        else { Write-Host "Full source history audit passed." }
    }
    Invoke-TestFault 'backup-after-source-audit' $resolvedProjectRoot
    Assert-SourceHead $resolvedProjectRoot $sourceHead
    if ($ScanOnly) { return }

    if (-not (Get-Command gh -ErrorAction SilentlyContinue)) { throw "GitHub CLI is unavailable; backup is pending." }
    if ((Invoke-External 'gh' @('auth', 'status') -AllowFailure).ExitCode -ne 0) { throw "GitHub authentication is unavailable; run gh auth login." }

    $targetRepository = if ($Repository) { $Repository } elseif ($config.repository) { [string]$config.repository } else { $null }
    $autoSelected = -not $targetRepository
    if ($autoSelected) {
        $loginResult = Invoke-External 'gh' @('api', 'user', '--jq', '.login')
        $login = (($loginResult.Output | Select-Object -First 1)).Trim()
        if ($login -notmatch '^[A-Za-z0-9-]+$') { throw "Unable to determine the authenticated GitHub owner." }
        $targetRepository = "$login/${projectName}-sanitized-backup-v2"
        $repoInfo = Resolve-GitHubRepository $targetRepository -AllowMissing
        if (-not $repoInfo) {
            Invoke-External 'gh' @('repo', 'create', $targetRepository, '--private') | Out-Null
            $repoInfo = Resolve-GitHubRepository $targetRepository
        }
    } else {
        $repoInfo = Resolve-GitHubRepository $targetRepository
    }
    $targetRepository = $repoInfo.Repository
    $targetUrl = [string]$repoInfo.Url
    $remoteMain = Get-RemoteBranchTip $targetUrl 'main'
    if ($autoSelected -and -not $repoInfo.IsEmpty -and -not $remoteMain.Exists) {
        throw "The automatically selected same-name backup repository is nonempty without an auditable main branch."
    }

    $backupKey = (Get-Sha256Text ("${projectIdentity}|" + $targetRepository.ToLowerInvariant())).Substring(0, 16)
    $backupRoot = Get-NormalizedFullPath (Join-Path $localData "Codex/github-backups/${projectName}-${backupKey}")
    $backupParent = Get-NormalizedFullPath (Split-Path -Parent $backupRoot)
    Assert-ContainedPath $backupRoot $backupParent 'Backup staging path' | Out-Null
    Assert-NoRedirectedPath $backupParent 'Backup staging parent' | Out-Null
    New-Item -ItemType Directory -Force -Path $backupParent | Out-Null
    Assert-NoRedirectedPath $backupParent 'Backup staging parent' | Out-Null
    $lockPath = Get-NormalizedFullPath "${backupRoot}.lock"
    Assert-ContainedPath $lockPath $backupParent 'Backup staging lock' | Out-Null
    if (Test-Path -LiteralPath $lockPath) { Assert-NoRedirectedPath $lockPath 'Backup staging lock' | Out-Null }
    try { $lockStream = [IO.File]::Open($lockPath, 'OpenOrCreate', 'ReadWrite', 'None') }
    catch { throw "Another backup run owns the staging lock: $lockPath" }

    if (Test-Path -LiteralPath $backupRoot) {
        Initialize-StagingOwnership $backupRoot $backupParent $projectKey $targetRepository
    } else {
        New-OwnedStaging $backupRoot $backupParent $projectKey $targetRepository $targetUrl $remoteMain
    }
    Assert-OwnedStaging $backupRoot $backupParent $projectKey $targetRepository

    $existingHead = Invoke-External 'git' @('-C', $backupRoot, 'rev-parse', '--verify', 'HEAD^{commit}') -AllowFailure
    if ($existingHead.ExitCode -eq 0) {
        $existingTip = (($existingHead.Output | Select-Object -First 1)).Trim()
        $existingFindings = @(Test-BackupHistory $backupRoot $existingTip $config $rules $tempRoot)
        if ($existingFindings.Count -gt 0) {
            Write-Host "Existing local backup history failed audit:"
            $existingFindings | ForEach-Object { Write-Host "  [$($_.Rule)] $($_.Path)" }
            throw "Refusing to clean or extend unverified local backup history."
        }
    }
    $status = (Invoke-External 'git' @('-C', $backupRoot, 'status', '--porcelain')).Output
    if ($status.Count -gt 0) { Clear-OwnedStagingWorktree $backupRoot $backupParent $projectKey $targetRepository }

    if ($remoteMain.Exists) {
        $captureRef = "refs/codex/github-backup/$([Guid]::NewGuid().ToString('N'))"
        try {
            Invoke-External 'git' @('-C', $backupRoot, 'fetch', '--no-tags', '--no-write-fetch-head', $targetUrl, "$($remoteMain.Ref):$captureRef") | Out-Null
            $capturedRemoteTip = ((Invoke-External 'git' @('-C', $backupRoot, 'rev-parse', '--verify', "${captureRef}^{commit}")).Output | Select-Object -First 1).Trim()
            if ($capturedRemoteTip -cne $remoteMain.Sha) { throw "Backup branch changed while it was being captured." }
            $remoteFindings = @(Test-BackupHistory $backupRoot $capturedRemoteTip $config $rules $tempRoot)
            if ($remoteFindings.Count -gt 0) {
                Write-Host "Existing remote backup history failed audit:"
                $remoteFindings | ForEach-Object { Write-Host "  [$($_.Rule)] $($_.Path)" }
                throw "Refusing to extend unverified remote backup history."
            }
            $currentBranch = ((Invoke-External 'git' @('-C', $backupRoot, 'branch', '--show-current')).Output | Select-Object -First 1).Trim()
            if ($currentBranch -cne 'main') {
                $mainExists = Invoke-External 'git' @('-C', $backupRoot, 'show-ref', '--verify', '--quiet', 'refs/heads/main') -AllowFailure
                if ($mainExists.ExitCode -eq 0) { Invoke-External 'git' @('-C', $backupRoot, 'checkout', 'main') | Out-Null }
                else { Invoke-External 'git' @('-C', $backupRoot, 'checkout', '-b', 'main', $capturedRemoteTip) | Out-Null }
            }
            Invoke-External 'git' @('-C', $backupRoot, 'merge', '--ff-only', $capturedRemoteTip) | Out-Null
        }
        finally { Invoke-External 'git' @('-C', $backupRoot, 'update-ref', '-d', $captureRef) -AllowFailure | Out-Null }
    }
    $currentBranch = ((Invoke-External 'git' @('-C', $backupRoot, 'branch', '--show-current')).Output | Select-Object -First 1).Trim()
    if ($currentBranch -cne 'main') { throw "Owned backup staging must remain on the exact main branch." }

    Clear-OwnedStagingWorktree $backupRoot $backupParent $projectKey $targetRepository
    Assert-NoRedirectedTree $sourceScan.SnapshotRoot 'Sanitized snapshot'
    Get-ChildItem -Force -LiteralPath $sourceScan.SnapshotRoot | ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination $backupRoot -Force -Recurse }
    $backupMarkerPath = Join-Path $backupRoot '.codex-sanitized-backup.json'
    $backupMarker = (([ordered]@{ format = $BackupFormat; source_project = $projectName } | ConvertTo-Json) + "`n")
    Write-Utf8Atomically $backupMarkerPath $backupMarker $backupRoot 'Backup marker'

    Invoke-External 'git' @('-C', $backupRoot, 'config', 'user.name', $BackupAuthorName) | Out-Null
    Invoke-External 'git' @('-C', $backupRoot, 'config', 'user.email', $BackupAuthorEmail) | Out-Null
    Assert-SourceHead $resolvedProjectRoot $sourceHead
    Invoke-External 'git' @('-C', $backupRoot, 'add', '--all') | Out-Null
    Invoke-External 'git' @('-C', $backupRoot, 'diff', '--cached', '--check') | Out-Null

    $staged = Invoke-External 'git' @('-C', $backupRoot, 'diff', '--cached', '--quiet') -AllowFailure
    if ($staged.ExitCode -eq 1) {
        Assert-SourceHead $resolvedProjectRoot $sourceHead
        Invoke-External 'git' @('-C', $backupRoot, 'commit', '-m', "Update sanitized backup for $projectName") | Out-Null
    } elseif ($staged.ExitCode -ne 0) {
        throw "Unable to determine staged backup changes."
    }

    Assert-SourceHead $resolvedProjectRoot $sourceHead
    $localTip = ((Invoke-External 'git' @('-C', $backupRoot, 'rev-parse', '--verify', 'HEAD^{commit}')).Output | Select-Object -First 1).Trim()
    $allLocalFindings = @(Test-BackupHistory $backupRoot $localTip $config $rules $tempRoot)
    if ($allLocalFindings.Count -gt 0) {
        Write-Host "Local backup history failed final audit:"
        $allLocalFindings | ForEach-Object { Write-Host "  [$($_.Rule)] $($_.Path)" }
        throw "Refusing to push unverified local backup history."
    }
    Invoke-TestFault 'backup-after-local-history-audit' $backupRoot
    Assert-SourceHead $resolvedProjectRoot $sourceHead
    Assert-OwnedStaging $backupRoot $backupParent $projectKey $targetRepository
    $currentLocalTip = ((Invoke-External 'git' @('-C', $backupRoot, 'rev-parse', '--verify', 'HEAD^{commit}')).Output | Select-Object -First 1).Trim()
    if ($currentLocalTip -cne $localTip) { throw "Backup staging HEAD changed after its final audit. No push was made." }

    $repoBeforePush = Resolve-GitHubRepository $targetRepository
    if (-not (Test-SameRepository $repoBeforePush.Repository $targetRepository)) { throw "Backup repository identity changed. No push was made." }
    $currentRemote = Get-RemoteBranchTip ([string]$repoBeforePush.Url) 'main'
    if ($currentRemote.Exists -ne $remoteMain.Exists -or ($currentRemote.Exists -and $currentRemote.Sha -cne $remoteMain.Sha)) {
        throw "Backup branch changed after validation. No push was made."
    }
    if ($currentRemote.Exists) {
        $ancestor = Invoke-External 'git' @('-C', $backupRoot, 'merge-base', '--is-ancestor', $currentRemote.Sha, $localTip) -AllowFailure
        if ($ancestor.ExitCode -ne 0) { throw "Backup history is diverged or behind. No push was made." }
    }
    Assert-SourceHead $resolvedProjectRoot $sourceHead
    Assert-OwnedStaging $backupRoot $backupParent $projectKey $targetRepository
    $currentLocalTip = ((Invoke-External 'git' @('-C', $backupRoot, 'rev-parse', '--verify', 'HEAD^{commit}')).Output | Select-Object -First 1).Trim()
    if ($currentLocalTip -cne $localTip) { throw "Backup staging HEAD changed before push. No push was made." }
    if (-not $currentRemote.Exists -or $localTip -cne $currentRemote.Sha) {
        Invoke-External 'git' @('-C', $backupRoot, 'push', [string]$repoBeforePush.Url, "${localTip}:refs/heads/main") | Out-Null
    }
    Assert-SourceHead $resolvedProjectRoot $sourceHead

    $auditCachePath = Join-Path $backupRoot '.git/codex-history-audit.json'
    $backupCache = (([ordered]@{ tip = $localTip; policy_hash = $policyHash } | ConvertTo-Json) + "`n")
    Write-Utf8Atomically $auditCachePath $backupCache (Join-Path $backupRoot '.git') 'Backup history cache'

    $sourceRemoteNames = [string[]]@((Invoke-External 'git' @('-C', $resolvedProjectRoot, 'remote')).Output | ForEach-Object { [string]$_ })
    $sourceRemoteExists = $false
    foreach ($name in $sourceRemoteNames) { if ($name -ceq $RemoteName) { $sourceRemoteExists = $true; break } }
    if ($sourceRemoteExists -and -not (Test-RemoteMatchesRepository (Get-RemoteUrlState $resolvedProjectRoot $RemoteName) $targetRepository)) {
        $legacyName = "${RemoteName}-legacy"
        $suffix = 1
        while ($sourceRemoteNames -ccontains $legacyName) {
            $suffix++
            $legacyName = "${RemoteName}-legacy-${suffix}"
        }
        Invoke-External 'git' @('-C', $resolvedProjectRoot, 'remote', 'rename', $RemoteName, $legacyName) | Out-Null
        Disable-RemotePush $resolvedProjectRoot $legacyName
        $sourceRemoteExists = $false
    }
    if (-not $sourceRemoteExists) { Invoke-External 'git' @('-C', $resolvedProjectRoot, 'remote', 'add', $RemoteName, $targetUrl) | Out-Null }

    $sourceRemoteNames = [string[]]@((Invoke-External 'git' @('-C', $resolvedProjectRoot, 'remote')).Output | ForEach-Object { [string]$_ })
    $legacyPattern = '^' + [Regex]::Escape($RemoteName) + '-legacy(?:-[0-9]+)?$'
    foreach ($name in $sourceRemoteNames) {
        if ($name -cmatch $legacyPattern) {
            $legacyState = Get-RemoteUrlState $resolvedProjectRoot $name
            if ($legacyState.Push.Count -ne 1 -or $legacyState.Push[0] -cne 'DISABLED') {
                Disable-RemotePush $resolvedProjectRoot $name
            }
        }
    }

    Write-Host "Isolated sanitized backup is current at $targetRepository. Local audit: $auditPath"
}
finally {
    if ($lockStream) { $lockStream.Dispose() }
    if (Test-Path -LiteralPath $tempRoot) { Remove-SafeDirectory $tempRoot $tempParent 'Temporary backup path' }
}
