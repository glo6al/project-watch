const fs = require('fs');
const path = require('path');
const sharp = require('sharp');
const root = path.resolve(__dirname, '..');
const project = path.resolve(root, '..');
const colors = {green:'#36D879', 'green-on-dark':'#36D879', 'graphite':'#17212B', 'graphite-on-light':'#17212B', white:'#FFFFFF'};
const backgrounds = {'green-on-dark':'#090D13', 'graphite-on-light':'#F4F7F5'};
// Rebuild the original display lettering from its editable vector source.
require('./build-wordmark.js');
const typeFile = path.join(__dirname, 'wordmark-paths.json');
const type=JSON.parse(fs.readFileSync(typeFile));
// Three empty screens viewed from inside the room. No activity indicator.
const monitorPath = 'M0 0L142 36V176L0 212ZM23 29V183L121 159V53Z M154 30H332V176H154ZM173 49V157H313V49Z M344 36L486 0V212L344 176ZM365 53V159L463 183V29Z';
function mark(color){return `<path fill="${color}" fill-rule="evenodd" d="${monitorPath}"/>`;}
function word(which,x,y,width,color){const p=type[which],s=width/p.w;return `<g transform="translate(${x} ${y}) scale(${s}) translate(${-p.x} ${-p.y})"><path fill="${color}" fill-rule="evenodd" d="${p.d}"/></g>`;}
function doc(w,h,content,bg){return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${w} ${h}" role="img" aria-label="Project W.A.T.C.H. — three monitors"><title>Project W.A.T.C.H.</title>${bg?`<rect width="${w}" height="${h}" fill="${bg}"/>`:''}${content}</svg>\n`;}
function icon(color,bg){return doc(512,256,`<g transform="translate(13 22)">${mark(color)}</g>`,bg);}
function horizontal(color,bg){return doc(1280,360,`<g transform="translate(40 74)">${mark(color)}</g>${word('project',580,85,310,color)}${word('watch',575,167,660,color)}`,bg);}
function stacked(color,bg){return doc(1000,720,`<g transform="translate(120 70) scale(${760/486})">${mark(color)}</g>${word('project',315,438,370,color)}${word('watch',90,508,820,color)}`,bg);}
function app(color='#36D879',bg='#090D13'){return doc(512,512,`<g transform="translate(38 161) scale(${436/486})">${mark(color)}</g>`,bg);}
function write(rel,data){const p=path.join(root,rel);fs.mkdirSync(path.dirname(p),{recursive:true});fs.writeFileSync(p,data);}
async function png(svg,rel,width){const p=path.join(root,rel);fs.mkdirSync(path.dirname(p),{recursive:true});await sharp(Buffer.from(svg)).resize({width}).png().toFile(p);}
function ico(entries){const head=Buffer.alloc(6);head.writeUInt16LE(1,2);head.writeUInt16LE(entries.length,4);const table=Buffer.alloc(entries.length*16);let offset=6+table.length;entries.forEach((e,i)=>{const p=i*16;table[p]=e.size;table[p+1]=e.size;table.writeUInt16LE(1,p+4);table.writeUInt16LE(32,p+6);table.writeUInt32LE(e.data.length,p+8);table.writeUInt32LE(offset,p+12);offset+=e.data.length;});return Buffer.concat([head,table,...entries.map(e=>e.data)]);}
async function main(){
  for(const [variant,color] of Object.entries(colors)){
    for(const [kind,render,width] of [['icon',icon,512],['logo',horizontal,1280],['logo-stacked',stacked,1000]]){
      const svg=render(color,backgrounds[variant]);
      write(`svg/agent-watch-${kind}-${variant}.svg`,svg);
      await png(svg,`png/agent-watch-${kind}-${variant}-${width}.png`,width);
    }
  }
  write('svg/agent-watch-app-icon.svg',app());
  for(const size of [16,32,48])await png(app(),`favicon/favicon-${size}.png`,size);
  for(const size of [192,512])await png(app(),`favicon/app-icon-${size}.png`,size);
  await png(app(),'favicon/apple-touch-icon-180.png',180);
  write('favicon/favicon.ico',ico([16,32,48].map(size=>({size,data:fs.readFileSync(path.join(root,`favicon/favicon-${size}.png`))}))));
  fs.copyFileSync(path.join(root,'svg/agent-watch-icon-green.svg'),path.join(project,'src/web/static/icon.svg'));
  for(const name of ['favicon.ico','favicon-32.png','apple-touch-icon-180.png'])fs.copyFileSync(path.join(root,'favicon',name),path.join(project,'src/web/static',name));
  for(const variant of ['green','graphite'])fs.copyFileSync(path.join(root,`svg/agent-watch-logo-stacked-${variant}.svg`),path.join(project,`docs/media/logo-${variant}.svg`));
  // Original public paths remain stable even though the light-surface color is now graphite.
  // Keep the graphite names too so references created during the color change also work.
  for (const dir of ['svg', 'png']) {
    for (const name of fs.readdirSync(path.join(root, dir)).filter(name => name.includes('graphite'))) {
      fs.copyFileSync(path.join(root, dir, name), path.join(root, dir, name.replace('graphite', 'dark-green')));
    }
  }
  fs.copyFileSync(path.join(project, 'docs/media/logo-graphite.svg'), path.join(project, 'docs/media/logo-dark-green.svg'));
  const preview=doc(1400,1100,`<rect width="1400" height="1100" fill="#F4F7F5"/><text x="60" y="65" font-family="sans-serif" font-size="26" fill="#090D13">PROJECT W.A.T.C.H. / production assets</text><svg x="50" y="105" width="1300" height="366" viewBox="0 0 1280 360">${horizontal('#36D879','#090D13').replace(/^.*?<title>.*?<\/title>/,'').replace(/<\/svg>\s*$/,'')}</svg><svg x="50" y="500" width="760" height="547" viewBox="0 0 1000 720">${stacked('#17212B').replace(/^.*?<title>.*?<\/title>/,'').replace(/<\/svg>\s*$/,'')}</svg><svg x="910" y="535" width="320" height="320" viewBox="0 0 512 512">${app().replace(/^.*?<title>.*?<\/title>/,'').replace(/<\/svg>\s*$/,'')}</svg><text x="900" y="915" font-family="sans-serif" font-size="22" fill="#090D13">Actual size: 16 / 32 / 48 px</text>${[16,32,48].map((s,i)=>`<image x="${925+i*90}" y="950" width="${s}" height="${s}" href="data:image/png;base64,${fs.readFileSync(path.join(root,`favicon/favicon-${s}.png`)).toString('base64')}"/>`).join('')}`);
  write('preview/contact-sheet.svg',preview);await png(preview,'preview/contact-sheet.png',1400);
  console.log('Generated brand assets, integrated copies, and original-filename compatibility copies.');
}
main().catch(e=>{console.error(e);process.exit(1);});
