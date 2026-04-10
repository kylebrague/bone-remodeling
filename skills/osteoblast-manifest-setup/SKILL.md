---
name: osteoblast-manifest-setup
description: Configure or repair `.github/osteoblast.toml` for the current repository. Use when bootstrapping Osteoblast, fixing broken include_paths, replacing placeholder verification commands, or tuning the maintenance scope and risk policy for a target repo.
---

# Osteoblast Manifest Setup

Use this skill whenever you need to create, review, or repair `.github/osteoblast.toml`.

## Objective

Make the manifest match the actual repository layout and the actual build/test workflow.

The most common failure mode is a manifest copied from defaults that points at directories the repo does not have. Do not keep bootstrap defaults if the repo layout is different.

## Read First

Before editing the manifest, inspect:

1. The repo root directory layout
2. `AGENTS.md`
3. `.github/copilot-instructions.md` if present
4. `package.json`, `pyproject.toml`, `go.mod`, `Cargo.toml`, `Makefile`, `justfile`, `turbo.json`, or other build entrypoints
5. Existing CI workflow files in `.github/workflows/`

## Manifest Rules

### `include_paths`

Choose existing top-level directories that are likely to contain meaningful, reviewable maintenance work.

Good candidates:

- `src`
- `app`
- `lib`
- `docs`
- `documentation`
- `packages`
- `services`
- `internal`
- `cmd`
- `specs`

Avoid adding directories that are mostly generated, vendored, or deployment-only unless the user explicitly wants them:

- `node_modules`
- `dist`
- `build`
- `vendor`
- `.git`
- `.github`
- infrastructure-only paths such as `infra` unless serious infrastructure findings are intentionally in scope

Prefer the smallest useful scope set. If a repo is clearly package-oriented, `packages` is usually the right anchor.

### `exclude_paths`

Keep generated and vendored paths excluded. Preserve defaults unless the repo needs extra exclusions.

### `allowed_categories`

Keep categories conservative by default:

- `bugs`
- `dead-code`
- `hardening`
- `consistency`
- `readability`
- `performance`
- `docs`

Do not invent new categories unless the controller already supports them.

### `severity_rules`

Keep serious-routing strict. If a repo has auth, workflows, migrations, schema files, or infrastructure, they should remain in serious-routing logic rather than routine local fixes.

### `verify.commands`

Replace the placeholder command with real repository checks.

Choose the narrowest commands that are standard for the repo:

- Node/TypeScript repos: `npm test`, `npm run check`, `npm run lint`, `npm run typecheck`
- Python repos: `pytest`, `ruff check`, `python -m unittest`
- Go repos: `go test ./...`
- Rust repos: `cargo test`

Use commands that already exist in the repo. Do not invent scripts that are not defined.

If the repo has a strong single verification entrypoint such as `npm run check`, prefer that over a long list.

### `pr.reviewers`

Only add reviewers when the repo has an obvious standard owner or the user already specified them.

## Repair Workflow

When the manifest is broken:

1. Compare `include_paths` to actual existing directories.
2. Replace nonexistent include paths with real ones.
3. Keep the scope conservative.
4. Replace placeholder `verify.commands` with real commands if they can be proven from the repo.
5. Leave uncertain fields alone and call them out explicitly.

## Example Decisions

- Monorepo with `packages/` and `documentation/`: prefer `["packages", "documentation", "specs"]`
- App repo with `src/` and `docs/`: prefer `["src", "docs"]`
- Service repo with `cmd/`, `internal/`, and `docs/`: prefer `["cmd", "internal", "docs"]`

## Hard Constraints

- Do not point `include_paths` at nonexistent directories.
- Do not keep the placeholder verify command once the real repo commands are known.
- Do not expand the routine scope into workflows, migrations, or infra unless the user explicitly wants that.
