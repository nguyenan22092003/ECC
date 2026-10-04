import test from 'node:test';
import assert from 'node:assert/strict';
import { escapeHTML, safeURL, formPayload } from './utils.mjs';
test('news is inert even with HTML injection',()=>assert.equal(escapeHTML('<img onerror="x">'), '&lt;img onerror=&quot;x&quot;&gt;'));
test('unsafe article links never become navigable',()=>{assert.equal(safeURL('javascript:alert(1)'), '#');assert.equal(safeURL('https://example.com/news'),'https://example.com/news');});
test('form numbers and checkbox values preserve their types',()=>{const f=new FormData();f.set('hours','24');f.set('enabled','on');assert.deepEqual(formPayload(f,['enabled'],['hours']),{hours:24,enabled:true});});
