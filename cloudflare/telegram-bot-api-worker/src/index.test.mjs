import { test } from 'node:test';
import assert from 'node:assert/strict';
import worker from './index.js';

const ENV = { WORKER_API_KEY: 'a'.repeat(64) };
let lastFetch = null; // { input, method, body, headers }

function resetFetch() {
  lastFetch = null;
  globalThis.fetch = async (input) => {
    const url = String(input.url);
    const method = input.method || 'GET';
    let body = null;
    if (typeof input.text === 'function') {
      try { body = await input.text(); } catch { body = null; }
    }
    lastFetch = { input: url, method, body, headers: input.headers };
    return new Response(JSON.stringify({ ok: true, result: { id: 1 } }),
      { status: 200, headers: { 'content-type': 'application/json' } });
  };
}

function makeReq(path, { method = 'GET', key = ENV.WORKER_API_KEY, body, headers = {} } = {}) {
  const h = {};
  if (key) h['X-Worker-Api-Key'] = key;
  Object.assign(h, headers);
  const opts = { method, headers: h };
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

test('File API without key returns 401 and does not fetch', async () => {
  resetFetch();
  const res = await worker.fetch(makeReq('/file/bot123TOKEN/x.bin', { key: null }), ENV);
  assert.equal(res.status, 401);
  assert.equal(lastFetch, null);
});

test('unrecognized path returns 404 without fetch', async () => {
  resetFetch();
  const res = await worker.fetch(makeReq('/random'), ENV);
  assert.equal(res.status, 404);
  assert.equal(lastFetch, null);
});

test('upstream failure returns controlled 502 without secret', async () => {
  globalThis.fetch = async () => { throw new Error('upstream down'); };
  const res = await worker.fetch(makeReq('/bot123TOKEN/getMe'), ENV);
  assert.equal(res.status, 502);
  const body = await res.text();
  assert.ok(!body.includes(ENV.WORKER_API_KEY));
});

test('POST sendMessage forwards exact body, method, content-type', async () => {
  resetFetch();
  const body = JSON.stringify({ chat_id: 1, text: 'hi', parse_mode: 'HTML' });
  const res = await worker.fetch(
    makeReq('/bot123TOKEN/sendMessage', { method: 'POST', body,
            headers: { 'Content-Type': 'application/json' } }), ENV);
  assert.equal(res.status, 200);
  assert.equal(lastFetch.method, 'POST');
  assert.equal(lastFetch.body, body); // exact body compare
  assert.equal(lastFetch.headers.get('content-type'), 'application/json');
  assert.ok(lastFetch.input.startsWith('https://api.telegram.org/bot123TOKEN/sendMessage'));
  // auth header must not be forwarded upstream
  assert.equal(lastFetch.headers.get('X-Worker-Api-Key'), null);
});

test('POST empty body forwarded as empty', async () => {
  resetFetch();
  const res = await worker.fetch(makeReq('/bot123TOKEN/getWebhookInfo', { method: 'POST', body: '' }), ENV);
  assert.equal(res.status, 200);
  assert.equal(lastFetch.method, 'POST');
  assert.equal(lastFetch.body, '');
});

test('external redirect is rejected with 502 and no Location', async () => {
  globalThis.fetch = async () =>
    new Response(null, { status: 302, headers: { location: 'https://evil.example/x?token=abc' } });
  const res = await worker.fetch(makeReq('/bot123TOKEN/getMe'), ENV);
  assert.equal(res.status, 502);
  assert.equal(res.headers.get('location'), null);
});

test('same-origin redirect on api.telegram.org is passed through', async () => {
  globalThis.fetch = async () =>
    new Response(null, { status: 302, headers: { location: 'https://api.telegram.org/file/bot123TOKEN/x.bin' } });
  const res = await worker.fetch(makeReq('/bot123TOKEN/getMe'), ENV);
  assert.equal(res.status, 302);
  assert.ok((res.headers.get('location') || '').startsWith('https://api.telegram.org'));
});

test('Connection-listed request headers are not forwarded upstream', async () => {
  resetFetch();
  const res = await worker.fetch(makeReq('/bot123TOKEN/getMe', {
    headers: { Connection: 'keep-alive, X-Internal-Header', 'X-Internal-Header': 'test-secret' },
  }), ENV);
  assert.equal(res.status, 200);
  assert.equal(lastFetch.headers.get('x-internal-header'), null);
});

test('Connection-listed response headers are stripped from client', async () => {
  globalThis.fetch = async () => {
    const h = new Headers();
    h.set('Content-Type', 'application/json');
    h.set('Connection', 'close, X-Debug-Header');
    h.set('X-Debug-Header', 'debug-secret');
    return new Response('{}', { status: 200, headers: h });
  };
  const res = await worker.fetch(makeReq('/bot123TOKEN/getMe'), ENV);
  assert.equal(res.headers.get('x-debug-header'), null);
  assert.equal(res.headers.get('content-type'), 'application/json');
});

test('File API response is streamed (ReadableStream, order+content preserved)', async () => {
  const chunks = [new Uint8Array([1, 2, 3]), new Uint8Array([4, 5]), new Uint8Array([6])];
  function makeStream() {
    let i = 0;
    return new ReadableStream({
      pull(controller) {
        if (i < chunks.length) { controller.enqueue(chunks[i]); i++; }
        else { controller.close(); }
      },
    });
  }
  globalThis.fetch = async (input) => {
    lastFetch = { input: String(input.url) };
    return new Response(makeStream(), { status: 200, headers: { 'content-type': 'application/octet-stream' } });
  };
  const res = await worker.fetch(makeReq('/file/bot123TOKEN/docs/stream.bin'), ENV);
  assert.equal(res.status, 200);
  // response body is a ReadableStream (not a pre-buffered blob)
  assert.ok(res.body instanceof ReadableStream);
  const reader = res.body.getReader();
  const got = [];
  let done = false;
  while (!done) {
    const { value, done: d } = await reader.read();
    done = d;
    if (value) got.push(Array.from(value));
  }
  assert.deepEqual(got, [[1, 2, 3], [4, 5], [6]]);
});

test('delayed upstream (>16s) is not cut by an artificial worker timeout', { timeout: 30000 }, async () => {
  globalThis.fetch = async () => {
    await new Promise((r) => setTimeout(r, 16500));
    return new Response(JSON.stringify({ ok: true, result: [] }), { status: 200 });
  };
  const res = await worker.fetch(makeReq('/bot123TOKEN/getUpdates?timeout=45'), ENV);
  assert.equal(res.status, 200);
  const j = await res.json();
  assert.equal(j.ok, true);
});

test('encoded path and dot-dot stay on api.telegram.org (no upstream escape)', async () => {
  resetFetch();
  const res = await worker.fetch(makeReq('/bot123TOKEN%2FgetMe%3Ffoo'), ENV);
  assert.equal(res.status, 200);
  assert.ok(lastFetch.input.startsWith('https://api.telegram.org/bot123TOKEN'));
});

test('path normalization cannot escape to another origin', async () => {
  resetFetch();
  const res = await worker.fetch(makeReq('/bot123TOKEN/../../evil/getMe'), ENV);
  assert.ok(res.status === 404 || res.status === 200);
  if (res.status === 200) {
    assert.ok(lastFetch.input.startsWith('https://api.telegram.org/'));
  } else {
    assert.equal(lastFetch, null);
  }
});
