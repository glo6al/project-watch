#!/usr/bin/env node
'use strict';
// Execute the repository's real synthetic seed and its first synchronous tick.
// No UI changes, copied seed facts, scripted Officer answers, or network access.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.resolve(__dirname, '../../..', 'src/web/index.html'), 'utf8').replace(/\r\n?/g, '\n');
const start = html.indexOf('function demo(){');
const end = html.indexOf('\nsetInterval(render,', start);
if (start < 0 || end <= start) throw new Error('demo function boundaries changed; review exporter');
const now = 1794412800; // Fixed fictional observation clock; no machine/session data.
const records = [];
const context = {
  Date: {now: () => now * 1000}, DSEQ: 0,
  handle: event => records.push(event),
  paintWatch() {}, lock() {}, $: () => ({after() {}}),
  document: {createElement: () => ({})},
  setInterval() {},
  setTimeout() {throw new Error('staged scripted Officer answers must not run');},
  location: {search: ''},
};
vm.runInNewContext(html.slice(start, end) + '\ndemo();', context, {timeout: 1000});
process.stdout.write(JSON.stringify({source: 'src/web/index.html:demo seed and first tick', now, records}) + '\n');
