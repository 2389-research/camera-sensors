# Open-source preparation

## Now
- Step: phase 2, waiting on Doctor Biz's choice of tree cleanup
- Next: carry out that choice, then phase 3 (license)
- Open: whether the image workflow should skip docs-only changes (proposed 2026-09-27, not approved)
- Approved: "let's use this skill to make sure we are ready for opensource https://github.com/2389-research/git-repo-prep/tree/main" (2026-09-27): prepare the repo for an open-source release with git-repo-prep
- Compactions: 1 (this session; the compaction came just before this plan began)

## Method

The git-repo-prep skill is not installed in this environment, and neither is a todo tool. This work follows `skills/prepare/SKILL.md` and `skills/review/SKILL.md` from a clone of github.com/2389-research/git-repo-prep, read on 2026-09-27. Each phase scans, presents its findings, gets a decision, and commits. The checklist below stands in for the skill's todo list.

## Phases
- [x] 1. Discovery
- [ ] 2. Secrets and personal info
- [ ] 3. License
- [ ] 4. Documentation
- [ ] 5. Gitignore
- [ ] 6. Project hardening
- [ ] 7. Repository metadata
- [ ] 8. Backlog and honesty
- [ ] 9. Final review

## Phase 1: Discovery

- Ecosystem: Python 3.12 (pyproject.toml, uv.lock), shipped as a Docker image. Tests live in tests/unit, tests/integration, and tests/e2e; the canonical check is `scripts/check`.
- Present: README.md, .gitignore, .dockerignore, tests, and one workflow (`.github/workflows/docker-image.yml`), which builds and publishes the image but runs no tests.
- Missing: LICENSE, CONTRIBUTING.md, SECURITY.md, CHANGELOG.md, CLAUDE.md, a Dependabot config, a pre-commit config.
- GitHub, 2026-09-27: private; no forks, pull requests, issues, releases, or tags; `main` is the only remote branch; the description is set; no topics; no license detected; wiki and projects enabled.
- Org conventions: 14 of the 25 most recently pushed public 2389-research repos carry a license, and all 14 are MIT. tracker and observatory publish CLAUDE.md, gotchas.md, and their superpowers plan docs. None of the five repos checked publishes a private journal.

## Phase 2: Secrets and personal info

Secrets: none found.
- gitleaks 8.30.1, run from its container over all 111 commits on every ref, flagged two lines. Both are the made-up key `lr_live_7f3a9c0d` in `tests/unit/test_config.py`, whose tests prove that config errors never quote a key.
- The real LunaRoute key (both local copies) and the four camera URLs in the local `.env` appear in no commit's tree and no commit message. No RTSP URL with a path token appears anywhere in history.
- The image copies only `pyproject.toml`, `uv.lock`, and `djev_sensors/`, so the published image holds no docs, notes, or config.

Personal info and internal details:
- `.private-journal/` (two entries) is tracked. Doctor Biz's rule keeps journals out of shared-repo history.
- `gotchas.md` names the home and office servers with their LAN addresses and the SSH user, cites a private sibling project, and cites two commit IDs that a rewritten history would orphan.
- The 2026-09-25 plan and the audit report name the deployment hosts. `docs/spec.md` and the plan cite the private sibling project.
- Four commit messages name the deployment hosts, and all 111 commits carry the author's personal email address.
- History holds everything above. How to publish history gets decided last, after every other change lands.
