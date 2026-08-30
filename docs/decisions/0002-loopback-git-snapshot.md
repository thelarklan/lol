# ADR 0002: Serve immutable snapshots through a loopback Git daemon

## Status

Accepted for v1.

## Context

Pipeline from SCM and `checkout scm` require a real Git remote. Jenkins' Git plugin restricts
local-path checkout, and enabling it would give the controller a broader filesystem read surface.
LOL must also include working-tree changes without modifying the source branch or index.

## Decision

For every run, LOL creates a bare repository beneath the run directory. A temporary Git index
combines HEAD, tracked modifications, deletions, and non-ignored untracked files into a synthetic
commit written only to that bare repository. Explicit `--revision` runs reference the selected
commit without applying working-tree state.

LOL exports only the run directory through an ephemeral `git daemon` bound to `127.0.0.1` with a
strict base path. The generated Pipeline job uses that URL and the `lol-run` branch. The daemon
stays alive for the build and is stopped in cleanup.

## Consequences

- Jenkins cannot use the transport to browse the source worktree or other repositories.
- Executable bits, symlinks, changelog behavior, and `checkout scm` retain Git semantics.
- The user branch, index, and object database are not changed.
- Git submodules and LFS hydration are rejected in v1 rather than silently producing an incomplete
  snapshot.
