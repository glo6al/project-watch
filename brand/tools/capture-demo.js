// Run with Playwright available in NODE_PATH and src/web served on localhost:18794.
const {chromium}=require('playwright');
const fs=require('fs');const path=require('path');
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
 const page=await browser.newPage({viewport:{width:1600,height:825},deviceScaleFactor:1});
 await page.addInitScript(()=>{if(window===window.top)localStorage.setItem('liveRoomHinted','1')});
 const errors=[];page.on('pageerror',e=>errors.push(String(e)));
 await page.goto('http://127.0.0.1:18794/?demo');await page.waitForTimeout(1800);
 const root=path.resolve(__dirname,'../..');
 const out=path.join(root,'docs/media');
 await page.screenshot({path:path.join(out,'demo-room.png')});
 const geometry=await page.locator('#mark').evaluate(e=>({width:e.getBoundingClientRect().width,height:e.getBoundingClientRect().height,loaded:e.complete&&e.naturalWidth>0,url:e.getAttribute('src')}));
 await page.evaluate(()=>{cam.yaw=pos['c:demo-cleo'].a;apply();});await page.waitForTimeout(700);
 await page.evaluate(()=>setPeek('c:demo-cleo'));await page.waitForTimeout(1000);
 await page.screenshot({path:path.join(out,'demo-held.png')});
 await page.evaluate(()=>setFocus('c:demo-cleo'));await page.waitForTimeout(1200);
 await page.screenshot({path:path.join(out,'demo-desk.png')});
 await page.evaluate(()=>setFocus(null));await page.waitForTimeout(700);
 const frames=fs.mkdtempSync('/tmp/watch-brand-demo-');
 for(let i=0;i<60;i++){
  if(i===15)await page.evaluate(()=>setPeek('c:demo-cleo'));
  if(i===30)await page.evaluate(()=>setFocus('c:demo-cleo'));
  if(i===50)await page.evaluate(()=>setFocus(null));
  await page.screenshot({path:path.join(frames,`${String(i).padStart(3,'0')}.png`)});await page.waitForTimeout(100);
 }
 console.log(JSON.stringify({geometry,errors,frames}));
 await browser.close();
 if(errors.length)process.exitCode=1;
})().catch(e=>{console.error(e);process.exit(1)});
