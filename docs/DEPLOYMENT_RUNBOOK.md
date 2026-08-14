# Production Deployment Runbook

## Authoritative path

Merge to `Main` is the production deployment trigger. The **Quality and Deploy**
workflow is the deployment authority for `mlb-analytics-hub`; no second deploy
command is required after merge.

From a phone or desktop:

1. Open the merged pull request and follow its **Checks** link, or open the
   repository **Actions** tab.
2. Open the **Quality and Deploy** run for the merge commit.
3. Confirm **quality**, **deploy**, and **Production smoke and readiness gate**
   succeed.
4. Read **Production deployment provenance** in the run summary. It records the
   commit, run, deploy result, smoke result, rollback result, rollback image, and
   application URL.

Production-triggered workflow runs share the
`mlb-analytics-hub-production` concurrency group and queue instead of
overlapping.

## Avoid competing deploys

Do not start a manual `flyctl deploy` while a **Quality and Deploy** run is
queued or in progress. Fly.io uses exclusive machine leases during deployment,
so a second deploy can collide with the workflow even when both images are
valid.

A manual lease error does not mean the GitHub Actions deployment failed. Check
the authoritative Actions run and the production smoke result before taking any
action.

## Lease-conflict recovery

The workflow wraps both deploy and rollback commands with a bounded retry. It
only retries the known transient machine-lease collision: output must contain
both `failed to acquire lease` and `lease currently held`.

The policy is four total attempts with 15, 30, and 45 second delays. Every other
error fails immediately so authentication, configuration, build, health, and
application failures remain visible and fail closed.

## Failure and rollback

If deployment or production smoke fails, the workflow attempts an immediate
rollback to the image captured before deployment. The provenance summary records
the outcome even when the job fails.

Before any emergency manual deploy:

1. Confirm no production workflow run is queued or active.
2. Identify the exact tested commit or previously healthy image.
3. Preserve the failing Actions run and logs for diagnosis.
4. Run one deployment path only, then verify `/health`, `/ready`, and the
   production product-contract smoke checks.
