# Pareeksha Gurukul v2 — Setup Guide

## Files
```
main.py          ← Bot + Flask server (single process)
static/index.html← WebApp UI (served by Flask)
requirements.txt ← Python packages
Procfile         ← Railway start command
railway.json     ← Railway config
.env.example     ← Variable reference
```

## Deploy on Railway (5 steps)

### 1. Push to GitHub
Create a repo and push all these files.

### 2. New Project on Railway
- railway.app → New Project → Deploy from GitHub repo
- Select your repo

### 3. Add Environment Variables
Go to your service → Variables tab → add:

| Variable      | Value |
|---------------|-------|
| BOT_TOKEN     | From @BotFather |
| EVAL_GROUP_ID | Negative number from your eval group |
| WEBAPP_URL    | Your Railway URL (set after first deploy) |
| ADMIN_IDS     | Your Telegram user ID (get from @userinfobot) |

### 4. Get your Railway URL
After first deploy → Settings → copy the public URL
Then update WEBAPP_URL variable with it → Railway auto-redeploys

### 5. That's it!
Railway auto-registers the webhook on startup.

---

## Admin Commands (in Telegram bot DM)

| Command | What it does |
|---------|-------------|
| /setquestion Your question here | Set today's question |
| /clearquestion | Disable submissions |
| /viewquestion | See current question |
| /stats | Submission count & breakdown |
| /resetsubmissions | Allow students to resubmit |

---

## Get Eval Group ID
1. Add bot to your evaluation group → make it Admin
2. Send any message in the group
3. Open: `https://api.telegram.org/bot<TOKEN>/getUpdates`
4. Find `"chat":{"id": -100xxxxxxxxxx}` — that's your EVAL_GROUP_ID

---

## Full Flow
```
Student → /start → clicks [Submit Your Answer]
→ WebApp opens → picks Text/Audio/Video → submits
→ server.py forwards to eval group with ⭐1-5 buttons
→ evaluator taps star → student gets DM with rating
```
