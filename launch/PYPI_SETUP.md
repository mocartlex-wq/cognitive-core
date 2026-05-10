# PyPI publishing setup (one-time, ~3 min)

The repo's `publish-pypi.yml` workflow uses **PyPI Trusted Publishing (OIDC)**
instead of a long-lived API token. This is more secure — no secret to leak —
and is now PyPI's recommended approach for CI.

## One-time owner setup

### 1. Register on PyPI (skip if already done)

1. Open https://pypi.org/account/register/
2. Create account (email + username + password)
3. Verify email
4. **Enable 2FA** at https://pypi.org/manage/account/2fa-provisioning/ (PyPI requires this since 2024)

### 2. Add a "pending publisher"

PyPI lets you pre-authorize a GitHub workflow to publish a package that doesn't
exist yet — perfect for first-time publish.

1. Open https://pypi.org/manage/account/publishing/
2. Scroll to **"Add a new pending publisher"** section
3. Fill exactly:

   | Field | Value |
   |-------|-------|
   | **PyPI Project Name** | `cognitive-core-mcp` |
   | **Owner** | `mocartlex-wq` |
   | **Repository name** | `cognitive-core` |
   | **Workflow filename** | `publish-pypi.yml` |
   | **Environment name** | `pypi` |

4. Click **Add**

### 3. Add a GitHub Environment named `pypi`

The workflow uses `environment: pypi` for the OIDC scope. Create it:

1. Open https://github.com/mocartlex-wq/cognitive-core/settings/environments
2. **New environment** → name: `pypi` → **Configure environment**
3. (Optional) Required reviewers: leave empty for auto-publish
4. (Optional) Deployment branches: limit to `main` and tags `v*`
5. **Save protection rules**

### 4. (Optional) Delete the old PYPI_API_TOKEN secret

It's no longer used. Cleanup:
1. https://github.com/mocartlex-wq/cognitive-core/settings/secrets/actions
2. Click `PYPI_API_TOKEN` → **Remove**

### 5. Trigger a release

```bash
git tag v0.5.1 && git push origin v0.5.1
```

The workflow runs at https://github.com/mocartlex-wq/cognitive-core/actions
and publishes within ~2 minutes. No token is exchanged — PyPI verifies the
GitHub OIDC token directly.

## Why OIDC over API tokens

- ✅ No long-lived secret to leak
- ✅ Auto-rotated by GitHub each run
- ✅ Scoped exactly to (this repo, this workflow, this environment)
- ✅ Works on first publish (account-token doesn't, requires manual upload first)
- ❌ Requires PyPI account + 2FA (one-time setup, ~3 min)

## If something goes wrong

| Symptom | Fix |
|---------|-----|
| `403 Forbidden` from PyPI | Pending publisher misconfigured. Re-check fields exactly match repo+workflow+environment. |
| `Environment 'pypi' not found` | Create environment per step 3. |
| `OIDC token request failed` | Repo must be public OR have GitHub Pro/Team plan. Public repos: free OIDC. |
| Project name `cognitive-core-mcp` taken | Adjust in `launch/mcp-wrapper/pyproject.toml` AND in PyPI pending publisher. |
