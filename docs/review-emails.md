# Review workflow emails

Uses the same SendGrid v3 transport, environment variables, branded HTML shell,
plain-text alternative, development `[TEST]` prefix, and sandbox mode as the admin dashboard.
No Microsoft Teams chat messages are dispatched by the notification service while email is in use.
In-app, SSE, MQTT and cross-portal notifications continue to work.

## Delivery

- **Reviewers, 9:00 a.m. in each reviewer's local time zone daily:** one personal digest with two sections:
  new submissions and updated/resubmitted work from the previous **recipient-local calendar day**.
  Only work still awaiting that reviewer's review is listed. Posts and scripts share the digest.
  Each item includes its title, reference, deadline and a direct portal link.
- **Reviewers, one hour before the deadline:** one reminder for each pending post/script.
- **Creators, after a decision:** an email when a post or script is approved, edited by a
  reviewer, returned for changes, or returned with permission to publish after changes.
  Review notes and the available changes deadline are included, with a direct link.
- **Creators, one hour before the changes deadline:** one reminder to edit and resubmit.

The scheduler runs every minute. Each recipient's 9 a.m. digest catches up later in
their local day after an outage. Empty digests are suppressed. Weekends are included.
IANA time zones handle each person's daylight saving changes, including 23/25-hour days.

The app detects the browser's IANA time zone on sign-in/app entry and on window focus,
and saves it through the authenticated `/api/notifications/email-timezone` endpoint.
No manual setting is required. After travel, focusing the app updates the saved zone.
The most recently used browser sets the person's zone. Reviewers must open the app
once to capture it; until then, the configured team zone is used as a fallback.
Email deadline dates are also displayed in each recipient's saved local zone.

Uploaded scripts and batch items are selected through their submissions. Scripts reviewed
in the shared editor use their canonical documents; linked submissions are excluded to
avoid duplicate emails. Checked-in script reviewers no longer receive reminders for
that round. Level-two review goes to content managers. Removed/inactive assignees,
withdrawn or deleted work, and completed reviews are checked again before delivery.

Explicit deadlines take precedence. Scripts without a persisted deadline use the same
standard review/revision windows configured by `REVIEW_DUE_HOURS` and `REVISION_DUE_HOURS`;
a live script's revision window starts at its review decision. This does not change the
existing in-app reminder schedule.

## Configuration

| Variable | Value |
| --- | --- |
| `SENDGRID_API_KEY` | Existing admin-compatible SendGrid credential; legacy `SENDGRIP_API_KEY` is also supported |
| `MAIL_FROM_ADDRESS` | Existing SendGrid verified sender |
| `MAIL_FROM_NAME` | Social media portal sender name |
| `FRONTEND_URL` | Full URL of this social portal, used for all links |
| `REVIEW_EMAIL_ENABLED` | `true` by default; set `false` to pause delivery |
| `REVIEW_EMAIL_TIMEZONE` | Fallback only until a person has a saved browser zone: `REVIEW_TIMEZONE` or `America/Toronto` |
| `APP_ENV` | `production` omits the `[TEST]` subject prefix |
| `SENDGRID_SANDBOX_MODE` | `true` validates requests with SendGrid without delivering |

The scheduler stays inactive without a SendGrid key or a valid `FRONTEND_URL`.
Do not point a deployed portal's email links at localhost. Never commit mail credentials.

## Persistence and operations

Startup creates `review_email_state`, `review_email_outbox` and `review_email_preferences` additively.
The activation timestamp prevents old review decisions from generating a historical
email burst when this feature is first installed. Existing pending work still qualifies
for digests and one-hour reminders. No old data is removed or rewritten.

Unique delivery keys suppress repeated scheduler passes, and a PostgreSQL advisory
lock coordinates API workers. Leased queue items are retried after errors or process
restarts; provider acceptance marks them sent. Deadline changes and resubmissions get
new reminder keys. Expired/completed reminders and yesterday's unsent digests are cancelled.

Monitor counts by `kind`/`status` in `review_email_outbox`, `attempts`, `available_at` and
`sent_at`. Provider failures stay pending with exponential backoff, capped at one hour.
A crash after provider acceptance but before recording it can still cause a retry;
SendGrid does not provide an exactly-once delivery guarantee for this API.

Verification: `python -m unittest tests.test_review_emails tests.test_review_email_timezones tests.test_deadlines`.
All automated tests mock delivery; PostgreSQL probes use temporary queue rows.
