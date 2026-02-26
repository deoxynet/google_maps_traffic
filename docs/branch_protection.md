# Branch Protection

Apply required CI checks for pull requests:

```bash
scripts/configure_branch_protection.sh --repo OWNER/REPO
```

If `origin` points to the GitHub repo, `--repo` can be omitted:

```bash
scripts/configure_branch_protection.sh
```

Dry run (prints payload, makes no changes):

```bash
scripts/configure_branch_protection.sh --repo OWNER/REPO --dry-run
```

Checks enforced by default:

- `Rust Checks`
- `Fuzz Smoke`
