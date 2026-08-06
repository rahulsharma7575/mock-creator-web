const LS_TK = 'mc_token', LS_EM = 'mc_email', LS_TH = 'mc_theme';

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

createApp({
  data(){return{
    authed:!!localStorage.getItem(LS_TK),who:localStorage.getItem(LS_EM)||'',
    loginEmail:'',loginPass:'',loginErr:'',loginBusy:false,clientBusy:false,newKeyHash:'',togglingId:'',
    theme:localStorage.getItem(LS_TH)||'dark',view:'overview',
    nav:[{id:'overview',label:'Overview'},{id:'clients',label:'Clients'},{id:'configs',label:'Configs'},{id:'jobs',label:'Jobs'}],
    clients:[],jobs:[],configs:[],meta:[],models:[],jobsBusy:false,startBusy:false,
    qs:{client:'',count:40,difficulty:'creative+difficult'},jobFilter:'all',
    drawerJob:null,drawerTimer:null,clientModal:false,newClientName:'',newKey:'',clientErr:'',
    confirmMsg:null,confirmFn:null,toasts:[],
    cfgClient:'',cfg:null,cfgLoading:false,cfgSaving:false,cfgSavedAt:'',cfgRaw:false,cfgRawText:'',cfgRawErr:'',
    cfgData:{},showPass:false,valIssues:[],valChecked:false,
  }},
  computed:{
    filteredJobs(){return this.jobFilter==='all'?this.jobs:this.jobs.filter(j=>j.status===this.jobFilter);},
    stats(){
      const now=Date.now(),day=864e5,t=this.jobs.filter(j=>now-new Date(this.fixT(j.created)).getTime()<day).length;
      const run=this.jobs.filter(j=>j.status==='running').length,fail=this.jobs.filter(j=>j.status==='failed'&&now-new Date(this.fixT(j.created)).getTime()<day).length;
      return[
        {k:'clients',v:this.clients.length,c:'var(--amber2)',i:0},{k:'configs',v:this.configs.length,c:'var(--ink)',i:1},
        {k:'jobs · 24h',v:t,c:'var(--ink)',i:2},{k:'running',v:run,c:'var(--amber)',i:3},{k:'failed · 24h',v:fail,c:'var(--red)',i:4},
      ];
    },
    groups(){const g=[...new Set(this.meta.map(m=>m.group))];return ['LLM','Exam','Images','Audio','Push','Advanced','General'].filter(x=>g.includes(x));},
    listeningCount(){const c=this.cfgData.question_count||40;const r=this.cfgData.reading_count||20;return Math.max(0,c-r);},
    costEst(){
      const q=this.cfgData.question_count||40,r=this.cfgData.reading_count||20,img=this.cfgData.image_count||22,list=Math.max(0,q-r);
      const authCostPerQ=0.0005,proofCostPerQ=0.00015,text=(q*0.75)*(authCostPerQ+proofCostPerQ);
      const imgCost=img*0.008,audioCost=list*0.0004;
      return{text:text,images:imgCost,audio:audioCost,total:text+imgCost+audioCost};
    },
    costPct(){const t=this.costEst;const m=t.total||0.0001;return{text:(t.text/m*100).toFixed(1),images:(t.images/m*100).toFixed(1),audio:(t.audio/m*100).toFixed(1)};},
    failedCount(){return this.jobs.filter(j=>j.status==='failed').length;},
    totalCost(){
      let t=0;for(const j of this.jobs){const r=j._report;if(r){t+=(r.llm_cost||0)+(r.img_cost||0);}}return t;
    },
    jobDuration(){
      if(!this.drawerJob)return'—';const c=new Date(this.fixT(this.drawerJob.created)),u=new Date(this.fixT(this.drawerJob.updated));
      if(isNaN(c)||isNaN(u))return'—';const s=(u-c)/1000;if(s<60)return s.toFixed(0)+'s';
      if(s<3600)return Math.floor(s/60)+'m '+Math.round(s%60)+'s';return Math.floor(s/3600)+'h '+Math.round(s%3600/60)+'m';
    },
  },
  watch:{
    view(v){if(v==='jobs')this.loadJobs();if(v==='clients')this.loadClients();},
    drawerJob(j){clearInterval(this.drawerTimer);this.drawerTimer=null;if(j&&(j.status==='queued'||j.status==='running')){this.drawerTimer=setInterval(()=>this.refreshDrawer(),4000);}},
  },
  methods:{
    fixT(s){return String(s||'').replace(' ','T');},
    fmtTime(s){if(!s)return'—';const d=new Date(this.fixT(s));if(isNaN(d))return s;return d.toLocaleString([],{month:'short',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'});},
    short(s){return s?String(s).slice(0,8):'—';},
    sc(st){return st==='running'?'var(--amber)':st==='done'?'var(--green)':st==='failed'?'var(--red)':'var(--mut)';},
    jobCount(id){return this.jobs.filter(j=>j.client===id).length;},
    pretty(o){return o?JSON.stringify(o,null,2):'(no report yet)';},
    toast(msg,type){const id=Date.now()+Math.random();this.toasts.push({id,msg,type:type||'ok'});setTimeout(()=>{this.toasts=this.toasts.filter(t=>t.id!==id);},4200);},
    async copy(txt){try{await navigator.clipboard.writeText(txt);this.toast('copied','ok');}catch(e){this.toast('copy failed','err');}},
    sel(e){e.target.select();},
    toggleTheme(){this.theme=this.theme==='dark'?'light':'dark';localStorage.setItem(LS_TH,this.theme);document.documentElement.setAttribute('data-theme',this.theme);},
    dryIcon(k){const i=DRY_ICONS[k]||'';return i?i+' ':'';},
    dryLabel(k){return DRY_LABELS[k]||k;},
    grpBg(g){const m={LLM:'var(--blue)',Images:'rgba(192,132,252,.12)',Exam:null,Audio:'rgba(34,211,238,.08)',Push:'rgba(245,166,35,.08)',Advanced:null,General:null};return m[g]||'transparent';},
    grpFg(g){const m={LLM:'#0c4a6e',Images:'var(--purple)',Audio:'var(--cyan)',Push:'var(--amber2)'};return m[g]||'var(--ink)';},
    grpDesc(g){const m={LLM:'Which AI models generate and review questions',Images:'Image generation settings for TOPIK-style picture questions',Exam:'Exam structure — question count, difficulty, marks, duration',Audio:'Text-to-speech for listening section audio clips',Push:'Where to send the finished exam (your teacher app)',Advanced:'LLM token limits, timeouts, retry behaviour',General:'Master on/off switch'};return m[g]||'';},
    hintText(f){if(!f||!this.cfgData)return'';const v=this.cfgData[f.field];
      if(f.field==='image_count'){const mn=this.cfgData.image_count_min||18,mx=this.cfgData.image_count_max||26;return v>=mn&&v<=mx?`✓ In range (${mn}–${mx})`:v<mn?`Too low — below minimum ${mn}`:`Too high — above maximum ${mx}`;}
      if(f.field==='reading_count'){const t=this.cfgData.question_count||0;return t?`${t-v} listening questions remaining (Q${v+1}–Q${t})`:'Set total questions first';}
      return'';},
    validateConfig(){this.valIssues=[];const c=this.cfgData;if(!c)return;
      if((c.question_count||0)<(c.reading_count||0))this.valIssues.push({w:'err',msg:`Reading count (${c.reading_count}) cannot exceed total questions (${c.question_count})`});
      if((c.image_count||0)<(c.image_count_min||0))this.valIssues.push({w:'warn',msg:`Image count (${c.image_count}) is below minimum (${c.image_count_min}) — repair pass will add images`});
      if((c.image_count||0)>(c.image_count_max||0))this.valIssues.push({w:'warn',msg:`Image count (${c.image_count}) is above maximum (${c.image_count_max}) — repair pass may strip images`});
      if(!c.push_pb_pass||!c.push_pb_pass.trim())this.valIssues.push({w:'warn',msg:'Push password is empty — the full pipeline will fail at the push step. Set it here or use MOCK_PB_PASS env var.'});
      if(!c.push_pb_base)this.valIssues.push({w:'err',msg:'Push base URL is empty — no exam can be delivered.'});
      if((c.marks_per_question||1)<0)this.valIssues.push({w:'err',msg:'Marks per question cannot be negative.'});
      if((c.timeout_s||600)<20)this.valIssues.push({w:'warn',msg:`LLM timeout (${c.timeout_s}s) is very low — may cause timeouts.`});
      this.valChecked=true;if(!this.valIssues.length)this.toast('All checks passed','ok');
    },
    async login(){this.loginErr='';this.loginBusy=true;
      try{const r=await fetch('/api/collections/_superusers/auth-with-password',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({identity:this.loginEmail,password:this.loginPass})});const b=await r.json();if(!r.ok)throw new Error(b.message||'auth failed');localStorage.setItem(LS_TK,b.token);localStorage.setItem(LS_EM,b.record.email||this.loginEmail);this.who=localStorage.getItem(LS_EM);this.authed=true;try{await api('/api/creator/ensure');}catch(e){this.toast('ensure: '+e.message,'err');}this.loadAll();}catch(e){this.loginErr=e.message;}this.loginBusy=false;},
    logout(){localStorage.removeItem(LS_TK);localStorage.removeItem(LS_EM);clearInterval(this.drawerTimer);this.drawerTimer=null;this.drawerJob=null;this.authed=false;this.who='';this.view='overview';this.clients=[];this.jobs=[];this.configs=[];this.cfgClient='';this.cfg=null;},
    loadAll(){this.loadClients();this.loadJobs();this.loadConfigs();this.loadMeta();this.loadModels();},
    async loadClients(){try{const r=await api('/api/collections/mock_clients/records?perPage=200&sort=name');this.clients=r.items||[];}catch(e){this.toast('clients: '+e.message,'err');}},
    async loadConfigs(){try{const r=await api('/api/collections/mock_config/records?perPage=200');this.configs=r.items||[];}catch(e){this.toast('configs: '+e.message,'err');}},
    async loadMeta(){try{const r=await api('/api/collections/mock_config_meta/records?perPage=200&sort=group,order');this.meta=r.items||[];}catch(e){}},
    async loadModels(){try{const r=await api('/api/collections/mock_models/records?perPage=200');this.models=r.items||[];}catch(e){}},
    async loadJobs(){this.jobsBusy=true;try{const r=await api('/api/creator/jobs');this.jobs=r.jobs||[];}catch(e){this.toast('jobs: '+e.message,'err');}this.jobsBusy=false;},
    startJob(params){const q=new URLSearchParams(params);return api('/api/creator/start?'+q.toString(),{method:'POST'});},
    async quickStart(kind){this.startBusy=true;try{const params={client:this.qs.client,count:this.qs.count,difficulty:this.qs.difficulty};if(kind&&kind!=='full'){params.kind=kind;params.count=2;}const r=await this.startJob(params);this.toast('job '+r.job_id.slice(0,8)+' queued ('+(r.kind||kind)+')','ok');this.loadJobs();}catch(e){this.toast('start: '+e.message,'err');}this.startBusy=false;},
    async dryRun(kind){if(!this.cfgClient)return this.toast('Select a client first','err');this.startBusy=true;try{const r=await this.startJob({client:this.cfgClient,kind:kind,count:2,difficulty:this.cfgData.difficulty_profile||'creative+difficult'});this.toast('dry run '+kind+' queued (job '+r.job_id.slice(0,8)+')','ok');this.loadJobs();}catch(e){this.toast('dry run: '+e.message,'err');}this.startBusy=false;},
    async dryRunConfig(kind){if(!this.qs.client)return this.toast('Select a client in Quick Start first','err');this.startBusy=true;try{const r=await this.startJob({client:this.qs.client,kind:kind,count:2,difficulty:this.qs.difficulty});this.toast('dry run '+kind+' queued (job '+r.job_id.slice(0,8)+')','ok');this.loadJobs();}catch(e){this.toast('dry run: '+e.message,'err');}this.startBusy=false;},
    async loadConfig(){if(!this.cfgClient){this.cfg=null;return;}this.cfgLoading=true;this.cfgSavedAt='';try{const r=await api('/api/creator/config?client='+encodeURIComponent(this.cfgClient));this.cfg=r;const rest=Object.assign({},r.record);delete rest.id;delete rest.created;delete rest.updated;this.cfgData=rest;this.cfgData.prompts_json=JSON.stringify((r.record&&r.record.prompts_json)||{},null,2);this.cfgRawText=JSON.stringify(r.record,null,2);this.cfgRawErr='';this.valChecked=false;this.valIssues=[];}catch(e){this.toast('config: '+e.message,'err');this.cfg=null;}this.cfgLoading=false;},
    metaByGroup(g){return this.meta.filter(m=>m.group===g);},
    validateRaw(){try{JSON.parse(this.cfgRawText);this.cfgRawErr='';this.toast('JSON valid','ok');}catch(e){this.cfgRawErr='Invalid JSON: '+e.message;}},
    async saveConfig(){this.cfgSaving=true;try{let body;if(this.cfgRaw){body=JSON.parse(this.cfgRawText);}else{const metaByField={};this.meta.forEach(m=>{metaByField[m.field]=m;});body={};for(const k of Object.keys(this.cfgData)){const m=metaByField[k];const v=this.cfgData[k];if(k==='prompts_json'){try{body[k]=JSON.parse(v||'{}');}catch(e){throw new Error('prompts_json is not valid JSON');}}else if(m&&m.ftype==='number')body[k]=(v===''||v===null||v===undefined)?null:Number(v);else if(m&&m.ftype==='bool')body[k]=!!v;else body[k]=v;}}await api('/api/collections/mock_config/records/'+this.cfg.config,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});this.toast('config saved','ok');await this.loadConfig();this.cfgSavedAt=new Date().toISOString();}catch(e){this.toast('save: '+e.message,'err');}this.cfgSaving=false;},
    async openJob(j){try{const r=await api('/api/creator/jobs/'+j.id);this.drawerJob=r;if(r.report){j._report=r.report;}}catch(e){this.toast('job: '+e.message,'err');}},
    async refreshDrawer(){if(!this.drawerJob)return;try{const r=await api('/api/creator/jobs/'+this.drawerJob.id);Object.assign(this.drawerJob,r);const j=this.jobs.find(x=>x.id===r.id);if(j)Object.assign(j,{status:r.status,error:r.error,pushed:r.pushed});if(r.status!=='queued'&&r.status!=='running')clearInterval(this.drawerTimer);}catch(e){}},
    retryJob(){const j=this.drawerJob;const k=j.kind||'full';this.confirmMsg='Re-queue '+k+' job for "'+j.client_name+'" ('+j.count+'q)?';this.confirmFn=async()=>{try{await this.startJob({client:j.client,count:j.count,difficulty:j.difficulty||'',kind:k});this.toast('re-queued','ok');this.drawerJob=null;this.loadJobs();}catch(e){this.toast('requeue: '+e.message,'err');}};},
    delJob(){const j=this.drawerJob;this.confirmMsg='Delete job '+j.id.slice(0,8)+' permanently?';this.confirmFn=async()=>{try{await api('/api/collections/mock_jobs/records/'+j.id,{method:'DELETE'});this.toast('job deleted','ok');this.drawerJob=null;this.loadJobs();}catch(e){this.toast('delete: '+e.message,'err');}};},
    confirmDo(){const fn=this.confirmFn;this.confirmMsg=null;this.confirmFn=null;if(fn)fn();},
    openClientModal(){this.clientModal=true;this.newClientName='';this.newKey=genKey();this.newKeyHash='';sha256Hex(this.newKey).then(h=>{this.newKeyHash=h;});this.clientErr='';},
    async createClient(){if(this.clientBusy)return;this.clientErr='';this.clientBusy=true;try{const h=await sha256Hex(this.newKey);await api('/api/collections/mock_clients/records',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:this.newClientName,api_key_hash:h,active:true})});this.clientModal=false;this.toast('client created','ok');this.loadClients();}catch(e){this.clientErr=e.message;}this.clientBusy=false;},
    async toggleClient(c){if(this.togglingId)return;this.togglingId=c.id;try{await api('/api/collections/mock_clients/records/'+c.id,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({active:!c.active})});c.active=!c.active;}catch(e){this.toast('toggle: '+e.message,'err');}this.togglingId='';},
    delClient(c){this.confirmMsg='Delete client "'+c.name+'"? Its config and jobs stay, but the key stops working.';this.confirmFn=async()=>{try{await api('/api/collections/mock_clients/records/'+c.id,{method:'DELETE'});this.toast('client deleted','ok');this.loadClients();}catch(e){this.toast('delete: '+e.message,'err');}};},
    openConfigFor(id){this.view='configs';this.cfgClient=id;this.cfgRaw=false;this.loadConfig();},
    stageFor(j){
      if(!j||(j.status!=='running'&&j.status!=='failed'))return'';
      const log=j.log||'';const stages=['author','repair','proofread','save','pb','audio','images'];
      const active=stages.filter(s=>log.includes('['+s+']'));const last=active[active.length-1];
      return stages.map(s=>`<span class="stage-dot ${s===last?'on':''}" style="color:${s==='author'?'var(--blue)':s==='images'?'var(--purple)':s==='audio'?'var(--cyan)':s==='pb'?'var(--amber2)':'var(--mut)'}" title="${s}"></span>`).join('');
    },
    async copyConfig(){const o={...this.cfgData};if(typeof o.prompts_json==='string')try{o.prompts_json=JSON.parse(o.prompts_json)}catch(e){}try{await navigator.clipboard.writeText(JSON.stringify(o,null,2));this.toast('config copied to clipboard','ok');}catch(e){this.toast('copy failed','err');}},
    cloneConfigDialog(){
      const targets=this.clients.filter(c=>c.id!==this.cfgClient);if(!targets.length)return this.toast('No other clients to clone to','err');
      const names=targets.map(c=>c.name).join(', ');this.confirmMsg='Clone config to: '+names+'? (overwrites their current config)';
      this.confirmFn=async()=>{try{for(const c of targets){const r=await api('/api/creator/config?client='+c.id);const body={...this.cfgData};delete body.client;if(typeof body.prompts_json==='string')body.prompts_json=JSON.parse(body.prompts_json);await api('/api/collections/mock_config/records/'+r.config,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});}this.toast('config cloned to '+targets.length+' clients','ok');}catch(e){this.toast('clone: '+e.message,'err');}};
    },
    togglePass(){this.showPass=!this.showPass;},
    async clearFailed(){
      const failed=this.jobs.filter(j=>j.status==='failed');if(!failed.length)return;
      this.confirmMsg='Delete '+failed.length+' failed jobs? (cannot undo)';
      this.confirmFn=async()=>{try{let d=0;for(const j of failed){try{await api('/api/collections/mock_jobs/records/'+j.id,{method:'DELETE'});d++;}catch(e){}}this.toast(d+' of '+failed.length+' deleted','ok');this.loadJobs();}catch(e){this.toast('clear: '+e.message,'err');}};
    },
  },
  mounted(){document.documentElement.setAttribute('data-theme',this.theme);if(this.authed)this.loadAll();this._onKey=e=>{const t=document.activeElement.tagName;if(e.key==='Escape'&&t!=='INPUT'&&t!=='TEXTAREA'&&t!=='SELECT'){this.drawerJob=null;this.clientModal=false;this.confirmMsg=null;}if(e.key==='r'&&!e.ctrlKey&&!e.metaKey&&document.activeElement===document.body)this.loadAll();};document.addEventListener('keydown',this._onKey);},
  unmounted(){document.removeEventListener('keydown',this._onKey);},
  updated(){const el=this.$refs.logBox;if(el)el.scrollTop=el.scrollHeight;},
}).mount('#app');
