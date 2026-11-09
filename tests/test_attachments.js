'use strict';

// Exercise the real attachment helper with synthetic responses and DOM/URL spies only.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '..', 'static/js/core.js'), 'utf8');

function runtime({ token = 'synthetic-token', type = 'application/pdf', disposition = '', status = 200, networkError = false } = {}) {
  const storage = new Map(token ? [['_dashboard_auth_token', token]] : []);
  const calls = { requests: [], opens: [], downloads: [], blobs: [], revoked: [], timers: [], alerts: [], prevented: 0, reloads: 0 };
  const context = vm.createContext({
    Blob,
    console,
    localStorage: {
      getItem: key => storage.get(key) ?? null,
      removeItem: key => storage.delete(key),
    },
    location: { reload: () => { calls.reloads += 1; } },
    document: {
      createElement: tag => {
        assert.equal(tag, 'a');
        const link = { attached: false, removed: false };
        link.click = () => { assert.equal(link.attached, true); calls.downloads.push(link); };
        link.remove = () => { link.removed = true; };
        return link;
      },
      body: { appendChild: link => { link.attached = true; } },
    },
    URL: {
      createObjectURL: blob => { calls.blobs.push(blob); return 'blob:synthetic-offline-' + calls.blobs.length; },
      revokeObjectURL: url => calls.revoked.push(url),
    },
    setTimeout: (callback, delay) => { calls.timers.push({ callback, delay }); },
    open: (...args) => calls.opens.push(args),
    alert: message => calls.alerts.push(message),
    fetch: async (url, options) => {
      calls.requests.push({ url, options });
      assert.ok(url.startsWith('/api/proxy-file?url='), 'only the local authenticated proxy is used');
      if (networkError) throw new Error('Synthetic offline failure');
      return {
        ok: status >= 200 && status < 300,
        status,
        headers: { get: name => ({ 'content-type': type, 'content-disposition': disposition })[name.toLowerCase()] || null },
        blob: async () => new Blob(['Synthetic attachment content'], { type }),
      };
    },
  });
  context.window = context;
  vm.runInContext(source, context, { filename: 'static/js/core.js' });
  const event = { preventDefault: () => { calls.prevented += 1; } };
  return { calls, storage, open: url => context.openAttachment(url, event) };
}

async function main() {
  let count = 0;
  async function check(label, run) { await run(); count += 1; console.log('PASS ' + label); }

  await check('passive previews use only authenticated proxy data and isolated new tabs', async () => {
    for (const type of ['application/pdf', 'image/png', 'image/jpeg', 'image/webp', 'text/plain; charset=utf-8']) {
      const local = runtime({ type, disposition: 'inline; filename="demo.pdf"' });
      assert.equal(await local.open('https://files.example.test/synthetic'), true);
      assert.equal(local.calls.prevented, 1);
      assert.equal(local.calls.requests.length, 1);
      assert.equal(local.calls.requests[0].options.headers.Authorization, 'Bearer synthetic-token');
      assert.equal(local.calls.requests[0].url, '/api/proxy-file?url=' + encodeURIComponent('https://files.example.test/synthetic'));
      assert.deepEqual(local.calls.opens, [['blob:synthetic-offline-1', '_blank', 'noopener,noreferrer']]);
      assert.equal(local.calls.downloads.length, 0);
      assert.equal(local.calls.blobs[0].type, type.split(';')[0]);
      assert.equal(local.calls.timers.length, 1);
      assert.equal(local.calls.timers[0].delay, 60000);
      local.calls.timers[0].callback();
      assert.deepEqual(local.calls.revoked, ['blob:synthetic-offline-1']);
    }
  });

  await check('proxy attachment disposition forces downloads even for otherwise previewable content', async () => {
    const local = runtime({ type: 'image/png', disposition: "attachment; filename=demo.png; filename*=UTF-8''%ED%95%A9%EC%84%B1.png" });
    assert.equal(await local.open('https://files.example.test/demo.png'), true);
    assert.equal(local.calls.opens.length, 0);
    assert.equal(local.calls.downloads.length, 1);
    assert.equal(local.calls.downloads[0].href, 'blob:synthetic-offline-1');
    assert.equal(local.calls.downloads[0].download, '합성.png');
    assert.equal(local.calls.downloads[0].removed, true);
    assert.equal(local.calls.blobs[0].type, 'application/octet-stream');
    local.calls.timers[0].callback();
    assert.deepEqual(local.calls.revoked, ['blob:synthetic-offline-1']);
  });

  await check('active, missing and unknown MIME types never open an app-origin Blob tab', async () => {
    for (const type of ['text/html', 'image/svg+xml', 'application/xhtml+xml', 'text/xml', 'application/xml', 'text/javascript', 'application/octet-stream', '', 'image/unknown']) {
      const local = runtime({ type, disposition: 'inline; filename="../../synthetic.html"' });
      assert.equal(await local.open('https://files.example.test/synthetic.html'), true);
      assert.equal(local.calls.opens.length, 0, type);
      assert.equal(local.calls.downloads.length, 1, type);
      assert.equal(local.calls.blobs[0].type, 'application/octet-stream', type);
      assert.doesNotMatch(local.calls.downloads[0].download, /[\u0000-\u001f/\\]/);
    }
  });

  await check('missing authentication, denied proxy responses and network failures never fall back to the supplied URL', async () => {
    for (const options of [{ token: '' }, { status: 401 }, { status: 403 }, { status: 503 }, { networkError: true }]) {
      const local = runtime(options);
      assert.equal(await local.open('javascript:synthetic_untrusted_url()'), false);
      assert.equal(local.calls.opens.length, 0);
      assert.equal(local.calls.downloads.length, 0);
      assert.equal(local.calls.blobs.length, 0);
      assert.equal(local.calls.alerts.length, 1);
      assert.ok(!local.calls.alerts[0].includes('javascript:'), 'failure notices do not echo untrusted URLs');
      if (options.token === '') assert.equal(local.calls.requests.length, 0);
      if (options.status === 401) {
        assert.equal(local.calls.reloads, 1);
        assert.equal(local.storage.has('_dashboard_auth_token'), false);
      }
    }
    const empty = runtime();
    assert.equal(await empty.open(''), false);
    assert.equal(empty.calls.requests.length, 0);
  });

  console.log(`Attachments: ${count} checks passed; synthetic offline responses only.`);
}

main().catch(error => { console.error(error.stack || error.message); process.exitCode = 1; });
