<!-- new-project-setup:v6:start -->
### New project setup invocation

A bare or primary `$new-project-setup` invocation runs install/sync. Use the
invoked installed apply helper for a normal target; in this skill's source use
the source helper, then sync runtime. Never only load; questions are
consultation-only.

### Adaptive efficient execution

Infer durability, operational risk, and effort independently. State them
briefly and continue:

- Lasting work preserves revisions and memory. Exploration is disposable only
  for clear learning or feasibility; `quick`, `prototype`, and `MVP` do not
  imply it. Promote reused or retained work; never demote. Delete only current
  uncommitted Codex-created artifacts confirmed unused, never pre-existing,
  shared, or lasting output.
- Risk controls authorization, not routine local implementation authority.
- Effort controls context and evidence, not authority: focused checks direct
  effects; standard covers primary workflows and distinct risks;
  release-critical gathers broad deduplicated evidence.

Ask one preservation question only for ambiguous durability. Do not ask for
routine implementation, context expansion, or validation transitions. Bounded
local work authorizes architecture, a reasonable initial stack for an empty project,
dependencies, tests, demo data, and empty-DB schemas.

### Progressive context and evidence

Start file changes with Git status and relevant files; durable work adds
`docs/codex-handoff.md`. Read logs only when useful. Expand for dependencies,
failures, or risk; exclude unrelated roots and artifacts. Rebuild stale
handoffs from Git and evidence; ask only if the objective remains unsafe.

Keep a compact ledger of acceptance criteria, material risks, boundaries,
evidence, invalidators, and completion conditions. Claim
completion only when every criterion passes, every material risk or protected
boundary has distinct evidence, no unresolved high-risk failure remains, and
durable records are current. Evidence is distinct only for a materially
different risk or protected boundary; code-path or presentation variation
alone is equivalent evidence.

Reuse valid evidence and batch failures by cause. After targeted checks pass,
run one effort-appropriate final matrix. On failure, preserve passing evidence,
retest only failed or invalidated checks, and do not restart a broad matrix.
Non-improving cycles require a different strategy, then a minimal reproducer;
they do not stop productive debugging. Stop unresolved
only when the latest strategy made no material progress and no credible bounded
probe remains. Preserve diagnostics and report the blocker.

### Proportional durable memory

Preserve every lasting change in Git. Log useful decisions, failures, validation,
or lessons; refresh the concise handoff at state boundaries with
valid and remaining evidence; update the changelog for notable behavior. Keep
private details in ignored `*.local.md` and recheck branch, HEAD, and scope.
Prepare the final handoff before its containing commit and record sync relative
to it; a matching push needs no bookkeeping-only commit.

Before every lasting commit, stage only the scoped files and run
`scripts/github-sync.ps1 -PreCommit -CommitMessage '<exact message>'`. Commit
that exact audited staged tree and public-ready message immediately. Missing or
mismatched audit evidence fails safe to immediate synchronization.

After a focused small-change commit, run `scripts/github-sync.ps1
-BatchEligible`: one through nine verified local commits may remain local, and
the tenth synchronizes the complete batch. There is no time trigger. Initial
setup, standard or substantial work, milestones, releases, explicit sync
requests, and absent remote branches synchronize immediately with the normal
command. Normal private sync audits the current snapshot and every commit after
the verified private remote tip, using private-source rules that block
high-confidence secrets and unsafe Git objects without treating operational
metadata as a push blocker. Exact findings inherited unchanged from that tip
are already transferred; changed, re-added, or new findings block. Empty
remotes use the same private-source rules across full ancestry.
Public-readiness and isolated fallback use stricter public-metadata review.
Never force-push or change visibility.

Existing unsafe ancestry already transferred to the exact private destination
is not a reason to use fallback. For local-only legacy ancestry and an empty
destination, offer the guarded one-time clean-baseline recovery only with
explicit approval; it preserves the old history in local hidden refs and never
force-pushes. Otherwise keep the commit and ask whether to use isolated
`scripts/github-backup.ps1` or remain local-only. Fallback never modifies the
normal source remote.

### Autonomous local work

Complete bounded objectives end-to-end through appropriate validation without
routine checkpoints.
Ask before deployment; credentials or live/paid services; auth/security changes;
global or native tool installation; framework or platform replacement;
consequential licensing changes; changes to existing, shared, or production
data; destructive operations; material product-direction expansion beyond the
request; or unrelated conflicting work. Internal refactoring, routine local
dependencies, and isolated local construction need no checkpoint.
Protected boundaries override implied authority. Deployment requires
confirmation immediately before the action unless the current request explicitly
names the target and effect and waives that checkpoint; that explicit waiver is
the confirmation. Merely asking to deploy is not a waiver. One confirmation may
cover several protected effects only when it names them all.
<!-- new-project-setup:v6:end -->
