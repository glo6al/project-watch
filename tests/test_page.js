#!/usr/bin/env node
/* The page's path helpers, run outside a browser. The block between the two markers in src/web/index.html is
   evaluated with a stub preview key, once as a macOS or demo page (paths split on / only) and once as a page served
   by a Windows server (either slash). Run: node tests/test_page.js */
'use strict';
const fs = require('fs'), path = require('path');
const html = fs.readFileSync(path.join(__dirname, '..', 'src', 'web', 'index.html'), 'utf8');
const a = html.indexOf('/* path helpers'), b = html.indexOf('/* end path helpers */');
if (a < 0 || b < 0 || b < a) throw new Error('the path helper markers are missing from index.html');
const block = html.slice(html.indexOf('\n', a) + 1, b);
const load = win => new Function('PK', block + ';WINHOST=' + win + ';return {short,tail2,projName,fsUrl}')('k');
const check = (name, cond) => { if (!cond) { console.log('FAIL ' + name); process.exit(1); } console.log('ok', name); };

const mac = load(false);
check('macOS: a backslash in a name is part of the name',
  mac.tail2('/Users/me/a\\b') === 'me/a\\b' && mac.short('/Users/me/x/y/a\\b\\c') === '…/x/y/a\\b\\c' && mac.projName('/Users/me/a\\b') === 'a\\b');
check('macOS: a name that looks like a drive is still a name', mac.projName('/Users/me/C:\\notes/') === 'C:\\notes' && mac.tail2('/Users/me/C:\\notes') === 'me/C:\\notes');
check('macOS: Projects folder wins, else the last name', mac.projName('/Users/me/Projects/site/x') === 'site' && mac.projName('/Users/me/work/site/') === 'site' && mac.projName('') === '');
check('macOS: short keeps the last three names', mac.short('/a/b/c/d/e.css') === '…/c/d/e.css' && mac.short('/a/b/c') === '/a/b/c');
check('macOS: preview address', mac.fsUrl('/Users/me/site/index.html') === '/fs/k/Users/me/site/index.html');
check('macOS: preview address is encoded', mac.fsUrl('/Users/me/my site/a"b.html') === '/fs/k/Users/me/my%20site/a%22b.html' && mac.fsUrl('/Users/me/C:\\x.html') === '/fs/k/Users/me/C%3A%5Cx.html');

const win = load(true);
check('Windows: either slash splits', win.tail2('C:\\Users\\me\\site\\a.css') === 'site/a.css' && win.tail2('C:/Users/me/site/a.css') === 'site/a.css');
check('Windows: short keeps the last three names', win.short('C:\\Users\\me\\x\\y\\z.css') === '…/x/y/z.css');
check('Windows: project name', win.projName('C:\\Users\\me\\Projects\\site') === 'site' && win.projName('C:\\Users\\me\\work\\site\\') === 'site' && win.projName('C:/Users/me/site') === 'site');
check('Windows: a UNC path splits too', win.tail2('\\\\srv\\share\\p\\f.css') === 'p/f.css');
check('Windows: preview address is a drive path written with /', win.fsUrl('C:\\Users\\me\\site\\index.html') === '/fs/k/C%3A/Users/me/site/index.html');
console.log('all ok');
