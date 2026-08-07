const { createApp } = Vue;
const LS_TK = 'mc_token', LS_EM = 'mc_email', LS_TH = 'mc_theme', LS_OR = 'mc_ormodels';

function api(path, opts) {
  opts = opts || {};
  const headers = opts.headers || {};
  const t = localStorage.getItem(LS_TK);
  if (t) headers['Authorization'] = 'Bearer ' + t;
  return fetch(path, Object.assign({}, opts, { headers })).then(async r => {
    let body = null;
    try { body = await r.json(); } catch (e) {}
    if (!r.ok) { const e = new Error((body && (body.message || body.error)) || ('HTTP ' + r.status)); e.status = r.status; throw e; }
    return body;
  });
}

const SHA256_K = [0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x6b1d2acf,0x76f988da,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2];
function sha256HexJS(s) {
  const t = new TextEncoder().encode(s), l = t.length;
  const n = 64 * Math.ceil((l + 9) / 64), buf = new Uint8Array(n), dv = new DataView(buf.buffer);
  buf.set(t); buf[l] = 0x80;
  const bits = l * 8;
  dv.setUint32(n - 8, Math.floor(bits / 0x100000000), false); dv.setUint32(n - 4, bits >>> 0, false);
  let h0 = 0x6a09e667, h1 = 0xbb67ae85, h2 = 0x3c6ef372, h3 = 0xa54ff53a, h4 = 0x510e527f, h5 = 0x9b05688c, h6 = 0x1f83d9ab, h7 = 0x5be0cd19;
  const w = new Uint32Array(64), rotr = (x, r) => (x >>> r) | (x << (32 - r));
  for (let i = 0; i < n; i += 64) {
    for (let j = 0; j < 16; j++) w[j] = dv.getUint32(i + j * 4, false);
    for (let j = 16; j < 64; j++) { const s0 = rotr(w[j-15],7)^rotr(w[j-15],18)^(w[j-15]>>>3); const s1 = rotr(w[j-2],17)^rotr(w[j-2],19)^(w[j-2]>>>10); w[j]=(w[j-16]+s0+w[j-7]+s1)>>>0; }
    let a=h0,b=h1,c=h2,d=h3,e=h4,f=h5,g=h6,h=h7;
    for (let j=0;j<64;j++){const S1=rotr(e,6)^rotr(e,11)^rotr(e,25),ch=(e&f)^(~e&g),t1=(h+S1+ch+SHA256_K[j]+w[j])>>>0,S0=rotr(a,2)^rotr(a,13)^rotr(a,22),maj=(a&b)^(a&c)^(b&c),t2=(S0+maj)>>>0;h=g;g=f;f=e;e=(d+t1)>>>0;d=c;c=b;b=a;a=(t1+t2)>>>0;}
    h0=(h0+a)>>>0;h1=(h1+b)>>>0;h2=(h2+c)>>>0;h3=(h3+d)>>>0;h4=(h4+e)>>>0;h5=(h5+f)>>>0;h6=(h6+g)>>>0;h7=(h7+h)>>>0;
  }
  return [h0,h1,h2,h3,h4,h5,h6,h7].map(x=>x.toString(16).padStart(8,'0')).join('');
}
async function sha256Hex(s){if(crypto&&crypto.subtle){const b=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(s));return Array.from(new Uint8Array(b)).map(x=>x.toString(16).padStart(2,'0')).join('');}return sha256HexJS(s);}
function genKey(){const b=crypto.getRandomValues(new Uint8Array(16));return Array.from(b).map(x=>x.toString(16).padStart(2,'0')).join('');}

const DRY_LABELS = {full:'Full Mock',dry_questions:'Dry Q',dry_images:'Dry Img',dry_audio:'Dry Aud'};
const DRY_ICONS = {dry_questions:'Q',dry_images:'I',dry_audio:'A',full:''};

const ICON_MAP = {
  chart: 'line-chart-line', users: 'group-line', sliders: 'equalizer-line', fileText: 'file-list-3-line',
  plus: 'add-line', key: 'key-2-line', copy: 'file-copy-line', check: 'check-line', x: 'close-line',
  alertTriangle: 'alert-line', info: 'information-line', refresh: 'refresh-line', trash: 'delete-bin-6-line',
  play: 'play-circle-line', zap: 'flashlight-line', image: 'image-line', headphones: 'headphone-line',
  save: 'save-line', undo: 'arrow-go-back-line', sparkles: 'magic-line', cpu: 'cpu-line',
  mic: 'mic-line', upload: 'upload-2-line', download: 'download-2-line', eye: 'eye-line',
  eyeOff: 'eye-off-line', settings: 'settings-3-line', clock: 'time-line', dollar: 'money-dollar-circle-line',
  send: 'send-plane-line', lock: 'lock-line', mail: 'mail-line', clipboard: 'clipboard-line',
  bookOpen: 'book-open-line', activity: 'heart-pulse-line', rocket: 'rocket-2-line',
  checkCircle: 'checkbox-circle-line', circle: 'circle-line', chevronDown: 'arrow-down-s-line',
  arrowRight: 'arrow-right-line', logOut: 'logout-box-r-line', moon: 'moon-line', sun: 'sun-line',
  database: 'database-2-line', terminal: 'terminal-box-line', gauge: 'dashboard-3-line',
  wifi: 'wifi-line', help: 'question-line'
};

const LOG_COLORS = {
  author: 'var(--blue)', repair: 'var(--amber2)', proofread: 'var(--purple)',
  save: 'var(--green)', pb: 'var(--amber2)', audio: 'var(--cyan)', images: 'var(--purple)',
  local: 'var(--green)', 'dry-run': 'var(--blue)', resume: 'var(--mut)'
};
const PIPE_STAGES = ['author', 'repair', 'proofread', 'save', 'pb', 'audio', 'images'];

createApp({
  data(){return{
    authed:!!localStorage.getItem(LS_TK),who:localStorage.getItem(LS_EM)||'',
    loginEmail:'',loginPass:'',loginErr:'',loginBusy:false,clientBusy:false,newKeyHash:'',togglingId:'',
    theme:localStorage.getItem(LS_TH)||'light',view:'overview',
    nav:[{id:'overview',label:'Overview'},{id:'clients',label:'Clients'},{id:'configs',label:'Configs'},{id:'jobs',label:'Jobs'},{id:'fullruns',label:'Full runs'},{id:'dryruns',label:'Dry runs'}],
    clients:[],jobs:[],configs:[],meta:[],models:[],jobsBusy:false,startBusy:false,
    dryruns:[],dryrunsBusy:false,fullruns:[],fullrunsBusy:false,previewRun:null,falStatus:null,orBalance:null,cfgByClient:{},
    qs:{client:'',count:40,difficulty:'creative+difficult'},jobFilter:'all',
    drawerJob:null,drawerTimer:null,drawerLastUpd:'',clientModal:false,clientStep:0,newClientName:'',newKey:'',clientErr:'',
    confirmMsg:null,confirmFn:null,toasts:[],
    cfgClient:'',cfg:null,cfgLoading:false,cfgSaving:false,cfgSavedAt:'',cfgRaw:false,cfgRawText:'',cfgRawErr:'',
    cfgData:{},showPass:false,valIssues:[],valChecked:false,
    groupIssues:{},verified:{},openGroup:'Exam',vmCache:{},dryBusy:null,
    orModels:{},orStatus:'loading',
  }},
  computed:{
    filteredJobs(){return this.jobFilter==='all'?this.jobs:this.jobs.filter(j=>j.status===this.jobFilter);},
    stats(){
      const now=Date.now(),day=864e5,t=this.jobs.filter(j=>now-new Date(this.fixT(j.created)).getTime()<day).length;
      const run=this.jobs.filter(j=>j.status==='running').length,fail=this.jobs.filter(j=>j.status==='failed'&&now-new Date(this.fixT(j.created)).getTime()<day).length;
      return[
        {k:'clients',v:this.clients.length,c:'var(--amber2)',i:0,icon:'users'},
        {k:'configs',v:this.configs.length,c:'var(--ink)',i:1,icon:'sliders'},
        {k:'jobs · 24h',v:t,c:'var(--ink)',i:2,icon:'clock'},
        {k:'running',v:run,c:'var(--amber)',i:3,icon:'activity'},
        {k:'failed · 24h',v:fail,c:'var(--red)',i:4,icon:'alertTriangle'},
      ];
    },
    groups(){const g=[...new Set(this.meta.map(m=>m.group))];return ['LLM','Exam','Images','Audio','Push','Advanced'].filter(x=>g.includes(x));},
    ttsOptions(){const BASE=['fish-audio/s2.1-pro-free:free','microsoft/mai-voice-2-flash','x-ai/grok-voice-tts-1.0','google/gemini-3.1-flash-tts-preview','hexgrad/kokoro-82m','mistralai/voxtral-mini-tts-2603','deepgram/deepgram-multi-voice-tts'];const from=(this.models||[]).filter(m=>m&&m.kind==='tts').map(m=>m.model).filter(Boolean);for(const m of from){if(!BASE.includes(m))BASE.push(m);}const cur=this.cfgData?this.cfgData.tts_model:null;if(cur&&!BASE.includes(cur))BASE.unshift(cur);return BASE;},
    listeningCount(){const c=this.cfgData.question_count||40;const r=this.cfgData.reading_count||20;return Math.max(0,c-r);},
    orCount(){return Object.keys(this.orModels).length;},
    costEst(){
      const q=+this.cfgData.question_count||40,r=+this.cfgData.reading_count||20,img=+this.cfgData.image_count||18,list=Math.max(0,q-r);
      const price=s=>{const m=this.orModels[s];return m?{p:+m.p||0,c:+m.c||0}:null;};
      const stage=(slug,inTok,outTok)=>{const m=price(slug);if(!m)return 0;return inTok*m.p+outTok*m.c;};
      const text=stage(this.cfgData.llm_author_model,3500+q*80,q*150)
                +stage(this.cfgData.llm_proofread_model,2000+q*170,q*150)
                +stage(this.cfgData.llm_repair_model,2400,2000)*2;
      const audio=stage(this.cfgData.tts_model,0,list*90);
      const nano=price('google/gemini-2.5-flash-image');
      const im=String(this.cfgData.image_primary||this.cfgData.img_model||'z-image');
      let images=null,imgNote=null;
      if((im==='nano-banana'||im==='black-forest-labs/flux.2-klein-4b')&&nano)images=img*1320*nano.c;
      else if(im==='z-image')imgNote='Magnific credits';
      else if(im.startsWith('fal-ai/'))imgNote='Fal.ai (512x512)';
      const known=this.orStatus==='live';
      return{text,audio,images,total:text+audio+(images||0),imgNote,known};
    },
    costPct(){const t=this.costEst;const m=t.total||0.0001;return{text:(t.text/m*100).toFixed(1),images:((t.images||0)/m*100).toFixed(1),audio:(t.audio/m*100).toFixed(1)};},
    failedCount(){return this.jobs.filter(j=>j.status==='failed').length;},
    totalCost(){
      let t=0;for(const j of this.jobs){const r=j._report;if(r){t+=(r.llm_cost||0)+(r.img_cost||0);}}return t;
    },
    jobDuration(){
      if(!this.drawerJob)return'—';const c=new Date(this.fixT(this.drawerJob.created)),u=new Date(this.fixT(this.drawerJob.updated));
      if(isNaN(c)||isNaN(u))return'—';const s=(u-c)/1000;if(s<60)return s.toFixed(0)+'s';
      if(s<3600)return Math.floor(s/60)+'m '+Math.round(s%60)+'s';return Math.floor(s/3600)+'h '+Math.round(s%3600/60)+'m';
    },
    previewData(){return this.previewSafe(this.previewRun);},
    pipeIndex(){
      if(!this.drawerJob)return-1;const log=this.drawerJob.log||'';
      let last=-1;PIPE_STAGES.forEach((s,i)=>{if(log.includes('['+s+']'))last=i;});
      if(this.drawerJob.status==='done')return PIPE_STAGES.length;
      return last;
    },
    logLines(){
      if(!this.drawerJob)return[];
      const lines=(this.drawerJob.log||'').split('\n').filter(Boolean);
      return lines.map(l=>{
        const m=l.match(/^\[([a-z-]+)\]/);
        let cls='plain';
        if(m&&LOG_COLORS[m[1]])cls=m[1];
        else if(/FAILED|error|failed/i.test(l))cls='err';
        else if(/OK|COMPLETE|saved|created/i.test(l))cls='ok';
        return{cls,text:l};
      });
    },
  },
  watch:{
    view(v){if(v==='jobs')this.loadJobs();if(v==='clients')this.loadClients();if(v==='dryruns')this.loadDryruns();if(v==='fullruns')this.loadFullruns();this.$nextTick(()=>this.viewEnter());},
    drawerJob(j){clearInterval(this.drawerTimer);this.drawerTimer=null;if(j&&(j.status==='queued'||j.status==='running')){this.drawerTimer=setInterval(()=>this.refreshDrawer(),4000);}},
  },
  methods:{
    fixT(s){return String(s||'').replace(' ','T');},
    fmtTime(s){if(!s)return'—';const d=new Date(this.fixT(s));if(isNaN(d))return s;return d.toLocaleString([],{month:'short',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'});},
    short(s){return s?String(s).slice(0,8):'—';},
    sc(st){return st==='running'?'var(--amber)':st==='done'?'var(--green)':st==='failed'?'var(--red)':'var(--mut)';},
    jobCount(id){return this.jobs.filter(j=>j.client===id).length;},
    jobRep(j){return (j&&j.report)||(j&&j._report)||null;},
    jobTime(j){const r=this.jobRep(j);const st=(r&&r.stage_times)||{};const t=st.total;if(t==null)return'—';const b=Object.entries(st).filter(([k])=>k!=='total').map(([k,v])=>`${k} ${v}s`).join(' · ');return{label:`${t}s`,title:b||'no breakdown'};},
    jobCost(j){const r=this.jobRep(j);const c=(r&&(r.total_llm_cost_usd!=null?r.total_llm_cost_usd:r.llm_cost))||0;return c?`$${c.toFixed(3)}`:'—';},
    jobCredits(j){const r=this.jobRep(j);if(!r)return'—';const c=r.img_credits||0;const n=r.img_count!=null?r.img_count:r.image_count;if(!c&&!n)return'—';return c?`${c}`+(n?` (${n} img × 5)`:'') : '—';},
    pretty(o){return o?JSON.stringify(o,null,2):'(no report yet)';},
    ic(name,size){const body=(window.MC_ICONS||{})[ICON_MAP[name]||'circle-line']||'';return `<svg width="${size||18}" height="${size||18}" viewBox="0 0 24 24" fill="currentColor">${body}</svg>`;},
    toast(msg,type){const titles={ok:'Done',err:'Something went wrong',warn:'Heads up',info:'Note'};const id=Date.now()+Math.random();this.toasts.push({id,msg,type:type||'ok',title:titles[type||'ok']});setTimeout(()=>this.dismissToast(id),4200);this.$nextTick(()=>{if(window.anime){const el=document.querySelector('.toast-card:last-child');if(el)window.anime({targets:el,translateY:[28,0],opacity:[0,1],duration:420,easing:'easeOutCubic'});}});},
    dismissToast(id){this.toasts=this.toasts.filter(t=>t.id!==id);},
    async copy(txt){try{await navigator.clipboard.writeText(txt);this.toast('Copied to clipboard','ok');}catch(e){this.toast('Copy failed','err');}},
    sel(e){e.target.select();},
    toggleTheme(){this.theme=this.theme==='dark'?'light':'dark';localStorage.setItem(LS_TH,this.theme);document.documentElement.setAttribute('data-theme',this.theme);this.$nextTick(()=>this.viewEnter());},
    dryIcon(k){const i=DRY_ICONS[k]||'';return i?i+' ':'';},
    dryLabel(k){return DRY_LABELS[k]||k;},
    grpBg(g){const m={LLM:'var(--blue)',Images:'rgba(138,95,181,.1)',Exam:null,Audio:'rgba(47,143,143,.08)',Push:'rgba(226,114,63,.08)',Advanced:null,General:null};return m[g]||'transparent';},
    grpFg(g){const m={LLM:'#2f6ea5',Images:'var(--purple)',Audio:'var(--cyan)',Push:'var(--amber2)'};return m[g]||'var(--ink)';},
    grpDesc(g){const m={LLM:'Which AI models generate and review questions',Images:'Image questions — one number, applied randomly across the exam',Exam:'Exam structure — question count, difficulty, marks, duration',Audio:'Text-to-speech for listening section audio clips',Push:'Where to send the finished exam — or toggle off to generate locally',Advanced:'LLM token limits, timeouts, retry behaviour',General:'Master on/off switch'};return m[g]||'';},
    grpIcon(g){return{LLM:'cpu',Images:'image',Exam:'bookOpen',Audio:'headphones',Push:'send',Advanced:'settings',General:'sliders'}[g]||'sliders';},
    hintText(f){if(!f||!this.cfgData)return'';
      if(f.field==='image_count'){const n=this.cfgData.image_count||18;return`${n} questions will have pictures, spread randomly across reading & listening`;}
      if(f.field==='image_primary'){const im=String(this.cfgData.image_primary||'');if(im.startsWith('fal-ai/'))return'Runs via Fal.ai (512x512, 1:1) - FAL_KEY must be in the container env';if(im==='z-image')return'Runs via Magnific (5 credits per image)';if(im==='nano-banana'||im==='black-forest-labs/flux.2-klein-4b')return'Runs via OpenRouter (flux.2 klein 4b, billed per image)';return'';}
      if(f.field==='reading_count'){const t=this.cfgData.question_count||0;const v=this.cfgData[f.field]||0;return t?`${t-v} listening questions remaining (Q${v+1}-Q${t})`:'Set total questions first';}
      if(f.field==='sample_rate'){const m=String(this.cfgData.tts_model||'');const need=m.includes('mai-voice')||m.includes('gemini')?'24000':m.includes('fish')||m.includes('grok')?'44100':'';return need?`Selected TTS (${String(m).split('/').pop()}) expects ${need} Hz`:'';}
      if(f.field==='tts_voices'){const n=this.listeningCount;return n?`${n} listening questions - each dialog's speakers are labelled V1..V4; map them here if you want distinct voices`:'Add listening questions first';}
      if(f.field==='llm_author_model'&&this.cfgData[f.field]){const m=this.orModels[this.cfgData[f.field]];return m?`OpenRouter: ${m.name}`:this.orStatus==='live'?`Model not found on OpenRouter`:'';
      }
      return'';},
    modelFieldValue(field){return this.cfgData[field]||'';},
    async verifyModel(slug){
      if(!slug)return{exists:true,status:200,message:''};
      const c=this.vmCache[slug];
      if(c&&Date.now()-c.at<600000)return c;
      try{
        const r=await api('/api/creator/verify-model?slug='+encodeURIComponent(slug));
        const out={exists:r.exists,status:r.status,message:r.message||'',at:Date.now()};
        this.vmCache[slug]=out;return out;
      }catch(e){
        const out={exists:null,status:-1,message:e.status===404?'verification endpoint not deployed - Pull the latest build and redeploy':e.status===401?'session expired - please login again':'verify-model call failed: '+e.message,at:Date.now()};
        this.vmCache[slug]=out;return out;
      }
    },
    async checkIssues(groups){
      const c=this.cfgData;if(!c)return;
      const scope=groups?new Set(groups):null;
      if(scope){scope.forEach(g=>delete this.groupIssues[g]);}else{this.groupIssues={};}
      const add=(g,w,msg)=>{if(scope&&!scope.has(g))return;if(!this.groupIssues[g])this.groupIssues[g]=[];this.groupIssues[g].push({w,msg});};
      if((c.question_count||0)<=0)add('Exam','err','Total questions must be at least 1');
      if((c.question_count||0)<(c.reading_count||0))add('Exam','err',`Reading count (${c.reading_count}) cannot exceed total (${c.question_count})`);
      if((c.marks_per_question||1)<0)add('Exam','err','Marks per question cannot be negative');
      const n=Number(c.image_count)||18;
      if(n<0||n>40)add('Images','err','Image questions must be between 0 and 40');
      if((c.image_count_min||0)>n||n>(c.image_count_max||26))add('Images','warn','Image count is outside the allowed range - it will be auto-adjusted on save');
      const probeGroups={LLM:['llm_author_model','llm_proofread_model','llm_repair_model'],Audio:['tts_model','tts_fallback_model']};
      for(const g in probeGroups){
        if(scope&&!scope.has(g))continue;
        for(const f of probeGroups[g]){
          const v=c[f];
          if(!v)continue;
          const r=await this.verifyModel(v);
          if(r.exists===false)add(g,'err',`Model "${v}" is NOT on OpenRouter (probe said: ${r.message||'not found'})`);
          else if(r.exists===null)add(g,'warn',`Cannot verify "${v}" right now: ${r.message}`);
        }
      }
      if(!scope||scope.has('Images')){
        const im=String(c.image_primary||c.img_model||'');
        if(im==='nano-banana'||im==='black-forest-labs/flux.2-klein-4b'){
          const r=await this.verifyModel(im==='nano-banana'?'google/gemini-2.5-flash-image':'black-forest-labs/flux.2-klein-4b');
          if(r.exists===false)add('Images','err','Image model gemini-2.5-flash-image not found on OpenRouter');
          else if(r.exists===null)add('Images','warn','Cannot verify image model right now: '+(r.message||'unknown'));
        }else if(im==='z-image'){
          add('Images','info','z-image runs via Magnific (credits) - verified at generation time');
        }else if(im.startsWith('fal-ai/')){
          add('Images','info','Fal.ai provider - verified at generation time (FAL_KEY from container env)');
        }else if(im){
          add('Images','warn',`Unknown image provider "${im}" - the run will attempt it and fall back`);
        }
      }
      if(c.push_enabled!==false){
        if(!c.push_pb_base)add('Push','err','Push is on but no target URL is set - add it or toggle push off');
        if(!c.push_pb_pass)add('Push','warn','No push password - exams will be generated locally and not uploaded');
      }
      if((c.timeout_s||600)<20)add('Advanced','warn',`LLM timeout (${c.timeout_s}s) is very low - may cause timeouts`);
      if(!scope){this.valIssues=[];for(const g in this.groupIssues){for(const i of this.groupIssues[g])this.valIssues.push({...i,group:g});}}
    },
    async validateConfig(){await this.checkIssues();this.valChecked=true;Object.keys(this.groupIssues).forEach(g=>this.verified[g]=true);
      const e=this.valIssues.filter(i=>i.w==='err').length,w=this.valIssues.filter(i=>i.w==='warn').length;
      if(!e&&!w)this.toast('All checks passed - every section is healthy','ok');
      else this.toast(`${e} error${e===1?'':'s'}, ${w} warning${w===1?'':'s'} - see the sections`,'warn');},
    async verifyGroup(g){
      await this.checkIssues([g]);this.verified[g]=true;
      const list=this.groupIssues[g]||[];
      if(!list.length)this.toast(`${g} section looks good`,'ok');
      else this.toast(`${g}: ${list.map(i=>i.msg).join(' · ')}`,'warn');},
    async ensureOrModels(force){
      if(this.orStatus==='live'&&!force)return true;
      try{const cache=JSON.parse(localStorage.getItem(LS_OR)||'null');
        if(cache&&cache.at&&Date.now()-cache.at<900000&&Object.keys(cache.models||{}).length){this.orModels=cache.models;this.orStatus='live';return true;}
      }catch(e){}
      try{
        const ctrl=new AbortController();const to=setTimeout(()=>ctrl.abort(),10000);
        const r=await fetch('https://openrouter.ai/api/v1/models',{signal:ctrl.signal});clearTimeout(to);
        const j=await r.json();
        const m={};(j.data||[]).forEach(x=>{m[x.id]={name:x.name||x.id,p:+(x.pricing&&x.pricing.prompt)||0,c:+(x.pricing&&x.pricing.completion)||0};});
        this.orModels=m;this.orStatus='live';
        try{localStorage.setItem(LS_OR,JSON.stringify({at:Date.now(),models:m}));}catch(e){}
        this.toast('Connected to OpenRouter — '+Object.keys(m).length+' models loaded','info');
      }catch(e){this.orStatus='offline';this.toast('OpenRouter unreachable — model checks skipped (offline)','warn');}
      return this.orStatus==='live';
    },
    cardState(g){const list=this.groupIssues[g]||[];if(list.some(i=>i.w==='err'))return'err';if(list.some(i=>i.w==='warn'))return'warn';if(this.verified[g])return'ok';return'idle';},
    groupIssueList(g){return this.groupIssues[g]||[];},
    hiddenField(f){return['image_count_min','image_count_max','is_active','shuffle_questions','shuffle_options','negative_marks','push_subject_id','push_exam_type','image_fallback','tts_voices'].includes(f.field);},
    toggleGroup(g){this.openGroup=this.openGroup===g?'':g;this.$nextTick(()=>this.deckEnter());},
    viewEnter(){
      if(!window.anime)return;
      const els=document.querySelectorAll('main .view-block > *');
      if(!els.length)return;
      window.anime({targets:els,opacity:[0,1],translateY:[18,0],duration:520,delay:window.anime.stagger(60),easing:'easeOutCubic'});
    },
    deckEnter(){
      if(!window.anime)return;
      const els=document.querySelectorAll('.acc-body:not([style*="display: none"]) .acc-field');
      if(!els.length)return;
      window.anime({targets:els,opacity:[0,1],translateY:[10,0],duration:380,delay:window.anime.stagger(35),easing:'easeOutCubic'});
    },
    async login(){this.loginErr='';this.loginBusy=true;
      try{const r=await fetch('/api/collections/_superusers/auth-with-password',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({identity:this.loginEmail,password:this.loginPass})});const text=await r.text();let b;try{b=JSON.parse(text);}catch(e){throw new Error('Server returned unexpected response - is PocketBase running?');}if(!r.ok)throw new Error(b.message||'auth failed');localStorage.setItem(LS_TK,b.token);localStorage.setItem(LS_EM,b.record.email||this.loginEmail);this.who=localStorage.getItem(LS_EM);this.authed=true;try{await api('/api/creator/ensure');}catch(e){this.toast('ensure: '+e.message,'err');}this.loadAll();}catch(e){this.loginErr=e.message;}this.loginBusy=false;},
    logout(){localStorage.removeItem(LS_TK);localStorage.removeItem(LS_EM);clearInterval(this.drawerTimer);this.drawerTimer=null;this.drawerJob=null;this.authed=false;this.who='';this.view='overview';this.clients=[];this.jobs=[];this.configs=[];this.cfgClient='';this.cfg=null;},
    loadAll(){this.loadClients();this.loadJobs();this.loadConfigs();this.loadMeta();this.loadModels();this.loadFalStatus();this.loadOrBalance();this.ensureOrModels();this.$nextTick(()=>this.viewEnter());},
    async loadClients(){try{const r=await api('/api/collections/mock_clients/records?perPage=200&sort=name');this.clients=r.items||[];}catch(e){this.toast('clients: '+e.message,'err');}},
    async loadConfigs(){try{const r=await api('/api/collections/mock_config/records?perPage=200');this.configs=r.items||[];const m={};for(const c of this.configs){m[c.client]=c;}this.cfgByClient=m;}catch(e){this.toast('configs: '+e.message,'err');}},
    async loadMeta(){try{const r=await api('/api/collections/mock_config_meta/records?perPage=200&sort=group,order');this.meta=r.items||[];}catch(e){}},
    async loadModels(){try{const r=await api('/api/collections/mock_models/records?perPage=200');this.models=r.items||[];}catch(e){}},
    async loadDryruns(){this.dryrunsBusy=true;try{const r=await api('/api/collections/dryrun/records?perPage=50&sort=-created');this.dryruns=r.items||[];}catch(e){this.toast('dry runs: '+e.message,'err');}this.dryrunsBusy=false;},
    async loadFullruns(){this.fullrunsBusy=true;try{const r=await api('/api/collections/fullrun/records?perPage=50&sort=-created');this.fullruns=r.items||[];}catch(e){this.toast('full runs: '+e.message,'err');}this.fullrunsBusy=false;},
    async loadFalStatus(){try{const r=await api('/api/creator/fal-status');this.falStatus=r;}catch(e){this.falStatus=null;}},
    async loadOrBalance(){try{const r=await api('/api/creator/or-status');this.orBalance=r;}catch(e){this.orBalance=null;}},
    dryKindLabel(k){return k==='dry_questions'?'Questions':k==='dry_images'?'Images':k==='dry_audio'?'Audio':(k||'—');},
    dryCost(d){const r=(d&&d.report)||{};const c=r.total_llm_cost_usd!=null?r.total_llm_cost_usd:r.llm_cost;return c?`$${Number(c).toFixed(3)}`:'—';},
    dryFalCost(d){const r=(d&&d.report)||{};const c=r.fal_cost||0;const im=String(r.img_model||'');return c&&im.startsWith('fal-ai/')?`$${Number(c).toFixed(4)}`:'—';},
    dryTime(d){const st=((d&&d.report)||{}).stage_times||{};return st.total!=null?`${st.total}s`:'—';},
    openPreview(d){this.previewRun=d;},
    runFiles(d,field){const v=d&&d[field];if(Array.isArray(v))return v.filter(x=>x&&typeof x==='string');if(typeof v==='string'&&v)return[v];return[];},
    parseObj(v){if(v==null)return null;if(typeof v==='object')return v;try{const p=JSON.parse(v);return p&&typeof p==='object'?p:null;}catch(e){return null;}},
    previewSafe(d){
      if(!d||typeof d!=='object')return null;
      let qs=d.questions;
      if(typeof qs==='string'){try{qs=JSON.parse(qs);}catch(e){qs=null;}}
      if(!Array.isArray(qs))qs=[];
      const rep=this.parseObj(d.report)||{};
      const amap=this.parseObj(d.audio_map)||{};
      return{
        id:String(d.id||''),collectionName:String(d.collectionName||'dryrun'),kind:String(d.kind||''),
        count:d.count!=null?d.count:qs.length,created:d.created||'',
        questions:qs.filter(q=>q&&typeof q==='object'),
        report:rep,audio_map:amap,
        images:this.runFiles(d,'images'),audio:this.runFiles(d,'audio'),
        summary:typeof d.summary==='string'?d.summary:''
      };
    },
    pushReadyFor(clientId){
      const cfg=this.cfgByClient[clientId];
      if(!cfg)return false;
      if(cfg.push_enabled===false)return false;
      return !!(cfg.push_pb_pass);
    },
    async loadJobs(){this.jobsBusy=true;try{const r=await api('/api/creator/jobs');this.jobs=r.jobs||[];}catch(e){this.toast('jobs: '+e.message,'err');}this.jobsBusy=false;},
    startJob(params){const q=new URLSearchParams(params);return api('/api/creator/start?'+q.toString(),{method:'POST'});},
    async quickStart(kind){this.startBusy=true;try{
      if(kind==='full'&&!this.pushReadyFor(this.qs.client)){
        this.startBusy=false;
        return this.toast('Cannot create a mock: no Push password set for this client. Set it in Configs > Push first.','err');
      }
      const params={client:this.qs.client,count:this.qs.count,difficulty:this.qs.difficulty};if(kind&&kind!=='full'){params.kind=kind;params.count=3;}const r=await this.startJob(params);this.toast('Job '+r.job_id.slice(0,8)+' queued ('+(r.kind||kind)+')','ok');this.loadJobs();}catch(e){this.toast('start: '+e.message,'err');}this.startBusy=false;},
    async dryRun(kind){await this.runDry(kind,this.cfgClient,this.cfgData&&this.cfgData.difficulty_profile);},
    async dryRunConfig(kind){await this.runDry(kind,this.qs.client,this.qs.difficulty);},
    async runDry(kind,client,difficulty){
      if(this.dryBusy)return;
      if(!client)return this.toast('Choose a client first','err');
      this.dryBusy={kind};
      this.$nextTick(()=>{if(window.anime)window.anime({targets:'.dry-btn.faded',opacity:[0.45,0.6],duration:300,easing:'easeOutCubic'});});
      try{
        const r=await this.startJob({client:client,kind:kind,count:3,difficulty:difficulty||'creative+difficult'});
        const jid=r.job_id;
        this.toast('Dry run '+kind+' queued (job '+String(jid).slice(0,8)+')','info');
        this.loadJobs();
        await this.pollDry(jid);
      }catch(e){
        this.toast('dry run: '+e.message,'err');
        this.dryBusy=null;
      }
    },
    async pollDry(jid){
      for(let i=0;i<150;i++){
        await new Promise(r=>setTimeout(r,4000));
        let j=null;
        try{const r=await api('/api/creator/jobs/'+jid);j=r;}catch(e){continue;}
        if(j&&j.status==='done'){this.dryBusy=null;this.toast('Dry run completed','ok');this.loadJobs();return;}
        if(j&&j.status==='failed'){this.dryBusy=null;this.toast('Dry run failed: '+(j.error||'see the job log'),'err');this.loadJobs();return;}
      }
      this.dryBusy=null;this.toast('Dry run still running - check the Jobs page','info');
    },
    async loadConfig(){if(!this.cfgClient){this.cfg=null;return;}this.cfgLoading=true;this.cfgSavedAt='';try{const r=await api('/api/creator/config?client='+encodeURIComponent(this.cfgClient));this.cfg=r;const rest=Object.assign({},r.record);delete rest.id;delete rest.created;delete rest.updated;this.cfgData=rest;this.cfgData.prompts_json=JSON.stringify((r.record&&r.record.prompts_json)||{},null,2);this.cfgRawText=JSON.stringify(r.record,null,2);this.cfgRawErr='';this.valChecked=false;this.valIssues=[];this.groupIssues={};this.verified={};this.$nextTick(()=>this.deckEnter());}catch(e){this.toast('config: '+e.message,'err');this.cfg=null;}this.cfgLoading=false;},
    metaByGroup(g){return this.meta.filter(m=>m.group===g);},
    validateRaw(){try{JSON.parse(this.cfgRawText);this.cfgRawErr='';this.toast('JSON is valid','ok');}catch(e){this.cfgRawErr='Invalid JSON: '+e.message;}},
    async saveConfig(){this.cfgSaving=true;try{let body;if(this.cfgRaw){body=JSON.parse(this.cfgRawText);}else{
      const n=Number(this.cfgData.image_count)||18;
      this.cfgData.image_count=n;
      this.cfgData.image_count_min=Math.max(0,n-2);
      this.cfgData.image_count_max=Math.min(26,n+2);
      const metaByField={};this.meta.forEach(m=>{metaByField[m.field]=m;});body={};
      for(const k of Object.keys(this.cfgData)){const m=metaByField[k];const v=this.cfgData[k];if(k==='prompts_json'){try{body[k]=JSON.parse(v||'{}');}catch(e){throw new Error('prompts_json is not valid JSON');}}else if(m&&m.ftype==='number')body[k]=(v===''||v===null||v===undefined)?null:Number(v);else if(m&&m.ftype==='bool')body[k]=!!v;else body[k]=v;}
    }await api('/api/collections/mock_config/records/'+this.cfg.config,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});this.toast('Config saved','ok');await this.loadConfig();this.cfgSavedAt=new Date().toISOString();}catch(e){this.toast('save: '+e.message,'err');}this.cfgSaving=false;},
    async openJob(j){try{const r=await api('/api/creator/jobs/'+j.id);this.drawerJob=r;this.drawerLastUpd=Date.now();if(r.report){j._report=r.report;}}catch(e){this.toast('job: '+e.message,'err');}},
    async refreshDrawer(){if(!this.drawerJob)return;try{const r=await api('/api/creator/jobs/'+this.drawerJob.id);Object.assign(this.drawerJob,r);this.drawerLastUpd=Date.now();const j=this.jobs.find(x=>x.id===r.id);if(j)Object.assign(j,{status:r.status,error:r.error,pushed:r.pushed});if(r.status!=='queued'&&r.status!=='running')clearInterval(this.drawerTimer);}catch(e){}},
    retryJob(){const j=this.drawerJob;const k=j.kind||'full';this.confirmMsg='Re-queue '+k+' job for "'+j.client_name+'" ('+j.count+'q)?';this.confirmFn=async()=>{try{await this.startJob({client:j.client,count:j.count,difficulty:j.difficulty||'',kind:k});this.toast('Re-queued','ok');this.drawerJob=null;this.loadJobs();}catch(e){this.toast('requeue: '+e.message,'err');}};},
    delJob(){const j=this.drawerJob;this.confirmMsg='Delete job '+j.id.slice(0,8)+' permanently?';this.confirmFn=async()=>{try{await api('/api/collections/mock_jobs/records/'+j.id,{method:'DELETE'});this.toast('Job deleted','ok');this.drawerJob=null;this.loadJobs();}catch(e){this.toast('delete: '+e.message,'err');}};},
    confirmDo(){const fn=this.confirmFn;this.confirmMsg=null;this.confirmFn=null;if(fn)fn();},
    openClientModal(){this.clientModal=true;this.clientStep=0;this.newClientName='';this.clientErr='';this.newKey=genKey();this.newKeyHash='';sha256Hex(this.newKey).then(h=>{this.newKeyHash=h;});},
    async createClient(){if(this.clientBusy)return;this.clientErr='';this.clientBusy=true;try{const h=await sha256Hex(this.newKey);await api('/api/collections/mock_clients/records',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:this.newClientName,api_key_hash:h,active:true})});this.clientStep=2;this.toast('Client created — the key from the previous step is now active','ok');this.loadClients();}catch(e){this.clientErr=e.message;}this.clientBusy=false;},
    async toggleClient(c){if(this.togglingId)return;this.togglingId=c.id;try{await api('/api/collections/mock_clients/records/'+c.id,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({active:!c.active})});c.active=!c.active;}catch(e){this.toast('toggle: '+e.message,'err');}this.togglingId='';},
    delClient(c){this.confirmMsg='Delete client "'+c.name+'"? Its config and jobs stay, but the key stops working.';this.confirmFn=async()=>{try{await api('/api/collections/mock_clients/records/'+c.id,{method:'DELETE'});this.toast('Client deleted','ok');this.loadClients();}catch(e){this.toast('delete: '+e.message,'err');}};},
    openConfigFor(id){this.view='configs';this.cfgClient=id;this.cfgRaw=false;this.loadConfig();},
    stageFor(j){
      if(!j||(j.status!=='running'&&j.status!=='failed'))return'';
      const log=j.log||'';const stages=['author','repair','proofread','save','pb','audio','images'];
      const active=stages.filter(s=>log.includes('['+s+']'));const last=active[active.length-1];
      return stages.map(s=>`<span class="stage-dot ${s===last?'on':''}" style="color:${s==='author'?'var(--blue)':s==='images'?'var(--purple)':s==='audio'?'var(--cyan)':s==='pb'?'var(--amber2)':'var(--mut)'}" title="${s}"></span>`).join('');
    },
    logCls(c){return c==='err'?'var(--red)':c==='ok'?'var(--green)':LOG_COLORS[c]?LOG_COLORS[c]:'var(--mut)';},
    async copyConfig(){const o={...this.cfgData};if(typeof o.prompts_json==='string')try{o.prompts_json=JSON.parse(o.prompts_json)}catch(e){}try{await navigator.clipboard.writeText(JSON.stringify(o,null,2));this.toast('Config copied to clipboard','ok');}catch(e){this.toast('copy failed','err');}},
    cloneConfigDialog(){
      const targets=this.clients.filter(c=>c.id!==this.cfgClient);if(!targets.length)return this.toast('No other clients to clone to','err');
      const names=targets.map(c=>c.name).join(', ');this.confirmMsg='Clone config to: '+names+'? (overwrites their current config)';
      this.confirmFn=async()=>{try{for(const c of targets){const r=await api('/api/creator/config?client='+c.id);const body={...this.cfgData};delete body.client;if(typeof body.prompts_json==='string')body.prompts_json=JSON.parse(body.prompts_json);await api('/api/collections/mock_config/records/'+r.config,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});}this.toast('Config cloned to '+targets.length+' clients','ok');}catch(e){this.toast('clone: '+e.message,'err');}};
    },
    togglePass(){this.showPass=!this.showPass;},
    async clearFailed(){
      const failed=this.jobs.filter(j=>j.status==='failed');if(!failed.length)return;
      this.confirmMsg='Delete '+failed.length+' failed jobs? (cannot undo)';
      this.confirmFn=async()=>{try{let d=0;for(const j of failed){try{await api('/api/collections/mock_jobs/records/'+j.id,{method:'DELETE'});d++;}catch(e){}}this.toast(d+' of '+failed.length+' deleted','ok');this.loadJobs();}catch(e){this.toast('clear: '+e.message,'err');}};
    },
  },
  mounted(){document.documentElement.setAttribute('data-theme',this.theme);if(this.authed)this.loadAll();this._onKey=e=>{const t=document.activeElement.tagName;if(e.key==='Escape'&&t!=='INPUT'&&t!=='TEXTAREA'&&t!=='SELECT'){this.drawerJob=null;this.clientModal=false;this.confirmMsg=null;this.previewRun=null;}if(e.key==='r'&&!e.ctrlKey&&!e.metaKey&&document.activeElement===document.body)this.loadAll();};document.addEventListener('keydown',this._onKey);
    if(!this._errBound){
      this._errBound=true;
      window.addEventListener('error',ev=>{if(ev&&ev.message&&!String(ev.message).includes('favicon')){try{this.toast('UI error: '+String(ev.message).slice(0,120),'err');}catch(e2){}}});
    }},
  unmounted(){document.removeEventListener('keydown',this._onKey);},
  updated(){const el=this.$refs.logBox;if(el)el.scrollTop=el.scrollHeight;},
}).mount('#app');
