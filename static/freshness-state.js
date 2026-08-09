/* Shared Phase 4.53 freshness/computation contract. */
(function(global){
  const STATES = Object.freeze(['ready','computing','partial','stale','failed','unavailable']);
  const MAX_RETRIES = 3;
  const attempts = Object.create(null);
  function age(payload){ const n=Number(payload && (payload.cacheAgeSec ?? payload.ageSeconds)); return Number.isFinite(n)&&n>=0?n:null; }
  function normalize(payload){
    const p=payload||{}; const explicit=String(p.state||p.freshnessState||'').toLowerCase();
    let state=STATES.includes(explicit)?explicit:null;
    if(!state){
      if(p.unavailable || p.available===false) state='unavailable';
      else if(p.error || p.failed) state='failed';
      else if(p.computing) state=(Array.isArray(p.props)&&p.props.length)||(Array.isArray(p.markets)&&p.markets.some(m=>(m.rows||[]).length))?'partial':'computing';
      else if(p.partial) state='partial';
      else if(p.stale || (age(p)!=null && age(p)>3600)) state='stale';
      else state='ready';
    }
    return {state,ageSeconds:age(p),cached:!!p.cached,generatedAt:p.generatedAt||p.updatedAt||null,message:p.message||null};
  }
  function label(info){ return ({ready:'READY',computing:'COMPUTING',partial:'PARTIAL — updating',stale:'STALE — refresh required',failed:'FAILED — retry available',unavailable:'UNAVAILABLE'})[info.state]||'UNKNOWN'; }
  function apply(id, info, suffix){ const el=document.getElementById(id); if(!el)return; const ageText=info.ageSeconds!=null?' · '+Math.round(info.ageSeconds/60)+'m old':''; el.textContent=label(info)+ageText+(suffix?' · '+suffix:''); el.dataset.freshnessState=info.state; }
  function shouldRetry(key){ attempts[key]=(attempts[key]||0)+1; return attempts[key]<=MAX_RETRIES; }
  function reset(key){ delete attempts[key]; }
  global.FreshnessState={STATES,normalize,label,apply,shouldRetry,reset,isComputing:p=>['computing','partial'].includes(normalize(p).state)};
})(window);
