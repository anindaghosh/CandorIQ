// Run: node --test scenario_2/
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const here = path.dirname(fileURLToPath(import.meta.url));
const load = (rel) => readFileSync(path.join(here, rel), 'utf8');
const sample = (name) => JSON.parse(load(`samples/${name}.json`));

// Evaluate the exact source that gets pasted into n8n. `$input` is undefined here,
// so the n8n glue at the bottom is skipped and the function is returned instead.
const formatMessage = new Function(`${load('format_message.js')}\nreturn formatMessage;`)();

const fieldText = (msg) => msg.blocks.find((b) => b.type === 'section').fields.map((f) => f.text).join('\n');
const contextText = (msg) => msg.blocks.find((b) => b.type === 'context').elements[0].text;

test('happy path renders all four fields and no warning', () => {
  const r = formatMessage(sample('headcount_approved'));
  assert.equal(r.ok, true);
  assert.equal(r.eventId, 'evt_01J9ZK3M8Q');
  assert.deepEqual(r.warnings, []);
  const t = fieldText(r);
  assert.match(t, /Senior Data Engineer/);
  assert.match(t, /Data Platform/);
  assert.match(t, /\$185,000\.00/);
  assert.match(t, /John Doe/);
  assert.doesNotMatch(contextText(r), /Missing/);
  assert.match(r.text, /Senior Data Engineer/);
});

test('missing fields use placeholders, warn, and still send', () => {
  const r = formatMessage(sample('missing_fields'));
  assert.equal(r.ok, true);
  assert.deepEqual(r.warnings, ['department', 'approved budget']);
  assert.match(fieldText(r), /\*Department\*\nNot provided/);
  assert.match(fieldText(r), /\*Approved budget\*\nNot provided/);
  assert.match(fieldText(r), /bruce\.wayne@example\.com/, 'requester falls back to email');
  assert.match(contextText(r), /Missing from CandorIQ, check the request in CandorIQ: department, approved budget/);
});

test('wrong event is rejected with 400', () => {
  const r = formatMessage(sample('wrong_event'));
  assert.equal(r.ok, false);
  assert.equal(r.status, 400);
  assert.match(r.error, /headcount\.rejected/);
});

test('missing event id is rejected (cannot de-duplicate)', () => {
  const body = sample('headcount_approved');
  delete body.id;
  const r = formatMessage(body);
  assert.equal(r.ok, false);
  assert.match(r.error, /event id/);
});

test('non-object and empty bodies are rejected, not thrown', () => {
  for (const bad of [undefined, null, 'x', 42, []]) {
    const r = formatMessage(bad);
    assert.equal(r.ok, false);
  }
});

test('missing data object warns on every field', () => {
  const r = formatMessage({ id: 'evt_1', event: 'headcount.approved' });
  assert.equal(r.ok, true);
  assert.equal(r.warnings.length, 4);
});

test('budget: currencies, no currency, bad values', () => {
  const withBudget = (approved_budget) =>
    formatMessage({ id: 'e', event: 'headcount.approved', data: { approved_budget } });
  assert.match(fieldText(withBudget({ amount: 90000, currency: 'EUR' })), /€90,000\.00/);
  assert.match(fieldText(withBudget({ amount: 90000, currency: 'usd' })), /\$90,000\.00/);
  // No currency: plain number, never a made-up $.
  const noCur = fieldText(withBudget({ amount: 90000 }));
  assert.match(noCur, /90,000/);
  assert.doesNotMatch(noCur, /\$/);
  // Unknown currency code is shown, not thrown.
  assert.match(fieldText(withBudget({ amount: 5, currency: 'ZZZZ' })), /5 ZZZZ/);
  // Numeric string accepted; junk is treated as missing.
  assert.match(fieldText(withBudget({ amount: '1200', currency: 'USD' })), /\$1,200\.00/);
  assert.deepEqual(withBudget({ amount: 'lots', currency: 'USD' }).warnings, [
    'position title', 'department', 'approved budget', 'requester',
  ]);
  assert.deepEqual(withBudget({ amount: null }).warnings.includes('approved budget'), true);
});

test('mrkdwn control characters in payload text are escaped', () => {
  const body = sample('headcount_approved');
  body.data.position_title = '<!channel> Chief <https://evil.example|Officer> & Co';
  const t = fieldText(formatMessage(body));
  assert.doesNotMatch(t, /<!channel>/);
  assert.doesNotMatch(t, /<https/);
  assert.match(t, /&lt;!channel&gt; Chief &lt;https:\/\/evil\.example\|Officer&gt; &amp; Co/);
});

test('whitespace-only strings count as missing', () => {
  const body = sample('headcount_approved');
  body.data.department = '   ';
  const r = formatMessage(body);
  assert.deepEqual(r.warnings, ['department']);
});

test('wording comes from the SETTINGS block', () => {
  const src = load('format_message.js').replace("title: 'Headcount approved'", "title: 'New role approved'");
  const fm = new Function(`${src}\nreturn formatMessage;`)();
  const r = fm(sample('headcount_approved'));
  assert.equal(r.blocks[0].text.text, 'New role approved');
  assert.match(r.text, /^New role approved:/);
});

test('very long values are clipped so Slack still accepts the message', () => {
  const body = sample('headcount_approved');
  body.data.position_title = '&'.repeat(2500);
  body.data.request_id = 'r'.repeat(2500);
  const r = formatMessage(body);
  for (const f of r.blocks[1].fields) assert.ok(f.text.length <= 2000, `field is ${f.text.length} chars`);
  assert.ok(contextText(r).length <= 2000);
  assert.match(fieldText(r), /…/);
  assert.equal(r.eventId, 'evt_01J9ZK3M8Q', 'event id is never clipped');
});
