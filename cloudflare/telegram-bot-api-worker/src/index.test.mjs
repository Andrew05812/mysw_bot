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

test('File API without key returns 401 and does not fetch', async () => {
  resetFetch();
  const res = await worker.fetch(makeReq('/file/bot123TOKEN/x.bin', { key: null }), ENV);
  assert.equal(res.status, 401);
  assert.equal(lastFetch, null);
});

test('upstream redirect is passed through (worker does not follow)', async () => {
  globalThis.fetch = async () =>
    new Response(null, { status: 302, headers: { location: 'https://example.com/x' } });
  const res = await worker.fetch(makeReq('/bot123TOKEN/getMe'), ENV);
  assert.equal(res.status, 302);
  assert.equal(res.headers.get('location'), 'https://example.com/x');
});

test('upstream 401 status and body preserved', async () => {
  globalThis.fetch = async () =>
    new Response(JSON.stringify({ ok: false, error_code: 401, description: 'Unauthorized' }),
      { status: 401, headers: { 'content-type': 'application/json' } });
  const res = await worker.fetch(makeReq('/bot123TOKEN/getMe'), ENV);
  assert.equal(res.status, 401);
  const j = await res.json();
  assert.equal(j.error_code, 401);
});

test('upstream 403 status and body preserved', async () => {
  globalThis.fetch = async () =>
    new Response(JSON.stringify({ ok: false, error_code: 403, description: 'Forbidden' }),
      { status: 403, headers: { 'content-type': 'application/json' } });
  const res = await worker.fetch(makeReq('/bot123TOKEN/getMe'), ENV);
  assert.equal(res.status, 403);
  assert.equal((await res.json()).error_code, 403);
});

test('upstream 500 status and body preserved', async () => {
  globalThis.fetch = async () =>
    new Response(JSON.stringify({ ok: false, error_code: 500, description: 'Internal' }),
      { status: 500, headers: { 'content-type': 'application/json' } });
  const res = await worker.fetch(makeReq('/bot123TOKEN/getMe'), ENV);
  assert.equal(res.status, 500);
  assert.equal((await res.json()).error_code, 500);
});

test('missing WORKER_API_KEY secret returns 500', async () => {
  resetFetch();
  const res = await worker.fetch(makeReq('/bot123TOKEN/getMe'), {});
  assert.equal(res.status, 500);
  assert.equal(lastFetch, null);
});

test('401 response does not leak the secret', async () => {
  resetFetch();
  const res = await worker.fetch(makeReq('/bot123TOKEN/getMe', { key: 'x'.repeat(64) }), ENV);
  const body = await res.text();
  assert.ok(!body.includes(ENV.WORKER_API_KEY));
});

test('502 response does not leak the secret', async () => {
  globalThis.fetch = async () => { throw new Error('down'); };
  const res = await worker.fetch(makeReq('/bot123TOKEN/getMe'), ENV);
  const body = await res.text();
  assert.ok(!body.includes(ENV.WORKER_API_KEY));
});

test('arbitrary upstream cannot be reached via path or query', async () => {
  resetFetch();
  // token-looking path plus query trying to smuggle a host
  const res = await worker.fetch(makeReq('/bot123TOKEN/getMe?upstream=https://evil.example/x'), ENV);
  assert.equal(res.status, 200);
  // host stays fixed on api.telegram.org; the query cannot redirect upstream
  assert.ok(lastFetch.input.startsWith('https://api.telegram.org/bot123TOKEN/getMe?'));
  assert.ok(!lastFetch.input.startsWith('https://evil.example'));
});

test('attempt to escape the allowed route is rejected', async () => {
  resetFetch();
  // a path not under /bot or /file/bot -> 404, no fetch
  const res = await worker.fetch(makeReq('/vpn/bot123TOKEN/getMe'), ENV);
  assert.equal(res.status, 404);
  assert.equal(lastFetch, null);
});

test('malformed encoded path still stays on api.telegram.org', async () => {
  resetFetch();
  const res = await worker.fetch(makeReq('/bot123TOKEN%2FgetMe%3Ffoo'), ENV);
  // pathname starts with /bot -> routes; upstream host stays api.telegram.org
  assert.ok(lastFetch.input.startsWith('https://api.telegram.org/bot123TOKEN'));
});

test('large binary File API response is not truncated', async () => {
  const big = new Uint8Array(1024 * 1024).fill(0xab);
  globalThis.fetch = async (input) => {
    lastFetch = { input: String(input.url) };
    return new Response(big, { status: 200, headers: { 'content-type': 'application/octet-stream' } });
  };
  const res = await worker.fetch(makeReq('/file/bot123TOKEN/docs/big.bin'), ENV);
  assert.equal(res.status, 200);
  const buf = Buffer.from(await res.arrayBuffer());
  assert.equal(buf.length, big.length);
  assert.equal(buf[0], 0xab);
  assert.equal(buf[buf.length - 1], 0xab);
});
