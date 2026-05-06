# Marriott Points Watcher — Residence Inn Wenatchee

This repo runs a scheduled GitHub Actions job that checks whether **Residence Inn by Marriott Wenatchee** is bookable with Marriott Bonvoy points for:

- Check-in: June 19, 2026
- Check-out: June 20, 2026
- 1 room
- 1 adult
- Marriott points enabled

When points availability appears, it sends you an email.

## 1) Create the GitHub repo

Create a new private GitHub repo, then upload these files exactly as-is, including the `.github/workflows/marriott-watch.yml` file.

## 2) Add GitHub Actions secrets

In your GitHub repo:

**Settings → Secrets and variables → Actions → Secrets → New repository secret**

Add these secrets:

| Secret | Example |
|---|---|
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_PORT` | `587` |
| `SMTP_USER` | `your_email@gmail.com` |
| `SMTP_PASS` | Gmail app password, not your normal Gmail password |
| `EMAIL_FROM` | `your_email@gmail.com` |
| `EMAIL_TO` | `your_email@gmail.com` |

For Gmail, create a Gmail App Password and use that as `SMTP_PASS`.

## 3) Optional variables

You can leave these unset because the workflow already defaults to Wenatchee Residence Inn.

**Settings → Secrets and variables → Actions → Variables → New repository variable**

| Variable | Default |
|---|---|
| `HOTEL_NAME` | `Residence Inn by Marriott Wenatchee` |
| `BOOKING_URL` | Marriott hotel overview URL |
| `WATCH_URL` | Marriott rate-list URL with `propertyCode=EATRI` and points enabled |

## 4) Test it manually

Go to:

**Actions → Marriott Points Watch → Run workflow**

Open the run logs. You should see JSON output showing whether it detected points availability.

## 5) Schedule

The workflow runs every 20 minutes using cron:

```yaml
- cron: "*/20 * * * *"
```

GitHub Actions cron runs in UTC. You can change this in `.github/workflows/marriott-watch.yml`.

## 6) How duplicate alerts are avoided

The script stores the previous availability state in `.marriott_points_watch_state.json`.

The workflow persists that file between runs using GitHub Actions cache restore/save steps. It emails only when availability flips from unavailable to available.

## 7) Important limitations

Marriott pages are dynamic and sometimes change their layout. This script uses browser automation plus text heuristics, so it is not as reliable as an official Marriott inventory API.

Use the manual **Run workflow** button if you want to test after changing secrets or URLs.
