# new-project-setup:managed-helper:v1
#requires -Version 5.1

[CmdletBinding()]
param(
    [string]$ProjectRoot = ".",
    [string]$StatePath = ".codex/new-project-setup.json",
    [string]$Repository,
    [string]$RemoteName,
    [switch]$Initialize,
    [switch]$ScanOnly,
    [switch]$PublicReadiness,
    [switch]$PreCommit,
    [string]$CommitMessage,
    [switch]$BatchEligible,
    [switch]$RecoverLegacyAncestry,
    [string]$ExpectedLegacyHead
)

$ErrorActionPreference = "Stop"
if ($PSVersionTable.PSEdition -eq 'Core' -and $PSVersionTable.PSVersion.Major -lt 7) {
    throw 'PowerShell 7 or Windows PowerShell 5.1 is required.'
}
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
    finally { $ErrorActionPreference = $previousPreference }
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
    if (-not (Test-SamePath $candidate $resolved)) {
        throw "ProjectRoot must not be a redirected link."
    }
    $gitRoot = ((Invoke-External 'git' @('-C', $resolved, 'rev-parse', '--show-toplevel')).Output | Select-Object -First 1).Trim()
    if (-not (Test-SamePath $resolved $gitRoot)) {
        throw "ProjectRoot must be the Git repository root."
    }
    Assert-NoRedirectedPath $resolved 'ProjectRoot' | Out-Null
    return Get-NormalizedFullPath $resolved
}

function Get-SafeRelativePath {
    param([string]$RepoRoot, [string]$RelativePath, [string]$Label)

    if ([string]::IsNullOrWhiteSpace($RelativePath) -or [IO.Path]::IsPathRooted($RelativePath)) {
        throw "$Label must be project-relative."
    }
    $normalized = $RelativePath.Replace('\', '/')
    if ($normalized.StartsWith('/') -or $normalized -match '(^|/)\.\.(/|$)' -or $normalized.IndexOf([char]0) -ge 0) {
        throw "$Label must be project-relative."
    }
    if ([Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT -and $normalized -match ':') {
        throw "$Label must not use an alternate data stream."
    }
    $full = Get-NormalizedFullPath (Join-Path $RepoRoot $normalized.Replace('/', [IO.Path]::DirectorySeparatorChar))
    Assert-ContainedPath $full $RepoRoot $Label | Out-Null
    return [pscustomobject]@{ Relative = $normalized; FullPath = $full }
}

function Invoke-TestFault {
    param([string]$Name, [string]$ContextPath)

    if ([string]$env:NEW_PROJECT_SETUP_TEST_FAULT -cne $Name) { return }
    $action = [string]$env:NEW_PROJECT_SETUP_TEST_ACTION
    if ([string]::IsNullOrWhiteSpace($action)) {
        throw "Injected test fault at $Name."
    }
    if (-not [IO.Path]::IsPathRooted($action) -or [IO.Path]::GetExtension($action) -ine '.ps1') {
        throw "The injected test action must be an absolute local PowerShell script path."
    }
    $actionPath = Assert-NoRedirectedPath $action 'Injected test action'
    if (-not (Test-Path -LiteralPath $actionPath -PathType Leaf)) {
        throw "The injected test action is unavailable."
    }
    & $actionPath $Name $ContextPath | Out-Null
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
    $urlRepository = Get-RepositoryFromUrl $url
    if ($canonical -notmatch '^[^/\s]+/[^/\s]+$' -or -not (Test-SameRepository $canonical $urlRepository)) {
        throw "GitHub returned inconsistent repository identity metadata."
    }
    if ([string]$data.visibility -cne 'PRIVATE') {
        throw "GitHub repository must remain private: $canonical"
    }
    return [pscustomobject]@{
        Repository = $canonical
        Url = $url
        Visibility = [string]$data.visibility
        IsEmpty = [bool]$data.isEmpty
    }
}

function Get-ExactRemoteNames {
    param([string]$RepoRoot)
    return [string[]]@((Invoke-External 'git' @('-C', $RepoRoot, 'remote')).Output | ForEach-Object { [string]$_ })
}

function Test-ExactRemoteName {
    param([string[]]$Names, [string]$Name)
    foreach ($candidate in $Names) {
        if ([string]::Equals($candidate, $Name, [StringComparison]::Ordinal)) { return $true }
    }
    return $false
}

function Get-RemoteUrlState {
    param([string]$RepoRoot, [string]$Name)

    $fetchResult = Invoke-External 'git' @('-C', $RepoRoot, 'remote', 'get-url', '--all', $Name)
    $pushResult = Invoke-External 'git' @('-C', $RepoRoot, 'remote', 'get-url', '--push', '--all', $Name)
    return [pscustomobject]@{
        Fetch = [string[]]@($fetchResult.Output | ForEach-Object { ([string]$_).Trim() } | Where-Object { $_ })
        Push = [string[]]@($pushResult.Output | ForEach-Object { ([string]$_).Trim() } | Where-Object { $_ })
    }
}

function Get-CanonicalRemoteRepository {
    param([object]$UrlState, [switch]$AllowDisabledPush)

    $repository = $null
    foreach ($url in @($UrlState.Fetch) + @($UrlState.Push)) {
        if ($AllowDisabledPush -and $url -ceq 'DISABLED') { continue }
        $candidate = Get-RepositoryFromUrl ([string]$url)
        if (-not $candidate) { return $null }
        if ($repository -and -not (Test-SameRepository $repository $candidate)) { return $null }
        $repository = $candidate
    }
    return $repository
}

function Assert-RemoteMatchesRepository {
    param([string]$RepoRoot, [string]$Name, [string]$ExpectedRepository, [switch]$AllowDisabledPush)

    $state = Get-RemoteUrlState $RepoRoot $Name
    if ($state.Fetch.Count -eq 0 -or $state.Push.Count -eq 0) {
        throw "Remote $Name has incomplete URL configuration."
    }
    foreach ($url in @($state.Fetch) + @($state.Push)) {
        if ($AllowDisabledPush -and $url -ceq 'DISABLED') { continue }
        $actual = Get-RepositoryFromUrl ([string]$url)
        if (-not (Test-SameRepository $actual $ExpectedRepository)) {
            throw "Remote $Name contains a URL for a different or unrecognized repository."
        }
    }
    return $state
}

function Get-RemoteBranchTip {
    param([string]$Url, [string]$Branch)

    if ((Invoke-External 'git' @('check-ref-format', '--branch', $Branch) -AllowFailure).ExitCode -ne 0) {
        throw "The local branch name is not safe for synchronization."
    }
    $ref = "refs/heads/$Branch"
    $result = Invoke-External 'git' @('ls-remote', '--exit-code', '--heads', $Url, $ref) -AllowFailure
    if ($result.ExitCode -eq 2) { return [pscustomobject]@{ Exists = $false; Sha = $null; Ref = $ref } }
    if ($result.ExitCode -ne 0) { throw "Unable to inspect the exact remote branch."
    }
    $lines = @($result.Output | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) })
    if ($lines.Count -ne 1 -or [string]$lines[0] -notmatch '^([0-9a-fA-F]{40,64})\s+(.+)$' -or $Matches[2] -cne $ref) {
        throw "Remote branch inspection returned an ambiguous result."
    }
    return [pscustomobject]@{ Exists = $true; Sha = $Matches[1].ToLowerInvariant(); Ref = $ref }
}

function Assert-SourceState {
    param([string]$RepoRoot, [string]$ExpectedHead, [string]$ExpectedBranch)

    $currentHead = ((Invoke-External 'git' @('-C', $RepoRoot, 'rev-parse', '--verify', 'HEAD^{commit}')).Output | Select-Object -First 1).Trim()
    $currentBranch = ((Invoke-External 'git' @('-C', $RepoRoot, 'branch', '--show-current')).Output | Select-Object -First 1).Trim()
    if ($currentHead -cne $ExpectedHead -or ($ExpectedBranch -and $currentBranch -cne $ExpectedBranch)) {
        throw "Source HEAD or branch changed during GitHub synchronization. No push was made."
    }
}

function Get-SourceHeadState {
    param([string]$RepoRoot, [switch]$AllowUnborn)
    $branch = (((Invoke-External git @('-C', $RepoRoot, 'branch', '--show-current')).Output | Select-Object -First 1)).Trim()
    if (-not $branch -or (Invoke-External git @('check-ref-format', '--branch', $branch) -AllowFailure).ExitCode -ne 0) {
        throw 'A safe named local branch is required.'
    }
    $result = Invoke-External git @('-C', $RepoRoot, 'rev-parse', '--verify', 'HEAD^{commit}') -AllowFailure
    if ($result.ExitCode -ne 0) {
        if (-not $AllowUnborn) { throw 'A committed source HEAD is required.' }
        return [pscustomobject]@{ Head = $null; Branch = $branch }
    }
    $head = ((($result.Output | Select-Object -First 1)).Trim()).ToLowerInvariant()
    if ($head -notmatch '^[0-9a-f]{40,64}$') { throw 'Unable to resolve the exact source HEAD.' }
    return [pscustomobject]@{ Head = $head; Branch = $branch }
}

function Resolve-ExactCommit {
    param([string]$RepoRoot, [string]$Value, [string]$Label)
    if ($Value -notmatch '^[0-9a-fA-F]{40,64}$') { throw "$Label must be an exact commit object ID." }
    $value = $Value.ToLowerInvariant()
    $result = Invoke-External git @('-C', $RepoRoot, 'rev-parse', '--verify', "${value}^{commit}") -AllowFailure
    if ($result.ExitCode -ne 0 -or ((($result.Output | Select-Object -First 1)).Trim()).ToLowerInvariant() -cne $value) {
        throw "$Label must identify an existing exact immutable commit."
    }
    return $value
}

function Get-IndexTree {
    param([string]$RepoRoot)
    $tree = (((Invoke-External git @('-C', $RepoRoot, 'write-tree')).Output | Select-Object -First 1)).Trim().ToLowerInvariant()
    if ($tree -notmatch '^[0-9a-f]{40,64}$') { throw 'Unable to resolve the exact staged source tree.' }
    return $tree
}

function Assert-PreCommitState {
    param([string]$RepoRoot, [object]$Expected, [string]$Tree)
    $actual = Get-SourceHeadState $RepoRoot -AllowUnborn
    if ([string]$actual.Head -cne [string]$Expected.Head -or $actual.Branch -cne $Expected.Branch -or (Get-IndexTree $RepoRoot) -cne $Tree) {
        throw 'Source branch, HEAD, or staged tree changed during pre-commit audit.'
    }
}

function Get-GitCommonDirectory {
    param([string]$RepoRoot)
    $result = Invoke-External git @('-C', $RepoRoot, 'rev-parse', '--path-format=absolute', '--git-common-dir') -AllowFailure
    if ($result.ExitCode -eq 0) { $path = (($result.Output | Select-Object -First 1)).Trim() }
    else {
        $path = (((Invoke-External git @('-C', $RepoRoot, 'rev-parse', '--git-common-dir')).Output | Select-Object -First 1)).Trim()
        if (-not [IO.Path]::IsPathRooted($path)) { $path = Join-Path $RepoRoot $path }
    }
    $path = Get-NormalizedFullPath $path
    if (-not (Test-Path -LiteralPath $path -PathType Container)) { throw 'The Git common directory is unavailable.' }
    Assert-NoRedirectedPath $path 'Git common directory' | Out-Null
    return $path
}

function Initialize-LocalSyncPaths {
    param([string]$RepoRoot)
    $directory = Join-Path (Get-GitCommonDirectory $RepoRoot) 'codex'
    if (-not (Test-Path -LiteralPath $directory)) { New-Item -ItemType Directory -Path $directory | Out-Null }
    if (-not (Test-Path -LiteralPath $directory -PathType Container)) { throw 'Local synchronization state path is invalid.' }
    Assert-NoRedirectedPath $directory 'Local synchronization state directory' | Out-Null
    return [pscustomobject]@{ Directory=$directory; Lock=(Join-Path $directory 'github-sync.lock')
        Attestations=(Join-Path $directory 'github-sync-attestations.json') }
}

function Enter-SyncLock {
    param([string]$Path)
    try { return [IO.File]::Open($Path, 'OpenOrCreate', 'ReadWrite', 'None') }
    catch { throw 'Another GitHub synchronization operation is already active for this repository.' }
}

function Get-Sha256Text {
    param([string]$Text)
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [Text.UTF8Encoding]::new($false).GetBytes($Text)
        return ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant()
    } finally { $sha.Dispose() }
}

function Get-CommitSemanticState {
    param([string]$RepoRoot, [string]$Commit)
    $commit = Resolve-ExactCommit $RepoRoot $Commit Commit
    $tree = (((Invoke-External git @('-C',$RepoRoot,'show','-s','--format=%T',$commit)).Output | Select-Object -First 1)).Trim().ToLowerInvariant()
    $parentLine = [string](((Invoke-External git @('-C',$RepoRoot,'show','-s','--format=%P',$commit)).Output | Select-Object -First 1))
    $parents = @($parentLine.Trim().Split([char[]]@(' '), [StringSplitOptions]::RemoveEmptyEntries))
    if ($parents.Count -gt 1) { throw 'Audited commits must have at most one parent.' }
    $message = ((Invoke-External git @('-C',$RepoRoot,'show','-s','--format=%B',$commit)).Output -join "`n").TrimEnd([char[]]@("`r","`n"))
    $metadata = [ordered]@{
        message=$message
        author_name=[string](((Invoke-External git @('-C',$RepoRoot,'show','-s','--format=%an',$commit)).Output | Select-Object -First 1))
        author_email=[string](((Invoke-External git @('-C',$RepoRoot,'show','-s','--format=%ae',$commit)).Output | Select-Object -First 1))
        committer_name=[string](((Invoke-External git @('-C',$RepoRoot,'show','-s','--format=%cn',$commit)).Output | Select-Object -First 1))
        committer_email=[string](((Invoke-External git @('-C',$RepoRoot,'show','-s','--format=%ce',$commit)).Output | Select-Object -First 1))
    } | ConvertTo-Json -Compress
    $parent = if ($parents.Count) { ([string]$parents[0]).ToLowerInvariant() } else { $null }
    return [pscustomobject]@{ Commit=$commit; Tree=$tree
        Parent=$parent
        MetadataHash=(Get-Sha256Text $metadata) }
}

function New-CommitCandidate {
    param([string]$RepoRoot,[string]$Tree,[AllowNull()][string]$Parent,[string]$Message,[string]$StateDirectory)
    $file = Join-Path $StateDirectory ('.message-' + [Guid]::NewGuid().ToString('N'))
    try {
        [IO.File]::WriteAllText($file,$Message,[Text.UTF8Encoding]::new($false))
        $arguments=@('-C',$RepoRoot,'commit-tree',$Tree); if($Parent){$arguments+=@('-p',$Parent)}; $arguments+=@('-F',$file)
        return Resolve-ExactCommit $RepoRoot (((Invoke-External git $arguments).Output | Select-Object -First 1).Trim()) CandidateCommit
    } finally { if(Test-Path $file){Remove-Item $file -Force} }
}

function Get-EmptyAttestationState { return [pscustomobject]@{format=1;pending=$null;commits=@()} }
function Read-AttestationState {
    param([string]$Path)
    if (-not (Test-Path $Path -PathType Leaf)) { return Get-EmptyAttestationState }
    try { $state = Get-Content -Raw $Path | ConvertFrom-Json } catch { return Get-EmptyAttestationState }
    if ([int]$state.format -ne 1) { return Get-EmptyAttestationState }
    if (-not $state.PSObject.Properties['commits']) {
        $state | Add-Member -NotePropertyName commits -NotePropertyValue @()
    }
    if (-not $state.PSObject.Properties['pending']) {
        $state | Add-Member -NotePropertyName pending -NotePropertyValue $null
    }
    return $state
}
function Write-AttestationState {
    param([string]$Path,[string]$Parent,[object]$State)
    $temp=Join-Path $Parent ('.attest-'+[Guid]::NewGuid().ToString('N'));$backup="$temp.bak"
    try{
        [IO.File]::WriteAllText($temp,(($State|ConvertTo-Json -Depth 8)+"`n"),[Text.UTF8Encoding]::new($false))
        if(Test-Path $Path){[IO.File]::Replace($temp,$Path,$backup);if(Test-Path $backup){Remove-Item $backup -Force}}
        else{[IO.File]::Move($temp,$Path)}
    }finally{if(Test-Path $temp){Remove-Item $temp -Force};if(Test-Path $backup){Remove-Item $backup -Force}}
}

function Convert-PendingAttestation {
    param([string]$RepoRoot,[object]$Pending,[string]$Branch,[string]$Tip)
    try {
        if (-not $Pending -or [int]$Pending.format -ne 1 -or [string]$Pending.branch -cne $Branch) { return $null }
        $candidate = Get-CommitSemanticState $RepoRoot ([string]$Pending.candidate_commit)
        $actual = Get-CommitSemanticState $RepoRoot $Tip
        $parent = if ($Pending.source_head) { [string]$Pending.source_head } else { $null }
        if ($candidate.Tree -cne [string]$Pending.tree -or $candidate.Parent -cne $parent -or
            $candidate.MetadataHash -cne [string]$Pending.metadata_hash -or $actual.Tree -cne $candidate.Tree -or
            $actual.Parent -cne $parent -or $actual.MetadataHash -cne $candidate.MetadataHash) { return $null }
        return [pscustomobject]@{format=1;commit=$actual.Commit;parent=$actual.Parent;tree=$actual.Tree
            metadata_hash=$actual.MetadataHash;candidate_commit=$candidate.Commit}
    } catch { return $null }
}

function Test-AttestationRecord {
    param([string]$RepoRoot,[object]$Record,[string]$Commit,[string]$Parent)
    try {
        if (-not $Record -or [int]$Record.format -ne 1 -or [string]$Record.commit -cne $Commit -or
            [string]$Record.parent -cne $Parent) { return $false }
        $actual = Get-CommitSemanticState $RepoRoot $Commit
        return $actual.Tree -ceq [string]$Record.tree -and $actual.Parent -ceq $Parent -and
            $actual.MetadataHash -ceq [string]$Record.metadata_hash
    } catch { return $false }
}

function Get-VerifiedAttestationChain {
    param([string]$RepoRoot,[object]$State,[string]$Branch,[string]$Base,[string]$Tip)
    $records = @{}
    foreach ($record in @($State.commits)) { if ($record.commit) { $records[[string]$record.commit] = $record } }
    $pending = Convert-PendingAttestation $RepoRoot $State.pending $Branch $Tip
    if ($pending) { $records[$Tip] = $pending }
    $verified = New-Object Collections.Generic.List[object]
    $parent = $Base
    foreach ($commit in (Invoke-External git @('-C',$RepoRoot,'rev-list','--reverse',"${Base}..${Tip}")).Output) {
        $commit = ([string]$commit).Trim().ToLowerInvariant()
        if (-not $records.ContainsKey($commit) -or
            -not (Test-AttestationRecord $RepoRoot $records[$commit] $commit $parent)) { return $null }
        $verified.Add($records[$commit])
        $parent = $commit
    }
    if ($parent -cne $Tip) { return $null }
    return [pscustomobject]@{ Records = [object[]]$verified.ToArray() }
}

function Test-RemoteUrlStateEqual {
    param([object]$Left,[object]$Right)
    return (($Left.Fetch-join"`n")-ceq($Right.Fetch-join"`n"))-and(($Left.Push-join"`n")-ceq($Right.Push-join"`n"))
}

function Assert-NoGitOperation {
    param([string]$RepoRoot)
    foreach($marker in @('MERGE_HEAD','CHERRY_PICK_HEAD','REVERT_HEAD','BISECT_LOG','rebase-apply','rebase-merge','sequencer')){
        $path=(((Invoke-External git @('-C',$RepoRoot,'rev-parse','--git-path',$marker)).Output|Select-Object -First 1)).Trim()
        if(-not[IO.Path]::IsPathRooted($path)){$path=Join-Path $RepoRoot $path}
        if(Test-Path $path){throw 'Legacy ancestry recovery requires no Git operation to be in progress.'}
    }
}

function Assert-CleanRepository {
    param([string]$RepoRoot)
    if(@((Invoke-External git @('-C',$RepoRoot,'status','--porcelain=v1','--untracked-files=all')).Output|
        Where-Object{-not[string]::IsNullOrWhiteSpace([string]$_)}).Count){
        throw 'Legacy ancestry recovery requires a completely clean working tree and index.'
    }
}

function New-NeutralBaselineCandidate {
    param([string]$RepoRoot,[string]$Tree,[string]$StateDirectory)
    $names=@('GIT_AUTHOR_NAME','GIT_AUTHOR_EMAIL','GIT_AUTHOR_DATE','GIT_COMMITTER_NAME','GIT_COMMITTER_EMAIL','GIT_COMMITTER_DATE')
    $old=@{};foreach($name in $names){$old[$name]=[Environment]::GetEnvironmentVariable($name,'Process')}
    try{
        $env:GIT_AUTHOR_NAME='Codex Audited Baseline';$env:GIT_AUTHOR_EMAIL='codex-audited-baseline@users.noreply.github.com'
        $env:GIT_AUTHOR_DATE='2000-01-01T00:00:00+00:00';$env:GIT_COMMITTER_NAME=$env:GIT_AUTHOR_NAME
        $env:GIT_COMMITTER_EMAIL=$env:GIT_AUTHOR_EMAIL;$env:GIT_COMMITTER_DATE=$env:GIT_AUTHOR_DATE
        return New-CommitCandidate $RepoRoot $Tree $null 'Establish audited project baseline' $StateDirectory
    }finally{foreach($name in $names){[Environment]::SetEnvironmentVariable($name,$old[$name],'Process')}}
}

function Test-BaselineCandidateInUnbornRepository {
    param([string]$RepoRoot,[string]$Candidate,[string]$AuditScript)
    $parent=Get-NormalizedFullPath([IO.Path]::GetTempPath());$temp=Join-Path $parent('codex-baseline-'+[Guid]::NewGuid().ToString('N'))
    $old=[Environment]::GetEnvironmentVariable('GIT_ALTERNATE_OBJECT_DIRECTORIES','Process')
    try{
        New-Item -ItemType Directory $temp|Out-Null
        Invoke-External git @('-C',$temp,'init','--quiet','--initial-branch=codex-audit')|Out-Null
        $env:GIT_ALTERNATE_OBJECT_DIRECTORIES=Join-Path(Get-GitCommonDirectory $RepoRoot)objects
        Invoke-External git @('-C',$temp,'read-tree',"${Candidate}^{tree}")|Out-Null
        &$AuditScript -ProjectRoot $temp -CandidateCommit $Candidate -ScanOnly
    }finally{
        [Environment]::SetEnvironmentVariable('GIT_ALTERNATE_OBJECT_DIRECTORIES',$old,'Process')
        if(Test-Path $temp){Assert-ContainedPath $temp $parent 'Temporary baseline audit path'|Out-Null;Remove-Item $temp -Recurse -Force}
    }
}

function Invoke-AtomicBaselineTransition {
    param([string]$RepoRoot,[string]$Branch,[string]$Legacy,[string]$Baseline)
    $legacyRef="refs/codex/legacy-history/$Branch/$Legacy"
    $baselineRef="refs/codex/history-baselines/$Branch/$Baseline"
    $git=if([Environment]::OSVersion.Platform-eq[PlatformID]::Win32NT){
        (Get-Command git.exe -CommandType Application|Select-Object -First 1).Source
    }else{(Get-Command git -CommandType Application|Select-Object -First 1).Source}
    $psi=New-Object Diagnostics.ProcessStartInfo;$psi.FileName=$git;$psi.Arguments='update-ref --stdin';$psi.WorkingDirectory=$RepoRoot
    $psi.UseShellExecute=$false;$psi.RedirectStandardInput=$true;$psi.RedirectStandardError=$true;$psi.CreateNoWindow=$true
    $process=New-Object Diagnostics.Process;$process.StartInfo=$psi
    try{
        [void]$process.Start()
        $process.StandardInput.Write("start`ncreate $legacyRef $Legacy`ncreate $baselineRef $Baseline`nupdate refs/heads/$Branch $Baseline $Legacy`nprepare`ncommit`n")
        $process.StandardInput.Close();$errorText=$process.StandardError.ReadToEnd();$process.WaitForExit()
        if($process.ExitCode){throw "Atomic legacy ancestry recovery failed: $errorText"}
    }finally{$process.Dispose()}
    return [pscustomobject]@{LegacyRef=$legacyRef;BaselineRef=$baselineRef}
}

function Set-ObjectProperty {
    param([object]$Object, [string]$Name, [object]$Value)
    if ($Object.PSObject.Properties[$Name]) { $Object.$Name = $Value }
    else { $Object | Add-Member -NotePropertyName $Name -NotePropertyValue $Value }
}

function Write-StateAtomically {
    param([string]$RepoRoot, [string]$StateFile, [string]$ExpectedText, [string]$NewText)

    Assert-ContainedPath $StateFile $RepoRoot 'StatePath' | Out-Null
    Assert-NoRedirectedPath $StateFile 'StatePath' | Out-Null
    $currentText = Get-Content -Raw -LiteralPath $StateFile
    if ($currentText -cne $ExpectedText) {
        throw "Workflow state changed during initialization. No state or remote update was made."
    }
    $directory = Split-Path -Parent $StateFile
    Assert-NoRedirectedPath $directory 'StatePath parent' | Out-Null
    $operationId = [Guid]::NewGuid().ToString('N')
    $tempPath = Join-Path $directory ('.' + [IO.Path]::GetFileName($StateFile) + '.codex-' + $operationId + '.tmp')
    $backupPath = Join-Path $directory ('.' + [IO.Path]::GetFileName($StateFile) + '.codex-' + $operationId + '.bak')
    Assert-ContainedPath $tempPath $RepoRoot 'Temporary state path' | Out-Null
    Assert-ContainedPath $backupPath $RepoRoot 'State rollback path' | Out-Null
    try {
        [IO.File]::WriteAllText($tempPath, $NewText, [Text.UTF8Encoding]::new($false))
        Invoke-TestFault 'sync-before-state-replace' $StateFile
        Assert-NoRedirectedPath $StateFile 'StatePath' | Out-Null
        Assert-NoRedirectedPath $tempPath 'Temporary state path' | Out-Null
        Assert-ContainedPath $StateFile $RepoRoot 'StatePath' | Out-Null
        Assert-ContainedPath $tempPath $RepoRoot 'Temporary state path' | Out-Null
        [IO.File]::Replace($tempPath, $StateFile, $backupPath)
        Assert-NoRedirectedPath $backupPath 'State rollback path' | Out-Null
        Assert-ContainedPath $backupPath $RepoRoot 'State rollback path' | Out-Null
        Remove-Item -LiteralPath $backupPath -Force
    }
    finally {
        if (Test-Path -LiteralPath $tempPath) {
            Assert-NoRedirectedPath $tempPath 'Temporary state path' | Out-Null
            Assert-ContainedPath $tempPath $RepoRoot 'Temporary state path' | Out-Null
            Remove-Item -LiteralPath $tempPath -Force
        }
        if (Test-Path -LiteralPath $backupPath) {
            Assert-NoRedirectedPath $backupPath 'State rollback path' | Out-Null
            Assert-ContainedPath $backupPath $RepoRoot 'State rollback path' | Out-Null
            Remove-Item -LiteralPath $backupPath -Force
        }
    }
}

foreach ($required in @('git', 'tar')) {
    if (-not (Get-Command $required -ErrorAction SilentlyContinue)) { throw "Required command is unavailable: $required" }
}

$primaryModes = @($Initialize, $PreCommit, $ScanOnly, $PublicReadiness, $RecoverLegacyAncestry) | Where-Object { [bool]$_ }
if ($primaryModes.Count -gt 1) {
    throw 'Initialize, PreCommit, ScanOnly, PublicReadiness, and RecoverLegacyAncestry are mutually exclusive modes.'
}
if ($CommitMessage -and -not $PreCommit) { throw 'CommitMessage is available only with PreCommit.' }
if ($PreCommit -and [string]::IsNullOrWhiteSpace($CommitMessage)) { throw 'PreCommit requires a non-empty CommitMessage.' }
if ($PreCommit -and $CommitMessage.IndexOf([char]0) -ge 0) { throw 'CommitMessage must not contain a NUL character.' }
if ($BatchEligible -and ($Initialize -or $PreCommit -or $ScanOnly -or $PublicReadiness -or $RecoverLegacyAncestry)) {
    throw 'BatchEligible is available only for ordinary post-commit synchronization.'
}
if ($RecoverLegacyAncestry -and [string]::IsNullOrWhiteSpace($ExpectedLegacyHead)) {
    throw 'RecoverLegacyAncestry requires ExpectedLegacyHead.'
}
if ($ExpectedLegacyHead -and -not $RecoverLegacyAncestry) {
    throw 'ExpectedLegacyHead is available only with RecoverLegacyAncestry.'
}

$root = Resolve-SafeProjectRoot $ProjectRoot
$localSyncPaths = Initialize-LocalSyncPaths $root
$syncLock = Enter-SyncLock $localSyncPaths.Lock
$auditScript = Join-Path $PSScriptRoot 'github-backup.ps1'
if (-not (Test-Path -LiteralPath $auditScript -PathType Leaf)) { throw 'The GitHub audit helper is unavailable.' }

try {
    if ($PreCommit) {
        $preCommitHead = Get-SourceHeadState $root -AllowUnborn
        $preCommitTree = Get-IndexTree $root
        $candidateCommit = New-CommitCandidate $root $preCommitTree $preCommitHead.Head $CommitMessage $localSyncPaths.Directory
        $candidateState = Get-CommitSemanticState $root $candidateCommit
        if ($candidateState.Tree -cne $preCommitTree -or $candidateState.Parent -cne [string]$preCommitHead.Head) {
            throw 'The pre-commit candidate does not match the exact staged source state.'
        }

        & $auditScript -ProjectRoot $root -CandidateCommit $candidateCommit -ScanOnly -PrivateSourceSync
        Invoke-TestFault 'sync-precommit-after-candidate-audit' $root
        Assert-PreCommitState $root $preCommitHead $preCommitTree

        $attestations = Read-AttestationState $localSyncPaths.Attestations
        Set-ObjectProperty $attestations 'pending' ([pscustomobject]@{
            format = 1
            candidate_commit = $candidateCommit
            branch = $preCommitHead.Branch
            source_head = $preCommitHead.Head
            tree = $candidateState.Tree
            metadata_hash = $candidateState.MetadataHash
        })
        Write-AttestationState $localSyncPaths.Attestations $localSyncPaths.Directory $attestations
        Assert-PreCommitState $root $preCommitHead $preCommitTree
        Write-Host 'Pre-commit source audit passed for the exact staged tree.'
        return
    }

    $stateLocation = Get-SafeRelativePath $root $StatePath 'StatePath'
    $stateRelative = $stateLocation.Relative
    $stateWorktreePath = $stateLocation.FullPath
    Assert-NoRedirectedPath $stateWorktreePath 'StatePath' | Out-Null

    if ($Initialize) {
        if (-not (Test-Path -LiteralPath $stateWorktreePath -PathType Leaf)) { throw "Workflow state is missing: $stateRelative" }
        $stateText = Get-Content -Raw -LiteralPath $stateWorktreePath
        try { $state = $stateText | ConvertFrom-Json }
        catch { throw "Workflow state is invalid JSON: $stateRelative" }
    } else {
        $headState = Get-SourceHeadState $root
        $sourceHead = $headState.Head
        $branch = $headState.Branch
        $stateResult = Invoke-External 'git' @('-C', $root, 'show', "${sourceHead}:$stateRelative") -AllowFailure
        if ($stateResult.ExitCode -ne 0) { throw "Committed workflow state is missing: $stateRelative" }
        try { $state = ($stateResult.Output -join [Environment]::NewLine) | ConvertFrom-Json }
        catch { throw "Committed workflow state is invalid JSON: $stateRelative" }
    }
    $supportedGitHubModes = @('private-public-ready', 'private-source-strict-public-readiness')
    if ([int]$state.workflow_version -lt 3 -or $supportedGitHubModes -notcontains [string]$state.github_mode) {
        throw 'Project setup workflow v3-or-later private GitHub state is required before synchronization.'
    }

    $remoteNames = Get-ExactRemoteNames $root
    $requestedRemote = if ($RemoteName) { $RemoteName } elseif ($state.remote) { [string]$state.remote } else { $null }
    if ($requestedRemote) {
        $effectiveRemote = $requestedRemote
    } elseif (Test-ExactRemoteName $remoteNames 'origin') {
        $originState = Get-RemoteUrlState $root 'origin'
        $effectiveRemote = if (Get-CanonicalRemoteRepository $originState -AllowDisabledPush) { 'origin' } else { 'github' }
    } else {
        $effectiveRemote = 'origin'
    }
    $remoteExists = Test-ExactRemoteName $remoteNames $effectiveRemote
    $effectiveRepository = if ($Repository) { $Repository } elseif ($state.repository) { [string]$state.repository } else { $null }

    if ($Initialize) {
        if (-not (Get-Command gh -ErrorAction SilentlyContinue)) { throw 'GitHub CLI is unavailable; repository initialization is pending.' }
        if ((Invoke-External 'gh' @('auth', 'status') -AllowFailure).ExitCode -ne 0) { throw 'GitHub authentication is unavailable; run gh auth login.' }

        $remoteCandidate = $null
        if ($remoteExists) {
            $remoteCandidate = Get-CanonicalRemoteRepository (Get-RemoteUrlState $root $effectiveRemote) -AllowDisabledPush
            if (-not $remoteCandidate) { throw "Remote $effectiveRemote does not have one canonical GitHub repository identity." }
            if (-not $effectiveRepository) { $effectiveRepository = $remoteCandidate }
        }

        $autoSelected = -not $effectiveRepository
        if ($autoSelected) {
            $loginResult = Invoke-External 'gh' @('api', 'user', '--jq', '.login')
            $login = (($loginResult.Output | Select-Object -First 1)).Trim()
            if ($login -notmatch '^[A-Za-z0-9-]+$') { throw 'Unable to determine the authenticated GitHub owner.' }
            $projectName = Split-Path -Leaf $root
            $candidate = "$login/$projectName"
            $repoInfo = Resolve-GitHubRepository $candidate -AllowMissing
            if (-not $repoInfo) {
                Invoke-External 'gh' @('repo', 'create', $candidate, '--private') | Out-Null
                $repoInfo = Resolve-GitHubRepository $candidate
            } elseif (-not $repoInfo.IsEmpty) {
                throw 'The automatically selected same-name GitHub repository is not empty. Specify a repository explicitly or configure its matching remote.'
            }
        } else {
            $repoInfo = Resolve-GitHubRepository $effectiveRepository
        }

        $effectiveRepository = $repoInfo.Repository
        if ($remoteCandidate -and -not (Test-SameRepository $remoteCandidate $effectiveRepository)) {
            throw "Remote $effectiveRemote points to a different repository."
        }
        if ($remoteExists) {
            Assert-RemoteMatchesRepository $root $effectiveRemote $effectiveRepository -AllowDisabledPush | Out-Null
        }

        Set-ObjectProperty $state 'repository' $effectiveRepository
        Set-ObjectProperty $state 'remote' $effectiveRemote
        $newStateText = ($state | ConvertTo-Json -Depth 6) + "`n"
        Write-StateAtomically $root $stateWorktreePath $stateText $newStateText

        if (-not $remoteExists) {
            Invoke-External 'git' @('-C', $root, 'remote', 'add', $effectiveRemote, [string]$repoInfo.Url) | Out-Null
        } else {
            $pushState = Get-RemoteUrlState $root $effectiveRemote
            if ($pushState.Push.Count -eq 1 -and $pushState.Push[0] -ceq 'DISABLED') {
                Invoke-External 'git' @('-C', $root, 'remote', 'set-url', '--push', $effectiveRemote, [string]$repoInfo.Url) | Out-Null
            }
        }
        Write-Host "Initialized private public-ready GitHub destination $effectiveRepository on remote $effectiveRemote. Commit workflow state before synchronization."
        return
    }

    Assert-SourceState $root $sourceHead $branch

    if ($PublicReadiness -or $ScanOnly) {
        $scanParameters = @{
            ProjectRoot = $root
            SourceCommit = $sourceHead
            ScanOnly = $true
            AuditSourceHistory = $true
            FullSourceHistory = $true
        }
        if (-not $PublicReadiness) { $scanParameters['PrivateSourceSync'] = $true }
        & $auditScript @scanParameters
        Invoke-TestFault 'sync-after-source-audit' $root
        Assert-SourceState $root $sourceHead $branch
        if ($PublicReadiness) {
            Write-Host "Public-readiness audit passed for committed source history at $sourceHead. Repository visibility was not changed."
        } else {
            Write-Host "GitHub source synchronization scan passed for $sourceHead."
        }
        return
    }

    if (-not $effectiveRepository) {
        throw 'Committed workflow state must record a GitHub repository before synchronization. Run github-sync.ps1 -Initialize during setup.'
    }
    if (-not (Get-Command gh -ErrorAction SilentlyContinue)) { throw 'GitHub CLI is unavailable; source synchronization is pending.' }
    if ((Invoke-External 'gh' @('auth', 'status') -AllowFailure).ExitCode -ne 0) { throw 'GitHub authentication is unavailable; run gh auth login.' }

    $repoInfo = Resolve-GitHubRepository $effectiveRepository
    $effectiveRepository = $repoInfo.Repository
    $remoteNames = Get-ExactRemoteNames $root
    if (Test-ExactRemoteName $remoteNames $effectiveRemote) {
        $verifiedRemoteState = Assert-RemoteMatchesRepository $root $effectiveRemote $effectiveRepository -AllowDisabledPush
    } else {
        Invoke-External 'git' @('-C', $root, 'remote', 'add', $effectiveRemote, [string]$repoInfo.Url) | Out-Null
        $verifiedRemoteState = Assert-RemoteMatchesRepository $root $effectiveRemote $effectiveRepository
    }
    $disabledPush = $verifiedRemoteState.Push.Count -eq 1 -and $verifiedRemoteState.Push[0] -ceq 'DISABLED'
    if (-not $disabledPush -and @($verifiedRemoteState.Push | Where-Object { [string]$_ -ceq 'DISABLED' }).Count -gt 0) {
        throw "Remote $effectiveRemote has an ambiguous disabled push configuration."
    }

    $initialRemote = Get-RemoteBranchTip ([string]$repoInfo.Url) $branch
    $auditRef = "refs/codex/github-sync/$([Guid]::NewGuid().ToString('N'))"
    $fetchedTip = $null
    try {
        if ($initialRemote.Exists) {
            Invoke-External 'git' @('-C', $root, 'fetch', '--no-tags', '--no-write-fetch-head', [string]$repoInfo.Url, "$($initialRemote.Ref):$auditRef") | Out-Null
            $fetchedTip = ((Invoke-External 'git' @('-C', $root, 'rev-parse', '--verify', "${auditRef}^{commit}")).Output | Select-Object -First 1).Trim().ToLowerInvariant()
            if ($fetchedTip -cne $initialRemote.Sha) { throw 'Remote branch changed while it was being captured. No push was made.' }
            $ancestor = Invoke-External 'git' @('-C', $root, 'merge-base', '--is-ancestor', $fetchedTip, $sourceHead) -AllowFailure
            if ($ancestor.ExitCode -ne 0) { throw 'Remote history is diverged or ahead. No push was made.' }
        }

        if ($RecoverLegacyAncestry) {
            $expected = Resolve-ExactCommit $root $ExpectedLegacyHead 'ExpectedLegacyHead'
            if ($sourceHead -cne $expected) { throw 'ExpectedLegacyHead does not match the exact current source HEAD.' }
            if ($initialRemote.Exists) {
                throw 'Legacy ancestry recovery requires the exact private destination branch to be absent.'
            }
            Assert-NoGitOperation $root
            Assert-CleanRepository $root
            Assert-SourceState $root $sourceHead $branch

            & $auditScript -ProjectRoot $root -SourceCommit $sourceHead -ScanOnly -PrivateSourceSync
            Assert-SourceState $root $sourceHead $branch
            $unsafeHistoryConfirmed = $false
            try {
                & $auditScript -ProjectRoot $root -SourceCommit $sourceHead -ScanOnly -AuditSourceHistory -FullSourceHistory -PrivateSourceSync
            }
            catch {
                if ($_.Exception.Message -like 'Source history is not safe to push.*') { $unsafeHistoryConfirmed = $true }
                else { throw }
            }
            if (-not $unsafeHistoryConfirmed) {
                throw 'Legacy ancestry recovery is unnecessary because the complete source history passed its audit.'
            }
            Assert-CleanRepository $root
            Assert-NoGitOperation $root
            Assert-SourceState $root $sourceHead $branch

            $legacyTree = ((Invoke-External 'git' @('-C', $root, 'rev-parse', '--verify', "${sourceHead}^{tree}")).Output | Select-Object -First 1).Trim().ToLowerInvariant()
            $baselineCommit = New-NeutralBaselineCandidate $root $legacyTree $localSyncPaths.Directory
            $baselineState = Get-CommitSemanticState $root $baselineCommit
            if ($baselineState.Parent -or $baselineState.Tree -cne $legacyTree) {
                throw 'The recovery baseline candidate is not a parentless copy of the exact current tree.'
            }
            Test-BaselineCandidateInUnbornRepository $root $baselineCommit $auditScript
            Invoke-TestFault 'sync-recovery-after-candidate-audit' $root
            Assert-CleanRepository $root
            Assert-NoGitOperation $root
            Assert-SourceState $root $sourceHead $branch
            $transition = Invoke-AtomicBaselineTransition $root $branch $sourceHead $baselineCommit
            $sourceHead = $baselineCommit
            Assert-SourceState $root $sourceHead $branch
            Assert-CleanRepository $root
            Write-Host "Legacy ancestry was preserved at $($transition.LegacyRef); the active branch now begins at $($transition.BaselineRef)."
        }

        if ($BatchEligible -and $fetchedTip) {
            $countResult = Invoke-External 'git' @('-C', $root, 'rev-list', '--count', "${fetchedTip}..${sourceHead}")
            $countText = (($countResult.Output | Select-Object -First 1)).Trim()
            $aheadCount = 0
            if (-not [int]::TryParse($countText, [ref]$aheadCount) -or $aheadCount -lt 0) {
                throw 'Unable to determine the exact private synchronization cadence.'
            }
            if ($aheadCount -eq 0) {
                Write-AttestationState $localSyncPaths.Attestations $localSyncPaths.Directory (Get-EmptyAttestationState)
                Write-Host 'Private public-ready GitHub history is already current.'
                return
            }
            if ($aheadCount -lt 10) {
                $attestationState = Read-AttestationState $localSyncPaths.Attestations
                $chain = Get-VerifiedAttestationChain $root $attestationState $branch $fetchedTip $sourceHead
                if ($chain) {
                    $deferredState = [pscustomobject]@{ format = 1; pending = $null; commits = [object[]]@($chain.Records | ForEach-Object { $_ }) }
                    Write-AttestationState $localSyncPaths.Attestations $localSyncPaths.Directory $deferredState
                    Assert-SourceState $root $sourceHead $branch
                    $remoteAfterDeferral = Get-RemoteBranchTip ([string]$repoInfo.Url) $branch
                    if (-not $remoteAfterDeferral.Exists -or $remoteAfterDeferral.Sha -cne $fetchedTip) {
                        throw 'Remote branch changed before synchronization could be deferred.'
                    }
                    Write-Host "GitHub synchronization deferred: $aheadCount/10 audited local commits accumulated."
                    return
                }
                Write-Host 'Pre-commit attestation chain is incomplete; continuing with immediate synchronization.'
            }
        }

        $auditParameters = @{
            ProjectRoot = $root
            SourceCommit = $sourceHead
            ScanOnly = $true
            AuditSourceHistory = $true
            PrivateSourceSync = $true
        }
        if ($fetchedTip) { $auditParameters['HistoryBaseCommit'] = $fetchedTip }
        else { $auditParameters['FullSourceHistory'] = $true }
        & $auditScript @auditParameters
        Invoke-TestFault 'sync-after-source-audit' $root
        Assert-SourceState $root $sourceHead $branch

        Invoke-TestFault 'sync-before-push-recheck' $root
        $repoBeforePush = Resolve-GitHubRepository $effectiveRepository
        if (-not (Test-SameRepository $repoBeforePush.Repository $effectiveRepository)) {
            throw 'GitHub repository identity changed during synchronization. No push was made.'
        }
        $currentRemote = Get-RemoteBranchTip ([string]$repoBeforePush.Url) $branch
        if ($currentRemote.Exists -ne $initialRemote.Exists -or ($currentRemote.Exists -and $currentRemote.Sha -cne $fetchedTip)) {
            throw 'Remote branch changed after validation. No push was made.'
        }
        Assert-SourceState $root $sourceHead $branch

        $pushWasTemporarilyEnabled = $false
        $pushSucceeded = $false
        try {
            $remoteBeforePush = Get-RemoteUrlState $root $effectiveRemote
            if (-not (Test-RemoteUrlStateEqual $remoteBeforePush $verifiedRemoteState)) {
                throw 'The configured GitHub remote changed after validation. No push was made.'
            }
            if ($disabledPush) {
                Invoke-External 'git' @('-C', $root, 'remote', 'set-url', '--push', $effectiveRemote, [string]$repoBeforePush.Url) | Out-Null
                $pushWasTemporarilyEnabled = $true
                $enabledState = Get-RemoteUrlState $root $effectiveRemote
                if ($enabledState.Push.Count -ne 1 -or $enabledState.Push[0] -cne [string]$repoBeforePush.Url -or
                    $enabledState.Fetch.Count -ne $verifiedRemoteState.Fetch.Count) {
                    throw 'The normal GitHub remote could not be enabled safely.'
                }
                for ($index = 0; $index -lt $enabledState.Fetch.Count; $index++) {
                    if ($enabledState.Fetch[$index] -cne $verifiedRemoteState.Fetch[$index]) {
                        throw 'The normal GitHub remote changed while push access was enabled.'
                    }
                }
            }

            $lastRemoteCheck = Get-RemoteBranchTip ([string]$repoBeforePush.Url) $branch
            if ($lastRemoteCheck.Exists -ne $initialRemote.Exists -or ($lastRemoteCheck.Exists -and $lastRemoteCheck.Sha -cne $fetchedTip)) {
                throw 'Remote branch changed immediately before push. No push was made.'
            }
            Assert-SourceState $root $sourceHead $branch
            Invoke-External 'git' @('-C', $root, 'push', '--no-follow-tags', $effectiveRemote, "${sourceHead}:refs/heads/${branch}") | Out-Null
            $pushSucceeded = $true
            Assert-SourceState $root $sourceHead $branch
        }
        finally {
            if ($pushWasTemporarilyEnabled) {
                $restoreFailure = $null
                try {
                    Invoke-External 'git' @('-C', $root, 'remote', 'set-url', '--push', $effectiveRemote, 'DISABLED') | Out-Null
                    $restored = Get-RemoteUrlState $root $effectiveRemote
                    if ($restored.Push.Count -ne 1 -or $restored.Push[0] -cne 'DISABLED') {
                        throw 'The disabled push boundary was not restored exactly.'
                    }
                }
                catch { $restoreFailure = $_ }
                if ($restoreFailure) { throw 'The normal GitHub remote push boundary could not be restored safely.' }
            }
        }
        if (-not $pushSucceeded) { throw 'Private GitHub synchronization did not complete.' }

        $pushedTip = Get-RemoteBranchTip ([string]$repoBeforePush.Url) $branch
        if (-not $pushedTip.Exists -or $pushedTip.Sha -cne $sourceHead) {
            throw 'The exact private remote branch tip could not be verified after push.'
        }
        Write-AttestationState $localSyncPaths.Attestations $localSyncPaths.Directory (Get-EmptyAttestationState)
    }
    finally {
        if ($fetchedTip) { Invoke-External 'git' @('-C', $root, 'update-ref', '-d', $auditRef) -AllowFailure | Out-Null }
    }
    Write-Host "Private public-ready GitHub history is current at $effectiveRepository ($branch $sourceHead)."
}
finally {
    if ($syncLock) { $syncLock.Dispose() }
}
