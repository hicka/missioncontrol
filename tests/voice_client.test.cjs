const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('web/app.js', 'utf8');
const voiceSource = source.slice(source.indexOf('const V ='), source.indexOf('/* ----- Team voice'));
function harness(fetch) {
  const played = [];
  class Audio {
    constructor(url) { this.url = url; }
    play() {
      played.push(this.url);
      if (!this.url.includes('hang')) setTimeout(() => this.onended?.(), 5);
      return Promise.resolve();
    }
    pause() {}
  }
  const context = vm.createContext({
    fetch, Audio, AbortController, TextDecoder, Uint8Array, FormData, Blob,
    setTimeout, clearTimeout, setInterval, clearInterval,
    document: { readyState: 'loading', addEventListener() {} },
    navigator: {}, S: { current: 'dm:test', msgs: [] },
    $: () => ({ checked: false }),
  });
  vm.runInContext(voiceSource + '\nvoiceUI = () => {}; V.on = true;', context);
  return { context, played, run: code => vm.runInContext(code, context) };
}
const response = url => new Response(JSON.stringify({ ok: true, url }) + '\n');
const settle = promise => Promise.race([promise, new Promise((_, reject) => {
  const timeout = setTimeout(() => reject(new Error('Voice queue stalled')), 1000);
  timeout.unref();
})]);

test('stopping a playing sentence releases the queue and the next reply plays', async () => {
  let count = 0;
  const h = harness(async () => response(++count === 1 ? '/hang.wav' : '/next.wav'));
  const old = h.run('voiceSpeak("First."); V.speaking');
  await new Promise(resolve => setTimeout(resolve, 20));
  assert.deepEqual(h.played, ['/hang.wav']);
  h.run('voiceStopAll(); voiceSpeak("Second.")');
  await settle(h.run('V.speaking'));
  await settle(old);
  assert.deepEqual(h.played, ['/hang.wav', '/next.wav']);
  assert.equal(h.run('V.pending'), 0);
});

test('closing during transcription prevents sending a stale message', async () => {
  let resolve, sends = 0;
  const h = harness(url => {
    if (url === '/api/send') sends++;
    return new Promise(r => { resolve = r; });
  });
  const pending = h.run('voiceUtterance(new Blob(["audio"]), "audio/webm")');
  h.run('voiceClose()');
  resolve(new Response(JSON.stringify({ ok: true, text: 'Do something' })));
  await settle(pending);
  assert.equal(sends, 0);
  assert.equal(h.run('V.phase'), 'idle');
});

test('first sentence plays before the server finishes generating the reply', async () => {
  let stream;
  const h = harness(async () => new Response(new ReadableStream({ start(c) { stream = c; } })));
  h.run('voiceSpeak("First. Second.")');
  await new Promise(resolve => setTimeout(resolve, 5));
  const encoder = new TextEncoder();
  stream.enqueue(encoder.encode(JSON.stringify({ ok: true, url: '/one.wav' }) + '\n'));
  await new Promise(resolve => setTimeout(resolve, 20));
  assert.deepEqual(h.played, ['/one.wav']);
  stream.enqueue(encoder.encode(JSON.stringify({ ok: true, url: '/two.wav' }) + '\n'));
  stream.close();
  await settle(h.run('V.speaking'));
  assert.deepEqual(h.played, ['/one.wav', '/two.wav']);
});
