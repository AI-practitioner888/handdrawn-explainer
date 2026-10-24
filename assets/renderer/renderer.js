/* Semantic drawing renderer. Every frame is derived from absolute time.
 * Geometry is new; font centerlines are prepared by upstream stream_render.py.
 * One pencil uses the actual active stroke's terminal point as its anchor.
 */
(() => {
  'use strict';
  const canvas = document.querySelector('#board'), boardCtx = canvas.getContext('2d');
  let ctx = boardCtx;
  const params = new URLSearchParams(location.search);
  const narration = document.querySelector('#narration'), sfx = document.querySelector('#sfx');
  const seek = document.querySelector('#seek'), output = document.querySelector('#time');
  const clamp = x => Math.max(0, Math.min(1, x));
  const smooth = x => { x = clamp(x); return x*x*(3-2*x); };
  let timeline, atlasData, atlas, playing = false, currentTime = 0, startClock = 0;
  const glyphCache = new Map();
  let boardWidth=1280, boardHeight=720;
  const scratch = document.createElement('canvas'); scratch.width=112; scratch.height=120;
  const scratchCtx = scratch.getContext('2d');
  const paper = document.createElement('canvas'); paper.width=1280; paper.height=720;
  const eraseLayer = document.createElement('canvas'); eraseLayer.width=1280; eraseLayer.height=720;
  const eraseCtx = eraseLayer.getContext('2d');
  const elementsByKey = new Map();
  const imageAssets = new Map();
  let toolRenderCount = 0;
  const inkTypes=new Set(['stroke','text','hatch','crosshatch','paint','erase','image_ink']);
  const toolProfiles={pencil:{width:2.8,alpha:1},pen:{width:2.2,alpha:1},marker:{width:16,alpha:.38},eraser:{width:24,alpha:1}};
  let debugTotals={maxVisibleTools:0,maxConcurrentInk:0,renderCount:0};

  function toolOf(e){return e.tool||(e.type==='erase'?'eraser':timeline.defaultTool||'pencil');}
  function imageFade(e){return e.mode==='fade'||(typeof e.animation==='object'?(e.animation.type||e.animation.mode):e.animation)==='fade';}
  function imageDraw(e){return e.mode==='draw'||(typeof e.animation==='object'?(e.animation.type||e.animation.mode):e.animation)==='draw';}
  function imageAssetKey(e){return JSON.stringify([e.src,e.traceSrc,e.inkSrc||null]);}
  function isAnimatedInk(e){return !e.instant&&e.durationMs>0&&(inkTypes.has(e.type)||(e.type==='image'&&!imageFade(e)));}
  function imageInkPhase(e){return imageDraw(e)?1:(e.inkPhase??(e.type==='image_ink'?1:.82));}

  function metrics(paths) {
    let total = 0;
    const parts = paths.map(points => {
      const cum = [0];
      for(let i=1;i<points.length;i++) cum.push(cum[i-1]+Math.hypot(points[i][0]-points[i-1][0],points[i][1]-points[i-1][1]));
      const length = cum[cum.length-1] || 1;
      total += length;
      return {points,cum,length};
    });
    return {parts,total};
  }

  function sampleSvg(d, wobble=true) {
    // M boundaries are pen lifts, never a line connecting unrelated contours.
    const subpaths = d.split(/(?=[Mm])/).map(part=>part.trim()).filter(Boolean);
    let previousEnd={x:0,y:0};
    return metrics(subpaths.map((part,sub) => {
      // Relative moves retain the previous current point after splitting paths.
      if(part[0]==='m'){
        const tokens=part.match(/[AaCcHhLlMmQqSsTtVvZz]|[-+]?(?:\d*\.\d+|\d+\.?\d*)(?:[eE][-+]?\d+)?/g)||[];
        if(tokens.length<3)throw new Error('Invalid relative SVG move');
        const tail=tokens.slice(3);
        part=`M ${previousEnd.x+Number(tokens[1])} ${previousEnd.y+Number(tokens[2])} `+
          ((tail.length&&Number.isFinite(Number(tail[0])))?'l ':'')+tail.join(' ');
      }
      const path = document.createElementNS('http://www.w3.org/2000/svg','path');
      path.setAttribute('d',part);
      const len = path.getTotalLength();
      previousEnd=path.getPointAtLength(len);
      const n = Math.max(2,Math.ceil(len/2.8));
      const pts=[];
      for(let i=0;i<=n;i++) {
        const p = path.getPointAtLength(len*i/n);
        const noise=wobble?.30:0;
        pts.push([p.x+noise*Math.sin(i*.42+sub),p.y+noise*Math.sin(i*.31+sub+1.2)]);
      }
      return pts;
    }));
  }

  function trace(c, data, progress, width, color, tool=null) {
    c.save();
    if(tool)c.globalAlpha*=toolProfiles[tool]?.alpha??1;
    let remaining = data.total * clamp(progress), tip = null;
    c.strokeStyle=color; c.lineWidth=width; c.lineCap='round'; c.lineJoin='round';
    for(const part of data.parts) {
      if(remaining<=0) break;
      const pts=part.points, amount=Math.min(remaining,part.length);
      c.beginPath(); c.moveTo(pts[0][0],pts[0][1]); tip=pts[0];
      for(let i=1;i<pts.length;i++) {
        if(part.cum[i]<=amount) {c.lineTo(pts[i][0],pts[i][1]);tip=pts[i];}
        else {
          const f=(amount-part.cum[i-1])/Math.max(.001,part.cum[i]-part.cum[i-1]);
          tip=[pts[i-1][0]+(pts[i][0]-pts[i-1][0])*f,pts[i-1][1]+(pts[i][1]-pts[i-1][1])*f];
          c.lineTo(tip[0],tip[1]);break;
        }
      }
      c.stroke();remaining-=part.length;
    }
    c.restore();return tip;
  }

  function svgBounds(d){
    const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');
    svg.style.cssText='position:absolute;width:0;height:0;overflow:hidden;pointer-events:none';
    const p=document.createElementNS(svg.namespaceURI,'path');p.setAttribute('d',d);svg.appendChild(p);document.body.appendChild(svg);
    try{const b=p.getBBox();return {x:b.x,y:b.y,width:b.width,height:b.height};}finally{svg.remove();}
  }

  function sweepPaths(e,angle,spacing){
    if(!(spacing>0))throw new Error(e.id+': spacing must be positive');
    const b=e.bounds||svgBounds(e.d), a=angle*Math.PI/180, u=[Math.cos(a),Math.sin(a)], n=[-u[1],u[0]];
    const corners=[[b.x,b.y],[b.x+b.width,b.y],[b.x,b.y+b.height],[b.x+b.width,b.y+b.height]];
    const along=corners.map(p=>p[0]*u[0]+p[1]*u[1]), across=corners.map(p=>p[0]*n[0]+p[1]*n[1]);
    const lo=Math.min(...along)-2,hi=Math.max(...along)+2,t0=Math.min(...across),t1=Math.max(...across);
    const rows=Math.max(1,Math.ceil((t1-t0)/spacing));
    if(rows>5000)throw new Error(e.id+': excessive paint/hatch density');
    const paths=[];
    for(let row=0;row<=rows;row++){
      const t=t0+Math.min(row*spacing,t1-t0), reverse=row%2;
      let current=[];const segments=[];
      for(let s=lo;s<=hi+1;s+=2){
        const at=Math.min(s,hi), p=[at*u[0]+t*n[0],at*u[1]+t*n[1]];
        if(boardCtx.isPointInPath(e._path,p[0],p[1]))current.push(p);
        else if(current.length){if(current.length>1)segments.push(current);current=[];}
      }
      if(current.length>1)segments.push(current);
      if(reverse){segments.reverse();for(const line of segments)line.reverse();}
      paths.push(...segments);
    }
    return paths;
  }

  function coloredGlyph(char,color) {
    const key=char+'|'+color;
    if(glyphCache.has(key))return glyphCache.get(key);
    const g=atlasData.glyphs[char], c=document.createElement('canvas');
    c.width=g.w;c.height=g.h;
    const a=c.getContext('2d');
    a.drawImage(atlas,g.x,g.y,g.w,g.h,0,0,g.w,g.h);
    a.globalCompositeOperation='source-in';a.fillStyle=color;a.fillRect(0,0,g.w,g.h);
    glyphCache.set(key,c);return c;
  }

  function prepareElement(e) {
    if(!toolProfiles[toolOf(e)])throw new Error(e.id+': unsupported tool '+toolOf(e));
    if(e.d)e._path = new Path2D(e.d);
    if(e.type==='stroke')e._trace=sampleSvg(e.d,e.wobble!==false);
    if(e.type==='hatch')e._trace=e.lines?metrics(e.lines.flatMap(d=>sampleSvg(d,false).parts.map(p=>p.points))):metrics(sweepPaths(e,e.angle??45,e.spacing??9));
    if(e.type==='crosshatch'||e.type==='paint'){
      if(!e.d)throw new Error(e.id+': '+e.type+' needs a closed d mask');
      if(e.type==='crosshatch')e._trace=metrics([...sweepPaths(e,e.angle??45,e.spacing??9),...sweepPaths(e,e.crossAngle??135,e.spacing??9)]);
      else e._trace=metrics(sweepPaths(e,e.angle??0,e.spacing??((e.brushSize??18)*.72)));
      if(!e._trace.parts.length)throw new Error(e.id+': mask produces no paint/hatch strokes');
    }
    if(e.type==='erase') {
      if(!e.region)throw new Error('Erase needs an explicit region');
      const r=e.region, points=[], step=e.brushSize||24;
      let direction=1;
      for(let y=r.y+step*.35;y<=r.y+r.height+step*.2;y+=step*.65) {
        const a=[r.x+step*.3,y],b=[r.x+r.width-step*.3,y];
        points.push(...(direction>0?[a,b]:[b,a]));direction*=-1;
      }
      e._trace=metrics([points]);
    }
    if(e.type==='image'||e.type==='image_ink') {
      e._asset=imageAssets.get(imageAssetKey(e));
      if(!e._asset)throw new Error('Missing compiled image '+e.src);
      const paths=e._asset.data.strokes??e._asset.data.paths;
      if(!Array.isArray(paths)||!paths.length)throw new Error(e.id+': image trace needs strokes or paths');
      e._trace=metrics(paths);
    }
    if(e.type==='text') {
      let x=e.x, weight=0;
      e._chars=[];
      const scale=e.size/atlasData.fontSize;
      for(const char of Array.from(e.text)) {
        const g=atlasData.glyphs[char];
        if(!g)throw new Error('Uncompiled glyph: '+char);
        const data=metrics(g.strokes), durationWeight=Math.max(data.total,40);
        e._chars.push({char,g,data,x,weight:durationWeight});
        weight+=durationWeight;x+=g.advance*scale;
      }
      e._weight=weight;e._width=x-e.x;
      if(e.align==='center')for(const char of e._chars)char.x-=e._width/2;
    }
  }

  function drawText(e,p) {
    const scale=e.size/atlasData.fontSize;
    let remaining=e._weight*p, tip=null;
    for(const char of e._chars) {
      const gp=clamp(remaining/char.weight);
      if(gp<=0)break;
      const x=char.x-atlasData.leftPad*scale;
      // y is the top of the line's em box, not a varying glyph-specific top.
      const y=e.y-12*scale;
      const glyph=coloredGlyph(char.char,e.color||timeline.theme.ink);
      if(gp>=1 || !char.g.strokes.length)ctx.drawImage(glyph,x,y,char.g.w*scale,char.g.h*scale);
      else {
        if(scratch.width!==char.g.w||scratch.height!==char.g.h){scratch.width=char.g.w;scratch.height=char.g.h;}
        scratchCtx.clearRect(0,0,scratch.width,scratch.height);
        scratchCtx.globalCompositeOperation='source-over';
        const raw=trace(scratchCtx,char.data,gp,13,'#fff');
        scratchCtx.globalCompositeOperation='source-in';scratchCtx.drawImage(glyph,0,0);
        scratchCtx.globalCompositeOperation='source-over';
        ctx.drawImage(scratch,x,y,char.g.w*scale,char.g.h*scale);
        if(raw)tip=[x+raw[0]*scale,y+raw[1]*scale];
      }
      remaining-=char.weight;
    }
    return tip;
  }

  function drawElement(e,localMs) {
    if(localMs<e.startMs)return null;
    const p=e.instant||e.durationMs===0?1:clamp((localMs-e.startMs)/Math.max(1,e.durationMs));
    const originalCtx=ctx;
    const erased=(e._erasers||[]).filter(x=>localMs>x.startMs);
    if(erased.length){
      eraseCtx.setTransform(1,0,0,1,0,0);eraseCtx.globalAlpha=1;eraseCtx.globalCompositeOperation='source-over';
      eraseCtx.clearRect(0,0,boardWidth,boardHeight);ctx=eraseCtx;
    }
    ctx.save();
    const tr=e.transform||{scale:1,x:0,y:0};
    ctx.translate(tr.x||0,tr.y||0);ctx.scale(tr.scale||1,tr.scale||1);
    ctx.globalAlpha*=e.opacity??1;
    let tip=null;
    if(e.type==='stroke')tip=trace(ctx,e._trace,p,e.width??toolProfiles[toolOf(e)].width,e.color||timeline.theme.ink,toolOf(e));
    else if(e.type==='text')tip=drawText(e,p);
    else if(e.type==='fill'){ctx.globalAlpha*=smooth(p);ctx.fillStyle=e.color;ctx.fill(e._path);}
    else if(e.type==='hatch'||e.type==='crosshatch'||e.type==='paint'){
      ctx.clip(e._path);tip=trace(ctx,e._trace,p,e.type==='paint'?(e.brushSize??18):(e.width??1.2),e.color||timeline.theme.ink,toolOf(e));
    }
    else if(e.type==='image'||e.type==='image_ink') {
      const image=e._asset, scale=e.width/image.data.width, phase=imageInkPhase(e), ip=clamp(p/Math.max(.001,phase));
      const ic=image.canvas, ix=image.ctx;
      ix.clearRect(0,0,ic.width,ic.height);ix.globalCompositeOperation='source-over';
      const raw=trace(ix,e._trace,ip,e.brushSize??image.data.brushSize??7,'#fff');
      // Full-color drawing uses exactly the same progressively stroked mask
      // and tip trajectory. Even at completion, pixels outside the swept
      // brush stay hidden; there is no final color fade or full-image snap.
      ix.globalCompositeOperation='source-in';ix.drawImage(imageDraw(e)?image.color:image.ink,0,0);ix.globalCompositeOperation='source-over';
      if(!imageFade(e))ctx.drawImage(ic,e.x,e.y,e.width,image.data.height*scale);
      if(!imageDraw(e)&&(imageFade(e)||p>phase)){
        ctx.save();ctx.globalAlpha*=imageFade(e)?smooth(p):smooth((p-phase)/Math.max(.001,1-phase));
        ctx.drawImage(image.color,e.x,e.y,e.width,image.data.height*scale);ctx.restore();
      }
      if(raw&&ip<1&&!imageFade(e))tip=[e.x+raw[0]*scale,e.y+raw[1]*scale];
    }
    else if(e.type==='erase')tip=pointOnTrace(e._trace,p);
    else throw new Error('Unsupported element type: '+e.type);
    ctx.restore();
    if(erased.length){
      ctx.save();ctx.setTransform(1,0,0,1,0,0);ctx.globalCompositeOperation='destination-out';ctx.globalAlpha=1;
      for(const eraser of erased){
        const ep=clamp((localMs-eraser.startMs)/Math.max(1,eraser.durationMs));
        const r=eraser.region;
        if(ep>=1)ctx.clearRect(r.x-2,r.y-2,r.width+4,r.height+4);
        else {ctx.save();ctx.beginPath();ctx.rect(r.x,r.y,r.width,r.height);ctx.clip();trace(ctx,eraser._trace,ep,eraser.brushSize||24,'#fff');ctx.restore();}
      }
      ctx.restore();ctx=originalCtx;ctx.drawImage(eraseLayer,0,0);
    }
    if(tip && p>0 && p<1 && !e.instant) {
      return {x:tip[0]*(tr.scale||1)+(tr.x||0),y:tip[1]*(tr.scale||1)+(tr.y||0),start:e.startMs,color:e.color,tool:toolOf(e)};
    }
    return null;
  }

  function pointOnTrace(data,p) {
    let left=data.total*clamp(p);
    for(const part of data.parts){
      if(left>part.length){left-=part.length;continue;}
      const pts=part.points;
      for(let i=1;i<pts.length;i++)if(part.cum[i]>=left){
        const f=(left-part.cum[i-1])/Math.max(.001,part.cum[i]-part.cum[i-1]);
        return [pts[i-1][0]+(pts[i][0]-pts[i-1][0])*f,pts[i-1][1]+(pts[i][1]-pts[i-1][1])*f];
      }
      return pts[pts.length-1]||[0,0];
    }
    const last=data.parts[data.parts.length-1];return last?.points.at(-1)||[0,0];
  }

  function elementAnchor(e,p) {
    let point;
    if(e.type==='text'){
      const chars=e._chars.filter(x=>x.g.strokes.length);
      const ch=p===0?chars[0]:chars.at(-1), scale=e.size/atlasData.fontSize;
      if(!ch)return [e.x,e.y];
      const raw=pointOnTrace(ch.data,p);
      point=[ch.x-atlasData.leftPad*scale+raw[0]*scale,e.y-12*scale+raw[1]*scale];
    } else if(e.type==='image'||e.type==='image_ink'){
      const raw=pointOnTrace(e._trace,p),scale=e.width/e._asset.data.width;
      point=[e.x+raw[0]*scale,e.y+raw[1]*scale];
    }else point=pointOnTrace(e._trace,p);
    const tr=e.transform||{};
    return [point[0]*(tr.scale||1)+(tr.x||0),point[1]*(tr.scale||1)+(tr.y||0)];
  }

  function serialTool(ms,activeTip) {
    const track=timeline.toolTrack||[];
    const index=track.findIndex(a=>ms>=a.startMs&&ms<a.endMs);
    if(index>=0){
      const action=track[index],e=elementsByKey.get(action.sceneId+'|'+action.elementId);
      const p=clamp((ms-action.startMs)/(action.endMs-action.startMs));
      if((e.type==='image'||e.type==='image_ink')&&p>imageInkPhase(e))return null;
      if(activeTip)return {...activeTip,tool:action.tool};
      const xy=elementAnchor(e,p);return {x:xy[0],y:xy[1],tool:action.tool,color:e.color};
    }
    const nextIndex=track.findIndex(a=>ms>=a.travelStartMs&&ms<a.startMs);
    if(nextIndex<0)return null;
    const next=track[nextIndex], e=elementsByKey.get(next.sceneId+'|'+next.elementId);
    const b=elementAnchor(e,0), prev=track[nextIndex-1];
    const a=prev?elementAnchor(elementsByKey.get(prev.sceneId+'|'+prev.elementId),1):[b[0]+12,b[1]-38];
    const p=smooth((ms-next.travelStartMs)/Math.max(1,next.startMs-next.travelStartMs));
    return {x:a[0]+(b[0]-a[0])*p,y:a[1]+(b[1]-a[1])*p-Math.sin(p*Math.PI)*22,
      tool:next.tool,color:e.color,lift:true,opacity:.65+.35*Math.abs(2*p-1)};
  }

  function pencil(tip,time,opacity=1) {
    toolRenderCount++;
    // Pencil local origin is its physical graphite point; no floating anchor.
    // Canvas y is down, so a POSITIVE angle tilts the body clockwise -> top ends up
    // right of the tip, i.e. a natural right-hand grip. Flip the sign for left-leaning.
    ctx.save();ctx.translate(tip.x,tip.y);ctx.rotate(.70+Math.sin(time*8)*.035);
    ctx.globalAlpha=opacity;
    ctx.fillStyle='#24302a';ctx.beginPath();ctx.moveTo(0,0);ctx.lineTo(1,-10);ctx.lineTo(7,-6);ctx.closePath();ctx.fill();
    ctx.fillStyle='#edc89b';ctx.strokeStyle='#343b31';ctx.lineWidth=1.3;
    ctx.beginPath();ctx.moveTo(1,-9);ctx.lineTo(-5,-24);ctx.lineTo(11,-24);ctx.lineTo(7,-6);ctx.closePath();ctx.fill();ctx.stroke();
    ctx.fillStyle='#f0c65e';ctx.beginPath();ctx.moveTo(-5,-24);ctx.lineTo(-5,-72);ctx.lineTo(11,-72);ctx.lineTo(11,-24);ctx.closePath();ctx.fill();ctx.stroke();
    ctx.strokeStyle='#ffedab';ctx.lineWidth=2;ctx.beginPath();ctx.moveTo(0,-28);ctx.lineTo(0,-68);ctx.stroke();
    ctx.fillStyle='#afb2a5';ctx.fillRect(-5,-79,16,8);ctx.strokeStyle='#343b31';ctx.strokeRect(-5,-79,16,8);
    ctx.fillStyle='#d78d8e';ctx.beginPath();ctx.roundRect(-5,-88,16,10,3);ctx.fill();ctx.stroke();
    ctx.restore();
  }

  function eraser(tip,time) {
    toolRenderCount++;
    // Positive angle -> right-hand grip (see pencil() note).
    ctx.save();ctx.translate(tip.x,tip.y);ctx.rotate(.15+Math.sin(time*13)*.025);
    ctx.globalAlpha=tip.opacity??1;ctx.strokeStyle='#6c5b54';ctx.lineWidth=1.4;
    ctx.fillStyle='#e9b4ad';ctx.beginPath();ctx.roundRect(-23,-14,46,25,5);ctx.fill();ctx.stroke();
    ctx.fillStyle='#f5e9d8';ctx.fillRect(-22,-3,44,11);ctx.strokeStyle='#b9a998';ctx.beginPath();ctx.moveTo(-21,-3);ctx.lineTo(21,-3);ctx.stroke();
    ctx.restore();
  }

  function inkPen(tip,time,tool){
    toolRenderCount++;
    // Positive angle -> right-hand grip (see pencil() note).
    ctx.save();ctx.translate(tip.x,tip.y);ctx.rotate(.62+Math.sin(time*7)*.018);ctx.globalAlpha=tip.opacity??1;
    ctx.strokeStyle='#303a36';ctx.lineWidth=1.3;
    if(tool==='marker'){
      ctx.fillStyle=tip.color||'#3d8d65';ctx.beginPath();ctx.moveTo(-5,-1);ctx.lineTo(5,-1);ctx.lineTo(7,-13);ctx.lineTo(-7,-13);ctx.closePath();ctx.fill();
      ctx.fillStyle='#e6e8df';ctx.beginPath();ctx.roundRect(-10,-76,20,64,5);ctx.fill();ctx.stroke();
      ctx.fillStyle=tip.color||'#3d8d65';ctx.fillRect(-10,-72,20,12);ctx.fillRect(-10,-27,20,9);
    }else{
      ctx.fillStyle='#d9d4ba';ctx.beginPath();ctx.moveTo(0,0);ctx.lineTo(-5,-15);ctx.lineTo(5,-15);ctx.closePath();ctx.fill();ctx.stroke();
      ctx.strokeStyle='#756f57';ctx.beginPath();ctx.moveTo(0,-3);ctx.lineTo(0,-12);ctx.stroke();
      ctx.fillStyle='#2d3d48';ctx.beginPath();ctx.roundRect(-5,-77,10,64,3);ctx.fill();ctx.stroke();
      ctx.strokeStyle='#b8c1c1';ctx.beginPath();ctx.moveTo(3,-69);ctx.lineTo(3,-41);ctx.stroke();
    }
    ctx.restore();
  }

  function drawScene(s,time,alpha=1) {
    let candidate=null;
    ctx.save();ctx.globalAlpha=alpha;
    for(const e of s.elements) {
      const tip=drawElement(e,time-s.startMs);
      if(tip && (!candidate||tip.start>=candidate.start))candidate=tip;
    }
    ctx.restore();return candidate;
  }

  function caption(ms) {
    const cue=timeline.cues.find(c=>c.startMs<=ms&&ms<c.endMs);
    if(!cue)return;
    const text=cue.text.replace(/\s+/g,' ');
    ctx.save();ctx.font=`${timeline.captionStyle?.size||28}px WenKai`;
    ctx.fillStyle=timeline.captionStyle?.color||timeline.theme.ink;
    ctx.textAlign='center';ctx.textBaseline='middle';
    ctx.globalAlpha=Math.min(1,(ms-cue.startMs)/120,(cue.endMs-ms)/120);
    ctx.fillText(text,timeline.captionStyle?.x??boardWidth/2,timeline.captionStyle?.y??boardHeight-47,timeline.captionStyle?.maxWidth??boardWidth*.90);ctx.restore();
  }

  function renderAt(seconds,returnData=true) {
    currentTime=Math.max(0,Math.min(timeline.durationMs/1000,seconds));
    const ms=currentTime*1000;
    toolRenderCount=0;
    ctx.setTransform(1,0,0,1,0,0);ctx.globalAlpha=1;ctx.globalCompositeOperation='source-over';
    ctx.drawImage(paper,0,0);
    let candidate=null;
    for(const e of timeline.elements||[]) {const tip=drawElement(e,ms);if(tip)candidate=tip;}
    let index=timeline.scenes.findIndex(s=>ms>=s.startMs&&ms<s.endMs);
    if(index<0&&timeline.scenes.length)index=ms<timeline.scenes[0].startMs?0:timeline.scenes.length-1;
    const scene=timeline.scenes[index];
    const age=scene?ms-scene.startMs:0, trans=timeline.transition?.durationMs||0;
    if(index>0&&age<trans) {
      // A quick blank-paper wipe preserves a fixed camera and familiar header.
      const p=smooth(age/trans), boundary=boardWidth*p,top=timeline.transition?.top??boardHeight*.174,bottom=timeline.transition?.bottom??boardHeight*.117;
      ctx.save();ctx.beginPath();ctx.rect(boundary,top,boardWidth-boundary,boardHeight-top-bottom);ctx.clip();
      drawScene(timeline.scenes[index-1],timeline.scenes[index-1].endMs-1);ctx.restore();
      ctx.save();ctx.beginPath();ctx.rect(0,top,boundary,boardHeight-top-bottom);ctx.clip();
      candidate=drawScene(scene,ms)||candidate;ctx.restore();
    } else if(scene)candidate=drawScene(scene,ms)||candidate;
    candidate=serialTool(ms,candidate);
    if(candidate&&!params.has('hideTools')){
      if(candidate.tool==='eraser')eraser(candidate,currentTime);
      else if(candidate.tool==='pen'||candidate.tool==='marker')inkPen(candidate,currentTime,candidate.tool);
      else pencil(candidate,currentTime,candidate.opacity??1);
    }
    caption(ms);
    seek.value=String(currentTime);output.textContent=`${currentTime.toFixed(1)} / ${(timeline.durationMs/1000).toFixed(1)}`;
    const concurrentInk=(timeline.toolTrack||[]).filter(a=>{
      if(ms<a.startMs||ms>=a.endMs)return false;
      const e=elementsByKey.get(a.sceneId+'|'+a.elementId);
      return !((e.type==='image'||e.type==='image_ink')&&(ms-a.startMs)/(a.endMs-a.startMs)>=imageInkPhase(e));
    }).length;
    debugTotals.maxVisibleTools=Math.max(debugTotals.maxVisibleTools,toolRenderCount);
    debugTotals.maxConcurrentInk=Math.max(debugTotals.maxConcurrentInk,concurrentInk);debugTotals.renderCount++;
    window.lastRenderStats={seconds:currentTime,visibleTools:toolRenderCount,concurrentInk,
      activeActions:(timeline.toolTrack||[]).filter(a=>ms>=a.startMs&&ms<a.endMs).length,
      tool:candidate?.tool||null,lift:!!candidate?.lift,tip:candidate?{x:candidate.x,y:candidate.y}:null,...debugTotals};
    return returnData?canvas.toDataURL('image/jpeg',.94):null;
  }

  function paperTexture() {
    const p=paper.getContext('2d');p.fillStyle=timeline.theme.paper;p.fillRect(0,0,boardWidth,boardHeight);
    if(timeline.drawPolicy?.profile==='all-drawn')return;
    p.fillStyle=timeline.theme.dot||'#d5d8c8';p.globalAlpha=.64;
    const spacing=timeline.theme.dotSpacing??26;
    if(spacing>0)for(let y=12;y<boardHeight;y+=spacing)for(let x=12;x<boardWidth;x+=spacing){p.beginPath();p.arc(x,y,.7,0,Math.PI*2);p.fill();}
    // Fixed, extremely subtle fibres do not flicker across exported frames.
    p.globalAlpha=.04;p.strokeStyle='#819478';p.lineWidth=.6;
    for(let i=0;i<80;i++){const x=(i*271)%boardWidth,y=(i*157)%boardHeight;p.beginPath();p.moveTo(x,y);p.lineTo(x+1.8,y+.4);p.stroke();}
    p.globalAlpha=1;
  }

  function elementLayout(sceneId,e){
    let b;
    if(e.type==='text'){
      const scale=e.size/atlasData.fontSize;
      const visible=e._chars.filter(ch=>ch.g.inkBounds);
      if(visible.length){
        const left=Math.min(...visible.map(ch=>ch.x-atlasData.leftPad*scale+ch.g.inkBounds[0]*scale));
        const right=Math.max(...visible.map(ch=>ch.x-atlasData.leftPad*scale+ch.g.inkBounds[2]*scale));
        const top=Math.min(...visible.map(ch=>e.y-12*scale+ch.g.inkBounds[1]*scale));
        const bottom=Math.max(...visible.map(ch=>e.y-12*scale+ch.g.inkBounds[3]*scale));
        b={x:left,y:top,width:right-left,height:bottom-top};
      }else b={x:e.x,y:e.y,width:0,height:0};
    }else if(e.type==='image'||e.type==='image_ink')b={x:e.x,y:e.y,width:e.width,height:e.width*e._asset.data.height/e._asset.data.width};
    else if(e.type==='erase')b={...e.region};
    else if(e.d)b=e.bounds||svgBounds(e.d);
    else b={x:0,y:0,width:0,height:0};
    const tr=e.transform||{},scale=tr.scale??1,padding=e.type==='stroke'?(e.width??toolProfiles[toolOf(e)].width)/2:0;
    const x=(b.x-padding)*scale+(tr.x||0),y=(b.y-padding)*scale+(tr.y||0),width=(b.width+2*padding)*scale,height=(b.height+2*padding)*scale;
    return {sceneId,id:e.id,type:e.type,text:e.text,textWidth:e._width??null,bounds:{x,y,width,height},outOfCanvas:x<0||y<0||x+width>boardWidth||y+height>boardHeight,
      traceParts:e._trace?.parts.length??0,traceLength:e._trace?.total??0};
  }

  async function jsonFile(src){const response=await fetch(src);if(!response.ok)throw new Error('Missing JSON '+src);return response.json();}

  async function initialize() {
    timeline=await jsonFile(params.get('timeline')||'./timeline.json');
    if(!(timeline.durationMs>0&&Number.isFinite(timeline.durationMs)))throw new Error('durationMs must be positive');
    timeline.theme={paper:'#fdfcf5',ink:'#303d37',dot:'#d5d8c8',...timeline.theme};
    timeline.scenes??=[];timeline.elements??=[];timeline.cues??=[];
    timeline.toolMode='serial';
    boardWidth=timeline.canvas?.width??1280;boardHeight=timeline.canvas?.height??720;
    if(!(boardWidth>0&&boardHeight>0&&Number.isFinite(boardWidth)&&Number.isFinite(boardHeight)))throw new Error('Invalid canvas dimensions');
    for(const c of [canvas,paper,eraseLayer]){c.width=boardWidth;c.height=boardHeight;}
    const all=[...timeline.elements,...timeline.scenes.flatMap(s=>s.elements)];
    if(all.some(e=>e.type==='text')){
      const glyphPath=params.get('glyphs')||'./assets/glyphs.json';
      atlasData=await jsonFile(glyphPath);
      atlas=await loadImage(params.get('atlas')||'./assets/glyph-atlas.png');
      if(atlasData.font){
        const url=new URL(atlasData.font,new URL(glyphPath,location.href)).href;
        const face=new FontFace('WenKai',`url("${url}")`);await face.load();
        for(const previous of document.fonts)if(previous.family==='WenKai')document.fonts.delete(previous);
        document.fonts.add(face);
      }
    }else atlasData={glyphs:{},fontSize:80,leftPad:0};
    const audioDir=params.get('audio');
    const narrationPath=params.get('narration')||(audioDir?audioDir+'/narration.wav':null),sfxPath=params.get('sfx')||(audioDir?audioDir+'/sfx.wav':null);
    if(narrationPath)narration.src=narrationPath;if(sfxPath)sfx.src=sfxPath;
    seek.max=String(timeline.durationMs/1000);document.querySelector('h1').textContent=timeline.title||'手绘讲解预览';
    const summary=document.querySelector('#summary');if(summary)summary.textContent=`${(timeline.durationMs/1000).toFixed(1)} 秒 · 单工具绘制`;
    await document.fonts.load('28px WenKai');
    await Promise.all(all.filter(e=>e.type==='image'||e.type==='image_ink').map(async e=>{
      const key=imageAssetKey(e);
      if(imageAssets.has(key))return;
      const [data,color,ink]=await Promise.all([
        jsonFile(e.traceSrc),loadImage(e.src),loadImage(e.inkSrc||e.src)]);
      const c=document.createElement('canvas');c.width=data.width;c.height=data.height;
      imageAssets.set(key,{data,color,ink,canvas:c,ctx:c.getContext('2d')});
    }));
    for(const glyph of Object.values(atlasData.glyphs)){
      glyph._trace=metrics(glyph.strokes);
      // Legacy atlases do not include inkBounds. Measure their alpha rather
      // than mistaking transparent glyph padding for visible text overflow.
      if(!Object.prototype.hasOwnProperty.call(glyph,'inkBounds')){
        const layer=document.createElement('canvas');layer.width=glyph.w;layer.height=glyph.h;
        const c=layer.getContext('2d');c.drawImage(atlas,glyph.x,glyph.y,glyph.w,glyph.h,0,0,glyph.w,glyph.h);
        const pixels=c.getImageData(0,0,glyph.w,glyph.h).data;
        let left=glyph.w,top=glyph.h,right=-1,bottom=-1;
        for(let y=0;y<glyph.h;y++)for(let x=0;x<glyph.w;x++)if(pixels[(y*glyph.w+x)*4+3]>0){left=Math.min(left,x);right=Math.max(right,x);top=Math.min(top,y);bottom=Math.max(bottom,y);}
        glyph.inkBounds=right>=0?[left,top,right+1,bottom+1]:null;
      }
    }
    for(const e of all)prepareElement(e);
    for(const e of timeline.elements||[]){
      if(e.type==='erase')throw new Error('Global erase is unsupported; use a scene target');
      elementsByKey.set('global|'+e.id,e);
    }
    for(const scene of timeline.scenes){
      const lookup=new Map(scene.elements.map(e=>[e.id,e]));
      for(const e of scene.elements){
        elementsByKey.set(scene.id+'|'+e.id,e);
        if(e.type==='erase')for(const id of e.targets||[]){
          const target=lookup.get(id);if(!target)throw new Error('Unknown eraser target '+id);
          (target._erasers||=[]).push(e);
        }
      }
    }
    const expected=new Set([...elementsByKey].filter(([,e])=>isAnimatedInk(e)).map(([key])=>key));
    let previousEnd=-Infinity;
    for(const action of timeline.toolTrack||[]){
      const key=action.sceneId+'|'+action.elementId,e=elementsByKey.get(key);
      if(!e)throw new Error('Unknown toolTrack action '+key);
      if(action.startMs<previousEnd-.001||action.endMs<=action.startMs)throw new Error('Overlapping or invalid toolTrack action '+key);
      const scope=action.sceneId==='global'?0:timeline.scenes.find(s=>s.id===action.sceneId).startMs;
      if(Math.abs(action.startMs-(scope+e.startMs))>.01||Math.abs(action.endMs-(scope+e.startMs+e.durationMs))>.01)throw new Error('toolTrack timing differs from element '+key);
      if(!expected.delete(key))throw new Error('Duplicate or passive toolTrack action '+key);
      action.tool??=toolOf(e);previousEnd=action.endMs;
    }
    if(expected.size)throw new Error('Run schedule.py before rendering; unscheduled ink: '+[...expected].join(', '));
    window.layoutReport=[...elementsByKey].map(([key,e])=>elementLayout(key.split('|')[0],e));
    paperTexture();renderAt(0,false);
    if(new URLSearchParams(location.search).has('export'))document.body.classList.add('export');
    return true;
  }

  async function loadImage(src){const image=new Image();image.src=src;await image.decode();return image;}

  window.renderAt=renderAt;
  window.debugElements=()=>window.layoutReport;
  window.debugTotals=()=>({...debugTotals});
  window.resetRenderStats=()=>{debugTotals={maxVisibleTools:0,maxConcurrentInk:0,renderCount:0};};
  window.debugTrace=id=>{
    const e=elementsByKey.get(id)||[...elementsByKey.values()].find(e=>e.id===id);
    return e?{type:e.type,parts:e._trace?.parts.map(p=>p.points)||[],length:e._trace?.total??0}:null;
  };
  window.ready=initialize();
  window.ready.catch(error=>{document.querySelector('.note').textContent=error.message;});
  document.querySelector('#play').onclick=async()=>{
    await window.ready;
    if(playing){playing=false;narration.pause();sfx.pause();document.querySelector('#play').textContent='播放';return;}
    if(currentTime>=timeline.durationMs/1000)currentTime=0;
    if(narration.hasAttribute('src')){narration.currentTime=currentTime;await narration.play().catch(()=>{});}
    if(sfx.hasAttribute('src')){sfx.currentTime=currentTime;sfx.volume=.8;sfx.play().catch(()=>{});}
    playing=true;startClock=performance.now()-currentTime*1000;document.querySelector('#play').textContent='暂停';
    requestAnimationFrame(tick);
  };
  function tick(now){if(!playing)return;const t=(now-startClock)/1000;renderAt(t,false);if(t>=timeline.durationMs/1000){playing=false;narration.pause();sfx.pause();document.querySelector('#play').textContent='播放';}else requestAnimationFrame(tick);}
  seek.oninput=()=>{playing=false;narration.pause();sfx.pause();renderAt(Number(seek.value),false);document.querySelector('#play').textContent='播放';};
  document.querySelector('#restart').onclick=()=>{playing=false;narration.pause();sfx.pause();renderAt(0,false);document.querySelector('#play').textContent='播放';document.querySelector('#play').click();};
})();
