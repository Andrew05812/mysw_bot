import { test } from 'node:test';
import assert from 'node:assert/strict';
import worker from './index.js';

const ENV = { WORKER_API_KEY: 'a'.repeat(64) };
let lastFetch = null;

function resetFetch() {
  lastFetch = null;
  globalThis.fetch = async (input) => {
    const url = String(input.url);
    const method = input.method || 'GET';
    let body = null;
    if (typeof input.text === 'function') {
      try { body = await input.text(); } catch { body = null; }
    }
    lastFetch = { input: url, method, body };
    return new Response(JSON.stringify({ ok: true, result: { id: 1 } }),
      { status: 200, headers: { 'content-type': 'application/json' } });
  };
}

function makeReq(path, { method = 'GET', key = ENV.WORKER_API_KEY, body } = {}) {
  const headers = {};
  if (key) headers['X-Worker-Api-Key'] = key;
  const opts = { method, headers };
  if (body !== undefined) opts.body = body;
  return new Request('https://tg-bot-api-proxy.test.workers.dev' + path, opts);
}

test('GET getMe with key proxies to api.telegram.org', async () => {
  resetFetch();
  const res = await worker.fetch(makeReq('/bot123TOKEN/getMe'), ENV);
  assert.equal(res.status, 200);
  assert.ok(lastFetch.input.startsWith('https://api.telegram.org/bot123TOKEN/getMe'));
});

test('GET getMe without key returns 401 and does not fetch', async () => {
  resetFetch();
  const res = await worker.fetch(makeReq('/bot123TOKEN/getMe', { key: null }), ENV);
  assert.equal(res.status, 401);
  assert.equal(lastFetch, null);
});

test('GET getMe with wrong key returns 401', async () => {
  resetFetch();
  const res = await worker.fetch(makeReq('/bot123TOKEN/getMe', { key: 'x'.repeat(64) }), ENV);
  assert.equal(res.status, 401);
  assert.equal(lastFetch, null);
});

test('POST sendMessage forwards body', async () => {
  resetFetch();
  globalThis.fetch = async (input) => {
    lastFetch = { input: String(input.url), method: 'POST', body: lastFetch && lastFetch.body };
    return new Response(JSON.stringify({ ok: true }), { status: 200 });
  };
  const body = JSON.stringify({ chat_id: 1, text: 'hi' });
  const res = await worker.fetch(makeReq('/bot123TOKEN/sendMessage', { method: 'POST', body }), ENV);
  assert.equal(res.status, 200);
  assert.ok(lastFetch.input.startsWith('https://api.telegram.org/bot123TOKEN/sendMessage'));
});

test('GET getUpdates preserves timeout query param', async () => {
  resetFetch();
  const res = await worker.fetch(makeReq('/bot123TOKEN/getUpdates?timeout=45&limit=100'), ENV);
  assert.equal(res.status, 200);
  assert.ok(lastFetch.input.includes('?timeout=45&limit=100'));
});

test('File API proxies binary body', async () => {
  const bin = new Uint8Array([0x89, 0x50, 0x4e, 0x47]);
  globalThis.fetch = async (input) => {
    lastFetch = { input: String(input.url) };
    return new Response(bin, { status: 200, headers: { 'content-type': 'application/octet-stream' } });
  };
  const res = await worker.fetch(makeReq('/file/bot123TOKEN/docs/file_1.bin'), ENV);
  assert.equal(res.status, 200);
  assert.ok(lastFetch.input.startsWith('https://api.telegram.org/file/bot123TOKEN/docs/file_1.bin'));
  const buf = Buffer.from(await res.arrayBuffer());
  assert.deepEqual(Array.from(buf), Array.from(bin));
});

test('unrecognized path returns 404 without fetch', async () => {
  resetFetch();
  const res = await worker.fetch(makeReq('/random'), ENV);
  assert.equal(res.status, 404);
  assert.equal(lastFetch, null);
});

test('upstream failure returns controlled 502', async () => {
  globalThis.fetch = async () => { throw new Error('upstream down'); };
  const res = await worker.fetch(makeReq('/bot123TOKEN/getMe'), ENV);
  assert.equal(res.status, 502);
  const j = await res.json();
  assert.equal(j.ok, false);
  assert.ok(!JSON.stringify(j).includes(ENV.WORKER_API_KEY));
});

test('Telegram 429 status and body preserved', async () => {
  globalThis.fetch = async () =>
    new Response(JSON.stringify({ ok: false, error_code: 429, description: 'Too Many Requests' }),
      { status: 429, headers: { 'content-type': 'application/json' } });
  const res = await worker.fetch(makeReq('/bot123TOKEN/sendMessage', { method: 'POST' }), ENV);
  assert.equal(res.status, 429);
  const j = await res.json();
  assert.equal(j.error_code, 429);
});

test('POST empty body accepted', async () => {
  resetFetch();
  globalThis.fetch = async (input) => {
    lastFetch = { input: String(input.url), method: 'POST' };
    return new Response('{}', { status: 200 });
  };
  const res = await worker.fetch(makeReq('/bot123TOKEN/getWebhookInfo', { method: 'POST', body: '' }), ENV);
  assert.equal(res.status, 200);
});
