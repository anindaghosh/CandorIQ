// Validate a headcount.approved webhook body and build the Slack message.
// Paste this whole file into the n8n Code node ("Run Once for All Items").
// The same file is what test_format_message.mjs runs, so tested code == deployed code.
//
// ===== MESSAGE SETTINGS: safe to edit =====
// Change the words inside the quotes to change what the Slack message says.
// Keep the quotes and the commas. Don't change anything below the END line.
const SETTINGS = {
  title: 'Headcount approved',
  positionLabel: 'Position',
  departmentLabel: 'Department',
  budgetLabel: 'Approved budget',
  requesterLabel: 'Requested by',
  requestLabel: 'Request',
  missingValue: 'Not provided',
  missingWarning: 'Missing from CandorIQ, check the request in CandorIQ',
};
// ===== END OF MESSAGE SETTINGS =====

const EVENT_NAME = 'headcount.approved';
const NOT_PROVIDED = SETTINGS.missingValue;
// Slack rejects the whole message if one field is over 2000 characters, so a very
// long value would block delivery on every retry. Escaping can grow text up to 5x.
const MAX_VALUE_LENGTH = 300;

// Slack mrkdwn treats & < > as control characters (<!channel>, <http://...|label>).
function esc(value) {
  return String(value).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function text(value) {
  if (typeof value !== 'string') return null;
  const trimmed = value.trim();
  return trimmed === '' ? null : trimmed;
}

function clip(value) {
  return value.length > MAX_VALUE_LENGTH ? `${value.slice(0, MAX_VALUE_LENGTH - 1)}…` : value;
}

function formatBudget(budget) {
  if (!budget || typeof budget !== 'object') return null;
  const raw = budget.amount;
  const amount = typeof raw === 'string' && raw.trim() !== '' ? Number(raw) : raw;
  if (typeof amount !== 'number' || !Number.isFinite(amount)) return null;
  const currency = text(budget.currency);
  if (currency) {
    try {
      return new Intl.NumberFormat('en-US', { style: 'currency', currency: currency.toUpperCase() }).format(amount);
    } catch (e) {
      // Unknown currency code: fall through and show the raw code next to the number.
      return `${amount.toLocaleString('en-US')} ${currency}`;
    }
  }
  // No currency in the payload: never assume $.
  return amount.toLocaleString('en-US');
}

function buildMessage(f, warnings) {
  const S = SETTINGS;
  const summary = `${S.title}: ${f.position} (${f.department})`;
  const blocks = [
    { type: 'header', text: { type: 'plain_text', text: S.title } },
    {
      type: 'section',
      fields: [
        { type: 'mrkdwn', text: `*${esc(S.positionLabel)}*\n${esc(f.position)}` },
        { type: 'mrkdwn', text: `*${esc(S.departmentLabel)}*\n${esc(f.department)}` },
        { type: 'mrkdwn', text: `*${esc(S.budgetLabel)}*\n${esc(f.budget)}` },
        { type: 'mrkdwn', text: `*${esc(S.requesterLabel)}*\n${esc(f.requester)}` },
      ],
    },
  ];
  const context = [`${esc(S.requestLabel)} ${esc(f.requestId)}`];
  if (warnings.length) context.push(`:warning: ${esc(S.missingWarning)}: ${warnings.join(', ')}`);
  blocks.push({ type: 'context', elements: [{ type: 'mrkdwn', text: context.join('  |  ') }] });
  // `text` is the fallback shown in phone and desktop notifications.
  return { text: summary, blocks };
}

function formatMessage(body) {
  if (!body || typeof body !== 'object') {
    return { ok: false, status: 400, error: 'body is not a JSON object' };
  }
  const eventId = text(body.id);
  if (body.event !== EVENT_NAME) {
    return { ok: false, status: 400, eventId, error: `unexpected event: ${JSON.stringify(body.event)}` };
  }
  if (!eventId) {
    // Without an id we cannot de-duplicate redeliveries, so refuse rather than risk double posts.
    return { ok: false, status: 400, error: 'missing event id' };
  }

  const data = body.data && typeof body.data === 'object' ? body.data : {};
  const requester = data.requester && typeof data.requester === 'object' ? data.requester : {};
  const warnings = [];
  const pick = (value, label) => {
    if (value === null) warnings.push(label);
    return value === null ? NOT_PROVIDED : clip(value);
  };

  const fields = {
    position: pick(text(data.position_title), 'position title'),
    department: pick(text(data.department), 'department'),
    budget: pick(formatBudget(data.approved_budget), 'approved budget'),
    // Fall back to email so we still name a person when only one of the two is sent.
    requester: pick(text(requester.name) || text(requester.email), 'requester'),
    requestId: clip(text(data.request_id) || eventId),
  };

  return { ok: true, status: 200, eventId, warnings, ...buildMessage(fields, warnings) };
}

// n8n glue. Skipped when the file is loaded outside n8n (tests).
// The Webhook node puts the JSON payload under `.body`.
if (typeof $input !== 'undefined') {
  return $input.all().map((item) => ({ json: formatMessage(item.json.body) }));
}
