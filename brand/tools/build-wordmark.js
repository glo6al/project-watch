// Original display lettering for the W.A.T.C.H. wordmark, not a general-purpose font.
// Option B: original 80-unit lettering expanded by 3.5 units to match the approved heavy study.
const fs=require('fs');const path=require('path');
const glyphs={
 W:[80,'M0 0H11L24 62L35 20H45L56 62L69 0H80L62 80H51L40 40L29 80H18Z'],
 A:[64,'M0 80L21 0H43L64 80H53L47 57H17L11 80ZM20 47H44L32 10Z'],
 T:[64,'M0 0H64V10H37V80H27V10H0Z'],
 C:[64,'M12 0H52L64 12V22H54V16L48 10H16L10 16V64L16 70H48L54 64V58H64V68L52 80H12L0 68V12Z'],
 H:[64,'M0 0H10V35H54V0H64V80H54V45H10V80H0Z'],
 P:[64,'M0 80V0H52L64 12V35L52 47H10V80ZM10 10V37H48L54 31V16L48 10Z'],
 R:[64,'M0 80V0H52L64 12V35L52 47H43L66 80H54L31 47H10V80ZM10 10V37H48L54 31V16L48 10Z'],
 O:[64,'M12 0H52L64 12V68L52 80H12L0 68V12ZM16 10L10 16V64L16 70H48L54 64V16L48 10Z'],
 J:[64,'M0 0H64V68L52 80H12L0 68V56H10V64L16 70H48L54 64V10H0Z'],
 E:[64,'M0 0H64V10H10V35H53V45H10V70H64V80H0Z'],
 '.':[10,'M0 70H10V80H0Z']
};
// Translate the absolute polygon coordinates before combining the letters.
function translate(d,dx){let axis=0,cmd='';return d.match(/[A-Z]|-?\d+(?:\.\d+)?/g).map(t=>{if(/^[A-Z]$/.test(t)){cmd=t;axis=0;return t;}const n=Number(t);if(cmd==='H')return String(n+dx);if(cmd==='V')return t;return String(n+(axis++%2===0?dx:0));}).join(' ');}
function line(text,gap){let x=0,parts=[];for(const c of text){const [w,d]=glyphs[c];parts.push(translate(d,x));x+=w+gap;}return {d:parts.join(' '),x:0,y:0,w:x-gap,h:80};}
// Expand the approved 7-unit centered stroke into filled polygons, including counters.
const clipper=require('clipper-lib');
function expand(p){
 const tokens=p.d.match(/[A-Z]|-?\d+(?:\.\d+)?/g),contours=[];let poly=[],x=0,y=0,i=0;
 while(i<tokens.length){const c=tokens[i++];
  if(c==='M'||c==='L'){x=Number(tokens[i++]);y=Number(tokens[i++]);poly.push({X:Math.round(x*1000),Y:Math.round(y*1000)});}
  else if(c==='H'){x=Number(tokens[i++]);poly.push({X:Math.round(x*1000),Y:Math.round(y*1000)});}
  else if(c==='V'){y=Number(tokens[i++]);poly.push({X:Math.round(x*1000),Y:Math.round(y*1000)});}
  else if(c==='Z'){contours.push(poly);poly=[];}
  else throw Error('Unexpected polygon command '+c);
 }
 contours.forEach((c,i)=>{const depth=contours.filter((q,j)=>i!==j&&clipper.Clipper.PointInPolygon(c[0],q)===1).length;if(clipper.Clipper.Orientation(c)!==(depth%2===0))c.reverse();});
 const offset=new clipper.ClipperOffset(4,0.25);offset.AddPaths(contours,clipper.JoinType.jtMiter,clipper.EndType.etClosedPolygon);
 const result=new clipper.Paths();offset.Execute(result,3500);
 const xs=result.flat().map(p=>p.X/1000),ys=result.flat().map(p=>p.Y/1000);
 return {d:result.map(c=>'M'+c.map(p=>`${p.X/1000} ${p.Y/1000}`).join('L')+'Z').join(' '),x:Math.min(...xs),y:Math.min(...ys),w:Math.max(...xs)-Math.min(...xs),h:Math.max(...ys)-Math.min(...ys)};
}
const type={font:'WATCH Squared Heavy — approved option B; expanded filled vector outlines',project:require('./project-label-path.json'),watch:expand(line('W.A.T.C.H.',12))};
fs.writeFileSync(path.join(__dirname,'wordmark-paths.json'),JSON.stringify(type,null,2)+'\n');
