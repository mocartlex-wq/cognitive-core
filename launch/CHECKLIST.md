# Pre-launch checklist

Run through this list **the day before** you press "submit" on any of the launch
posts.  Each unchecked item is a chance for a hostile commenter to dunk on you in the
first hour.

## 0. The product itself

- [ ] `curl | bash` quickstart works on a clean Ubuntu 22.04 VM (test in a fresh
      LXC container)
- [ ] `make smoke` passes
- [ ] `make backup` produces a non-empty file; `make restore` round-trips
- [ ] Hard-stop the box with `kill -9` on postgres, restart — no data loss
- [ ] `docker compose -f docker-compose.public.yml down -v && make init && make up`
      from scratch is < 3 minutes on a 100 Mb/s connection

## 1. GitHub repo hygiene

- [ ] Repo description set, with one emoji and a clear hook
- [ ] Repo topics: `ai-agents`, `multi-agent`, `claude`, `chatgpt`, `mcp`, `deepseek`,
      `self-hosted`, `mit-license`
- [ ] Star the repo from a second account so it's not at zero
- [ ] Branch protection on `main`: require PR, require passing CI
- [ ] Issue templates installed (`bug.yml`, `feature.yml`, `docs.yml`)
- [ ] Pull request template installed
- [ ] `LICENSE` file at repo root (MIT)
- [ ] `SECURITY.md` references a real email that you actually monitor
- [ ] `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1)
- [ ] `.github/FUNDING.yml` if you accept donations
- [ ] CI green on `main`

## 2. Documentation

- [ ] `README.md` opens with the product hook in the first 3 lines (no fluff)
- [ ] First code block runs in < 60 seconds on a fresh box
- [ ] At least one **GIF** under 5 MB in the README (terminal install or demo)
- [ ] Architecture diagram in the README
- [ ] Comparison table vs alternatives
- [ ] `docs/` index linked from README
- [ ] Every doc has a "last updated" date or version pin

## 3. Demo assets

- [ ] 5-min screencast uploaded (YouTube unlisted + direct MP4 ≤ 50 MB)
- [ ] Hero GIF (< 5 MB) embedded in README
- [ ] At least 3 PNG screenshots in `docs/screenshots/`
- [ ] All assets show real, working flows — no fake terminal output

## 4. Hosting / infra

- [ ] Public demo instance up at `https://demo.cognitive-core.dev` with
      rate-limit + read-only flag
- [ ] Status page (Uptime Kuma or Better Stack free tier) on a separate host
- [ ] Public Discord server with `#general`, `#help`, `#showcase` channels
- [ ] Discord invite link in README has no expiry

## 5. Posts ready

- [ ] HN draft proofread (< 1500 chars body)
- [ ] HN account has > 2-week-old karma (HN flags new accounts)
- [ ] Habr account verified
- [ ] Reddit accounts not shadow-banned (check with /r/ShadowBan)
- [ ] Three "first-comment" answers prepared per post
- [ ] Product Hunt assets uploaded but launch scheduled, not pressed yet

## 6. Defensive prep

- [ ] You have time blocked for **6 hours** the day of launch to answer comments
- [ ] One friend / colleague briefed to upvote within first 30 min on each platform
- [ ] Three friends willing to leave a thoughtful comment (not "great work!")
- [ ] List of at least 5 known weaknesses, with planned response phrasing
- [ ] List of likely "but what about X?" questions with answers ready

## 7. Analytics

- [ ] PostHog (free tier) wired into the demo instance
- [ ] PostHog dashboard bookmarked, refreshing every 5 min on launch day
- [ ] GitHub stargazer notifications turned ON for your phone
- [ ] Discord notifications routed to your phone

## 8. Recovery plan

- [ ] If something breaks during launch: known-good rollback tag pinned at
      `git tag launch-day-stable`
- [ ] Public roadmap (`ROADMAP.md` or GitHub Projects) so people see issues are
      tracked, not lost
- [ ] Template for "thanks for the report, tracking in #N" comment ready

## 9. Don't-do-list

- ❌ Do not name-drop big companies in the headline ("used by Anthropic" — false)
- ❌ Do not promise features you haven't built ("comes with K8s operator" — no)
- ❌ Do not call it "production-ready" — it's alpha and you said so in the README
- ❌ Do not delete bad comments — answer them or leave them
- ❌ Do not cross-post the same text verbatim across HN+Reddit (Google catches it)

## 10. After 24 hours

- [ ] Tally: stars, GitHub issues opened, Discord joins, PostHog DAU
- [ ] Decide: another push (HN second-chance, more subreddits) or pivot
- [ ] Triage every issue within 48h with at least an ack comment
- [ ] Write the "how the launch went" post for Habr / dev.to
