# Eurostar Snap watcher

Checks Eurostar Snap every ~5 minutes for these routes and alerts you when new
tickets appear:

- Rotterdam Centraal → London St Pancras
- Amsterdam Centraal → London St Pancras
- London St Pancras → Rotterdam Centraal
- London St Pancras → Amsterdam Centraal

You only get an alert when something **new** shows up (one message per run,
however many tickets). If tickets vanish and later reappear, you're alerted again.
If the watcher itself breaks (e.g. Eurostar starts blocking it), you get one
"watcher is failing" message so you're not silently missing tickets.

Runs free on GitHub Actions. No server, no payment.

---

## Step 1 — Set up phone notifications (recommended over email)

1. Install the **ntfy** app (iOS App Store / Google Play). Free, no account.
2. Invent a long, unguessable topic name, e.g. `harry-snap-8f3k29xq`.
   Anyone who knows the name can read the messages, so don't use something obvious.
3. In the app, tap **+** and subscribe to that topic name.

## Step 2 — (Optional) Email as well

Gmail won't accept your normal password from a script. You need an *app password*:

1. Go to https://myaccount.google.com/security and make sure **2-Step Verification** is on.
2. Go to https://myaccount.google.com/apppasswords, create one called "snap watcher".
3. Copy the 16-character password it shows (you'll only see it once).

## Step 3 — Put the code on GitHub

1. Make a free account at https://github.com if you don't have one.
2. Top right **+** → **New repository**. Name it `snap-watcher`.
   Choose **Public** (public repos get unlimited free Actions minutes; private ones
   get 2,000/month, which this would use up in about a week).
   Your secrets (topic, email, password) stay hidden even in a public repo.
3. Click **Create repository**, then on the next page click **uploading an existing file**.
4. Drag in `snap_watch.py`, `requirements.txt`, `README.md`, `.gitignore`.
   Click **Commit changes**.
5. The workflow file lives in a hidden folder, so add it by hand:
   **Add file → Create new file**, type the name
   `.github/workflows/snap-watch.yml` (typing the slashes creates the folders),
   paste in the contents of `snap-watch.yml`, **Commit changes**.

## Step 4 — Add your secrets

In the repo: **Settings → Secrets and variables → Actions → New repository secret**.
Add each of these (name exactly as written):

| Name | Value |
|---|---|
| `NTFY_TOPIC` | your topic name from Step 1 |
| `GMAIL_ADDRESS` | the Gmail account that sends (optional) |
| `GMAIL_APP_PASSWORD` | the app password from Step 2 (optional) |
| `ALERT_EMAIL` | where alerts go, e.g. harryfyjiswalker@gmail.com (optional) |

Optional, under the **Variables** tab on the same page:

| Name | Value |
|---|---|
| `DAYS_AHEAD` | how many days ahead to scan (default 7) |
| `DATES` | e.g. `2026-11-20,2026-11-21` to watch only specific dates |

## Step 5 — Test it

1. **Actions** tab → if asked, click the green button to enable workflows.
2. Click **Eurostar Snap watcher** → **Run workflow**, set debug to `1`, run.
3. Click the run to watch the log. Each route/date line should say
   `N priced item(s) on page`, not `BLOCKED` or `ERROR`.
4. At the bottom of the run page, download the **debug** artifact and look at a
   couple of screenshots to confirm it's seeing real Snap results.

After that it runs on its own. The first real run will probably alert you about
everything currently on sale; after that only new releases.

---

## Things to know

- **GitHub's "every 5 minutes" is best-effort.** In practice runs are often
  10–20 minutes apart at busy times. If you need tighter timing, run it on your
  own computer instead: `pip install -r requirements.txt`,
  `python -m playwright install chromium`, set the env vars, then
  `python snap_watch.py --loop`.
- **Eurostar has bot protection.** GitHub's servers are data-centre IPs, which
  are more likely to be blocked than a home connection. If you get the
  "watcher is failing" alert, running it at home (laptop, or an old
  Raspberry Pi) is the fix.
- **How it decides there are tickets:** it reads the visible page and looks for
  prices (£/€). If a page has a fixed price somewhere unrelated to availability,
  you'd get one spurious alert for it — check the debug screenshots on the first run.
- GitHub pauses scheduled workflows in repos with no commits for 60 days. The
  script touches `state.json` monthly to prevent this.
- Snap's terms may not welcome automated checking. This only loads the same
  pages you would, at a modest rate, but use it at your own discretion.
