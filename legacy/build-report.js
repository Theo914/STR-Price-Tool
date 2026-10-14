// Builds report/index.html: the viewer with the three CSVs embedded, so it opens with no file picking.
// Usage: node build-report.js   (run from the parity-tool folder)
const fs = require('fs'), path = require('path');
const root = __dirname;
const read = f => { const p = path.join(root, 'data', f); return fs.existsSync(p) ? fs.readFileSync(p, 'utf8') : ''; };
const data = { listings: read('listings.csv'), prices: read('prices.csv'), comps: read('comps.csv') };
let html = fs.readFileSync(path.join(root, 'viewer', 'index.html'), 'utf8');
const inject = `<script>window.PARITY_DATA=${JSON.stringify(data).replace(/</g, '\\u003c')};</script>`;
html = html.replace('<script>', inject + '\n<script>');
fs.mkdirSync(path.join(root, 'report'), { recursive: true });
const out = path.join(root, 'report', 'index.html');
fs.writeFileSync(out, html);
console.log('Report written to', out);
