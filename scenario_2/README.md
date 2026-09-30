# Scenario 2: Slack alerts for approved headcount

Whenever a headcount request is approved in CandorIQ, a message shows up in the customer's Slack channel. The first section is for anyone. The technical design follows it. If you're the one looking after this day to day, go straight to the [handoff doc](HANDOFF.md).

## In plain terms

**What it does.** When someone approves a headcount request in CandorIQ, CandorIQ sends out a notice. An automation tool (n8n) catches it, turns it into a readable message, and posts it to one Slack channel. It usually takes a few seconds.

**What the channel sees:**

```
Headcount approved
Position:         Senior Data Engineer     Department:    Data Platform
Approved budget:  $185,000.00              Requested by:  John Doe
Request hc_4821
```

**If something goes wrong, nothing is lost quietly.** The thing I cared about most was that nobody should ever have to wonder whether an approval got missed.

| What happens | What people see | What someone needs to do |
| --- | --- | --- |
| Slack is down for a moment | Nothing. The message arrives a little late. | Nothing. It retries on its own. |
| Slack is down for a long time, or the Slack connection was removed | The approval goes into a **missed notifications log**, and our team gets an alert. | We fix the connection and resend. In rare cases the channel sees a message twice, which we accept over a missed one. |
| CandorIQ leaves out a detail, such as the budget | The message still posts, with "Not provided" in that spot and a note saying what was missing. | Look up the request in CandorIQ if you need that detail. |
| The same approval is sent twice | One message. | Nothing. |

**What we need from the customer (one time, about 15 minutes):**

1. Choose the Slack channel. The message includes the approved budget, so **it should be a private channel** limited to people who may see compensation data.
2. Their Slack admin creates the Slack connection for that channel and shares it with us securely. We send step-by-step instructions.
3. Name a contact we can reach if the connection breaks.

**Who looks after it.** A named CS owner. Everything they need is in the **[handoff doc](HANDOFF.md)**, a short operating guide written for people who don't code. It has contacts, the step-by-step fixes for "a notification didn't show up", how to change the channel or wording, and a list of terms.

---

# Technical design

## Where this stands

I tested the message-building code ([format_message.js](format_message.js)). It covers validation, missing fields, currency, escaping, very long values, and the editable wording, and you can run it with `node --test scenario_2/test_format_message.mjs` (Node 18 or later, no packages to install).

I haven't built the n8n workflow itself, so the header check, the "sent" table, the Slack retries, the 503 reply and the missed notifications log are design, not something I've run. The steps in section 2 are detailed enough to build from. Once it's built, I'd check it by sending the three files in [samples/](samples/), then a wrong token, a repeated event, and a deliberately broken Slack link, and confirming each gives the reply and Slack result described in section 3.

## Assumptions

The brief left a few things open, so here is what I'm assuming.

CandorIQ sends a `POST` with a JSON body and can attach one custom header. I'd use a shared secret in that header for auth, and switch to checking a signature (HMAC) if CandorIQ starts signing its webhooks.

I'm assuming the payload looks like [samples/headcount_approved.json](samples/headcount_approved.json). The brief names the fields (requester, position title, approved budget, department) but not what they're called or how they're nested, so I'd confirm the exact shape with product before going live.

I'm also assuming each event has an `id` that stays the same when CandorIQ redelivers it. The duplicate protection relies on that, so it's worth confirming alongside the payload shape.

Finally, I'm assuming the customer owns the Slack side: a Slack app with an Incoming Webhook tied to one channel for delivery of the messages.

## 1. Approach

The customer doesn't want us hosting code, and this is a small job: receive an event, check it, reformat it, post it. That's a good fit for n8n (their instance or ours, their choice) as the glue. The Webhook node receives the event, one Code node validates and formats it, and an HTTP Request node posts to Slack. Everything else is configuration, which matters for four reasons:

- **Credentials** live in n8n's credential store, not in code.
- **Retries and execution history** are built in. Every run is inspectable, so "did it fire?" is something a non-engineer can answer by looking.
- **Handoff** is a canvas of a few boxes plus one short script that only a message-wording change ever touches.
- **Nothing custom to host or patch.** The customer's exposure is the n8n instance they already chose to run.

I picked an Incoming Webhook over the Slack Web API (`chat.postMessage`) because it needs no OAuth scopes or token refresh, it's bound to a single channel, and the customer's Slack admin can revoke or rotate it in seconds. If they later need threads, edits, or multiple channels, that's the point to switch to a bot token.

## 2. Flow design

Read top to bottom. Each numbered box is one box on the n8n canvas, and the arrows to the right are the ways out of the flow early.

```
CandorIQ sends a POST with the header X-CandorIQ-Token
        |
        v
1. Receive webhook ---------- wrong or missing token --> reply 401, stop
        |
        v
2. Format message ----------- payload unusable --------> A. Record failure --> reply 400
        |
        v
3. Already sent? ------------ yes ---------------------> reply 200, stop
        |  no                (event id is in "sent" table)
        v
4. Post to Slack ------------ still failing after -----> A. Record failure --> reply 503
        |                      3 tries
        |  Slack accepted
        v
5. Mark as sent               (write event id to "sent" table, and mark any
        |                      open log row for this event as resolved)
        |
        v
6. Reply 200 to CandorIQ

A. Record failure = add the event to the missed notifications log, then alert ops
```

The reply codes matter because they tell CandorIQ what to do next:

| Reply | Meaning to CandorIQ |
| --- | --- |
| **200** | Handled. Either posted to Slack, or already posted before. Don't resend. |
| **400** | This delivery is unusable. Resending the same thing won't help. |
| **401** | Wrong or missing token. |
| **503** | We couldn't deliver it right now. Please try again later. |

| Concern | Where it is handled |
| --- | --- |
| **Authentication** | Step 1, *Receive webhook*. A Header Auth credential compares `X-CandorIQ-Token` to a secret. A wrong or missing token gets a 401 before any other step runs. The Slack URL is itself a secret, stored as an n8n credential and never logged in the message. |
| **Payload validation** | Step 2, *Format message*, checks that the body is an object, the event is `headcount.approved`, and an event `id` is present. Anything else returns 400. |
| **Formatting** | *Format message* also builds the Slack message. It trims values, formats the budget with its currency, substitutes `Not provided` for missing fields, and escapes `& < >` so payload text can't ping `@channel` or inject links. |
| **De-duplication** | Step 3, *Already sent?*, looks up the event `id` in a small "sent" table. Step 5, *Mark as sent*, writes the id **only after Slack accepts the message**, and closes any open row for that event in the missed notifications log. A redelivery doesn't post twice, and a failed attempt doesn't block its own retry. |
| **Retries** | Two layers. *Post to Slack* (step 4) retries up to 3 times with a short wait, kept under CandorIQ's request timeout. If that fails, the flow replies 503 so **CandorIQ's own retry policy** redelivers later. |
| **Failure capture** | *Record failure* (A). Record the event (id, time, raw payload, error, status) in the missed notifications log (a dead-letter table) with status `open`, and alert a human. The row flips to `resolved` when the same event later posts, so an `open` row always means "not delivered yet". |

Two of those choices need a bit more explanation.

**Why an explicit "sent" table instead of n8n's Remove Duplicates node.** That node records an item as seen when it passes through, which is before Slack is called. If Slack was down, the retry would look like a duplicate and get dropped, so the exact case I'm trying to protect would silently lose the message. An explicit lookup before, and a write after Slack accepts, avoids that.

**Why the id is written after Slack, not before.** This makes the flow at-least-once instead of at-most-once. In the rare case that Slack accepts the message but the "Mark as sent" write fails, a retry could post a second copy. I'm fine with that trade. A duplicate message is an annoyance, and a lost approval notice is a real miss.

The message the customer sees:

```
Headcount approved
Position: Senior Data Engineer        Department: Data Platform
Approved budget: $185,000.00          Requested by: John Doe
Request hc_4821
```

Settings to record on the canvas: webhook path `/webhook/candoriq/headcount-approved`, header `X-CandorIQ-Token`, credentials **CandorIQ - webhook token** and **Slack - headcount channel**, Slack retry 3 tries with about 3s wait. The script is [format_message.js](format_message.js).

## 3. Edge cases

| Situation | Behavior | Why |
| --- | --- | --- |
| **Slack unreachable or 5xx** | *Post to Slack* retries (3 tries, short wait). If it still fails, the event is written to the missed notifications log, ops is alerted, and the webhook answers **503**. CandorIQ retries per its own policy, and de-duplication makes that safe. | An approval notification is worth delivering late, and a 200 would tell CandorIQ not to retry. |
| **Slack returns 429** | Treated as any other Slack failure: a few retries with a short wait, then the failure path. | Volume is low (approvals), so a 429 means a Slack incident, not our load. |
| **Slack returns 404/410** | Same path as any other Slack failure: a few retries, then the missed notifications log and an alert. The alert text should say the Slack link was probably revoked or the channel deleted. | Retrying can't fix it, but it's harmless and keeps the design to one failure path. Someone must issue a new Slack link. |
| **Payload missing a field the template uses** | The message **still sends**, with `Not provided` in that slot and a footer: ":warning: Missing from CandorIQ, check the request in CandorIQ: department, approved budget". | A partial notification is better than silent loss. The footer keeps a missing value from looking like a real one. |
| **Budget present but no currency** | Show the number only, never assume `$`. | Same rule as scenario 1: a symbol alone doesn't identify a currency. |
| **Budget is not a number** | Treated as missing (`Not provided` plus warning). | I won't guess at it or display garbage. |
| **Wrong event type or no event id** | 400, recorded in the missed notifications log, no Slack post. | A 400 tells CandorIQ this delivery is malformed, so it shouldn't be retried. Without an id we can't de-duplicate. |
| **Duplicate delivery** | *Already sent?* finds the id in the "sent" table, skips the post, and answers 200. | Sender retries after a timeout are normal webhook behavior. |
| **Bad or missing token** | 401 from *Receive webhook*, nothing recorded except n8n's own log. | Unauthenticated traffic shouldn't create work or alerts. |
| **n8n itself is down or restarting** | Nothing on our side can help. CandorIQ's retry policy is the safety net. | **Open question for product:** does CandorIQ retry non-2xx or timed-out deliveries, how many times, and over what window? If it doesn't retry, the missed notifications log only covers failures after n8n has received the event, and we should tell the customer that. |
| **Malicious or odd text in fields** | Escaped before posting (tested). | Requester and title are user-entered. |
| **A very long value** | Cut to 300 characters with "…" (tested). | Slack rejects the whole message if one field is over 2,000 characters, and retrying wouldn't help, so the approval would never post. |
| **Sensitive data** | The message contains budget, which is compensation-adjacent data. Post only to a restricted channel and confirm this with the customer. | Slack messages are searchable and retained. |

Other open questions for product: the exact field names, whether `id` is stable across redeliveries, and CandorIQ's webhook timeout.

## 4. Handoff

Handoff happens through **[HANDOFF.md](HANDOFF.md)**, a filled-in template that lives next to the workflow (here, or copied into the customer's wiki), plus a sticky note on the n8n canvas that links to it. This is how it's organized and why:

| Handoff section | Why it's there |
| --- | --- |
| **At a glance / People** | The first question on any handover is where this runs and who to call. Contacts get updated when ownership changes. |
| **Secrets it uses** | Names each credential, who can issue a new one, and how to replace it. Replacing the CandorIQ token means updating both sides together, and that's easy to get wrong. |
| **Common tasks** | Click-by-click steps for the things that actually come up: a missing notification, a new channel, new wording, pausing it, sending a test. |
| **Normal vs. not normal** | Lets a non-engineer tell a harmless "Not provided" from a broken connection. |
| **Escalate when** | A clear line between what CS handles and what needs engineering, like new fields or events. |
| **Terms / Change log** | Plain definitions, and a record of who changed what, with an exported copy of the workflow after each change. |

Engineering changes the handoff doc doesn't cover: a *layout* change means editing `buildMessage()`. *A new field* means adding it to the CandorIQ payload, then a `pick(...)` line and a field in `buildMessage()`. *A new event* (say `headcount.rejected`) means duplicating the workflow rather than branching this one, so each stays simple.

### Keeping it maintainable by non-engineers

The test I kept in mind: could someone who never opens the code still run this?

- The handoff doc is a separate page for readers who never open the code. The engineering detail stays here in the README.
- Wording lives in a clearly marked `MESSAGE SETTINGS` block, so a non-engineer can edit text without touching logic. A test confirms the message is built from that block.
- Names describe what things do: **Format message** for the box, **Slack - headcount channel** for the credential, "missed notifications log" instead of "dead-letter table" in anything the customer reads.
- Tests protect the script: `node --test scenario_2/test_format_message.mjs` covers the happy path, missing fields, currency handling, escaping, long values, rejections, and the settings block. Run it after any edit to [format_message.js](format_message.js), then paste the file back into the Code node. The file is the source of truth, and the tests run that exact file.
- The failure path is visible without reading code: a red execution, a row in the missed notifications log, and an alert.
- Purpose, owner, and a link to the handoff doc go in a sticky note on the canvas.

## Files

| File | Purpose |
| --- | --- |
| [HANDOFF.md](HANDOFF.md) | Operating guide for CS and customer admins: contacts, secrets, step-by-step fixes, change log. |
| [format_message.js](format_message.js) | Validation and Slack formatting. Paste into the n8n Code node. |
| [test_format_message.mjs](test_format_message.mjs) | Tests, run with `node --test scenario_2/test_format_message.mjs`. |
| [samples/](samples/) | Happy path, missing fields, wrong event. |