# Handoff: Slack alerts for approved headcount

This is the page to open when you take over this automation, or when something about it needs attention. You don't need to know how to code. Anything in `[brackets]` gets filled in for each customer at setup.

**What it does.** When a headcount request is approved in CandorIQ, a message is posted to the customer's Slack channel within a few seconds. It runs by itself and needs no day-to-day attention.

## At a glance

| | |
| --- | --- |
| Customer | `[Customer name]` |
| Slack channel | `[#channel-name]` (private, because messages include budget) |
| Where it runs | `[n8n URL]`, workflow **Customer - headcount.approved - Slack** |
| Status | `[Active since YYYY-MM-DD]` |
| Failure alerts go to | `[ops Slack channel or email]` |
| Missed notifications log | `[link to the table]` |

## People

| Role | Name | When to contact them |
| --- | --- | --- |
| Owner (CS) | `[name]` | Anything about this automation. Update this row on handover. |
| Customer contact | `[name, email]` | Before changing the channel or the message, and whenever notifications stop. |
| Customer Slack admin | `[name, email]` | To create a new Slack connection. |
| Engineering backup | `[name or team]` | New fields, new events, or anything under "Escalate" below. |

## Secrets it uses

Both are stored in n8n under **Credentials**. Never paste them into Slack, email, tickets, or this page. Anyone who sees one of them can post as this automation or send it fake approvals.

| Credential in n8n | What it is | Who can issue a new one | How to replace it |
| --- | --- | --- | --- |
| **CandorIQ - webhook token** | The password CandorIQ sends with each notice, so n8n knows it's genuine. | Us (CandorIQ admin settings) | Generate a new token, then update it in CandorIQ's webhook settings **and** in this n8n credential within the same few minutes. If only one side is updated, every notice gets rejected until the other catches up. If CandorIQ retries failed deliveries (to be confirmed), approvals during that gap are resent. Otherwise, check with the customer for approvals made in that window. |
| **Slack - headcount channel** | The secret link that lets n8n post to the channel. | Customer Slack admin | They create a new one, you paste it into the credential and save, then send a test. |

## Common tasks

Every task starts the same way: log in to n8n and open the workflow **Customer - headcount.approved - Slack**. You'll see a row of boxes, one per step.

### A notification didn't show up

1. Click **Executions** at the top. Each row is one approval that came in.
2. **Green** means it was posted. Check that you're looking at the right channel.
3. **Red** means it failed. Click the row to see which box turned red and read its message.
   - The Slack box says "not found" or "gone": the Slack connection was removed. Get a new one (see *Post to a different channel*), then click **Retry** on the failed execution.
   - The Slack box says "timeout" or "server error": Slack was down. Click **Retry** once Slack is back.
   - The **Format message** box failed: CandorIQ sent something unexpected. Escalate.
4. **No row at all** means CandorIQ never sent the notice, or n8n was down at the time. Escalate.
5. Afterwards, check the **missed notifications log**. Every approval that never reached Slack is listed there as `open`, and switches to `resolved` on its own once that approval is posted. An `open` row means it still hasn't been delivered. Retrying is safe: an approval that already posted is skipped. In rare cases a message can appear twice, which is better than a missed one.

### Post to a different channel

1. Tell the customer contact. The new channel should also be private.
2. The customer's Slack admin creates a Slack connection (an "incoming webhook") for the new channel and shares it with you securely.
3. In n8n, open **Credentials**, then **Slack - headcount channel**. Paste the new link and save.
4. Send a test (below). You don't need to change the workflow itself.
5. Update *At a glance* and the change log.

### Change the wording of the message

1. Open the box named **Format message**.
2. At the very top there's a section marked `MESSAGE SETTINGS: safe to edit`. Change only the words inside the quotes. For example, change `title: 'Headcount approved'` to `title: 'New role approved'`.
3. Don't change anything below the `END OF MESSAGE SETTINGS` line. That's the logic, and a small slip there can stop notifications.
4. Save, send a test, and add a line to the change log.

### Turn it off for a while

Flip the **Active** switch at the top right of the workflow. Approvals made while it's off are **not** posted later, so let the channel know first.

### Send a test

Ask engineering to send the sample approval, or run the command below with the sample file (`headcount_approved.json`, kept with the engineering files for this automation). Use a scratch channel if you can.

```sh
curl -X POST "[n8n URL]/webhook/candoriq/headcount-approved" \
  -H "X-CandorIQ-Token: [token]" -H "Content-Type: application/json" \
  --data @headcount_approved.json
```

A message like the one below should appear in the channel, and a green row should show under **Executions**.

```
Headcount approved
Position:         Senior Data Engineer     Department:    Data Platform
Approved budget:  $185,000.00              Requested by:  John Doe
Request hc_4821
```

## What "normal" and "not normal" look like

| You see | Meaning | Action |
| --- | --- | --- |
| A message that says "Not provided" and has a warning note | CandorIQ left out a detail. The message was still delivered. | None, unless it happens every time. Then escalate, because CandorIQ may have changed its format. |
| An alert in `[ops channel]` | An approval couldn't be delivered after several tries. | Follow *A notification didn't show up*. |
| Several red executions in a row | The Slack connection is probably broken. | Follow *A notification didn't show up*. |
| No executions for weeks | Either there were no approvals, or CandorIQ stopped sending. | Ask the customer whether anything was approved. If it was, escalate. |

## Escalate to engineering when

- The **Format message** box fails, or messages keep saying "Not provided".
- An approval never shows up under **Executions**.
- Someone asks for a new field, a new kind of event (for example, rejected requests), or a second channel.

Engineers: the design, edge cases, and code are in [README.md](README.md#technical-design). After any change to the **Format message** code, edit [format_message.js](format_message.js), run `node --test scenario_2/test_format_message.mjs`, and paste the file back into the box.

## Terms

| Term | Meaning |
| --- | --- |
| **Handoff** | This page: everything the next owner needs to run, check, and fix the automation. |
| **Webhook** | An automatic notice one system sends another when something happens. CandorIQ sends one each time a request is approved. |
| **n8n** | The tool that runs this automation, built by connecting boxes on a screen. |
| **Workflow** | One automation in n8n: the row of boxes. |
| **Execution** | One run of the workflow, which here means one approval. |
| **Credential** | A saved password or secret link in n8n. |
| **Missed notifications log** | A table of approvals that couldn't be delivered. Each row is `open` until that approval posts, then `resolved`. Engineers call this a dead-letter table. |

## Change log

Add a line every time something changes. After any change to the workflow, also export it (**⋯ → Download**) and save the file to `[team drive or repo]` so it can be restored.

| Date | Who | What changed | Why |
| --- | --- | --- | --- |
| `[YYYY-MM-DD]` | `[name]` | Initial setup | `[customer request / ticket]` |