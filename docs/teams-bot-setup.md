# Teams Bot Registration Guide

The portal can send each person their notifications (post sent for review, changes requested, decision made) as a
direct message from a Teams bot. This guide is for IT. It follows the same steps as the app registration guide, with
three differences:

- There is **no callback URL** and **no API permissions** step. The bot doesn't sign anyone in.
- Only **three keys** are needed (listed at the end). The Object ID and Secret ID are not.
- There are **three extra parts** after the registration: create the bot, upload the Teams app, and (optionally) push it to users.

**Cost:** none if only the bot resource is created. Microsoft Teams is a standard channel on Azure Bot Service, which has
unlimited messages on the free tier (F0), and the app registration is free. An Azure subscription is needed to create the
bot resource. Check the cost estimate shown in the Azure portal before creating it.

**Roles needed:** Parts 1 and 2 need someone who can register apps in Entra and create resources in the Azure subscription.
Parts 3 and 4 need a Teams administrator.

---

## Part 1. App registration

1. Go to Microsoft Entra Admin Centre (https://entra.microsoft.com/#home)
2. Search "App registrations" in the search bar.
3. Click on "New registration"
4. Enter name "Compliance Portal Bot"
5. Select "Accounts in this organizational directory only (Single tenant)"
6. Click on Register. **Do not add a Callback URL or Redirect URI.**
7. Copy 2 keys: **Application (client) ID** and **Directory (tenant) ID**
8. Click on "Add a certificate or secret"
9. Click on "New client secret"
10. Enter "Compliance Portal Bot client secret" as description
11. Choose the longest expiry the policy allows (180 days is the default) and note the date, so it can be renewed
12. Click on Add
13. Copy the **Value** (it is shown only once). The Secret ID is not needed.
14. **Do not add any API permissions.**

## Part 2. Create the bot (Azure portal)

15. Go to the Azure portal (https://portal.azure.com) and click "Create a resource"
16. Search "Azure Bot" and click Create
17. Bot handle: "compliance-portal-bot". Choose the subscription and resource group.
18. Pricing tier: **Free (F0)**
19. Type of App: **Single Tenant**. Select "Use existing app registration" and enter the Application (client) ID and Directory (tenant) ID from Part 1.
20. Click Create, then open the new bot resource. **Create only the bot.** Do not add a Web App, App Service plan or
    Application Insights: the portal's own server answers the bot, and those extras are billed.
21. Go to **Configuration** and set **Messaging endpoint** to: `https://<the portal's API address>/api/teams/messages`
    (it must be public HTTPS, because Microsoft calls it) and click Apply
22. Go to **Channels**, click **Microsoft Teams**, accept the terms and click Apply

## Part 3. Upload the Teams app

23. Open `teams/manifest.json` from the portal repository. Replace both `REPLACE_WITH_BOT_APP_ID` values with the Application (client) ID.
24. Optional: replace `teams/color.png` (192x192) and `teams/outline.png` (32x32, white on transparent) with the company logo.
25. Select the three files (`manifest.json`, `color.png`, `outline.png`) and zip them. The files must be at the top level of the zip, not inside a folder.
26. Go to the Teams admin center (https://admin.teams.microsoft.com), then Teams apps, then Manage apps
27. Click "Upload new app" and choose the zip
28. Open the app and make sure its status is Allowed

## Part 4. Install it for people (recommended)

29. In the Teams admin center go to Teams apps, then Setup policies
30. Open the policy the users have (or Global), click "Add apps" under Installed apps, and add "Compliance Portal"
31. Save. People get the bot within a few hours and it greets them. Nobody has to do anything.

If this is skipped, each person adds the app themselves (Teams, Apps, search "Compliance Portal") and writes "hi" to it once.

## Keys to send back

Application (client) ID
Directory (tenant) ID
Secret **Value**

Send them securely (not by email or chat) to the person deploying the portal, who puts them in the server's `.env`.
They are not needed: Object ID, Secret ID.

---

# For the developer deploying it

## What goes in the server's `.env`

```
TEAMS_BOT_APP_ID=<Application (client) ID>
TEAMS_BOT_APP_PASSWORD=<client secret Value>
TEAMS_BOT_TENANT_ID=<Directory (tenant) ID>
# Where the "Open in the portal" button goes (already used for sign-in redirects):
FRONTEND_URL=https://<the portal's address>
```

Then recreate the API container so it reads them (`docker compose ... up -d --build --force-recreate api`).
Setting `TEAMS_ENABLED=false` turns the feature off without removing the other settings. Until all three are set, the
feature does nothing.

## How people are matched to portal accounts

When someone installs the bot or writes to it, Teams tells the portal who they are. The portal links the chat to the
account with the same Microsoft Entra id, or failing that the same work email. Someone who has never signed in to the
portal is told so by the bot and gets nothing until they have (then they write to the bot once more).

## Checking it works

1. Install the app for yourself in Teams. The bot should greet you: "Hi! I'll send you your compliance portal notifications here...".
2. In the portal, send a post for review to yourself (or have someone do it). A card with an "Open in the portal"
   button should arrive in the chat.
3. Write `pause`, repeat step 2: nothing arrives. Write `resume`: it does.

## If it doesn't work

| Sign | Likely cause |
|---|---|
| The bot never greets you | The messaging endpoint isn't reachable from the internet, or is wrong. Check the Azure Bot's "Test in Web Chat". The server log shows "Teams request refused" lines when a token or address is wrong. |
| It greets you but says it can't find your account | You haven't signed in to the portal with this work account yet, or your Teams email differs from your portal email. |
| Messages stopped for one person | The bot was removed from their Teams, or they left. The portal marks the chat inactive and stops trying; they write to the bot again to restart. |
| `401` in the log | The app secret expired or the tenant id is wrong. |

For a local test, put a tunnel in front of the portal (for example `ngrok http 8009`) and use its address as the messaging endpoint.

## Security notes

- Requests to `/api/teams/messages` are accepted only with a valid Bot Framework token for this bot, and only from your tenant.
- The bot replies only to Microsoft addresses (`smba.trafficmanager.net` and the other Teams service hosts), so its
  credentials can't be sent anywhere else.
- Only 1:1 chats are used. The bot ignores group chats and channels.
- Rotate the client secret before it expires and update `TEAMS_BOT_APP_PASSWORD`.
