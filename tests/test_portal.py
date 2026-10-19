"""Browser regressions for the embedded portal; all devices, API calls and audio are synthetic.

Run with: python3 -m unittest discover -s tests -p 'test_portal.py'
Set CHROME_BIN when Chrome/Chromium is not at a conventional location.
"""
import ast
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import unittest


SOURCE_ROOT = Path(__file__).resolve().parents[1]
PORTAL = Path(os.environ.get("PORTAL_SOURCE", SOURCE_ROOT / "portal.py"))
BOOTSTRAP = r"""
localStorage.clear();
window.F={requests:[],streams:[],clones:[],contexts:[],errors:[],api:[],fail:null,wait:null,sessionWait:null,
 devices:[{kind:'audioinput',deviceId:'default',groupId:'builtin',label:'Default - Built-in microphone'},
 {kind:'audioinput',deviceId:'builtin',groupId:'builtin',label:'Built-in microphone'},
 {kind:'audioinput',deviceId:'headset',groupId:'headset',label:'Bluetooth headset'}]};
addEventListener('error',e=>F.errors.push(e.message));
addEventListener('unhandledrejection',e=>F.errors.push(String(e.reason)));
window.setInterval=()=>0;
F.fullHierarchy=/*FULL_HIERARCHY*/true;
F.defaultAssistant='boss';
F.agents=[{id:'boss',name:'Gideon',role:'PM and Enterprise Architect',skills:['Routing'],reports_to:'hermes',is_lead:true,color:0xa78bfa},
 {id:'watcher',name:'Sellen',role:'Observability',skills:['Metrics'],reports_to:'boss',color:0x22d3ee}];
F.agents.forEach(a=>Object.assign(a,{state:'idle',avatar:'',stats30:{runs:0,errors:0,input:0,output:0,cost:0},units_state:{}}));
F.catalog=/*HIERARCHY_ROSTER*/[];
F.roster=F.catalog.filter(a=>a.participates_in_hierarchy!==false);
window.AudioContext=class{
 constructor(){this.audioWorklet={addModule:async()=>{}};this.destination={};F.contexts.push(this)}
 async resume(){}
 async close(){}
 createMediaStreamSource(){return {connect(){},disconnect(){}}}
 createGain(){return {gain:{value:1},connect(){},disconnect(){}}}
};
window.AudioWorkletNode=class{
 constructor(){this.port={onmessage:null}}
 connect(){}
 disconnect(){}
};
F.makeStream=d=>{
 const track={kind:'audio',label:d.label,enabled:true,readyState:'live',
  getSettings:()=>({deviceId:d.deviceId,groupId:d.groupId}),stop(){this.readyState='ended'},
  clone(){const c={...this,enabled:this.enabled,readyState:this.readyState,stop(){this.readyState='ended'}};F.clones.push(c);return c}};
 const stream={getAudioTracks:()=>[track],getTracks:()=>[track]};
 F.streams.push(stream);return stream;
};
Object.defineProperty(navigator,'mediaDevices',{value:{
 enumerateDevices:async()=>{if(F.enumError)throw F.enumError;return F.devices.slice()},
 addEventListener:(name,fn)=>{F.deviceEvent=fn},
 getUserMedia:async constraints=>{
  F.requests.push(constraints);
  if(F.fail){const error=F.fail;F.fail=null;throw error}
  if(F.wait)await F.wait;
  const id=constraints.audio.deviceId&&constraints.audio.deviceId.exact;
  const def=F.devices.find(d=>d.deviceId=='default');
  const d=id?F.devices.find(d=>d.deviceId==id):F.devices.find(d=>d.deviceId!='default'&&d.deviceId!='communications'&&(!def||d.groupId==def.groupId));
  if(!d)throw new DOMException('Missing input','NotFoundError');
  return F.makeStream(d);
 }
}});
window.fetch=async(url,opts)=>{
 const u=String(url);F.api.push(u);let body={};
 if(u=='/api/agents')body=F.agents;
 else if(u=='/api/hierarchy')body={agents:F.hierarchyRoster||F.agents.filter(a=>a.participates_in_hierarchy!==false),status:F.hierarchyStatus||'configured',active_ids:F.hierarchyActive||F.agents.map(a=>a.id)};
 else if(u=='/api/graph'){
  if(F.graphError)return new Response(JSON.stringify({error:'Synthetic graph unavailable'}),{status:503});
  body=F.graphData||{nodes:[{id:'boss',type:'agent',label:'Gideon',x:300,y:100},
   {id:'system',type:'system',label:'Ledger',x:650,y:100},{id:'watcher',type:'agent',label:'Sellen',x:300,y:200}],
   edges:[{from:'boss',to:'system',kind:'data',label:'Records evidence'},{from:'watcher',to:'boss',kind:'report',label:'Reports to Gideon'}]};
 }
 else if(u.startsWith('/api/agents/'))body={agent:F.agents.find(a=>a.id==decodeURIComponent(u.slice(12))),
  by_model:[],by_trigger:[],daily:[],lifetime:{runs:0,ok:0,errors:0,cost:0},recent:[],approvals:[],connections:[]};
 else if(u=='/api/summary')body={team:'Test Roundtable',host:'test',spent:0,cap:65,month:'test',month_runs:0,month_tokens:0,open_approvals:0};
 else if(u=='/api/voice/info')body={ok:true,authed:true,spend:0,cap:20,model:'test',idle_seconds:120,max_seconds:900};
 else if(u=='/api/voice/session'){
  if(F.sessionWait)await F.sessionWait;
  if(F.sessionNetwork)throw new TypeError('Test network unavailable');
  if(F.sessionError)return new Response(JSON.stringify({error:'Test session rejected'}),{status:503});
  body={run_id:1,value:'synthetic-key'};
 }else if(u=='https://api.openai.com/v1/realtime/calls')return new Response('synthetic-sdp');
 else if(u=='/api/mirror')body={state:{},age:99,cmds:[],desk:false};
 else if(u=='/api/desk/events')body=F.deskEvents||[];
 else if(u=='/api/voice/local/info')body=F.localInfo||{enabled:false};
 else if(u.startsWith('/api/ipmap')){F.ipcalls=(F.ipcalls||[]).concat(u);body=F.ipmap||{error:'no stub'}}
 else if(!['/api/voice/end','/api/voice/usage'].includes(u))throw new Error('Unexpected fetch: '+u);
 return new Response(JSON.stringify(body),{headers:{'Content-Type':'application/json'}});
};
navigator.sendBeacon=()=>true;
window.NativePeerConnection=window.RTCPeerConnection;
window.RTCPeerConnection=class{
 constructor(){this.senders=[];this.connectionState='connected'}
 addTrack(track){this.senders.push({track,replaceTrack:async function(t){this.track=t}})}
 getSenders(){return this.senders}
 createDataChannel(){return {readyState:'open',send(){},close(){}}}
 async createOffer(){return {sdp:'synthetic-sdp'}}
 async setLocalDescription(){}
 async setRemoteDescription(){}
 close(){this.connectionState='closed'}
};
"""
NATIVE_TESTS = r"""
(async()=>{
 const out=document.createElement('pre');out.id='test-results';document.body.appendChild(out);let pc;
 try{
  await loadVoice();await vmics();vmicremember('');
  await vmic();const old=V.stream.getAudioTracks()[0];
  if(!(old instanceof MediaStreamTrack))throw new Error('Input is not a native MediaStreamTrack');
  if(old.readyState!='live')throw new Error('Native default input did not open');
  const available=MIC.devices.find(d=>d.deviceId&&d.deviceId!='default'&&d.deviceId!='communications');
  if(!available)throw new Error('Chromium synthetic microphone missing');
  pc=new NativePeerConnection();pc.addTrack(old,V.stream);V.pc=pc;
  await vmicchange(available.deviceId);
  if(pc.getSenders()[0].track!==V.tx||V.tx===V.stream.getAudioTracks()[0]||V.tx.label!=V.stream.getAudioTracks()[0].label)throw new Error('Native RTCRtpSender.replaceTrack (sent copy) failed');
  if(old.readyState!='ended')throw new Error('Native original microphone was not released');
  const current=V.stream.getAudioTracks()[0];vmicrelease();
  if(current.readyState!='ended'||V.actx||V.meter||V.tx)throw new Error('Native cleanup failed');
  if(F.api.some(u=>u.includes('/api/voice/session')||u.includes('api.openai.com')))throw new Error('Smoke test attempted a paid API call');
  out.textContent=JSON.stringify([{name:'Native getUserMedia, exact selection, replaceTrack and cleanup with Chromium fake input',ok:true}]);
 }catch(e){out.textContent=JSON.stringify([{name:'Native microphone integration',ok:false,error:e.stack}])}
 finally{if(pc)pc.close();V.pc=null;out.dataset.complete='true';
  const xhr=new XMLHttpRequest();xhr.open('POST','/results');xhr.send(out.textContent)}
})()
"""
TESTS = r"""
(async()=>{
 const results=[],assert=(ok,message)=>{if(!ok)throw new Error(message)};
 const out=document.createElement('pre');out.id='test-results';document.body.appendChild(out);
 const pause=()=>new Promise(resolve=>setTimeout(resolve,20));
 const error=(name,constraint)=>Object.assign(new Error('Synthetic '+name),{name,constraint});
 const initial=F.devices.slice();
 async function reset(){
  V.starting=false;V.pc=null;V.run=null;V.muted=false;V.speaking=false;V.duplex=false;V.responding=false;V.cutAt=0;
  MIC.busy=false;MIC.pending=false;MIC.testing=false;vmicrelease();vmicremember('');
  F.devices=initial.slice();F.fail=null;F.wait=null;F.enumError=null;F.sessionWait=null;F.sessionError=false;F.sessionNetwork=false;
  F.hierarchyRoster=null;F.hierarchyStatus=null;F.hierarchyActive=null;
  F.graphError=false;F.graphData=null;$('#map-reporting').checked=false;
  F.requests=[];F.api=[];await vmics();V.info={ok:true,authed:true,spend:0,cap:20,idle_seconds:120,max_seconds:900,model:'test'};
  B.on=false;C.want=false;if(audioDialog.open)vmicsettingsclose();
 }
 async function test(name,fn){out.dataset.running=name;try{await reset();await fn();results.push({name,ok:true})}
 catch(e){results.push({name,ok:false,error:e.stack||String(e)})}out.textContent=JSON.stringify(results)}
 async function withHierarchy(fn){
  const saved=F.agents;F.agents=F.catalog.map(a=>({...a,state:'idle',stats30:{runs:0,errors:0,input:0,output:0,cost:0},units_state:{}}));
  try{location.hash='hierarchy';route();await pause();await fn()}
  finally{F.agents=saved;location.hash='voice';route();await pause()}
 }
 await pause();await loadVoice();await vmics();
 results.push({name:'Startup never requests microphone permission',ok:F.requests.length==0});
 results.push({name:F.defaultAssistant+' is the preserved fresh-device default',ok:V.agent==F.defaultAssistant});
 if(DESK){await pause();results.push({name:'Desktop companion starts showing Gideon with his agent panel',ok:!B.el.hidden&&V.agent=='boss'&&$('#dpanel').children.length>0})}
 results.push({name:'Local PC voice is the fresh-device default engine',ok:V.engine=='local'&&$('#vengine').value=='local'});
 V.engine='openai';  // the call tests below exercise the OpenAI Realtime path
 await test('System default works without an Elgato',async()=>{
  const stream=await vmic();assert(stream.getAudioTracks()[0].label=='Built-in microphone','wrong default input');
  assert(!F.requests[0].audio.deviceId,'default must not constrain deviceId');
  assert(F.requests[0].audio.echoCancellation&&F.requests[0].audio.noiseSuppression&&F.requests[0].audio.autoGainControl,'audio processing was lost');
 });
 await test('Specific microphone is remembered locally',async()=>{
  await vmicchange('headset');await vmic();
  assert(F.requests[0].audio.deviceId.exact=='headset','selection ignored');
  assert(localStorage.getItem('roundtable.mic')=='headset','selection not saved');
  assert($('#vmiclabel').textContent=='Bluetooth headset','active device not shown');
 });
 await test('Stale machine-specific choice falls back explicitly',async()=>{
  vmicremember('unplugged');await vmic();
  assert(F.requests.length==2&&!F.requests[1].audio.deviceId,'no default retry');
  assert(MIC.selected==''&&localStorage.getItem('roundtable.mic')=='','stale preference retained');
  assert($('#vmicnote').textContent.includes('unavailable'),'fallback was silent');
 });
 await test('Missing deviceId constraint falls back',async()=>{
  vmicremember('headset');F.fail=error('OverconstrainedError','deviceId');await vmic();
  assert(F.requests.length==2&&MIC.selected=='','constraint fallback failed');
 });
 for(const name of ['NotAllowedError','NotReadableError','SecurityError']){
  await test(name+' is not hidden by a default retry',async()=>{
   vmicremember('headset');F.fail=error(name);let rejected=false;
   try{await vmic()}catch(e){rejected=e.name==name}
   assert(rejected&&F.requests.length==1&&!V.stream,'error hidden or microphone leaked');
  });
 }
 await test('Unrelated constraints do not trigger fallback',async()=>{
  vmicremember('headset');F.fail=error('OverconstrainedError','sampleRate');let rejected=false;
  try{await vmic()}catch(e){rejected=true}assert(rejected&&F.requests.length==1,'unrelated constraint hidden');
 });
 await test('No microphone produces an actionable error',async()=>{
  F.devices=[];await vmictest();assert(!MIC.testing&&!V.stream,'failed test retained input');
  assert($('#vmicnote').textContent.includes('No microphone'),'missing device error not shown');
 });
 await test('Local test makes no paid session and closing releases input',async()=>{
  audioDialog.showModal();await vmictest();const track=V.stream.getAudioTracks()[0];
  assert(MIC.testing&&V.meter,'local test has no meter');
  assert(!F.api.includes('/api/voice/session'),'local test started a paid session');
  $('#vaudioclose').click();await pause();
  assert(track.readyState=='ended'&&!V.stream&&!V.actx&&!V.meter,'local test leaked resources');
  assert($('#vmictestlevel').value==0,'meter not reset');
 });
 await test('Input meter updates both local test and voice room',async()=>{
  vmicLevel(32);assert($('#vmictestlevel').value==50&&$('#vlvl').style.width=='50%','meter level missing');
 });
 await test('HTTP/IP microphone requests explain security and never start capture or a paid call',async()=>{
  const original=Object.getOwnPropertyDescriptor(window,'isSecureContext');
  try{Object.defineProperty(window,'isSecureContext',{value:false,configurable:true});
   await loadVoice();await vmictest();await vstart('');
   assert(!F.requests.length&&!F.api.includes('/api/voice/session'),'insecure request captured or spent');
   assert($('#vwarn a').href=='https://roundtable.hrmsmrflrii.xyz/#voice','secure recovery link missing');
   assert($('#vmicnote').textContent.includes('HTTP/IP'),'browser-security cause missing');
  }finally{if(original)Object.defineProperty(window,'isSecureContext',original);else delete window.isSecureContext}
 });
 await test('Unauthenticated starts never open the mic or mint a paid session',async()=>{
  V.info.authed=false;await vstart('');
  assert(!F.requests.length&&!F.api.includes('/api/voice/session'),'unauthenticated capture or spend');
  assert($('#vstatus').textContent.includes('Sign in'),'sign-in recovery missing');
 });
 await test('Retired saved/requested agents recover visibly without importing history',async()=>{
  V.agent='retired';localStorage.setItem(DESK?'deskAgent':'vagent','retired');await loadVoice('retired');
  assert(V.agent==F.defaultAssistant,'retired voice target retained');
  assert(localStorage.getItem(DESK?'deskAgent':'vagent')==F.defaultAssistant,'retired browser preference retained');
  assert($('#vmicnote').textContent.includes('no longer available')&&$('#vmicnote').textContent.includes('no private history'),'selection recovery hidden or history imported');
  assert(!F.api.includes('/api/voice/session'),'selection recovery spent on a call');
 });
 await test('Intentional speaker-mode pause is not diagnosed as a dead microphone',async()=>{
  await vmic();V.pc=new RTCPeerConnection();V.pc.addTrack(V.stream.getAudioTracks()[0]);
  V.speaking=true;vgate();vmicnotice('Speakers mode');vmiccheck();
  assert($('#vmicnote').textContent=='Speakers mode','intentional pause falsely blamed on hardware');
 });
 await test('Listen now repairs a disabled live input, unmutes and retains the call',async()=>{
  await vmic();const track=V.stream.getAudioTracks()[0];V.pc=new RTCPeerConnection();V.pc.addTrack(track);
  const pc=V.pc;track.enabled=false;V.muted=true;await vmicresume();
  assert(track.enabled&&!V.muted&&V.pc===pc,'input was not restored or call restarted');
  assert(!F.api.includes('/api/voice/session'),'recovery minted another session');
 });
 await test('Listen now cancels playback before restoring speaker-mode input',async()=>{
  await vmic();V.pc=new RTCPeerConnection();V.pc.addTrack(V.stream.getAudioTracks()[0]);
  const messages=[];V.dc={readyState:'open',send:x=>messages.push(JSON.parse(x))};
  V.speaking=true;V.responding=true;vgate();await vmicresume();
  assert(messages.some(x=>x.type=='response.cancel')&&messages.some(x=>x.type=='output_audio_buffer.clear'),'agent playback not cancelled');
  assert(!V.speaking&&V.stream.getAudioTracks()[0].enabled,'microphone remained gated');
 });
 await test('Speakers mode: a loud sound only ducks the agent; confirmed speech cuts it off',async()=>{
  await vmic();const pc=new RTCPeerConnection();V.tx=V.stream.getAudioTracks()[0].clone();pc.addTrack(V.tx);V.pc=pc;
  const messages=[];V.dc={readyState:'open',send:x=>messages.push(JSON.parse(x))};AUD.volume=1;
  V.speaking=true;V.responding=true;V.spk0=Date.now();V.suspect={};vgate();
  assert(!V.tx.enabled&&V.stream.getAudioTracks()[0].enabled,'speaker-mode gate must close only the sent copy');
  for(let i=0;i<40;i++){vlevel(i%3?6:12);await new Promise(r=>setTimeout(r,5))}
  assert(V.speaking&&!V.duck&&!messages.length,'agent echo alone ducked or cut the agent off');
  for(let i=0;i<30&&!V.duck;i++){vlevel(50);await new Promise(r=>setTimeout(r,20))}
  assert(V.duck&&AUD.volume<.2&&V.tx.enabled&&V.speaking&&!messages.length,'loud sound should duck (quiet playback, mic sent) without cutting');
  vunduck();assert(!V.duck&&AUD.volume==1&&!V.tx.enabled&&V.speaking&&!messages.length,'noise-only duck did not restore the agent');
  for(let i=0;i<30&&!V.duck;i++){vlevel(50);await new Promise(r=>setTimeout(r,20))}
  await vevent({type:'input_audio_buffer.speech_started',item_id:'him'});
  assert(!V.speaking&&!V.duck&&AUD.volume==1&&messages.some(x=>x.type=='response.cancel')&&messages.some(x=>x.type=='output_audio_buffer.clear'),'confirmed speech did not interrupt');
  assert(V.tx.enabled,'sent microphone not reopened after the cut');
 });
 await test('Animated companion plays idle, listen and random talking gestures over a matching poster',async()=>{
  const a={id:'anim-test',name:'Animated',emoji:'*',role:'Test',color:0xc9a227,avatar:'/avatars/x.jpg',avatar_full:'/avatars/x-full.png',
   anim:{idle:['/avatars/x-idle.webm','/avatars/x-idle2.webm'],listen:'/avatars/x-listen.webm',talk:['/avatars/x-talk.webm','/avatars/x-talk2.webm','/avatars/x-talk3.webm'],poster:'/avatars/x-anim.png'}};
  const savedFull=B.full,savedMotion=DEPTH.motion;B.full=true;B.pip=null;DEPTH.motion={matches:false};
  try{B.state='idle';bLook(a);const box=B.img.parentElement,vids=[...box.querySelectorAll('video')];
   assert(vids.length==6&&vids.every(v=>v.muted)&&B.vids.idle.every(v=>!v.loop)&&B.vids.listen[0].loop&&B.vids.talk.every(v=>!v.loop),'clips missing, not muted, or variants looping');
   assert(B.img.getAttribute('src')=='/avatars/x-anim.png','poster not used for the figure/hit-test');
   B.vids.idle[0].dispatchEvent(new Event('canplay'));const idleOn=()=>B.vids.idle.find(v=>v.classList.contains('on'));assert(box.classList.contains('anim')&&idleOn(),'idle clip not shown');
   const i1=idleOn();i1.dispatchEvent(new Event('ended'));assert(idleOn()&&idleOn()!==i1,'idle variants do not rotate');
   bState('talking');const first=B.vids.talk.find(v=>v.classList.contains('on'));assert(first&&!idleOn(),'talking clip not shown');
   first.dispatchEvent(new Event('ended'));const next=B.vids.talk.find(v=>v.classList.contains('on'));
   assert(next&&next!==first&&!first.classList.contains('on'),'a different gesture did not follow when the clip ended');
   bState('listening');assert(B.vids.listen[0].classList.contains('on')&&!B.vids.talk.some(v=>v.classList.contains('on')),'listening clip not shown');
   bState('thinking');assert(B.vids.listen[0].classList.contains('on'),'thinking should keep the attentive clip');
   bState('idle');assert(idleOn(),'idle clip not restored');
   bLook({...a,anim:null});assert(!box.querySelector('video')&&!box.classList.contains('anim')&&B.img.getAttribute('src')=='/avatars/x-full.png','still figure not restored without clips')}
  finally{B.full=savedFull;DEPTH.motion=savedMotion;bAnimSet({})}
 });
 await test('IP map lists every address with its service, filters by VLAN, status and search, and escapes text',async()=>{
  try{
  const row=(ip,vlan,service,status,x={})=>({ip,vlan,service,status,names:[service],urls:[],sources:['proxmox'],purpose:'',purpose_by:'',notes:[],state:'',...x});
  F.ipmap={as_of:'2026-10-17T06:00:00Z',total:4,vlans:[20,40],errors:['Traefik: timeout'],counts:{ok:2,undocumented:1,retired:1},
   rows:[row('192.168.20.20',20,'node01','ok',{purpose:'Proxmox node',purpose_by:'vault'}),
    row('192.168.40.38',40,'study-lxc','ok',{purpose:'Study notebook',purpose_by:'builder',urls:['https://study.hrmsmrflrii.xyz'],sources:['proxmox','traefik','registry']}),
    row('192.168.40.41',40,'mystery <img src=x>','undocumented',{purpose:'Probably a lab VM',purpose_by:'AI suggestion'}),
    row('192.168.40.10',40,'old-box','retired')]};
  F.ipcalls=[];location.hash='ipmap';route();await pause();await pause();await pause();
  assert(!$('#v-ipmap').hidden,'ip map view hidden');
  assert(document.querySelectorAll('#iplist tbody tr').length==3,'retired rows must be hidden by default: '+document.querySelectorAll('#iplist tbody tr').length);
  assert($('#iplist a[href="https://study.hrmsmrflrii.xyz"]'),'Traefik URL not linked');
  assert($('#iplist').textContent.includes('recorded by builder')&&$('#iplist').textContent.includes('AI suggestion'),'purpose source missing');
  assert(!$('#iplist img')&&$('#iplist').textContent.includes('mystery <img src=x>'),'service text must be escaped');
  assert($('#ipsum').textContent.includes('Traefik: timeout'),'source errors must be shown');
  $('#ipvlans [data-vl="20"]').click();assert(document.querySelectorAll('#iplist tbody tr').length==1,'VLAN filter');
  $('#ipvlans [data-vl="all"]').click();$('#ipsum [data-st="undocumented"]').click();assert(document.querySelectorAll('#iplist tbody tr').length==1&&$('#iplist').textContent.includes('192.168.40.41'),'status filter');
  $('#ipsum [data-st=""]').click();const q=$('#ipq');q.value='notebook';q.dispatchEvent(new Event('input',{bubbles:true}));
  assert(document.querySelectorAll('#iplist tbody tr').length==1&&$('#iplist').textContent.includes('192.168.40.38'),'search');
  q.value='';q.dispatchEvent(new Event('input',{bubbles:true}));
  $('#iprefresh').click();await pause();await pause();assert(F.ipcalls.some(u=>u.includes('refresh=1')),'refresh must re-read sources');
  assert(document.documentElement.scrollWidth<=innerWidth+1,'ip map page scrolls horizontally');
  }finally{location.hash='voice';route();await pause()}
 });
 if(!DESK)await test('Team wall figures stand on a transparent background with their cut-out poster',async()=>{
  const sAG=AG;AG=[{id:'wall-test',name:'Wall',emoji:'*',role:'Test',group:'agents',is_lead:true,color:0xc9a227,avatar:'/avatars/w.jpg',
   anim:{idle:'/avatars/w-idle.webm',poster:'/avatars/w-anim.png'}}];
  try{wallShow(-1,'leads');const t=document.querySelector('#wring .wt'),cs=getComputedStyle(t),v=t.querySelector('video');
   assert(cs.backgroundColor=='rgba(0, 0, 0, 0)'&&cs.borderTopWidth=='0px','team wall card still has a box: '+cs.backgroundColor+' '+cs.borderTopWidth);
   assert(getComputedStyle(v).backgroundColor=='rgba(0, 0, 0, 0)'&&v.getAttribute('poster')=='/avatars/w-anim.png','wall video not transparent or poster is the photo: '+v.getAttribute('poster'))}
  finally{wallHide();AG=sAG}
 });
 await test('Closing while permission is pending discards late input',async()=>{
  let resolve;F.wait=new Promise(r=>resolve=r);audioDialog.showModal();const pending=vmictest();
  $('#vaudioclose').click();await pause();resolve();await pending;
  assert(!MIC.testing&&!V.stream&&F.streams.at(-1).getAudioTracks()[0].readyState=='ended','late capture leaked');
 });
 await test('Live selection replaces the track without restarting the call',async()=>{
  await vmic();const old=V.stream.getAudioTracks()[0],meter=V.meter,stop=meter.stop;let stopped=false;
  meter.stop=()=>{stopped=true;stop()};const pc=new RTCPeerConnection();pc.addTrack(old);V.pc=pc;
  await vmicchange('headset');
  assert(V.pc===pc&&pc.getSenders()[0].track===V.tx&&V.tx.label==V.stream.getAudioTracks()[0].label,'call restarted or sender unchanged');
  assert(old.readyState=='ended'&&stopped,'old input or meter leaked');
  assert(F.api.filter(u=>u=='/api/voice/session').length==0,'switch started another session');
 });
 for(const mode of ['muted','speakers','headphones']){
  await test('Live selection preserves '+mode+' audio gating',async()=>{
   await vmic();V.pc=new RTCPeerConnection();V.pc.addTrack(V.stream.getAudioTracks()[0]);
   V.muted=mode=='muted';V.speaking=['speakers','headphones'].includes(mode);V.duplex=mode=='headphones';
   await vmicchange('headset');assert(V.tx.enabled==(mode=='headphones'),'audio gate lost on replacement');
   assert(V.stream.getAudioTracks()[0].enabled==(mode!='muted'),'local barge-in detector closed or mute ignored');
  });
 }
 await test('Unplugging a selected mic falls back during a call',async()=>{
  vmicremember('headset');await vmic();const old=V.stream.getAudioTracks()[0];V.pc=new RTCPeerConnection();V.pc.addTrack(old);
  F.devices=initial.filter(d=>d.deviceId!='headset');await vmicdeviceschanged();
  assert(MIC.selected==''&&V.stream.getAudioTracks()[0].label=='Built-in microphone','unplug fallback failed');
  assert(old.readyState=='ended','unplugged stream retained');
 });
 await test('Default input follows a reported OS default change',async()=>{
  await vmic();V.pc=new RTCPeerConnection();V.pc.addTrack(V.stream.getAudioTracks()[0]);
  F.devices=initial.map(d=>d.deviceId=='default'?{...d,groupId:'headset'}:d);await vmicdeviceschanged();
  assert(MIC.selected==''&&V.stream.getAudioTracks()[0].label=='Bluetooth headset','default change ignored');
 });
 await test('Failed replacement retains the working input and selection',async()=>{
  await vmic();const old=V.stream;V.pc=new RTCPeerConnection();V.pc.addTrack(old.getAudioTracks()[0]);
  F.fail=error('NotReadableError');await vmicchange('headset');
  assert(V.stream===old&&old.getAudioTracks()[0].readyState=='live'&&MIC.selected=='','working stream lost');
  assert($('#vmicnote').className=='error','switch error not surfaced');
 });
 await test('Enumeration errors are explicit and do not switch inputs',async()=>{
  await vmic();const old=V.stream;F.enumError=error('NotAllowedError');await vmicdeviceschanged();
  assert(V.stream===old&&$('#vmicnote').textContent.includes('Could not list'),'enumeration error hidden');
 });
 await test('Hangup during replacement releases the late new track',async()=>{
  await vmic();let resolve,entered;const replacing=new Promise(r=>entered=r);
  V.pc={getSenders:()=>[{track:V.stream.getAudioTracks()[0],replaceTrack:()=>{entered();return new Promise(r=>resolve=r)}}]};
  const pending=vmicchange('headset');await replacing;vmicrelease();V.pc=null;resolve();await pending;
  assert(!V.stream&&F.streams.at(-1).getAudioTracks()[0].readyState=='ended','replacement leaked after hangup');
 });
 for(const failure of ['sessionError','sessionNetwork']){
  await test(failure+' cleans up a failed call',async()=>{
   F[failure]=true;await vstart('');
   assert(!V.stream&&!V.actx&&!V.pc&&!V.starting,'failed connection leaked mic or connection');
   assert($('#vstatus').textContent.includes('Test'),'connection error not visible');
  });
 }
 await test('Duplicate starts are ignored while a session is opening',async()=>{
  let resolve;F.sessionWait=new Promise(r=>resolve=r);const pending=vstart('');
  while(!F.api.includes('/api/voice/session'))await pause();
  await vstart('');assert(F.api.filter(u=>u=='/api/voice/session').length==1,'duplicate paid session');
  resolve();await pending;assert(V.pc&&!V.starting,'connection did not complete');vstop('test complete');
 });
 await test('Handovers retain the selected input',async()=>{
  vmicremember('headset');await vstart('');const stream=V.stream,count=F.requests.length;
  await vhandover('watcher','synthetic handover');
  assert(V.stream===stream&&F.requests.length==count,'handover reopened microphone');vstop('test complete');
 });
 await test('Picker search, groups and keyboard-native buttons',async()=>{
  VG='agents';$('#vsearch').value='metrics';vpickRender();
  const buttons=document.querySelectorAll('#vpick button');assert(buttons.length==1&&buttons[0].dataset.id=='watcher','skill search failed');
  buttons[0].click();assert(buttons[0].getAttribute('aria-pressed')=='true','selected state missing');
  $('#vsearch').value='no match';vpickRender();assert($('#vpick .empty'),'empty state missing');
  vgrp('agents');assert($('#vsearch').value==''&&
   [...document.querySelectorAll('#vpick button')].map(b=>b.dataset.id).join(',')=='boss,watcher','group change failed');
 });
 await test('Dependency map is reachable alongside hierarchy and defaults to operational edges',async()=>{
  location.hash='map';route();await pause();await loadMap();
  assert(!$('#v-map').hidden&&$('#v-hierarchy').hidden,'dependency view not reachable');
  assert($('#map svg')&&document.querySelectorAll('#map .node').length==3,'diagram not drawn');
  assert(G.edges.length==1&&G.edges[0].kind=='data','reporting replaced operational dependencies');
  assert(document.querySelector('nav [data-v="map"]').title.includes('Dependency'),'dependency navigation unclear');
  $('#map-reporting').checked=true;$('#map-reporting').dispatchEvent(new Event('change'));
  assert(G.edges.length==2,'optional reporting overlay missing');
  location.hash='voice';route();await pause();
 });
 await test('Dependency API errors are visible and refresh restores the diagram',async()=>{
  F.graphError=true;await loadMap();assert(!$('#map-error').hidden&&!$('#map svg'),'failed map silently blank');
  F.graphError=false;await loadMap();assert($('#map-error').hidden&&$('#map svg'),'refresh did not recover');
 });
 await test('Malformed dependency positions fail visibly instead of an empty diagram',async()=>{
  F.graphData={nodes:[{id:'bad',type:'agent'}],edges:[]};await loadMap();
  assert(!$('#map-error').hidden&&$('#map-error').textContent.includes('positions'),'invalid graph hidden');
 });
 await test('Explicit full-body SVG avatars work with existing companion controls',async()=>{
  const a={id:'test',name:'Original character',avatar:'/avatars/test-persona.svg',avatar_full:'/avatars/test-persona-full.svg'};
  B.full=true;B.pip=null;bLook(a);
  assert(B.img.getAttribute('src')==a.avatar_full,'full-body SVG not selected');
  B.img.onerror();assert(B.img.getAttribute('src')==a.avatar&&B.fullOk===false,'portrait fallback lost');
 });
 await test('Microphone settings fit the current viewport and companion',async()=>{
  vmicsettings();await pause();const rect=audioDialog.getBoundingClientRect();
  assert(audioDialog.open&&rect.width<=innerWidth&&rect.left>=0&&rect.right<=innerWidth,'dialog clipped');
  assert(getComputedStyle($('#vmicsel')).display!='none','input selector hidden');
  assert(audioDialog.scrollWidth<=audioDialog.clientWidth,'audio settings overflow');
  if(DESK)assert(deskHit(0,0)=='ui','companion audio settings remain click-through');
  audioDialog.close();
 });
 await test('Page has no horizontal overflow',async()=>{
  assert(document.documentElement.scrollWidth<=innerWidth,'page overflows viewport');
 });
 await test('Hierarchy navigation and deep link select a dedicated page',async()=>withHierarchy(async()=>{
  assert(VIEW=='hierarchy'&&!$('#v-hierarchy').hidden&&$('#v-voice').hidden,'hierarchy route missing');
  assert($('nav button[data-v="hierarchy"]').classList.contains('on'),'navigation not selected');
  assert($('#hierarchy-title').textContent=='Reporting hierarchy','page title missing');
  assert(!F.api.includes('/api/voice/session')&&F.requests.length==0,'hierarchy started a call or microphone');
  location.hash='voice';await pause();$('nav button[data-v="hierarchy"]').click();await pause();
  assert(VIEW=='hierarchy','sidebar link does not open hierarchy');
 }));
 await test('Hierarchy renders every homelab agent under the four leads',async()=>withHierarchy(async()=>{
  const items=[...document.querySelectorAll('#hierarchy-tree .hitem')];
  assert(items.length==F.roster.length&&new Set(items.map(n=>n.dataset.agent)).size==F.roster.length,'missing or duplicate agent');
  const leads=[...document.querySelectorAll('#hierarchy-tree .hlead')].map(n=>n.closest('.hitem').dataset.agent).sort();
  assert(leads.join(',')=='boss,governance,medic,security','wrong lead roster');
  assert($('#hierarchy-count').textContent.includes(F.roster.length+' agents · 4 leads'),'counts missing');
  const pm=items.find(n=>n.dataset.agent=='project_manager');
  assert(pm.dataset.parent=='governance'&&!pm.querySelector('.hnode .hlead'),'Morgott is not a PM specialist under Ranni');
 }));
 await test('Every visual reporting edge matches the registry',async()=>withHierarchy(async()=>{
  for(const a of F.roster){
   const item=[...document.querySelectorAll('#hierarchy-tree .hitem')].find(n=>n.dataset.agent==a.id);
   const parent=item.parentElement.closest('.hitem');
   assert(item.dataset.parent==a.reports_to,'wrong declared parent for '+a.id);
   assert((parent?parent.dataset.agent:'hermes')==a.reports_to,'wrong visual parent for '+a.id);
  }
 }));
 await test('Hierarchy branches collapse accessibly and survive refreshes',async()=>withHierarchy(async()=>{
  const branches=[...document.querySelectorAll('#hierarchy-tree details')];
  assert(branches.length>0&&branches.every(d=>d.open&&d.querySelector('summary')),'native branch disclosure missing');
  $('#hierarchy-collapse').click();assert(branches.every(d=>!d.open),'collapse all failed');
  await loadHierarchy();assert(branches.every(d=>!d.open&&d.isConnected),'unchanged refresh rebuilt collapsed branches');
  $('#hierarchy-expand').click();assert(branches.every(d=>d.open),'expand all failed');
  const security=branches.find(d=>d.dataset.agent=='security');security.open=false;
  F.agents=F.agents.map(a=>a.id=='security'?{...a,role:a.role+' updated'}:a);await loadHierarchy();
  assert(![...document.querySelectorAll('#hierarchy-tree details')].find(d=>d.dataset.agent=='security').open,'changed refresh lost collapsed state');
 }));
 await test('Hierarchy profile links and return navigation work',async()=>withHierarchy(async()=>{
  $('#hierarchy-expand').click();
  const link=[...document.querySelectorAll('#hierarchy-tree .hprofile')].find(a=>a.dataset.agent=='iam');
  assert(link.getAttribute('href')=='#agent/iam','profile link not bookmarkable');
  assert(link.tabIndex>=0,'profile link not keyboard accessible');
  if(!DESK){link.focus();assert(document.activeElement===link,'profile link not keyboard focusable')}
  link.click();await pause();
  assert(VIEW=='profile'&&PID=='iam'&&$('#profile').textContent.includes('Rogier'),'profile did not open');
  const back=[...document.querySelectorAll('#profile .back')].find(b=>b.textContent=='Reporting hierarchy');
  assert(back,'profile has no hierarchy return link');back.click();await pause();
  assert(VIEW=='hierarchy'&&!$('#v-hierarchy').hidden,'profile return failed');
 }));
 await test('Hierarchy rejects invalid parents, duplicates and reporting cycles',async()=>withHierarchy(async()=>{
  const invalid=[
   [],
   F.roster.map(a=>a.id=='iam'?{...a,reports_to:'missing'}:a),
   [...F.roster,F.roster[0]],
   F.roster.map(a=>a.id=='boss'?{...a,reports_to:'watcher'}:a)
  ];
  for(const roster of invalid){
   let rejected=false;try{hierarchyTree(roster)}catch(e){rejected=true}
   assert(rejected,'invalid reporting structure accepted');
  }
  F.agents=invalid[1];await loadHierarchy();
  assert(!$('#hierarchy-error').hidden&&$('#hierarchy-error').textContent.includes('reporting parent'),'configuration error hidden');
  assert(!$('#hierarchy-tree').textContent&&$('#hierarchy-count').textContent.includes('unavailable'),'invalid structure shown as success');
  F.agents=F.roster;await loadHierarchy();
  assert($('#hierarchy-error').hidden&&document.querySelectorAll('#hierarchy-tree .hitem').length==F.roster.length,'recovery failed');
 }));
 await test('Hierarchy fetch failure is visible rather than a stale success',async()=>withHierarchy(async()=>{
  const saved=window.fetch;
  try{
   window.fetch=async u=>{if(u=='/api/hierarchy')throw new TypeError('Synthetic roster network failure');return saved(u)};
   await loadHierarchy();
   assert(!$('#hierarchy-error').hidden&&$('#hierarchy-error').textContent.includes('network failure'),'network failure hidden');
   assert(!$('#hierarchy-tree').textContent,'stale hierarchy presented as current');
  }finally{window.fetch=saved;await loadHierarchy()}
 }));
 await test('Hierarchy displays only escaped role metadata, never private activity',async()=>withHierarchy(async()=>{
  F.agents=F.agents.map(a=>a.id=='watcher'?{...a,name:'<b>Sellen & friends</b>',last_summary:'synthetic private confidence',character:'synthetic private character'}:a);
  await loadHierarchy();
  const node=[...document.querySelectorAll('#hierarchy-tree .hitem')].find(n=>n.dataset.agent=='watcher').querySelector('.hnode');
  assert(node.querySelector('.name').textContent=='<b>Sellen & friends</b>'&&!node.querySelector('.name b'),'name not escaped');
  assert(!$('#hierarchy-tree').textContent.includes('synthetic private'),'private activity displayed');
 }));
 await test('Hierarchy fits the viewport with long roles and stacked phone branches',async()=>withHierarchy(async()=>{
  $('#hierarchy-expand').click();
  if(DESK)return;
  assert(document.documentElement.scrollWidth<=innerWidth,'hierarchy causes horizontal page overflow');
  assert($('#hierarchy-tree').scrollWidth<=$('#hierarchy-tree').clientWidth,'hierarchy tree clipped');
  const columns=getComputedStyle($('#hierarchy-tree .hlead-branches')).gridTemplateColumns.split(' ').length;
  assert(columns==(innerWidth<=1000?1:3),'lead branch layout does not adapt to viewport');
  const boss=[...document.querySelectorAll('#hierarchy-tree .hitem')].find(n=>n.dataset.agent=='boss');
  assert(boss.getBoundingClientRect().width>=$('#hierarchy-tree').clientWidth*.95,'single lead trunk narrowed the whole tree');
  document.querySelectorAll('#hierarchy-tree .hlead-branches .hprofile>span:last-child').forEach(label=>{
   assert(label.getBoundingClientRect().width>=110,'nested columns squeezed names into letter stacks');
  });
 }));
 await test('Hierarchy missing portraits fall back to the registry emoji',async()=>withHierarchy(async()=>{
  F.agents=F.agents.map(a=>a.id=='iam'?{...a,avatar:'missing-hierarchy-portrait.jpg'}:a);
  await loadHierarchy();
  const item=[...document.querySelectorAll('#hierarchy-tree .hitem')].find(n=>n.dataset.agent=='iam');
  const img=item.querySelector('img');if(img)img.onerror();await pause();
  assert(!item.querySelector('img')&&item.querySelector('.hportrait').textContent=='🔑','missing portrait fallback failed');
 }));
 await test('Page-only rollout labels the requested hierarchy without activating agents',async()=>withHierarchy(async()=>{
  F.hierarchyRoster=F.roster;F.hierarchyStatus='pending';
  F.agents=F.agents.filter(a=>!['governance','project_manager','security_engineer','iam','compliance','cost','ipam','devops'].includes(a.id));
  F.agents.push({id:'retired',name:'Retired',group:'agents'});await loadHierarchy();
  assert(!$('#hierarchy-stage').hidden&&$('#hierarchy-stage').textContent.includes('activation pending'),'staging notice missing');
  assert($('#hierarchy-count').textContent.includes('requested roster'),'requested roster presented as activated');
  const pm=[...document.querySelectorAll('#hierarchy-tree .hitem')].find(n=>n.dataset.agent=='project_manager');
  assert(pm.querySelector('.hnode .hpending')&&!pm.querySelector('.hnode').querySelector('a.hprofile'),'inactive Morgott links to nonexistent runtime profile');
  assert([...document.querySelectorAll('#hierarchy-tree a.hprofile')].some(a=>a.dataset.agent=='boss'),'existing runtime profile link lost');
  assert(AG.some(a=>a.id=='retired')&&!AG.some(a=>a.id=='project_manager'),'requested roster replaced live voice/profile registry');
  assert(!document.querySelector('#hierarchy-tree [data-agent="retired"]'),'retired-in-request agent inserted into requested hierarchy');
 }));
 await test('Avatar depth toggle persists locally and honors reduced motion',async()=>{
  DEPTH.enabled=true;bDepthRender();assert(B.el.classList.contains('dimensional')==!DEPTH.motion.matches,'reduced motion ignored');
  $('#bdepthtoggle').click();assert(localStorage.getItem('buddydepth')=='off'&&!B.el.classList.contains('dimensional'),'depth toggle ignored');
  $('#bdepthtoggle').click();assert(localStorage.getItem('buddydepth')=='on','depth preference not restored');
  const motion=DEPTH.motion;DEPTH.motion={matches:true};bDepthRender();B.el.style.setProperty('--depth-y','2deg');
  bDepthPointer(10000,10000);
  assert(!B.el.classList.contains('dimensional')&&getComputedStyle(B.el.querySelector('.bdepth')).transform=='none','reduced motion retained depth');
  assert(B.el.style.getPropertyValue('--depth-y')=='2deg','reduced motion accepted pointer updates');DEPTH.motion=motion;bDepthRender();
 });
 await test('Parallax is bounded and never changes audio state',async()=>{
  const saved=DEPTH.motion;DEPTH.motion={matches:false};DEPTH.enabled=true;B.el.hidden=false;
  const bounds=B.img.getBoundingClientRect;B.img.getBoundingClientRect=()=>({left:0,top:0,width:100,height:100});
  bDepthPointer(10000,-10000);
  assert(B.el.style.getPropertyValue('--depth-y')=='5.00deg'&&B.el.style.getPropertyValue('--depth-x')=='3.00deg','parallax was unbounded');
  assert(!V.pc&&!V.stream,'parallax started audio');B.img.getBoundingClientRect=bounds;DEPTH.motion=saved;
 });
 await test('Character preview does not request a mic or paid session',async()=>{
  V.agent='boss';$('#vpreview').click();
  assert(!B.el.hidden&&B.agent=='boss','preview missing');
  assert(F.requests.length==0&&!F.api.includes('/api/voice/session'),'preview opened microphone or paid session');
 });
 await test('Preview cannot reset an active or connecting call',async()=>{
  bState('talking');V.pc=new RTCPeerConnection();vbtns();$('#vpreview').click();
  assert($('#vpreview').disabled&&B.state=='talking','preview reset active call');
  V.pc=null;V.starting=true;vbtns();$('#vpreview').onclick();
  assert($('#vpreview').disabled&&B.state=='talking','preview reset connecting call');
 });
 await test('Missing full-body art keeps the circular portrait fallback',async()=>{
  B.full=true;bLook({...ag('boss'),avatar:'missing.jpg'});B.img.onerror();
  assert(B.fullOk==false&&!B.el.classList.contains('full'),'portrait fallback broken');
 });
 await test('Depth lighting and state glow react without changing audio',async()=>{
  const preference=DEPTH.motion;DEPTH.motion={matches:false};DEPTH.enabled=true;bDepthRender();B.fullOk=true;bMode();
  B.el.style.setProperty('--depth-light','.05');bState('listening');
  const listening=getComputedStyle(B.el.querySelector('.bdepth')).filter;
  B.el.style.setProperty('--amp','.8');bState('talking');
  const talking=getComputedStyle(B.el.querySelector('.bdepth')).filter;
  assert(listening!='none'&&talking!=listening,'state lighting missing');
  assert(getComputedStyle(B.img).filter.includes('brightness'),'pointer lighting missing');DEPTH.motion=preference;bDepthRender();
 });
 await test('Depth wrapper preserves full-body and compact sizing',async()=>{
  B.full=true;B.fullOk=true;B.el.hidden=false;bMode();
  const wrap=B.el.querySelector('.bwrap'),depth=B.el.querySelector('.bdepth');
  assert(Math.abs(wrap.getBoundingClientRect().height-depth.offsetHeight)<2,'full-body depth wrapper resized character');
  B.full=false;bMode();assert(wrap.offsetHeight==118&&depth.offsetHeight==118,'compact character resized');
 });
 await test('Picture-in-Picture preserves depth and returns the character',async()=>{
  const original=Object.getOwnPropertyDescriptor(window,'documentPictureInPicture'),frame=document.createElement('iframe');
  document.body.appendChild(frame);const w=frame.contentWindow;
  Object.defineProperty(window,'documentPictureInPicture',{configurable:true,value:{requestWindow:async()=>w}});
  try{
   B.el.hidden=false;await BP.onclick();assert(B.pip===w&&B.el.ownerDocument===w.document,'pop-out lost character');
   const enabled=DEPTH.enabled;B.el.querySelector('#bdepthtoggle').click();
   assert(DEPTH.enabled!=enabled,'pop-out depth toggle failed');B.el.querySelector('#bdepthtoggle').click();vbtns();
   w.document.dispatchEvent(new w.MouseEvent('pointermove',{clientX:10,clientY:10,bubbles:true}));
   assert(B.el.querySelector('.bdepth'),'pop-out lost depth wrapper');
   w.dispatchEvent(new w.Event('pagehide'));
   assert(!B.pip&&B.el.ownerDocument===document&&!B.el.classList.contains('inpip'),'character did not return from pop-out');
  }finally{
   if(B.pip)w.dispatchEvent(new w.Event('pagehide'));frame.remove();
   if(original)Object.defineProperty(window,'documentPictureInPicture',original);else delete window.documentPictureInPicture;
  }
 });
 await test('Desk pop-ups: Gideon announces new items once and can bring in the owner',async()=>{
  if(!DESK)return;
  const pc=V.pc,vs=window.vstart,desk=window.desk,calls=[],asked=[];let allow=true;
  V.pc=null;MV.on=false;N.q=[];N.cur=null;localStorage.removeItem('deskSeen');
  window.desk={clickThrough(){},hide(){},mirror(){},popup:async e=>{asked.push(e.kind);return allow}};
  window.vstart=async c=>{calls.push([V.agent,c])};
  try{
   F.deskEvents=[{id:'nudge:1',kind:'nudge',agent:'watcher',title:'backlog',at:null},{id:'urgent:a',kind:'urgent',agent:'watcher',title:'Disk <b>95%</b>',at:null}];
   await nPoll();await pause();
   assert(N.cur&&N.cur.id=='urgent:a'&&asked.join()=='urgent','first run must surface only open urgent items');
   assert($('#dnote').classList.contains('on')&&$('#dnote').innerHTML.includes('&lt;b&gt;95%'),'card missing or title not escaped');
   assert(V.agent=='boss','Gideon is not the one popping up');
   const r=$('#dnote').getBoundingClientRect();
   assert(r.width>0&&r.left>=0&&r.right<=innerWidth&&deskHit(r.left+8,r.top+8)=='ui','card clipped or click-through');
   $('#dnote [data-a=call]').click();
   assert(calls.length==1&&calls[0][0]=='watcher'&&calls[0][1].includes('Disk'),'call button did not bring in the owner');
   assert(!$('#dnote').classList.contains('on')&&!N.cur,'card stayed open');
   await nPoll();assert(asked.length==1,'seen item popped again');
   F.deskEvents.push({id:'approval:7',kind:'approval',agent:'boss',title:'Approval #7: patch',at:null});
   V.agent='boss';await nPoll();assert(N.cur&&N.cur.id=='approval:7','new approval not announced');
   $('#dnote [data-a=talk]').click();assert(calls.length==2&&calls[1][0]=='boss'&&calls[1][1].includes('Approval #7'),'talk did not start Gideon with context');
   allow=false;F.deskEvents.push({id:'nudge:9',kind:'nudge',agent:'boss',title:'muted',at:null});
   await nPoll();assert(!N.cur&&asked.at(-1)=='nudge','muted pop-up still shown');
  }finally{window.vstart=vs;window.desk=desk;V.pc=pc;N.q=[];N.cur=null;$('#dnote').classList.remove('on');F.deskEvents=[]}
 });
 await test('Desk summon_team stands the leads (or the entire team) on the desktop instead of a carousel',async()=>{
  if(!DESK)return;
  const fig='data:image/svg+xml,'+encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="300"><rect x="60" y="20" width="80" height="270" fill="red"/></svg>');
  const pc=V.pc,vs=window.vstart,desk=window.desk,team=[],calls=[];V.pc=null;V.agent='watcher';  // not a lead, so clicking a figure switches the call
  AG.forEach(a=>{a.avatar_full=fig});
  window.desk={clickThrough(){},hide(){},mirror(){},popup:async()=>true,team:on=>team.push(on)};window.vstart=async c=>{calls.push([V.agent,c])};
  try{
   const r=wallAction({});assert(r.ok&&/desktop/.test(r.note)&&!/carousel/.test(r.note)&&r.agents==W.list.length,'tool note wrong: '+r.note);
   await new Promise(x=>setTimeout(x,700));  // 500 ms resize fallback before layout
   document.getAnimations().filter(a=>a.animationName=='dtin').forEach(a=>a.finish());  // headless does not advance CSS animations
   const ts=[...document.querySelectorAll('#wring .wt.dt')];
   assert(team.join()=='true'&&W.desk&&document.documentElement.classList.contains('team'),'window not expanded for the team');
   assert(ts.length==W.list.length&&ts.length>0,'not every agent popped out');
   const leads=AG.filter(a=>a.is_lead).map(a=>a.id).sort().join();
   assert(leads&&W.list.map(a=>a.id).sort().join()==leads&&AG.some(a=>!a.is_lead),'team is not exactly the leads: '+W.list.map(a=>a.id));
   assert(getComputedStyle($('#wall')).backgroundColor=='rgba(0, 0, 0, 0)'&&getComputedStyle($('#wring')).transform=='none','still a dark carousel');
   const ar=B.img.getBoundingClientRect();
   for(const t of ts){const q=t.getBoundingClientRect();assert(q.height>100&&q.top>=0&&q.bottom<=innerHeight+1&&q.left>=-q.width/2,'figure off screen: '+t.dataset.id+' '+JSON.stringify(q)+' '+innerWidth+'x'+innerHeight)}
   const first=ts[0].getBoundingClientRect(),cx=first.left+first.width/2,cy=first.top+first.height/2;
   const x=$('#wallx').getBoundingClientRect();assert(x.width<innerWidth/3&&x.top>=0,'dismiss button stretched');
   assert(deskHit(cx,cy)=='team'&&deskTeamAt(cx,cy)==ts[0].dataset.id,'figure not clickable');
   assert(deskHit(first.left+2,first.top+2)!='team','transparent margin is not click-through');
   dispatchEvent(new MouseEvent('click',{clientX:cx,clientY:cy}));
   assert(calls.length==1&&calls[0][0]==ts[0].dataset.id,'click did not start a call with that agent');
   assert(!$('#wall').hidden,'team vanished when one agent was picked');
   wallHide();assert(team.join()=='true,false'&&!W.desk&&!document.documentElement.classList.contains('team'),'window not restored');
   const bossA=ag('boss'),keep=bossA.anim,motion=DEPTH.motion;DEPTH.motion={matches:false};bossA.anim={idle:'/avatars/none-idle.webm',talk:['/avatars/none-talk.webm'],poster:fig};
   wallAction({who:'leads'});await new Promise(x=>setTimeout(x,700));
   const bt=document.querySelector('#wring .wt.dt[data-id="boss"]');
   assert(bt&&bt.querySelector('video[data-k="idle"]')&&bt.querySelector('video[data-k="talk"]')&&bt.querySelector('img').getAttribute('src')==fig,'lead has no animation clips over the poster');
   assert(!bt.classList.contains('anim'),'a clip that cannot play must leave the still figure visible');
   const vs=getComputedStyle(bt.querySelector('video'));assert(vs.backgroundColor=='rgba(0, 0, 0, 0)'&&vs.backgroundImage=='none','animated figure video has an opaque background: '+vs.backgroundColor);
   wallHide();DEPTH.motion={matches:true};wallAction({who:'leads'});await new Promise(x=>setTimeout(x,700));
   assert(!document.querySelector('#wring video'),'reduced motion must keep the still figures');
   wallHide();DEPTH.motion=motion;bossA.anim=keep;
   const homelab=AG.filter(a=>GV(a.group)=='agents').map(a=>a.id).sort().join();
   const r2=wallAction({who:'all'});assert(r2.ok&&/whole team/.test(r2.note)&&W.who=='all'&&W.list.map(a=>a.id).sort().join()==homelab,'entire team not summoned: '+W.list.map(a=>a.id));
   assert(wallPhrase('summon my leads')&&W.who=='leads'&&W.list.every(a=>a.is_lead),'leads phrase did not switch to the leads');
   assert(wallPhrase('bring the entire team')&&W.who=='all'&&W.list.length==homelab.split(',').length,'entire-team phrase did not show everyone');
   wallHide();
  }finally{window.vstart=vs;window.desk=desk;V.pc=pc;if(W.desk)wallHide();V.agent='boss'}
 });
 await test('Desk companion returns to Gideon after a call with another agent ends',async()=>{
  if(!DESK)return;
  V.pc=null;vpick('watcher');assert(V.agent=='watcher','could not pick another agent');
  vstop('test');await new Promise(x=>setTimeout(x,2700));
  assert(V.agent=='boss','Gideon did not come back to the desktop');
 });
 await test('Desk Ctrl+Alt+A brings Gideon in and starts listening, and a second press stops and hides him',async()=>{
  if(!DESK)return;
  const pc=V.pc,vs=window.vstart,vst=window.vstop,desk=window.desk,calls=[],stops=[];let hid=0;V.pc=null;MV.on=false;vpick('watcher');
  window.vstart=async c=>{calls.push(V.agent)};window.vstop=r=>{stops.push(r);V.pc=null};window.desk={clickThrough(){},hide(){hid++},mirror(){},popup:async()=>true,team(){}};
  try{
   deskCmd('home');assert(V.agent=='boss'&&calls.join()=='boss'&&!hid,'Ctrl+Alt+A did not start a call with Gideon: '+calls);
   V.pc={};deskCmd('home');assert(calls.length==1&&stops.length==1&&hid==1,'Ctrl+Alt+A on a live Gideon call did not stop and hide him');
  }finally{window.vstart=vs;window.vstop=vst;window.desk=desk;V.pc=pc;V.agent='boss'}
 });
 await test('Desk roll call: each homelab agent introduces themself in turn, highlighted, then Gideon closes',async()=>{
  if(!DESK)return;
  const WS=window.WebSocket,desk=window.desk,said=[],nap=ms=>new Promise(x=>setTimeout(x,ms));
  window.WebSocket=class{constructor(u){this.url=u;this.readyState=0;setTimeout(()=>{this.readyState=1;this.onopen&&this.onopen()},5)}
   send(m){const d=JSON.parse(m),sp=[...document.querySelectorAll('#wring .wt.speak')];said.push({agent:d.agent,text:d.text,lit:sp.map(t=>t.dataset.id).join()});
    setTimeout(()=>this.onmessage&&this.onmessage({data:JSON.stringify({type:'tts_end',id:d.id})}),10)}
   close(){this.readyState=3}};
  window.desk={clickThrough(){},hide(){},mirror(){},popup:async()=>true,team(){}};
  F.localInfo={enabled:true,url:'ws://127.0.0.1:8765',token:'synthetic'};V.pc=null;V.local=false;V.agent='boss';
  try{
   wallShow();for(let i=0;i<80&&(RC.on||!said.length);i++)await nap(100);
   const team=W.list.map(a=>a.id);
   assert(said.map(x=>x.agent).join()==[...team,'boss'].join(),'wrong speaking order: '+said.map(x=>x.agent));
   assert(said.slice(0,-1).every(x=>x.lit==x.agent),'the speaker was not the only highlighted agent');
   assert(said.slice(0,-1).every(x=>x.text.includes(ag(x.agent).name)&&/your /.test(x.text)),'intro missing name or role');
   assert(said.at(-1).lit==''&&/Gideon/.test(said.at(-1).text)&&!RC.on,'Gideon did not close the roll call');
   assert(!document.querySelector('#wring .wt.speak,#wring .wsay')&&!$('#wring').classList.contains('rc'),'highlight left behind');
   wallHide();said.length=0;wallShow();for(let i=0;i<40&&!said.length;i++)await nap(50);
   wallHide();const n=said.length;await nap(600);
   assert(n>=1&&said.length==n&&!RC.on&&!RC.ws,'dismiss did not stop the roll call');
   const mutes=[],lvws=LV.ws;LV.ws={readyState:1,send:m=>{const d=JSON.parse(m);if(d.type=='mute')mutes.push(d.on)}};
   V.pc={close(){}};V.local=true;V.speaking=false;V.responding=false;said.length=0;
   try{wallShow();for(let i=0;i<80&&(RC.on||!said.length);i++)await nap(100);
    assert(said.length==W.list.length+1&&said.at(-1).agent=='boss','roll call did not run during a local call');
    assert(mutes.join()=='true,false','call mic not muted for the roll call and restored');
   }finally{LV.ws=lvws;V.pc=null;V.local=false;wallHide()}
  }finally{window.WebSocket=WS;window.desk=desk;F.localInfo=null;if(W.desk)wallHide()}
 });
 await reset();for(const stream of F.streams)stream.getTracks().forEach(t=>t.stop());
 for(const context of F.contexts)await context.close();
 results.push({name:'No unexpected script errors or unhandled promises',ok:F.errors.length==0,error:F.errors.join('\n')});
 out.dataset.complete='true';out.textContent=JSON.stringify(results);
})().catch(e=>{const out=document.createElement('pre');out.id='test-results';out.textContent=JSON.stringify([{name:'test harness',ok:false,error:e.stack}]);document.body.appendChild(out)});
"""


def embedded_page():
    module = ast.parse(PORTAL.read_text(encoding="utf-8"))
    return next(
        ast.literal_eval(node.value)
        for node in module.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "PAGE" for t in node.targets)
    )


def browser_bootstrap():
    fields = ("id", "name", "role", "emoji", "avatar", "color", "reports_to", "is_lead", "participates_in_hierarchy", "group")
    roster = [
        {key: agent[key] for key in fields if key in agent}
        for agent in json.loads((SOURCE_ROOT / "agents.json").read_text())["agents"]
    ]
    page_only = os.environ.get("PORTAL_BASELINE_RUNTIME") == "1"
    return BOOTSTRAP.replace("/*HIERARCHY_ROSTER*/[]", json.dumps(roster)).replace(
        "/*FULL_HIERARCHY*/true", "false" if page_only else "true"
    )


class PortalBrowserTests(unittest.TestCase):
    def test_browser_audio_and_layout(self):
        chrome = (
            os.environ.get("CHROME_BIN")
            or shutil.which("chromium")
            or shutil.which("google-chrome")
            or "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        )
        if not Path(chrome).is_file():
            self.skipTest("Chrome/Chromium unavailable; set CHROME_BIN")
        page = embedded_page().replace(
            "<script>\nconst DESK",
            "<script>" + browser_bootstrap() + "</script>\n<script>\nconst DESK",
            1,
        ).replace("</body>", "<script>" + TESTS + "</script></body>", 1)
        for mode, width, height in [
            ("browser", 1366, 900),
            ("mobile", 390, 844),
            ("desk", 780, 860),
        ]:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory(prefix="portal-test-") as tmp:
                root = Path(tmp)
                fixture = root / "portal.html"
                fixture.write_text(page, encoding="utf-8")
                url = fixture.as_uri() + ("?desk=1" if mode == "desk" else "") + "#voice"
                result = subprocess.run(
                    [
                        chrome, "--headless", "--disable-gpu", "--no-first-run",
                        "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream",
                        "--no-default-browser-check", "--autoplay-policy=no-user-gesture-required",
                        "--force-prefers-reduced-motion", "--disable-background-networking",
                        "--user-data-dir=" + str(root / "profile"),
                        f"--window-size={width},{height}", "--virtual-time-budget=15000",
                        "--dump-dom", url,
                    ],
                    capture_output=True, text=True, timeout=90,
                )
                match = re.search(r'<pre id="test-results"([^>]*)>(.*?)</pre>', result.stdout, re.S)
                self.assertIsNotNone(match, "Browser did not finish tests:\n" + result.stderr[-4000:])
                self.assertIn('data-complete="true"', match.group(1), "Tests stalled: " + match.group(1))
                cases = json.loads(html.unescape(match.group(2)))
                failures = [case for case in cases if not case["ok"]]
                print(f"{mode}: {len(cases) - len(failures)}/{len(cases)} browser checks passed")
                self.assertFalse(failures, json.dumps(failures, indent=2))

    def test_native_microphone(self):
        chrome = (
            os.environ.get("CHROME_BIN")
            or shutil.which("chromium")
            or shutil.which("google-chrome")
            or "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        )
        if not Path(chrome).is_file():
            self.skipTest("Chrome/Chromium unavailable; set CHROME_BIN")
        media_mock = re.search(
            r"Object.defineProperty\(navigator,'mediaDevices',.*?\n\}\}\);",
            BOOTSTRAP, re.S,
        ).group(0)
        page = embedded_page().replace(
            "<script>\nconst DESK",
            "<script>" + browser_bootstrap().replace(media_mock, "") + "</script>\n<script>\nconst DESK",
            1,
        ).replace("</body>", "<script>" + NATIVE_TESTS + "</script></body>", 1).encode()
        complete = threading.Event()
        cases = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(page)

            def do_POST(self):
                cases.extend(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.end_headers()
                complete.set()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory(prefix="portal-native-") as tmp:
                process = subprocess.Popen(
                    [
                        chrome, "--headless", "--disable-gpu", "--no-first-run",
                        "--no-default-browser-check", "--disable-background-networking",
                        "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream",
                        "--remote-debugging-port=0", "--user-data-dir=" + tmp,
                        f"http://127.0.0.1:{server.server_port}/#voice",
                    ],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
                try:
                    finished = complete.wait(30)
                finally:
                    process.terminate()
                    try:
                        _, stderr = process.communicate(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        _, stderr = process.communicate()
                self.assertTrue(finished, "Native microphone test timed out:\n" + stderr[-4000:])
                self.assertFalse([case for case in cases if not case["ok"]], json.dumps(cases, indent=2))
                print("native: getUserMedia, exact selection, RTCRtpSender replacement and cleanup passed")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
