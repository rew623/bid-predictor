'use strict';
/* 낙찰가 예측 도우미 — 1단계 (정적 PWA)
 * 데이터: data/*.json (GitHub Actions 가 매일 갱신). 형식은 CLAUDE.md 참고.
 * 개인 데이터(관심공고)는 WatchStore 한 곳에서만 저장한다 → 2단계에서 Firestore 로 교체.
 */

// ============================================================ 상수
const SIDOS = ['서울','부산','대구','인천','광주','대전','울산','세종','경기','강원','충북','충남','전북','전남','경북','경남','제주'];
const LICENSES = [
  '토목','건축','토목건축','산업환경설비','조경',
  '지반조성·포장','실내건축','금속창호·지붕건축물조성','도장·습식·방수·석공','조경식재·시설물',
  '철근·콘크리트','구조물해체·비계','상하수도설비','보링·그라우팅','철도·궤도','철강구조물','수중·준설','승강기·삭도',
  '전기','정보통신','소방시설','기계설비','가스시설시공'
];
const DEFAULT_FLOOR = 87.745;
const MIN_SAMPLE = 30;
const PAGE_SIZE = 50;
const TAB_TITLES = {bids:'입찰공고', predict:'예측분석', watch:'관심공고', stats:'통계', settings:'설정'};

// ============================================================ 유틸
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmtNum = (n, d=0) => (n==null || !isFinite(n)) ? '-' : Number(n).toLocaleString('ko-KR', {minimumFractionDigits:d, maximumFractionDigits:d});
const won = (n) => (n==null || !isFinite(n)) ? '-' : fmtNum(Math.round(n)) + '원';
const pct = (n, d=3) => (n==null || !isFinite(n)) ? '-' : n.toFixed(d) + '%';
function eok(n){
  if(n==null || !isFinite(n)) return '-';
  if(Math.abs(n) >= 1e8) return (n/1e8).toFixed(2).replace(/\.?0+$/,'') + '억';
  return fmtNum(Math.round(n/1e4)) + '만';
}
function parseKst(s){
  if(!s) return null;
  if(s.includes('T')) return new Date(s);
  const t = s.length > 10 ? s.slice(0,16).replace(' ','T') + ':00+09:00' : s + 'T00:00:00+09:00';
  const d = new Date(t);
  return isNaN(d) ? null : d;
}
function ddayLabel(close){
  const d = parseKst(close);
  if(!d) return {text:'마감 미정', urgent:false};
  const diff = d - Date.now();
  const days = Math.floor(diff / 86400000);
  const hh = close.length > 10 ? close.slice(11,16) : '';
  const md = close.slice(5,10).replace('-','/');
  if(diff < 0) return {text:`마감 ${md}`, urgent:false};
  if(days === 0) return {text:`오늘 ${hh} 마감`, urgent:true};
  return {text:`D-${days} · ${md} ${hh}`, urgent: days <= 2};
}
const monthsAgo = (m) => { const d = new Date(); d.setMonth(d.getMonth() - m); return d.toISOString().slice(0,10); };
function addMonths(dateStr, m){ const d = new Date(dateStr + 'T00:00:00'); d.setMonth(d.getMonth() + m); return d.toISOString().slice(0,10); }
function stats(vals){
  const n = vals.length;
  if(!n) return {n:0, mean:NaN, std:NaN};
  const mean = vals.reduce((a,b)=>a+b,0)/n;
  const std = n > 1 ? Math.sqrt(vals.reduce((a,v)=>a+(v-mean)**2,0)/(n-1)) : 0;
  return {n, mean, std};
}
function quantile(sorted, q){
  if(!sorted.length) return NaN;
  const i = (sorted.length-1)*q, lo = Math.floor(i), hi = Math.ceil(i);
  return sorted[lo] + (sorted[hi]-sorted[lo])*(i-lo);
}
const maxOf = (arr) => arr.reduce((a,b) => b > a ? b : a, -Infinity);
const sampleBadge = (n) => n < MIN_SAMPLE ? ` <span class="badge warn">참고 부족</span>` : '';
const sampleText = (n) => `표본 ${fmtNum(n)}건${sampleBadge(n)}`;

/** 투찰금액 = (예정가격 − A값) × 낙찰하한율 + A값 (원 단위 올림) */
function bidAmount(base, sr, a, floor){
  if(!base || !sr) return null;
  const plan = base * sr / 100;
  a = a || 0; floor = floor || DEFAULT_FLOOR;
  return Math.ceil((plan - a) * floor / 100 + a);
}

const LS = {
  get(k, d){ try{ const v = localStorage.getItem('bp.'+k); return v == null ? d : JSON.parse(v); }catch(e){ return d; } },
  set(k, v){ try{ localStorage.setItem('bp.'+k, JSON.stringify(v)); }catch(e){} },
};

function fillSelect(el, items, {all='전체', value=''}={}){
  el.innerHTML = (all != null ? `<option value="">${esc(all)}</option>` : '') +
    items.map(v => {
      const [val, label] = Array.isArray(v) ? v : [v, v];
      return `<option value="${esc(val)}">${esc(label)}</option>`;
    }).join('');
  el.value = value;
  if(el.value !== value) el.value = el.options[0]?.value ?? '';
}

function downloadCSV(filename, header, rows){
  const q = (v) => { const s = String(v ?? ''); return /[",\n]/.test(s) ? '"' + s.replace(/"/g,'""') + '"' : s; };
  const text = '﻿' + [header, ...rows].map(r => r.map(q).join(',')).join('\n');
  const url = URL.createObjectURL(new Blob([text], {type:'text/csv;charset=utf-8;'}));
  const a = document.createElement('a');
  a.href = url; a.download = filename; document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// ============================================================ 개인 데이터 저장 (2단계에서 Firestore 로 교체)
const WatchStore = {
  async list(){ return LS.get('watch', []); },
  async save(item){
    const list = await this.list();
    const i = list.findIndex(x => x.id === item.id);
    if(i >= 0) list[i] = {...list[i], ...item}; else list.unshift(item);
    LS.set('watch', list);
    return list;
  },
  async remove(id){
    const list = (await this.list()).filter(x => x.id !== id);
    LS.set('watch', list);
    return list;
  },
};

// ============================================================ 데이터 로딩 (선택한 지역 파일만, 메모리 + 서비스워커 캐시)
const Data = {
  meta: null, bids: null, scsbid: {}, opening: {}, _p: {},
  ver(){ return encodeURIComponent(this.meta?.updated_at || '0'); },
  url(path, ver=true){ return 'data/' + path.split('/').map(encodeURIComponent).join('/') + (ver ? '?v=' + this.ver() : ''); },
  async fetchJson(path, ver=true){
    const opt = ver ? {} : {cache:'no-store'};
    const r = await fetch(this.url(path, ver), opt).catch(() => fetch(this.url(path, ver), opt));   // 서비스워커 교체 순간 실패 대비 1회 재시도
    if(!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
    return r.json();
  },
  once(key, fn){
    if(!this._p[key]) this._p[key] = fn().catch(e => { delete this._p[key]; throw e; });
    return this._p[key];
  },
  async loadMeta(){
    try{ this.meta = await this.fetchJson('meta.json', false); }
    catch(e){ this.meta = this.meta || {files:{scsbid:{}, opening:{}}, counts:{}}; }
    this.meta.files = this.meta.files || {scsbid:{}, opening:{}};
    return this.meta;
  },
  loadBids(){
    return this.once('bids', async () => {
      try{ this.bids = (await this.fetchJson('bids.json')).items || []; }
      catch(e){ this.bids = []; }
      return this.bids;
    });
  },
  hasScsbid(sido){ return !!(this.meta.files.scsbid?.[sido]?.length); },
  hasDetail(sido){ return !!Object.keys(this.meta.files.opening?.[sido] || {}).length; },
  loadScsbid(sido){
    return this.once('s:' + sido, async () => {
      const paths = this.meta.files.scsbid?.[sido] || [];
      const parts = await Promise.all(paths.map(p => this.fetchJson(p)));
      const recs = parts.flatMap(p => p.items || []);
      recs.sort((a,b) => (b.date||'').localeCompare(a.date||''));
      const byId = new Map(recs.map(r => [r.id, r]));
      return this.scsbid[sido] = {recs, byId};
    });
  },
  async loadScsbidMany(sidos){
    const out = [];
    for(const s of await Promise.all(sidos.filter(s => this.hasScsbid(s)).map(s => this.loadScsbid(s)))) out.push(...s.recs);
    return out;
  },
  /** 개찰 상세: {bids: Map(id → {date, plan, base, p:[[번호,가격,추첨,횟수]], r:[[순위,업체idx,금액,투찰률,비고]], corps})} */
  loadOpening(sido){
    return this.once('o:' + sido, async () => {
      const years = this.meta.files.opening?.[sido] || {};
      const paths = Object.values(years).flat();
      const parts = await Promise.all(paths.map(p => this.fetchJson(p)));
      const bids = new Map();
      for(const part of parts){
        const corps = part.corps || [];
        for(const [id, b] of Object.entries(part.bids || {})){ b.corps = corps; bids.set(id, b); }
      }
      return this.opening[sido] = {bids};
    });
  },
};

// ============================================================ 예측
function recOrg(r){ return r.org || r.dmd || ''; }

function filterRecords(recs, o){
  const cut = o.recent ? monthsAgo(12) : '';
  return recs.filter(r =>
    r.sr != null &&
    (!o.sggs?.size || o.sggs.has(r.sgg)) &&
    (!o.lics?.size || (r.lic || []).some(l => o.lics.has(l))) &&
    (!cut || (r.date || '') >= cut) &&
    (!o.useBase || !o.base || (r.base && Math.abs(r.base - o.base) <= o.base * 0.4)) &&
    (!o.useCnt || !o.cnt || (r.cnt != null && Math.abs(r.cnt - o.cnt) <= 2))
  );
}

function confidence(n, std){
  if(!n) return {score:0, label:'없음'};
  const nF = Math.min(1, Math.log10(Math.max(n,1)) / 2);         // 100건이면 1
  const sF = Math.max(0, Math.min(1, 1 - (std - 0.4) / 1.6));     // 표준편차 0.4%p 이하면 1
  const score = Math.round(30 + 65 * nF * (0.4 + 0.6 * sF));
  return {score, label: score >= 75 ? '높음' : score >= 55 ? '보통' : '낮음'};
}

function predictFrom(rows){
  const s = stats(rows.map(r => r.sr));
  if(!s.n) return null;
  const k = s.std;
  return {
    ...s,
    conf: confidence(s.n, s.std),
    bands: {
      aggressive: {v: s.mean - 0.3*k, lo: s.mean - 0.5*k, hi: s.mean - 0.15*k},
      recommend: {v: s.mean, lo: s.mean - 0.15*k, hi: s.mean + 0.15*k},
      conservative: {v: s.mean + 0.3*k, lo: s.mean + 0.15*k, hi: s.mean + 0.5*k},
    },
  };
}

/** 공고 목록용 간단 예측: 같은 시도 + 면허 겹침, 최근 24개월. 표본이 적으면 면허 무관으로 넓힌다. */
function quickPredict(notice){
  const store = Data.scsbid[notice.sido];
  if(!store) return null;
  const cut = monthsAgo(24);
  const pool = store.recs.filter(r => r.sr != null && (r.date||'') >= cut);
  let rows = pool;
  let note = '';
  if(notice.lic?.length){
    const lic = new Set(notice.lic);
    const withLic = pool.filter(r => (r.lic||[]).some(l => lic.has(l)));
    if(withLic.length >= 10) rows = withLic; else note = '면허 무관';
  }
  const p = predictFrom(rows);
  if(!p) return null;
  return {sr: p.mean, n: p.n, note, bid: bidAmount(notice.base, p.mean, notice.a, notice.floor)};
}

// ============================================================ 차트 (SVG)
function histogram(values, {min, max, step, lines=[], h=130, color='var(--primary)', label=(x)=>x.toFixed(1)}){
  const W = 360, H = h, bins = Math.max(1, Math.round((max - min) / step));
  const counts = new Array(bins).fill(0);
  values.forEach(v => {
    if(v < min || v > max) return;
    counts[Math.min(bins-1, Math.floor((v - min) / step + 1e-9))]++;
  });
  const maxC = Math.max(...counts, 1), bw = W / bins;
  const X = (x) => (x - min) / (max - min) * W;
  const bars = counts.map((c,i) => {
    const bh = c / maxC * (H - 22);
    return c ? `<rect x="${(i*bw+0.5).toFixed(1)}" y="${(H-bh).toFixed(1)}" width="${Math.max(bw-1,1).toFixed(1)}" height="${bh.toFixed(1)}" fill="${color}" opacity=".85" rx="1.5"><title>${label(min+i*step)}~${label(min+(i+1)*step)}: ${c}건</title></rect>` : '';
  }).join('');
  const ls = lines.filter(l => l.x >= min && l.x <= max).map((l, i) => {
    const x = X(l.x).toFixed(1);
    const anchor = X(l.x) > W*0.8 ? 'end' : X(l.x) < W*0.2 ? 'start' : 'middle';
    return `<line x1="${x}" y1="${12 + i*11}" x2="${x}" y2="${H}" stroke="${l.color}" stroke-width="1.6" stroke-dasharray="3,3"/>
      <text x="${x}" y="${9 + i*11}" font-size="9.5" fill="${l.color}" text-anchor="${anchor}">${esc(l.label)}</text>`;
  }).join('');
  const ticks = [min, (min+max)/2, max].map((t,i) =>
    `<text x="${X(t).toFixed(1)}" y="${H+13}" font-size="9.5" fill="var(--text-faint)" text-anchor="${['start','middle','end'][i]}">${label(t)}</text>`).join('');
  return `<div class="chart"><svg viewBox="0 0 ${W} ${H+16}" role="img"><line x1="0" y1="${H}" x2="${W}" y2="${H}" stroke="var(--border)"/>${bars}${ls}${ticks}</svg></div>`;
}

function catBars(items, {h=130, valueFmt=(v)=>v, refLine=null, labelEvery=1}){
  const W = 360, H = h, n = items.length || 1, bw = W / n;
  const maxV = Math.max(maxOf(items.map(i => i.v)), refLine?.v || 0, 1e-9);
  const bars = items.map((it, i) => {
    const bh = it.v / maxV * (H - 22);
    return `<rect x="${(i*bw+1).toFixed(1)}" y="${(H-bh).toFixed(1)}" width="${Math.max(bw-2,1).toFixed(1)}" height="${bh.toFixed(1)}" fill="${it.color || 'var(--primary)'}" opacity="${it.dim ? .35 : .88}" rx="1.5"><title>${esc(it.label)}: ${valueFmt(it.v)}</title></rect>` +
      (i % labelEvery === 0 ? `<text x="${(i*bw+bw/2).toFixed(1)}" y="${H+13}" font-size="9.5" fill="var(--text-faint)" text-anchor="middle">${esc(it.label)}</text>` : '');
  }).join('');
  const ref = refLine ? (() => {
    const y = (H - refLine.v / maxV * (H - 22)).toFixed(1);
    return `<line x1="0" y1="${y}" x2="${W}" y2="${y}" stroke="${refLine.color}" stroke-dasharray="4,3" stroke-width="1.4"/><text x="${W}" y="${y-3}" font-size="9.5" fill="${refLine.color}" text-anchor="end">${esc(refLine.label)}</text>`;
  })() : '';
  return `<div class="chart"><svg viewBox="0 0 ${W} ${H+16}" role="img"><line x1="0" y1="${H}" x2="${W}" y2="${H}" stroke="var(--border)"/>${bars}${ref}</svg></div>`;
}

const loadingHtml = (t='불러오는 중…') => `<div class="loading"><span class="spinner"></span>${esc(t)}</div>`;
const noDetailHtml = (sido) => `<div class="empty">이 지역(${esc(sido)})은 상세 데이터 미수집</div>`;

// ============================================================ 탭 전환
let currentTab = null;
let prevVisit = null;       // 이번 세션 시작 전 마지막 방문 시각 (NEW 판정 기준)

function switchTab(tab, push=true){
  if(!TAB_TITLES[tab]) tab = 'bids';
  currentTab = tab;
  document.querySelectorAll('main > section').forEach(s => s.hidden = s.id !== 'view-' + tab);
  document.querySelectorAll('.nav-item').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
  $('pageTitle').textContent = TAB_TITLES[tab];
  if(push && location.hash !== '#' + tab) history.pushState(null, '', '#' + tab);
  window.scrollTo(0, 0);
  ({bids: renderBids, predict: renderPredictTab, watch: renderWatch, stats: renderStats, settings: renderSettings})[tab]();
}

// ============================================================ 입찰공고
let bidsShown = PAGE_SIZE;
const isNew = (b) => prevVisit && b.seen && new Date(b.seen) > prevVisit;

/** 탭 배지: 입찰공고 탭을 마지막으로 본 뒤 새로 수집된 공고 수 */
function updateNewBadge(){
  const el = $('newBadge');
  const seenAt = LS.get('bidsSeenAt', 0);
  if(!Data.bids || !seenAt){ el.hidden = true; return; }
  const n = Data.bids.filter(b => b.seen && new Date(b.seen).getTime() > seenAt).length;
  el.textContent = n > 99 ? '99+' : n;
  el.hidden = !n;
}

function initBidsFilters(){
  const saved = LS.get('bidsFilter', {});
  const defSido = saved.sido ?? (Data.meta.detail?.regions?.[0] || '');
  fillSelect($('bSido'), SIDOS, {value: defSido});
  fillSelect($('bLic'), LICENSES, {value: saved.lic || ''});
  fillBidsSgg(saved.sgg || '');
  const onChange = () => {
    LS.set('bidsFilter', {sido: $('bSido').value, sgg: $('bSgg').value, lic: $('bLic').value});
    bidsShown = PAGE_SIZE; renderBids();
  };
  $('bSido').addEventListener('change', () => { fillBidsSgg(''); onChange(); });
  $('bSgg').addEventListener('change', onChange);
  $('bLic').addEventListener('change', onChange);
  let t; $('bQuery').addEventListener('input', () => { clearTimeout(t); t = setTimeout(() => { bidsShown = PAGE_SIZE; renderBids(); }, 200); });
  $('bidsMore').addEventListener('click', () => { bidsShown += PAGE_SIZE; renderBids(); });
}
function fillBidsSgg(value){
  const sido = $('bSido').value;
  const sggs = [...new Set((Data.bids||[]).filter(b => b.sido === sido && b.sgg).map(b => b.sgg))].sort();
  fillSelect($('bSgg'), sggs, {value});
  $('bSgg').disabled = !sido;
}

let watchIds = new Set();
async function renderBids(){
  const list = $('bidsList');
  if(!Data.bids){ list.innerHTML = loadingHtml(); await Data.loadBids(); fillBidsSgg(LS.get('bidsFilter', {}).sgg || ''); }
  const sido = $('bSido').value, sgg = $('bSgg').value, lic = $('bLic').value;
  const q = $('bQuery').value.trim().toLowerCase();
  const now = new Date();
  const rows = Data.bids.filter(b =>
    (!sido || b.sido === sido) && (!sgg || b.sgg === sgg) &&
    (!lic || (b.lic||[]).includes(lic)) &&
    (!q || (b.nm||'').toLowerCase().includes(q) || (b.org||'').toLowerCase().includes(q)) &&
    (!b.close || parseKst(b.close) >= now)
  );
  const newCount = rows.filter(isNew).length;
  $('bidsInfo').innerHTML = `${fmtNum(rows.length)}건${newCount ? ` · <span class="new">NEW</span>${newCount}건` : ''}` +
    (Data.meta.updated_at ? ` · 데이터 ${esc(Data.meta.updated_at.slice(0,16).replace('T',' '))} 기준` : '') +
    (!sido ? ' · 시·도를 고르면 예상 사정율이 표시됩니다' : '');
  // 방문 기록
  LS.set('lastVisit', now.toISOString());
  LS.set('bidsSeenAt', Date.now());
  $('newBadge').hidden = true;

  if(!rows.length){
    list.innerHTML = `<div class="empty">${Data.bids.length ? '조건에 맞는 진행중 공고가 없습니다.' : '아직 수집된 공고가 없습니다. 데이터 수집이 실행되면 표시됩니다.'}</div>`;
    $('bidsMore').hidden = true;
    return;
  }
  if(sido && Data.hasScsbid(sido) && !Data.scsbid[sido]){
    list.innerHTML = loadingHtml(`${sido} 낙찰 데이터 불러오는 중…`);
    try{ await Data.loadScsbid(sido); }catch(e){ console.warn(e); }
    if(currentTab !== 'bids' || $('bSido').value !== sido) return;
  }
  watchIds = new Set((await WatchStore.list()).map(w => w.id));
  const shown = rows.slice(0, bidsShown);
  list.innerHTML = shown.map(bidCard).join('');
  $('bidsMore').hidden = rows.length <= bidsShown;
}

function bidCard(b){
  const dd = ddayLabel(b.close);
  const qp = b.sido && Data.scsbid[b.sido] ? quickPredict(b) : null;
  const price = b.base ? `기초 ${eok(b.base)}` : (b.est ? `추정 ${eok(b.est)}` : '');
  const pred = qp
    ? `<span>예상 사정율 <b>${pct(qp.sr, 3)}</b></span>
       <span>추천 투찰가 <b>${qp.bid ? won(qp.bid) : '기초금액 미공개'}</b></span>
       <span class="meta-line" style="margin:0;">${sampleText(qp.n)}${qp.note ? ` · ${qp.note}` : ''}</span>`
    : (b.sido && !Data.hasScsbid(b.sido) ? '<span class="meta-line" style="margin:0;">이 지역 낙찰 데이터 없음</span>' : '');
  return `<div class="bid">
    <div class="bid-top">
      <div>
        <div class="bid-title">${isNew(b) ? '<span class="new">NEW</span>' : ''}${esc(b.nm)}</div>
        <div class="bid-sub">${esc(b.org || b.dmd || '')} · ${esc([b.sido, b.sgg].filter(Boolean).join(' ') || '지역 미상')}${b.lic?.length ? ' · ' + esc(b.lic.join(', ')) : ''}${price ? ' · ' + price : ''}</div>
      </div>
      <button class="star ${watchIds.has(b.id) ? 'on' : ''}" data-watch="${esc(b.id)}" title="관심공고" type="button">${watchIds.has(b.id) ? '★' : '☆'}</button>
    </div>
    <div class="bid-pred"><span class="dday ${dd.urgent ? 'urgent' : ''}">${esc(dd.text)}</span>${pred}</div>
    <div class="bid-actions">
      <button class="btn sm" data-predict="${esc(b.id)}" type="button">이 공고로 예측</button>
      ${b.url ? `<a class="btn line sm" href="${esc(b.url)}" target="_blank" rel="noopener">공고 원문</a>` : ''}
    </div>
  </div>`;
}

async function toggleWatch(id, btn){
  const b = Data.bids.find(x => x.id === id);
  if(!b) return;
  if(watchIds.has(id)){
    await WatchStore.remove(id); watchIds.delete(id);
    btn.classList.remove('on'); btn.textContent = '☆';
  }else{
    const qp = b.sido && Data.scsbid[b.sido] ? quickPredict(b) : null;
    await WatchStore.save({...pickNotice(b), savedAt: new Date().toISOString(),
      pred: qp ? {sr: qp.sr, bid: qp.bid, n: qp.n, by: '자동'} : null});
    watchIds.add(id);
    btn.classList.add('on'); btn.textContent = '★';
  }
}
const pickNotice = (b) => ({id:b.id, no:b.no, ord:b.ord, nm:b.nm, org:b.org, dmd:b.dmd, sido:b.sido, sgg:b.sgg,
  lic:b.lic, base:b.base, a:b.a, floor:b.floor, net:b.net, rng:b.rng, close:b.close, url:b.url});

// ============================================================ 예측분석
const P = {
  sidos: new Set(LS.get('pSidos', [])),
  sggs: new Set(),
  lics: new Set(LS.get('pLics', [])),
  notice: null,
  last: null,     // 마지막 예측 {pred, rows}
};

function initPredict(){
  buildChips($('pSidoChips'), SIDOS, P.sidos, () => { LS.set('pSidos', [...P.sidos]); renderSggChips(); },
    (s) => Data.hasScsbid(s) ? '' : '데이터 없음');
  buildChips($('pLicChips'), LICENSES, P.lics, () => LS.set('pLics', [...P.lics]));
  $('runPredict').addEventListener('click', runPredict);
  ['cBase','cA','cFloor','cRate','cNet','cManual'].forEach(id => $(id).addEventListener('input', renderCalc));
  $('simRun').addEventListener('click', runSimulation);
  fillSelect($('orgSido'), SIDOS.map(s => [s, s + (Data.hasDetail(s) ? '' : ' (상세 미수집)')]), {all:'시·도 선택', value: [...P.sidos][0] || ''});
  $('orgSido').addEventListener('change', () => loadOrgOptions());
  $('orgSel').addEventListener('change', renderOrgProb);
  renderSggChips();
  renderCalc();
}

function buildChips(el, items, set, onChange, hint){
  el.innerHTML = '';
  items.forEach(v => {
    const c = document.createElement('button');
    c.type = 'button';
    c.className = 'chip' + (set.has(v) ? ' selected' : '');
    c.textContent = v;
    const h = hint?.(v);
    if(h) c.title = h;
    c.addEventListener('click', () => {
      set.has(v) ? set.delete(v) : set.add(v);
      c.classList.toggle('selected', set.has(v));
      onChange?.();
    });
    el.appendChild(c);
  });
}
function syncChips(el, set){ el.querySelectorAll('.chip').forEach(c => c.classList.toggle('selected', set.has(c.textContent))); }

async function renderSggChips(){
  const el = $('pSggChips');
  const sidos = [...P.sidos].filter(s => Data.hasScsbid(s));
  if(!sidos.length){ el.innerHTML = ''; P.sggs.clear(); return; }
  el.innerHTML = '<span class="meta-line">시·군 목록 불러오는 중…</span>';
  const recs = await Data.loadScsbidMany(sidos);
  const counts = {};
  recs.forEach(r => { if(r.sgg && P.sidos.has(r.sido)) counts[r.sgg] = (counts[r.sgg]||0) + 1; });
  const sggs = Object.keys(counts).sort();
  [...P.sggs].forEach(s => { if(!counts[s]) P.sggs.delete(s); });
  buildChips(el, sggs, P.sggs, null);
  el.querySelectorAll('.chip').forEach(c => c.classList.add('sm'));
}

let orgLoaded = false;
function renderPredictTab(){
  if(!orgLoaded && $('orgSido').value){ orgLoaded = true; loadOrgOptions(); }
  const card = $('pBidCard');
  const n = P.notice;
  card.hidden = !n;
  if(n){
    const dd = ddayLabel(n.close);
    card.innerHTML = `<div class="card-head"><div>
        <h2>${esc(n.nm)}</h2>
        <p class="sub" style="margin:0;">${esc(n.org || '')} · ${esc([n.sido, n.sgg].filter(Boolean).join(' '))} · ${esc((n.lic||[]).join(', ') || '면허 정보 없음')} · ${esc(dd.text)}</p>
        <div class="meta-line">기초금액 ${won(n.base)} · A값 ${won(n.a)} · 낙찰하한율 ${n.floor ? pct(n.floor) : `미확인(${DEFAULT_FLOOR}% 적용)`} · 순공사원가 ${won(n.net)}${n.rng ? ` · 예가범위 ${n.rng[0]}~+${n.rng[1]}%` : ''}</div>
      </div>
      <div class="btn-row"><button class="btn sm ghost" id="pSaveWatch" type="button">내 예측을 관심공고에 저장</button><button class="btn sm line" id="pClearNotice" type="button">공고 해제</button></div></div>`;
    $('pClearNotice').onclick = () => { P.notice = null; renderPredictTab(); };
    $('pSaveWatch').onclick = async () => {
      const c = calcValues();
      await WatchStore.save({...pickNotice(n), savedAt: new Date().toISOString(),
        pred: c.sr ? {sr: c.sr, bid: c.final, n: P.last?.pred?.n || 0, by: '직접'} : null});
      $('pSaveWatch').textContent = '저장됨 ✓';
    };
  }
}

function predictWithNotice(id){
  const b = Data.bids.find(x => x.id === id);
  if(!b) return;
  P.notice = b;
  if(b.sido){ P.sidos.clear(); P.sidos.add(b.sido); LS.set('pSidos', [...P.sidos]); }
  P.sggs.clear();
  P.lics = new Set(b.lic || []); LS.set('pLics', [...P.lics]);
  syncChips($('pSidoChips'), P.sidos); syncChips($('pLicChips'), P.lics);
  $('inBase').value = b.base || b.est || '';
  $('fBase').checked = !!(b.base || b.est);
  $('cBase').value = b.base || '';
  $('cA').value = b.a || '';
  $('cFloor').value = b.floor || DEFAULT_FLOOR;
  $('cNet').value = b.net || '';
  $('cManual').value = '';
  if(b.rng?.[1]) $('sRange').value = Math.abs(b.rng[1]);
  if(b.sido){ $('orgSido').value = b.sido; }
  orgLoaded = true;
  switchTab('predict');
  renderSggChips();
  loadOrgOptions(recOrg(b));
  runPredict();
}

async function runPredict(){
  const out = $('predictResult');
  const sidos = [...P.sidos];
  if(!sidos.length){ out.innerHTML = '<div class="card"><div class="empty">지역을 하나 이상 선택하세요.</div></div>'; return; }
  const missing = sidos.filter(s => !Data.hasScsbid(s));
  out.innerHTML = `<div class="card">${loadingHtml()}</div>`;
  let recs;
  try{ recs = await Data.loadScsbidMany(sidos); }
  catch(e){ out.innerHTML = `<div class="card"><div class="empty">데이터를 불러오지 못했습니다. (${esc(e.message)})</div></div>`; return; }
  const o = {sggs: P.sggs, lics: P.lics, recent: $('fRecent').checked,
    useBase: $('fBase').checked, base: +$('inBase').value || 0,
    useCnt: $('fCnt').checked, cnt: +$('inCnt').value || 0};
  const rows = filterRecords(recs, o);
  $('matchCount').textContent = `${fmtNum(rows.length)}건의 과거 낙찰로 계산`;
  const pred = predictFrom(rows);
  P.last = pred ? {pred, rows} : null;
  if(!pred || rows.length < 3){
    out.innerHTML = `<div class="card"><div class="empty">조건에 맞는 과거 데이터가 너무 적습니다 (${rows.length}건). 조건을 넓혀 보세요.${missing.length ? `<br>데이터 없는 지역: ${esc(missing.join(', '))}` : ''}</div></div>`;
    return;
  }
  $('cRate').value = pred.mean.toFixed(4);
  renderCalc();
  const base = +$('cBase').value || 0, a = +$('cA').value || 0, floor = +$('cFloor').value || DEFAULT_FLOOR;
  const band = (key, title, cls='') => {
    const b = pred.bands[key];
    return `<div class="band ${cls}"><div class="t">${title}</div><div class="v">${pct(b.v, 3)}</div>
      <div class="r">${b.lo.toFixed(3)} ~ ${b.hi.toFixed(3)}%</div>
      ${base ? `<div class="r" style="color:var(--text-sub);">${won(bidAmount(base, b.v, a, floor))}</div>` : ''}</div>`;
  };
  const floorLine = P.notice?.floor || floor;
  const srs = rows.map(r => r.sr);
  const sorted = [...srs].sort((x,y) => x-y);
  let lo = Math.max(90, Math.floor(Math.min(quantile(sorted, .01), pred.mean - 3*pred.std) * 2) / 2);
  let hi = Math.min(110, Math.ceil(Math.max(quantile(sorted, .99), pred.mean + 3*pred.std) * 2) / 2);
  if(hi - lo < 1){ lo -= 0.5; hi += 0.5; }
  const rates = rows.map(r => r.rate).filter(v => v != null && v > 80 && v <= 100);
  const rSorted = [...rates].sort((x,y) => x-y);
  let rLo = Math.floor(Math.min(floorLine, quantile(rSorted, .01))), rHi = Math.min(100, Math.ceil(quantile(rSorted, .98)));
  if(rHi - rLo < 1) rHi = rLo + 1;
  out.innerHTML = `
    <div class="card">
      <h2>예측 결과</h2>
      <p class="sub">선택한 조건의 과거 사정율(예정가격 ÷ 기초금액) 분포 기준 추정치입니다. 참고용이며 낙찰을 보장하지 않습니다.</p>
      <div class="result-grid">
        <div>
          <div class="big-number">${pred.mean.toFixed(3)}<small>% 평균 사정율</small></div>
          <div class="meta-line">표준편차 ±${pred.std.toFixed(3)}%p · ${sampleText(pred.n)}</div>
          <div class="meta-line">신뢰도 <b>${pred.conf.label}</b> (${pred.conf.score}점 · 표본 수와 분산 기준)</div>
          <div class="meta-line">중앙값 ${quantile(sorted,.5).toFixed(3)}% · 범위 ${sorted[0].toFixed(2)}~${sorted.at(-1).toFixed(2)}%</div>
          <div class="bands">${band('aggressive','공격')}${band('recommend','추천','rec')}${band('conservative','보수')}</div>
          <div class="meta-line">공격 = 낮게 써서 1순위 가능성↑, 하한 미달 위험↑ · 보수 = 그 반대</div>
        </div>
        <div>
          <h3 style="margin-top:0;">사정율 분포</h3>
          ${histogram(srs, {min:lo, max:hi, step:(hi-lo) > 6 ? 0.25 : 0.1, lines:[{x:pred.mean, color:'var(--primary-dark)', label:`평균 ${pred.mean.toFixed(2)}`}, {x:100, color:'var(--text-faint)', label:'100%'}]})}
          <h3>1위 낙찰율 분포</h3>
          ${rates.length ? histogram(rates, {min:rLo, max:rHi, step:0.1, color:'var(--text-sub)',
              lines:[{x:floorLine, color:'var(--target)', label:`낙찰하한율 ${floorLine}%`}]}) : '<div class="empty">낙찰율 정보 없음</div>'}
          <div class="meta-line">${sampleText(rates.length)}</div>
        </div>
      </div>
    </div>
    <div class="card">
      <div class="card-head">
        <div><h2>근거 과거 공고 (${fmtNum(rows.length)}건)</h2><p class="sub">예측에 쓰인 공고 전체 · 최근 순</p></div>
        <button class="btn sm ghost" id="csvBtn" type="button">CSV 내보내기</button>
      </div>
      <div class="table-wrap"><table>
        <thead><tr><th>공고명</th><th>기관</th><th class="num">낙찰율</th><th class="num">사정율</th><th class="num">기초금액</th><th class="num">참가</th><th>개찰일</th></tr></thead>
        <tbody>${rows.slice(0, 1000).map(r => `<tr><td class="wrap">${esc(r.nm)}</td><td>${esc(recOrg(r))}</td><td class="num">${pct(r.rate)}</td><td class="num">${pct(r.sr)}</td><td class="num">${eok(r.base)}</td><td class="num">${r.cnt ?? '-'}</td><td>${esc(r.date)}</td></tr>`).join('')}</tbody>
      </table></div>
      ${rows.length > 1000 ? `<div class="meta-line">화면에는 최근 1,000건만 표시합니다. 전체는 CSV로 받으세요.</div>` : ''}
    </div>`;
  $('csvBtn').onclick = () => downloadCSV('예측근거_과거공고.csv',
    ['공고번호','공고명','발주기관','수요기관','시도','시군','면허','기초금액','예정가격','낙찰금액','낙찰율','사정율','참가업체수','낙찰하한율','A값','개찰일'],
    rows.map(r => [r.no, r.nm, r.org, r.dmd, r.sido, r.sgg, (r.lic||[]).join(' '), r.base, r.plan, r.amt, r.rate, r.sr, r.cnt, r.floor, r.a, r.date]));
}

function calcValues(){
  const base = +$('cBase').value || 0, a = +$('cA').value || 0;
  const floor = +$('cFloor').value || DEFAULT_FLOOR, sr = +$('cRate').value || 0;
  const net = +$('cNet').value || 0, manual = +$('cManual').value || 0;
  const plan = base && sr ? base * sr / 100 : 0;
  const computed = bidAmount(base, sr, a, floor);
  return {base, a, floor, sr, net, manual, plan, computed, final: manual || computed};
}

function renderCalc(){
  const c = calcValues();
  const out = $('calcResult');
  if(!c.base || !c.sr){ out.innerHTML = '<div class="meta-line">기초금액과 적용 사정율을 입력하세요.</div>'; return; }
  const warns = [];
  if(c.manual && c.manual < c.computed) warns.push(`투찰금액이 적용 사정율 기준 낙찰하한가(${won(c.computed)})보다 ${won(c.computed - c.manual)} 낮습니다 — 낙찰하한가 미만`);
  if(c.net && c.final < c.net * 0.98) warns.push(`투찰금액이 순공사원가 × 98% (${won(Math.ceil(c.net*0.98))}) 미만입니다 — 입찰 무효 위험`);
  const rng = P.notice?.rng;
  const info = [];
  if(rng && rng[0] != null && rng[1] != null && (c.sr < 100 + rng[0] || c.sr > 100 + rng[1])) info.push(`적용 사정율이 공고 예가범위(${100+rng[0]}~${100+rng[1]}%) 밖입니다.`);
  if(P.last?.rows?.length){
    const above = P.last.rows.filter(r => r.sr > c.sr).length / P.last.rows.length;
    info.push(`과거 분포상 실제 사정율이 적용값보다 높을 확률 ${(above*100).toFixed(1)}% — 이 경우 이 금액은 실제 낙찰하한가에 못 미칩니다. (${sampleText(P.last.rows.length)})`);
  }
  out.innerHTML = `<div class="stat-grid">
      <div class="stat"><div class="t">예정가격</div><div class="v">${won(c.plan)}</div></div>
      <div class="stat"><div class="t">투찰금액 (낙찰하한가)</div><div class="v" style="color:var(--primary-dark);">${won(c.computed)}</div></div>
      ${c.manual ? `<div class="stat"><div class="t">직접 입력 투찰금액</div><div class="v">${won(c.manual)}</div></div>` : ''}
      ${c.net ? `<div class="stat"><div class="t">순공사원가 × 98%</div><div class="v">${won(Math.ceil(c.net*0.98))}</div></div>` : ''}
    </div>
    ${warns.map(w => `<div class="alert danger">⚠️ ${esc(w)}</div>`).join('')}
    ${info.map(w => `<div class="alert info">${w}</div>`).join('')}
    <div class="meta-line">낙찰하한율 ${c.floor}% · A값 ${won(c.a)} · 참고용이며 낙찰을 보장하지 않습니다.</div>`;
}

// ---------- 발주기관 예가 구간확률
async function loadOrgOptions(prefer){
  const sido = $('orgSido').value;
  const sel = $('orgSel'), out = $('orgResult');
  if(!sido){ fillSelect(sel, [], {all:'시·도를 먼저 고르세요'}); out.innerHTML = ''; return; }
  if(!Data.hasDetail(sido)){ fillSelect(sel, [], {all:'-'}); out.innerHTML = noDetailHtml(sido); return; }
  out.innerHTML = loadingHtml('복수예가 데이터 불러오는 중…');
  const [op, sc] = await Promise.all([Data.loadOpening(sido), Data.hasScsbid(sido) ? Data.loadScsbid(sido) : {byId:new Map()}]);
  const counts = {};
  for(const [id, b] of op.bids){
    if(!(b.p?.length >= 4)) continue;
    const org = recOrg(sc.byId.get(id) || {});
    if(org) counts[org] = (counts[org]||0) + 1;
  }
  const orgs = Object.entries(counts).sort((x,y) => y[1]-x[1]);
  const want = prefer && counts[prefer] ? prefer : (orgs[0]?.[0] || '');
  fillSelect(sel, orgs.map(([o,n]) => [o, `${o} (${n}건)`]), {all:null, value: want});
  if(prefer && !counts[prefer]) out.innerHTML = `<div class="alert warn">이 공고의 기관(${esc(prefer)})은 복수예가 기록이 없어 다른 기관을 표시합니다.</div>`;
  renderOrgProb(prefer && !counts[prefer]);
}

async function renderOrgProb(keepMsg){
  const sido = $('orgSido').value, org = $('orgSel').value, out = $('orgResult');
  if(!sido || !Data.hasDetail(sido)) return;
  if(!org){ out.innerHTML = '<div class="empty">복수예가 기록이 있는 기관이 없습니다.</div>'; return; }
  const op = Data.opening[sido], sc = Data.scsbid[sido] || {byId:new Map()};
  const bins = new Map();
  let nBids = 0;
  for(const [id, b] of op.bids){
    const rec = sc.byId.get(id) || {};
    if(recOrg(rec) !== org) continue;
    const base = b.base || rec.base;
    const pcts = (b.p||[]).map(x => x[1] / base * 100).filter(v => v > 80 && v < 120);
    if(!base || pcts.length < 4) continue;
    nBids++;
    const local = new Map();
    let combos = 0;
    const k = pcts.length;
    for(let i=0;i<k;i++) for(let j=i+1;j<k;j++) for(let l=j+1;l<k;l++) for(let m=l+1;m<k;m++){
      const avg = (pcts[i]+pcts[j]+pcts[l]+pcts[m]) / 4;
      const key = Math.floor(avg * 10 + 1e-9);
      local.set(key, (local.get(key)||0) + 1);
      combos++;
    }
    for(const [key, c] of local) bins.set(key, (bins.get(key)||0) + c / combos);
  }
  if(!nBids){ out.innerHTML = '<div class="empty">이 기관의 복수예가 기록이 없습니다.</div>'; return; }
  const keys = [...bins.keys()].sort((a,b) => a-b);
  const items = [];
  for(let k = keys[0]; k <= keys.at(-1); k++) items.push({key:k, label:(k/10).toFixed(1), v:(bins.get(k)||0) / nBids});
  const top = [...items].sort((a,b) => b.v - a.v).slice(0, 5);
  const topSet = new Set(top.map(t => t.key));
  const predKey = P.last ? Math.floor(P.last.pred.mean * 10 + 1e-9) : null;
  items.forEach(it => { if(!topSet.has(it.key)) it.dim = true; if(it.key === predKey) it.color = 'var(--target)'; });
  const every = Math.max(1, Math.ceil(items.length / 10));
  const msg = keepMsg === true ? out.querySelector('.alert')?.outerHTML || '' : '';
  out.innerHTML = `${msg}
    ${catBars(items, {valueFmt: v => (v*100).toFixed(2) + '%', labelEvery: every})}
    <div class="legend"><span><i style="background:var(--primary)"></i>상위 5개 구간</span>${predKey != null ? '<span><i style="background:var(--target)"></i>예측 평균 사정율 구간</span>' : ''}</div>
    <div class="meta-line">${esc(org)} · ${sampleText(nBids)} · 각 공고의 1,365개 조합을 같은 비중으로 합산</div>
    <div class="table-wrap" style="margin-top:10px; max-height:none;"><table>
      <thead><tr><th>사정율 구간</th><th class="num">확률</th><th class="num">투찰금액(계산기 값 기준)</th></tr></thead>
      <tbody>${top.map(t => { const c = calcValues(); const mid = t.key/10 + 0.05;
        return `<tr><td>${(t.key/10).toFixed(1)} ~ ${(t.key/10+0.1).toFixed(1)}%</td><td class="num">${(t.v*100).toFixed(2)}%</td><td class="num">${c.base ? won(bidAmount(c.base, mid, c.a, c.floor)) : '-'}</td></tr>`; }).join('')}</tbody>
    </table></div>`;
}

// ---------- 복수예비가격 시뮬레이터
function runSimulation(){
  const range = Math.abs(+$('sRange').value || 2) / 100;
  const runs = Math.min(100000, Math.max(100, +$('sRuns').value || 5000));
  const base = +$('cBase').value || 0;
  const srs = [];
  let example = null;
  for(let t=0; t<runs; t++){
    // 나라장터 방식: 범위를 15구간으로 나눠 구간마다 하나씩 생성한 뒤 4개 추첨
    const prices = Array.from({length:15}, (_, i) => {
      const lo = -range + (2*range) * i / 15, hi = -range + (2*range) * (i+1) / 15;
      return 100 * (1 + lo + Math.random() * (hi - lo));
    });
    const idx = [...Array(15).keys()].sort(() => Math.random() - 0.5).slice(0, 4);
    const sr = idx.reduce((a,i) => a + prices[i], 0) / 4;
    srs.push(sr);
    if(!example) example = {prices, idx, sr};
  }
  const sorted = [...srs].sort((a,b) => a-b);
  const r = range * 100;
  $('simResult').innerHTML = `
    ${histogram(srs, {min:100 - r, max:100 + r, step: r/20, lines:[{x:100, color:'var(--text-faint)', label:'100%'}]})}
    <div class="stat-grid" style="margin-top:8px;">
      <div class="stat"><div class="t">평균 사정율</div><div class="v">${pct(sorted.reduce((a,b)=>a+b,0)/runs)}</div></div>
      <div class="stat"><div class="t">90% 구간</div><div class="v">${quantile(sorted,.05).toFixed(2)}~${quantile(sorted,.95).toFixed(2)}%</div></div>
      ${base ? `<div class="stat"><div class="t">예시 예정가격</div><div class="v">${won(base * example.sr / 100)}</div></div>` : ''}
    </div>
    <div class="meta-line">예시 1회: 예비가격 ${example.prices.map((p,i) => example.idx.includes(i) ? `<b>${p.toFixed(2)}</b>` : p.toFixed(2)).join(' · ')} (굵게 = 추첨) → 사정율 ${example.sr.toFixed(3)}%</div>
    <div class="meta-line">무작위 가정에 따른 참고용 시뮬레이션입니다. 실제 예가 생성 방식·업체 추첨과 다를 수 있습니다.</div>`;
}

// ============================================================ 관심공고
async function renderWatch(){
  const el = $('watchList');
  const items = await WatchStore.list();
  if(!items.length){ el.innerHTML = '<div class="empty">아직 저장한 관심공고가 없습니다. 입찰공고 탭에서 ☆를 눌러 보세요.</div>'; return; }
  el.innerHTML = loadingHtml();
  const sidos = [...new Set(items.map(i => i.sido).filter(s => s && Data.hasScsbid(s)))];
  try{ await Data.loadScsbidMany(sidos); }catch(e){ console.warn(e); }
  const findRec = (w) => {
    const s = Data.scsbid[w.sido];
    if(!s) return null;
    return s.byId.get(w.id) || s.recs.find(r => r.no === w.no) || null;
  };
  el.innerHTML = `<div class="bid-list" style="margin-top:0;">${items.map(w => {
    const r = findRec(w);
    const dd = ddayLabel(w.close);
    let body;
    if(r && r.sr != null){
      const diff = w.pred?.sr != null ? w.pred.sr - r.sr : null;
      const floor = r.floor || w.floor || DEFAULT_FLOOR, a = r.a ?? w.a ?? 0;
      const realFloorPrice = r.plan ? Math.ceil((r.plan - a) * floor / 100 + a) : null;
      let verdict = '';
      if(w.pred?.bid && realFloorPrice && r.amt){
        verdict = w.pred.bid < realFloorPrice ? '<span class="badge">하한 미달</span>'
          : w.pred.bid < r.amt ? '<span class="badge ok">1위보다 낮음 (낙찰권)</span>'
          : '<span class="badge gray">1위보다 높음</span>';
      }
      body = `<div class="stat-grid" style="margin-top:8px;">
          <div class="stat"><div class="t">내 예측 사정율</div><div class="v">${w.pred?.sr != null ? pct(w.pred.sr) : '-'}</div></div>
          <div class="stat"><div class="t">실제 사정율</div><div class="v">${pct(r.sr)}</div></div>
          <div class="stat"><div class="t">오차</div><div class="v">${diff != null ? (diff >= 0 ? '+' : '') + diff.toFixed(3) + '%p' : '-'}</div></div>
          <div class="stat"><div class="t">1위 투찰률</div><div class="v">${pct(r.rate)}</div></div>
        </div>
        <div class="meta-line">개찰 ${esc(r.date)} · 1위 ${esc(r.win || '-')} ${won(r.amt)} · 내 투찰가 ${won(w.pred?.bid)} ${verdict}</div>`;
    }else{
      body = `<div class="meta-line">${r ? '개찰됨 · 사정율 계산 불가(기초금액 없음)' : '개찰 전 또는 결과 미수집'} · 내 예측 ${w.pred?.sr != null ? pct(w.pred.sr) + ' / ' + won(w.pred.bid) : '없음'}${w.pred?.n != null ? ` (${sampleText(w.pred.n)})` : ''}</div>`;
    }
    return `<div class="bid">
      <div class="bid-top"><div>
        <div class="bid-title">${esc(w.nm)}</div>
        <div class="bid-sub">${esc(w.org || '')} · ${esc([w.sido, w.sgg].filter(Boolean).join(' '))} · <span class="dday ${dd.urgent ? 'urgent' : ''}">${esc(dd.text)}</span></div>
      </div><button class="star on" data-unwatch="${esc(w.id)}" title="관심 해제" type="button">★</button></div>
      ${body}
      <div class="bid-actions">${Data.bids?.some(b => b.id === w.id) ? `<button class="btn sm" data-predict="${esc(w.id)}" type="button">이 공고로 예측</button>` : ''}
        ${w.url ? `<a class="btn line sm" href="${esc(w.url)}" target="_blank" rel="noopener">공고 원문</a>` : ''}</div>
    </div>`;
  }).join('')}</div>`;
}

// ============================================================ 통계
function initStats(){
  const saved = LS.get('statsFilter', {});
  fillSelect($('sSido'), SIDOS, {all:'시·도 선택', value: saved.sido ?? (Data.meta.detail?.regions?.[0] || '')});
  fillSelect($('sLic'), LICENSES, {value: saved.lic || ''});
  $('sPeriod').value = saved.period ?? '12';
  ['sSido','sLic','sPeriod'].forEach(id => $(id).addEventListener('change', () => {
    LS.set('statsFilter', {sido:$('sSido').value, lic:$('sLic').value, period:$('sPeriod').value});
    renderStats();
  }));
}

let statsToken = 0;
async function renderStats(){
  const token = ++statsToken;
  const el = $('statsBody');
  const sido = $('sSido').value, lic = $('sLic').value, months = +$('sPeriod').value;
  if(!sido){ el.innerHTML = '<div class="card"><div class="empty">시·도를 선택하세요.</div></div>'; return; }
  if(!Data.hasScsbid(sido)){ el.innerHTML = '<div class="card"><div class="empty">이 지역은 아직 낙찰 데이터가 없습니다.</div></div>'; return; }
  el.innerHTML = `<div class="card">${loadingHtml()}</div>`;
  const sc = await Data.loadScsbid(sido);
  if(token !== statsToken) return;
  const cut = months ? monthsAgo(months) : '';
  const rows = sc.recs.filter(r => r.sr != null && (!cut || r.date >= cut) && (!lic || (r.lic||[]).includes(lic)));
  const all = stats(rows.map(r => r.sr));
  const group = (keyFn) => {
    const m = new Map();
    rows.forEach(r => [].concat(keyFn(r)).forEach(k => { if(!k) return; if(!m.has(k)) m.set(k, []); m.get(k).push(r); }));
    return [...m].map(([k, rs]) => ({k, ...stats(rs.map(r => r.sr)), rate: stats(rs.map(r => r.rate).filter(v => v != null)).mean}))
      .sort((a,b) => b.n - a.n);
  };
  const byOrg = group(recOrg).slice(0, 50);
  const byLic = group(r => r.lic || []);
  const gTable = (list, name) => `<div class="table-wrap"><table>
      <thead><tr><th>${name}</th><th class="num">건수</th><th class="num">평균 사정율</th><th class="num">표준편차</th><th class="num">평균 낙찰율</th></tr></thead>
      <tbody>${list.map(g => `<tr><td>${esc(g.k)}${sampleBadge(g.n)}</td><td class="num">${fmtNum(g.n)}</td><td class="num">${pct(g.mean)}</td><td class="num">${g.std.toFixed(3)}</td><td class="num">${pct(g.rate)}</td></tr>`).join('') || '<tr><td colspan="5" class="empty">데이터 없음</td></tr>'}</tbody></table></div>`;

  let html = `<div class="card">
      <h2>${esc(sido)} 요약</h2>
      <p class="sub">${months ? `최근 ${months}개월` : '전체 기간'}${lic ? ` · ${esc(lic)}` : ''} · 사정율 = 예정가격 ÷ 기초금액</p>
      <div class="stat-grid">
        <div class="stat"><div class="t">공고 수</div><div class="v">${fmtNum(all.n)}</div></div>
        <div class="stat"><div class="t">평균 사정율</div><div class="v">${pct(all.mean)}</div></div>
        <div class="stat"><div class="t">표준편차</div><div class="v">${isFinite(all.std) ? all.std.toFixed(3) + '%p' : '-'}</div></div>
        <div class="stat"><div class="t">평균 참가업체</div><div class="v">${fmtNum(stats(rows.map(r => r.cnt).filter(v => v != null)).mean, 1)}</div></div>
      </div>
      <div class="meta-line">${sampleText(all.n)}</div>
    </div>
    <div class="card"><h2>발주기관별 평균 사정율</h2><p class="sub">건수 많은 순 상위 50곳</p>${gTable(byOrg, '발주기관')}</div>
    <div class="card"><h2>면허별 평균 사정율</h2><p class="sub">면허가 여러 개인 공고는 각 면허에 모두 포함</p>${gTable(byLic, '면허')}</div>`;

  if(!Data.hasDetail(sido)){
    html += `<div class="card"><h2>예가 번호 추첨 빈도</h2>${noDetailHtml(sido)}</div>
             <div class="card"><h2>경쟁사</h2>${noDetailHtml(sido)}</div>`;
    el.innerHTML = html;
    return;
  }
  el.innerHTML = html + `<div class="card">${loadingHtml('개찰 상세 불러오는 중…')}</div>`;
  const op = await Data.loadOpening(sido);
  if(token !== statsToken) return;

  // 예가 번호 빈도 + 경쟁사
  const freq = new Array(16).fill(0);
  let nDraw = 0;
  const corps = new Map();
  let nRank = 0;
  for(const [id, b] of op.bids){
    const r = sc.byId.get(id);
    if(!r || (cut && r.date < cut) || (lic && !(r.lic||[]).includes(lic))) continue;
    const drawn = (b.p||[]).filter(x => x[2]);
    if(drawn.length){ nDraw++; drawn.forEach(x => { if(x[0] >= 1 && x[0] <= 15) freq[x[0]]++; }); }
    const base = b.base || r.base, floor = r.floor || DEFAULT_FLOOR, a = r.a || 0;
    if(!b.r?.length) continue;
    nRank++;
    for(const row of b.r){
      const [rank, ci, amt] = row;
      const [name, biz] = b.corps[ci] || ['?', ''];
      const key = biz || name;
      let c = corps.get(key);
      if(!c) corps.set(key, c = {name, biz, n:0, wins:0, srs:[]});
      c.n++;
      if(r.winBiz ? biz === r.winBiz : rank === 1) c.wins++;
      if(base && amt){
        const sr = ((amt - a) / (floor/100) + a) / base * 100;
        if(sr > 90 && sr < 110) c.srs.push(sr);
      }
    }
  }
  const freqItems = freq.slice(1).map((c, i) => ({label: String(i+1), v: nDraw ? c / nDraw : 0}));
  const top = [...corps.values()].sort((a,b) => b.n - a.n).slice(0, 20);
  top.forEach(c => c.st = stats(c.srs));
  el.innerHTML = html + `
    <div class="card">
      <h2>예가 번호(1~15) 추첨 빈도</h2>
      <p class="sub">번호별로 4개 추첨에 뽑힌 비율. 완전 무작위라면 모두 26.7%입니다.</p>
      ${nDraw ? catBars(freqItems, {valueFmt: v => (v*100).toFixed(1) + '%', refLine:{v:4/15, color:'var(--target)', label:'기대값 26.7%'}}) : '<div class="empty">추첨 기록 없음</div>'}
      <div class="meta-line">${sampleText(nDraw)}</div>
    </div>
    <div class="card">
      <h2>경쟁사 상위 20곳</h2>
      <p class="sub">참여가 많은 순. 투찰 사정률 = 투찰금액을 예정가격으로 되돌린 값 ÷ 기초금액. 누르면 분포를 봅니다.</p>
      <div class="table-wrap" style="max-height:none;"><table>
        <thead><tr><th>#</th><th>업체</th><th class="num">참여</th><th class="num">낙찰</th><th class="num">평균 투찰 사정률</th><th class="num">표준편차</th></tr></thead>
        <tbody>${top.map((c,i) => `<tr class="click" data-corp="${i}"><td>${i+1}</td><td>${esc(c.name)}${sampleBadge(c.st.n)}</td><td class="num">${fmtNum(c.n)}</td><td class="num">${fmtNum(c.wins)}</td><td class="num">${pct(c.st.mean)}</td><td class="num">${isFinite(c.st.std) ? c.st.std.toFixed(3) : '-'}</td></tr>`).join('') || '<tr><td colspan="6" class="empty">데이터 없음</td></tr>'}</tbody>
      </table></div>
      <div class="meta-line">개찰 순위 ${sampleText(nRank)}</div>
      <div id="corpDetail"></div>
    </div>`;
  el.querySelectorAll('tr[data-corp]').forEach(tr => tr.addEventListener('click', () => {
    const c = top[+tr.dataset.corp];
    const s = [...c.srs].sort((a,b) => a-b);
    $('corpDetail').innerHTML = `<h3>${esc(c.name)} 투찰 사정률 분포</h3>
      ${s.length ? histogram(c.srs, {min: Math.floor(quantile(s,.01)*2)/2, max: Math.ceil(quantile(s,.99)*2)/2 || 101, step:0.1,
         lines:[{x:c.st.mean, color:'var(--primary-dark)', label:`평균 ${c.st.mean.toFixed(2)}`}]}) : '<div class="empty">계산 가능한 투찰 없음</div>'}
      <div class="meta-line">${sampleText(c.srs.length)} · 낙찰 ${fmtNum(c.wins)}회 / 참여 ${fmtNum(c.n)}회${c.biz ? ` · 사업자번호 ${esc(c.biz)}` : ''}</div>`;
    $('corpDetail').scrollIntoView({behavior:'smooth', block:'nearest'});
  }));
}

// ============================================================ 설정
async function renderSettings(){
  const m = Data.meta;
  const bf = m.backfill || {};
  const ymd = (s) => s ? `${s.slice(0,4)}-${s.slice(4,6)}-${s.slice(6,8)}` : '-';
  const sc = m.counts?.scsbid || {};
  $('setData').innerHTML = `<dl class="kv">
      <dt>데이터 갱신</dt><dd>${esc((m.updated_at || '아직 없음').slice(0,16).replace('T',' '))}</dd>
      <dt>마지막 수집 실행</dt><dd>${esc((m.last_run?.at || '-').slice(0,16).replace('T',' '))}${m.last_run?.errors?.length ? ` <span class="badge">오류 ${m.last_run.errors.length}</span>` : ''}</dd>
      <dt>진행중 공고</dt><dd>${fmtNum(m.counts?.bids || 0)}건</dd>
      <dt>과거 낙찰</dt><dd>${fmtNum(m.counts?.scsbid_total || 0)}건</dd>
      <dt>과거 수집 범위</dt><dd>${bf.done ? '완료' : '진행 중'} · ${ymd(bf.oldest)} ~ 오늘 (목표 ${ymd(bf.target_start)}부터)</dd>
    </dl>
    ${Object.keys(sc).length ? `<h3>시·도별 낙찰 건수</h3><div class="chip-group">${SIDOS.concat(['기타']).filter(s => sc[s]).map(s => `<span class="chip sm" style="cursor:default;">${s} ${fmtNum(sc[s])}</span>`).join('')}</div>` : ''}
    ${m.last_run?.errors?.length ? `<div class="alert warn">${m.last_run.errors.map(esc).join('<br>')}</div>` : ''}`;
  const d = m.detail || {};
  const regions = d.regions || [];
  $('setDetail').innerHTML = regions.length ? regions.map(s => {
    const x = d[s] || {};
    const p = x.total ? (x.done / x.total * 100) : 0;
    return `<div style="margin-bottom:12px;"><div class="card-head"><b>${esc(s)}</b><span class="meta-line" style="margin:0;">${fmtNum(x.done||0)} / ${fmtNum(x.total||0)}건 (${p.toFixed(1)}%)${x.failed ? ` · 실패 ${fmtNum(x.failed)}` : ''}</span></div>
      <div class="progress"><div style="width:${p.toFixed(1)}%"></div></div></div>`;
  }).join('') + `<div class="meta-line">남은 ${fmtNum(d.remaining || 0)}건 · 하루 약 ${fmtNum(d.per_day || 0)}건 → ${d.remaining ? `약 ${fmtNum(d.eta_days)}일 남음` : '완료'}</div>
    <div class="meta-line">수집 대상 지역은 저장소의 scripts/regions.json 에서 바꿀 수 있습니다.</div>`
    : '<div class="empty">아직 상세 수집 기록이 없습니다.</div>';
  $('appVersion').textContent = await getAppVersion();
  const theme = LS.get('theme', 'auto');
  $('themeSeg').querySelectorAll('button').forEach(b => b.classList.toggle('on', b.dataset.v === theme));
}

function initSettings(){
  fillSelect($('btSido'), SIDOS, {all:'시·도 선택', value: Data.meta.detail?.regions?.[0] || ''});
  $('btRun').addEventListener('click', runBacktest);
  $('themeSeg').addEventListener('click', (e) => {
    const v = e.target.dataset?.v; if(!v) return;
    LS.set('theme', v); applyTheme(); renderSettings();
  });
  $('clearCache').addEventListener('click', async () => {
    if('caches' in window) for(const k of await caches.keys()) if(k.startsWith('data')) await caches.delete(k);
    location.reload();
  });
}

async function runBacktest(){
  const out = $('btResult');
  const sido = $('btSido').value, N = Math.max(10, Math.min(2000, +$('btN').value || 100));
  if(!sido || !Data.hasScsbid(sido)){ out.innerHTML = '<div class="empty">낙찰 데이터가 있는 시·도를 선택하세요.</div>'; return; }
  out.innerHTML = loadingHtml('계산 중…');
  const sc = await Data.loadScsbid(sido);
  await new Promise(r => setTimeout(r, 20));
  const usable = sc.recs.filter(r => r.sr != null && r.base && r.amt && r.plan);   // 최근 순
  const tests = usable.slice(0, N);
  const res = [];
  for(const t of tests){
    const from = addMonths(t.date, -12);
    let pool = usable.filter(r => r.date < t.date && r.date >= from);
    if(t.lic?.length){
      const lic = new Set(t.lic);
      const wl = pool.filter(r => (r.lic||[]).some(l => lic.has(l)));
      if(wl.length >= 10) pool = wl;
    }
    if(pool.length < 5) continue;
    const pred = stats(pool.map(r => r.sr)).mean;
    const floor = t.floor || DEFAULT_FLOOR, a = t.a || 0;
    const rec = bidAmount(t.base, pred, a, floor);
    const floorPrice = Math.ceil((t.plan - a) * floor / 100 + a);
    res.push({t, pred, n: pool.length, rec, below: rec < floorPrice, win: rec >= floorPrice && rec < t.amt, err: Math.abs(pred - t.sr)});
  }
  if(!res.length){ out.innerHTML = '<div class="empty">백테스트할 수 있는 과거 데이터가 부족합니다.</div>'; return; }
  const rate = res.filter(r => r.win).length / res.length;
  const below = res.filter(r => r.below).length / res.length;
  const err = stats(res.map(r => r.err)).mean;
  out.innerHTML = `<div class="stat-grid">
      <div class="stat"><div class="t">성공 비율</div><div class="v" style="color:var(--primary-dark);">${(rate*100).toFixed(1)}%</div></div>
      <div class="stat"><div class="t">하한 미달 비율</div><div class="v">${(below*100).toFixed(1)}%</div></div>
      <div class="stat"><div class="t">1위보다 높았던 비율</div><div class="v">${((1-rate-below)*100).toFixed(1)}%</div></div>
      <div class="stat"><div class="t">평균 오차(사정율)</div><div class="v">${err.toFixed(3)}%p</div></div>
    </div>
    <div class="meta-line">${sampleText(res.length)} (요청 ${N}건 중 과거 표본 5건 이상인 공고) · 각 공고는 개찰일 이전 12개월, 같은 면허 데이터만 사용</div>
    <div class="meta-line">성공 = 추천 투찰가 ≥ 실제 낙찰하한가 그리고 &lt; 실제 1위 낙찰금액</div>
    <div class="table-wrap" style="margin-top:10px;"><table>
      <thead><tr><th>개찰일</th><th>공고명</th><th class="num">예측</th><th class="num">실제</th><th>결과</th></tr></thead>
      <tbody>${res.slice(0, 200).map(r => `<tr><td>${esc(r.t.date)}</td><td class="wrap">${esc(r.t.nm)}</td><td class="num">${pct(r.pred)}</td><td class="num">${pct(r.t.sr)}</td><td>${r.win ? '<span class="badge ok">성공</span>' : r.below ? '<span class="badge">하한 미달</span>' : '<span class="badge gray">1위보다 높음</span>'}</td></tr>`).join('')}</tbody>
    </table></div>`;
}

// ============================================================ 테마 · PWA
function applyTheme(){
  const t = LS.get('theme', 'auto');
  if(t === 'auto') document.documentElement.removeAttribute('data-theme');
  else document.documentElement.setAttribute('data-theme', t);
}

async function getAppVersion(){
  try{
    const ctl = navigator.serviceWorker?.controller;
    if(ctl){
      const v = await new Promise((res, rej) => {
        const ch = new MessageChannel();
        ch.port1.onmessage = (e) => res(e.data);
        ctl.postMessage({type:'GET_VERSION'}, [ch.port2]);
        setTimeout(() => rej(new Error('timeout')), 1500);
      });
      if(v) return v;
    }
  }catch(e){}
  try{
    const txt = await (await fetch('sw.js', {cache:'no-store'})).text();
    return (txt.match(/VERSION\s*=\s*['"]([^'"]+)/) || [])[1] || '-';
  }catch(e){ return '-'; }
}

function initServiceWorker(){
  if(!('serviceWorker' in navigator)) return;
  const show = (worker) => {
    $('updateBar').hidden = false;
    $('updateBtn').onclick = () => worker.postMessage({type:'SKIP_WAITING'});
  };
  navigator.serviceWorker.register('sw.js').then(reg => {
    if(reg.waiting && navigator.serviceWorker.controller) show(reg.waiting);
    reg.addEventListener('updatefound', () => {
      const w = reg.installing;
      w?.addEventListener('statechange', () => { if(w.state === 'installed' && navigator.serviceWorker.controller) show(w); });
    });
    setInterval(() => reg.update().catch(() => {}), 30 * 60 * 1000);
  }).catch(e => console.warn('SW 등록 실패', e));
  let reloading = false;
  navigator.serviceWorker.addEventListener('controllerchange', () => {
    if(reloading) return; reloading = true; location.reload();
  });
}

// ============================================================ 시작
async function init(){
  applyTheme();
  $('todayLabel').textContent = new Date().toLocaleDateString('ko-KR', {year:'numeric', month:'long', day:'numeric', weekday:'short'});
  const lv = LS.get('lastVisit', null);
  prevVisit = lv ? new Date(lv) : null;
  initServiceWorker();
  await Data.loadMeta();

  document.querySelectorAll('.nav-item').forEach(b => b.addEventListener('click', () => switchTab(b.dataset.tab)));
  window.addEventListener('popstate', () => switchTab(location.hash.slice(1), false));
  document.addEventListener('click', (e) => {
    const w = e.target.closest('[data-watch]');
    if(w){ toggleWatch(w.dataset.watch, w); return; }
    const u = e.target.closest('[data-unwatch]');
    if(u){ WatchStore.remove(u.dataset.unwatch).then(renderWatch); return; }
    const p = e.target.closest('[data-predict]');
    if(p){ predictWithNotice(p.dataset.predict); }
  });

  initBidsFilters();
  initPredict();
  initStats();
  initSettings();
  switchTab(location.hash.slice(1) || 'bids', false);
  Data.loadBids().then(() => { if(currentTab !== 'bids') updateNewBadge(); });
}

init();
