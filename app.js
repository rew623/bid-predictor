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
/** 면허 → 나라장터 업종코드 (공고 원문 "금속창호ㆍ지붕건축물조립공사업/4991" 에서 확인). 실시간 검색은 이름 대신 코드로 보낸다 */
const LIC_CODES = {
  '토목':'0001', '건축':'0002', '토목건축':'0003', '조경':'0005', '산업환경설비':'1449',
  '지반조성·포장':'4989', '실내건축':'4990', '금속창호·지붕건축물조성':'4991', '도장·습식·방수·석공':'4992',
  '조경식재·시설물':'4993', '철근·콘크리트':'4994', '구조물해체·비계':'4995', '상하수도설비':'4996',
  '철도·궤도':'4997', '철강구조물':'4998', '수중·준설':'4999', '승강기·삭도':'6201', '기계설비':'6202',
  '정보통신':'0036', '전기':'0037', '소방시설':'0040',
};
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

// ---------- 금액 입력칸: 쉼표를 넣어 보여주고, 읽을 때는 숫자만 (class="money")
const numOf = (el) => { const v = +String(el?.value ?? '').replace(/[^\d.]/g, ''); return isFinite(v) ? v : 0; };
const moneyText = (v) => (v === '' || v == null || !isFinite(+v) || +v === 0) ? '' : Math.round(+v).toLocaleString('ko-KR');
function setMoney(el, v){ if(el) el.value = moneyText(v); }
/** 입력하는 동안 쉼표 다시 찍기 (커서는 앞쪽 숫자 개수 기준으로 되돌림) */
function formatMoneyInput(el){
  const pos = el.selectionStart ?? el.value.length;
  const digitsBefore = el.value.slice(0, pos).replace(/\D/g, '').length;
  const digits = el.value.replace(/\D/g, '').replace(/^0+(?=\d)/, '');
  el.value = digits ? (+digits).toLocaleString('ko-KR') : '';
  let i = 0, seen = 0;
  while(i < el.value.length && seen < digitsBefore){ if(/\d/.test(el.value[i])) seen++; i++; }
  try{ el.setSelectionRange(i, i); }catch(e){}
}
document.addEventListener('input', (e) => { if(e.target.matches?.('input.money')) formatMoneyInput(e.target); }, true);

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
  /** 수집기가 만든 공고별 면허제한·참가가능지역 (최근 60일, 공사) → Map(공고ID → [면허]), Map(공고ID → [지역 원문]) */
  loadLicMap(){
    return this.once('licmap', async () => {
      try{
        const j = await this.fetchJson('lic_map.json');
        this.licMap = new Map(Object.entries(j.items || {}).map(([id, idx]) => [id, idx.map(i => j.lic[i])]));
        this.rgnMap = new Map(Object.entries(j.rgn || {}).map(([id, idx]) => [id, idx.map(i => j.rg[i])]));
      }catch(e){ this.licMap = new Map(); this.rgnMap = new Map(); }
      return this.licMap;
    });
  },
  loadModel(){
    return this.once('model', async () => {
      try{ Model.m = await this.fetchJson('model.json'); }catch(e){ console.warn('model', e); Model.m = null; }
      return Model.m;
    });
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

const sameRng = (a, b) => !!(a && b && a[0] === b[0] && a[1] === b[1]);
const rngText = (rng) => !rng ? '' : (rng[0] != null && rng[1] != null && -rng[0] === rng[1]) ? `±${rng[1]}%` : `${rng[0] ?? '?'}~+${rng[1] ?? '?'}%`;

function filterRecords(recs, o){
  const cut = o.recent ? monthsAgo(12) : '';
  return recs.filter(r =>
    r.sr != null &&
    (!o.sggs?.size || o.sggs.has(r.sgg)) &&
    (!o.lics?.size || (r.lic || []).some(l => o.lics.has(l))) &&
    (!o.rng || sameRng(r.rng, o.rng)) &&
    (!o.org || recOrg(r) === o.org) &&
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

// ---------- 낙찰확률 (투찰 사정률 기준) — 설계: CLAUDE.md "낙찰확률 곡선"
/** 투찰금액 → 투찰 사정률 x = ((투찰금액 − A) ÷ 낙찰하한율 + A) ÷ 기초금액 × 100.
 *  x ≥ 실제 사정율 S ⟺ 투찰금액 ≥ 낙찰하한가. bidAmount 의 역함수. */
function bidToSr(amt, base, a, floor){
  if(!amt || !base) return null;
  a = a || 0; floor = floor || DEFAULT_FLOOR;
  const x = ((amt - a) / (floor / 100) + a) / base * 100;
  return x > 90 && x < 110 ? x : null;
}

/** 공고 한 건의 승리 구간 [S, W): 내가 투찰 사정률 x 로 썼다면 S ≤ x < W 일 때 낙찰(다른 업체는 그대로라고 가정).
 *  S = 예정가격 ÷ 기초금액, W = 실제 낙찰자의 투찰 사정률.
 *  개찰 상세(op)가 있으면 정확한 예정가격과 "x ≥ S 인 투찰 중 최소"를 W 로 쓴다 (적격심사 탈락한 1순위 보정). */
function winWindow(r, op){
  const base = op?.base || r.base, plan = op?.plan || r.plan;
  if(!base || !plan) return null;
  const S = plan / base * 100;
  const a = r.a || 0, floor = r.floor || DEFAULT_FLOOR;
  let W = null;
  if(op?.r?.length){
    for(const row of op.r){
      const x = bidToSr(row[2], base, a, floor);
      if(x != null && x >= S && (W == null || x < W)) W = x;
    }
  }
  if(W == null && r.amt) W = bidToSr(r.amt, base, a, floor);
  if(W == null || !(W - S >= 0 && W - S < 1)) return null;
  return {S, W};
}

const MONTH_MS = 2629746000;
/** 가중치: 최근일수록 exp(−경과개월/12), 기준 시·도와 같으면 ×2 */
function recWeight(r, now, sido){
  const age = Math.max(0, (now - (parseKst(r.date)?.getTime() ?? now)) / MONTH_MS);
  return Math.exp(-age / 12) * (sido && r.sido === sido ? 2 : 1);
}
const median = (vals) => { const s = vals.filter(v => v != null && isFinite(v)).sort((a, b) => a - b); return s.length ? quantile(s, .5) : null; };

// 97.000 ~ 103.000% 를 0.001 간격(6,001칸)으로 본다
const G0 = 97, GS = 0.001, GN = 6000;
const gIdx = (x) => Math.round((x - G0) / GS);
const gX = (i) => +(G0 + i * GS).toFixed(3);
const curveSmooth = () => LS.get('curveSmooth', 0.01);

/** 낙찰확률 곡선. 과거 공고마다 승리 구간 [S, W) 를 차분 배열로 쌓고 누적합 ÷ 가중치 합 = 그 x 의 과거 낙찰 확률.
 *  ±smooth %p 이동평균으로 잡음을 줄여 최대점(추천 x*)을 고른다. */
function winCurve(rows, {now=Date.now(), sido=null, opening=null, smooth=curveSmooth()}={}){
  const diff = new Float64Array(GN + 2);
  let total = 0, n = 0, rnd = 0, rndW = 0;
  const cnts = [], sList = [];
  for(const r of rows){
    const w = winWindow(r, opening?.get(r.id));
    if(!w) continue;
    const wt = recWeight(r, now, sido);
    const i0 = Math.max(0, Math.ceil((w.S - G0) / GS - 1e-9));
    const i1 = Math.min(GN + 1, Math.ceil((w.W - G0) / GS - 1e-9));   // S ≤ x_i < W 인 칸
    if(i1 > i0){ diff[i0] += wt; diff[i1] -= wt; }
    total += wt; n++;
    sList.push([w.S, wt]);
    if(r.cnt){ rnd += wt / r.cnt; rndW += wt; cnts.push(r.cnt); }
  }
  if(n < 5) return null;
  const raw = new Float64Array(GN + 1);
  for(let i = 0, c = 0; i <= GN; i++){ c += diff[i]; raw[i] = c / total; }
  const k = Math.max(0, Math.round(smooth / GS));
  const pre = new Float64Array(GN + 2);
  for(let i = 0; i <= GN; i++) pre[i + 1] = pre[i] + raw[i];
  const sm = new Float64Array(GN + 1);
  for(let i = 0; i <= GN; i++){ const lo = Math.max(0, i - k), hi = Math.min(GN, i + k); sm[i] = (pre[hi + 1] - pre[lo]) / (hi - lo + 1); }
  let bi = 0;
  for(let i = 1; i <= GN; i++) if(sm[i] > sm[bi]) bi = i;
  const at = (x) => { const i = gIdx(x); return i >= 0 && i <= GN ? sm[i] : 0; };
  // 서로 0.05%p 이상 떨어진 상위 봉우리
  const gap = Math.max(50, 3 * k);
  const order = [...sm.keys()].sort((a, b) => sm[b] - sm[a]);
  const peaks = [];
  for(const i of order){
    if(sm[i] <= 0 || peaks.length >= 3) break;
    if(peaks.every(p => Math.abs(p - i) > gap)) peaks.push(i);
  }
  // 안전 범위: 최대점 주변에서 확률이 최대의 90% 이상인 연속 구간
  let lo = bi, hi = bi;
  while(lo > 0 && sm[lo - 1] >= sm[bi] * 0.9) lo--;
  while(hi < GN && sm[hi + 1] >= sm[bi] * 0.9) hi++;
  // 그림 범위: S 분포 1~99% 와 최대점을 포함
  const sSorted = sList.map(v => v[0]).sort((a, b) => a - b);
  const vMin = Math.max(G0, Math.floor(Math.min(quantile(sSorted, .01), gX(bi) - 0.3) * 10) / 10);
  const vMax = Math.min(G0 + GN * GS, Math.ceil(Math.max(quantile(sSorted, .99), gX(bi) + 0.3) * 10) / 10);
  const random = rndW ? rnd / rndW : null;   // 아무 전략 없는 평균 업체의 낙찰 확률 (1 / 참가업체 수)
  return {n, total, smooth, at, raw, sm, random, cntMedian: median(cnts), sList, view: [vMin, vMax],
    best: {x: gX(bi), p: sm[bi]}, peaks: peaks.map(i => ({x: gX(i), p: sm[i]})), safe: {lo: gX(lo), hi: gX(hi)}};
}
/** 곡선의 확률은 표본 공고들의 참가업체 수 기준 → 이 공고의 예상 참가업체 수로 보정 (승리 구간 폭 ≈ 1/참가수) */
const adjustP = (wc, p, nExp) => (wc?.cntMedian && nExp) ? p * wc.cntMedian / nExp : p;
const liftOf = (wc, p) => wc?.random ? p / wc.random : null;

/** 예상 참가업체 수 = 비슷한 과거 공고 cnt 중앙값 (같은 발주기관 → 같은 면허·비슷한 금액 → 같은 지역 순, 5건 이상인 첫 단계) */
function expectedCnt(pool, notice){
  const amt = notice.base || notice.est;
  const lic = new Set(notice.lic || []);
  const org = recOrg(notice);
  const steps = [
    (r) => org && recOrg(r) === org,
    (r) => (!lic.size || (r.lic || []).some(l => lic.has(l))) && (!amt || (r.base && r.base >= amt / 2 && r.base <= amt * 2)),
    () => true,
  ];
  for(const f of steps){
    const c = pool.filter(r => r.cnt && f(r)).map(r => r.cnt);
    if(c.length >= 5) return {n: median(c), k: c.length};
  }
  return null;
}

// ---------- 전국 추천 모델 (data/model.json — scripts/model.py 가 매일 계산, 설계·근거는 CLAUDE.md)
const Model = {m: null};
const rngKeyOf = (rng) => rng ? rng.map(v => +v).join(',') : '';
/** 예상 참가업체 수(로그). 금액대×예가범위 → 시도×금액대 → 시도×금액대×하한율 → 면허 → 시도×면허 → 기관 순으로 수축해 섞는다 (model.py 와 같은 계산) */
function predictLnN(n){
  const M = Model.m?.npred;
  if(!M) return null;
  const base = n.base || n.est;
  const ab = base ? Model.m.amt_edges.filter(e => Math.log10(base) >= e).length : null;
  const fl = n.floor ? String(+n.floor) : '';
  const lic = [...(n.lic || [])].sort().join('+');
  const keys = {o: recOrg(n), l: lic, sl: lic ? `${n.sido || ''}|${lic}` : ''};
  if(ab != null) Object.assign(keys, {a: `${ab}|${rngKeyOf(n.rng)}`, sa: `${n.sido || ''}|${ab}`, saf: `${n.sido || ''}|${ab}|${fl}`});
  let est = M.g;
  for(const name of ['a', 'sa', 'saf', 'l', 'sl', 'o']){
    if(!M.t[name]) continue;
    const [m, c] = (keys[name] && M.t[name][keys[name]]) || [est, 0];
    est = (c * m + M.shrink * est) / (c + M.shrink);
  }
  // 보정층(model.py ADJ_KEYS): 같은 시·군×면허의 과거 참가수 → 참가가능지역 범위
  if(M.adj){
    const sc = rgnScope(rgnOf(n));
    const k2 = {hl: n.sgg ? `${n.sido || ''}|${n.sgg}|${lic}` : '', r: sc, rs: ab != null ? `${sc}|${n.sido || ''}|${ab}` : ''};
    for(const name of ['hl', 'r', 'rs']){
      const [m, c] = (k2[name] && M.adj.t[name]?.[k2[name]]) || [0, 0];
      est += c * m / (c + M.adj.k);
    }
  }
  return est;
}
/** 참가가능지역 → 범위 코드 (model.py rgn_scope 와 같은 규칙). n 모름·제한없음 / g 시·군 하나 / G 시·군 2~4 / s 시·도 하나 / S 시·도 여럿 */
function rgnScope(rgn){
  if(!rgn?.length) return 'n';
  const pairs = new Set(), sidos = new Set();
  let sidoLevel = false;
  for(const t of rgn){
    const {sido, sgg} = parseRegion(t), head = String(t).trim().split(/\s+/)[0];   // 시·도는 원문 첫 단어로 센다
    if(sido) sidos.add(head);
    if(sgg) pairs.add(`${head} ${sgg}`); else if(sido) sidoLevel = true;
  }
  if(pairs.size && !sidoLevel && pairs.size <= 4) return pairs.size === 1 ? 'g' : 'G';
  return sidos.size <= 1 ? 's' : 'S';
}
/** 공고의 참가가능지역: 공고에 없으면(실시간 검색) 수집된 진행중 공고에서 */
let bidRgn = null;
function rgnOf(n){
  if(n.rgn) return n.rgn;
  const m = Data.rgnMap?.get(n.id);
  if(m) return m;
  if(!Data.bids?.length) return null;
  if(!bidRgn || bidRgn.src !== Data.bids) bidRgn = {src: Data.bids, map: new Map(Data.bids.map(b => [b.id, b.rgn]))};
  return bidRgn.map.get(n.id) || null;
}
/** 공고 → 모델 추천. cnt 를 주면 예상 참가수 대신 그 값을 쓴다 */
function modelPredict(n, cnt){
  if(!Model.m || (n.kind && n.kind !== '공사')) return null;
  if(n.live && licOf(n).length) n = {...n, lic: licOf(n)};   // 모델은 면허제한으로 학습 — 실시간 공고의 lic 는 주공종
  const rngKnown = rngKeyOf(n.rng) in Model.m.curves;
  const rng = rngKnown ? n.rng : [-3, 3];
  const ln = cnt ? Math.log(cnt) : predictLnN(n);
  const list = Model.m.curves[rngKeyOf(rng)];
  if(ln == null || !list?.length) return null;
  const k = Math.round(ln * 5) * 2;
  const e = list.reduce((b, x) => Math.abs(x.k - k) < Math.abs(b.k - k) ? x : b, list[0]);
  return {e, rng, rngKnown, nExp: Math.max(1, Math.round(Math.exp(ln))), byInput: !!cnt, meanS: Model.m.meanS[rngKeyOf(rng)]};
}
/** 추천 결과를 한 모양으로: 전국 모델(mp) 또는 이 지역 곡선(wc) */
function recFromModel(mp){
  const e = mp.e, g = Model.m.grid;
  const at = (x) => { const i = Math.round((x - g.x0) / g.step); return i >= 0 && i < e.c.length ? e.c[i] / g.scale : 0; };
  return {src: 'model', x: e.x, p: e.p, sb: e.sb, lo: e.lo, hi: e.hi, peaks: e.pk.map(([x, p]) => ({x, p})), random: e.r, n: e.n, nExp: mp.nExp,
    view: e.v, at, meanS: mp.meanS, rng: mp.rng, rngKnown: mp.rngKnown, byInput: mp.byInput,
    pts: () => { const out = []; for(let x = e.v[0]; x <= e.v[1] + 1e-9; x += g.step) out.push({x: +x.toFixed(2), y: at(x)}); return out; },
    note: `전국 최근 24개월 · 예가범위 ${rngText(mp.rng)}${mp.rngKnown ? '' : '(모름 → ±3% 기준)'} · 예상 참가 ${fmtNum(Math.max(1, Math.round(mp.nExp / Model.m.band)))}~${fmtNum(Math.round(mp.nExp * Model.m.band))}곳 공고 ${fmtNum(e.n)}건`};
}
function recFromLocal(wc, ec){
  return {src: 'local', x: wc.best.x, p: adjustP(wc, wc.best.p, ec?.n), lo: wc.safe.lo, hi: wc.safe.hi,
    peaks: wc.peaks.map(c => ({x: c.x, p: adjustP(wc, c.p, ec?.n)})), random: wc.random, n: wc.n, nExp: ec?.n, view: wc.view,
    at: (x) => adjustP(wc, wc.at(x), ec?.n), pts: () => curvePts(wc), note: '이 지역 과거 공고'};
}
/** 매일 자동 역검증(전국) 요약 배지 */
function valBadge(){
  const t = Model.m?.validation?.total;
  if(!t?.n) return btBadge('');
  const d = t.mean ? (t.near / t.mean - 1) * 100 : 0;
  return `<span class="badge ${d > 0 ? 'ok' : 'warn'}">역검증 ${fmtNum(t.n)}건: 평균 사정율 방식보다 ${d >= 0 ? '+' : ''}${d.toFixed(0)}% ${d >= 0 ? '더' : '덜'} 낙찰</span>`;
}
/** 예상 참가수가 속한 역검증 구간 */
const valSegment = (nExp) => Model.m?.validation?.segments?.find(s => nExp >= s.k[0] && (s.k[1] == null || nExp < s.k[1]));
/** 역검증(표본외) 기준 낙찰확률 = 비슷한 공고의 평균 업체 확률(1/참가수 평균) × 그 경쟁 규모 구간의 역검증 배수.
 *  곡선 최대값(e.p)은 과거 데이터에 맞춘 값이라 새 공고에서는 부풀려져 있다 */
function valWinP(random, nExp){
  const s = nExp ? valSegment(nExp) : null;
  if(!random || !s?.rand || s.n < 300) return null;
  const lift = s.near / s.rand;
  return {p: random * lift, lift, n: s.n};
}

/** 공고 목록용 간단 예측. 평균 사정율: 같은 시도 최근 24개월 → 면허 겹침(10건↑) → 예가범위 같음(30건↑).
 *  낙찰확률 곡선: 같은 시도 최근 24개월 → 예가범위 같음(30건↑) (면허로는 쪼개지 않음). 조건별 결과는 재사용 */
/** 공고 목록용 예측: 전국 모델이 있으면 그걸로(지역 파일 없이도 됨), 없으면 이 지역 곡선 */
function quickPredict(notice){
  if(notice.kind && notice.kind !== '공사') return null;
  const mp = modelPredict(notice);
  if(!mp) return quickPredictLocal(notice);
  const e = mp.e, amt = notice.base || notice.est, v = valWinP(e.r, mp.nExp);
  const winP = v ? v.p : e.p;
  return {sr: quickPredictLocal(notice)?.sr ?? mp.meanS, n: e.n, note: '', model: true, bestSr: e.x, winP,
    lift: v ? v.lift : e.r ? e.p / e.r : null, cnt: mp.nExp, value: amt ? winP * amt : null, bid: bidAmount(notice.base, e.x, notice.a, notice.floor)};
}
const qpCache = new Map();
function quickPredictLocal(notice){
  const store = Data.scsbid[notice.sido];
  if(!store) return null;
  if(notice.kind && notice.kind !== '공사') return null;
  const lics = [...(notice.lic || [])].sort();
  const key = [notice.sido, lics.join(','), (notice.rng || []).join(','), curveSmooth()].join('|');
  if(!qpCache.has(key)){
    const cut = monthsAgo(24);
    const pool = store.recs.filter(r => r.sr != null && (r.date||'') >= cut);
    let rows = pool;
    const notes = [];
    if(lics.length){
      const lic = new Set(lics);
      const withLic = rows.filter(r => (r.lic||[]).some(l => lic.has(l)));
      if(withLic.length >= 10) rows = withLic; else notes.push('면허 무관');
    }
    if(notice.rng){
      const withRng = rows.filter(r => sameRng(r.rng, notice.rng));
      if(withRng.length >= MIN_SAMPLE) rows = withRng;
    }
    const p = predictFrom(rows);
    const op = Data.opening[notice.sido]?.bids;
    const rngPool = notice.rng ? pool.filter(r => sameRng(r.rng, notice.rng)) : [];
    const curveRows = rngPool.length >= MIN_SAMPLE ? rngPool : pool;   // 곡선은 면허로 쪼개지 않는다
    qpCache.set(key, p ? {sr: p.mean, n: p.n, note: notes.join(' · '), pool, wc: winCurve(curveRows, {sido: notice.sido, opening: op})} : null);
  }
  const q = qpCache.get(key);
  if(!q) return null;
  const ec = expectedCnt(q.pool, notice);
  const x = q.wc?.best.x;
  const winP = q.wc ? adjustP(q.wc, q.wc.best.p, ec?.n) : null;
  const amt = notice.base || notice.est;
  return {...q, bestSr: x, winP, lift: q.wc ? liftOf(q.wc, q.wc.best.p) : null, cnt: ec?.n,
    value: winP != null && amt ? winP * amt : null,
    bid: bidAmount(notice.base, x ?? q.sr, notice.a, notice.floor)};
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

/** 여러 계열을 같은 가로축에 겹쳐 그린다. 계열마다 자기 최댓값 기준으로 높이를 맞춘다(모양 비교용).
 *  series: [{pts:[{x, y}], color, label, fill, bars}], marks: [{x, color, label}] */
function plot(series, {min, max, h=150, marks=[], xLabel=(x)=>x.toFixed(2)}){
  const W = 360, H = h, X = (x) => (x - min) / (max - min || 1) * W;
  const body = series.filter(s => s.pts.length).map(s => {
    const my = Math.max(maxOf(s.pts.map(p => p.y)), 1e-12);
    const Y = (y) => H - y / my * (H - 26);
    if(s.bars){
      const bw = s.pts.length > 1 ? X(s.pts[1].x) - X(s.pts[0].x) : 4;
      return s.pts.map(p => p.y ? `<rect x="${X(p.x).toFixed(1)}" y="${Y(p.y).toFixed(1)}" width="${Math.max(bw - 0.6, 0.8).toFixed(1)}" height="${(H - Y(p.y)).toFixed(1)}" fill="${s.color}" opacity=".22"/>` : '').join('');
    }
    const d = s.pts.map((p, i) => `${i ? 'L' : 'M'}${X(p.x).toFixed(1)},${Y(p.y).toFixed(1)}`).join('');
    return (s.fill ? `<path d="${d}L${X(s.pts.at(-1).x).toFixed(1)},${H}L${X(s.pts[0].x).toFixed(1)},${H}Z" fill="${s.color}" opacity=".12"/>` : '') +
      `<path d="${d}" fill="none" stroke="${s.color}" stroke-width="1.8" stroke-linejoin="round"/>`;
  }).join('');
  const ms = marks.filter(m => m.x >= min && m.x <= max).map((m, i) => {
    const x = X(m.x).toFixed(1);
    const anchor = X(m.x) > W*0.8 ? 'end' : X(m.x) < W*0.2 ? 'start' : 'middle';
    return `<line x1="${x}" y1="${12 + i*11}" x2="${x}" y2="${H}" stroke="${m.color}" stroke-width="1.5" stroke-dasharray="3,3"/>
      <text x="${x}" y="${9 + i*11}" font-size="9.5" fill="${m.color}" text-anchor="${anchor}" font-weight="600">${esc(m.label)}</text>`;
  }).join('');
  const ticks = [min, (min + max) / 2, max].map((t, i) =>
    `<text x="${X(t).toFixed(1)}" y="${H + 13}" font-size="9.5" fill="var(--text-faint)" text-anchor="${['start','middle','end'][i]}">${xLabel(t)}</text>`).join('');
  const legend = series.filter(s => s.label).map(s => `<span><i style="background:${s.color};${s.bars ? 'opacity:.35' : ''}"></i>${esc(s.label)}</span>`).join('');
  return `<div class="chart"><svg viewBox="0 0 ${W} ${H + 16}" role="img"><line x1="0" y1="${H}" x2="${W}" y2="${H}" stroke="var(--border)"/>${body}${ms}${ticks}</svg></div>
    ${legend ? `<div class="legend">${legend}</div>` : ''}`;
}
/** [값, 가중치] 목록 → 구간별 합 (막대/밀도용) */
function binPts(pairs, min, max, step){
  const n = Math.max(1, Math.round((max - min) / step)), out = Array.from({length: n}, (_, i) => ({x: min + i * step, y: 0}));
  for(const [v, w] of pairs){ if(v >= min && v < max) out[Math.min(n - 1, Math.floor((v - min) / step + 1e-9))].y += (w ?? 1); }
  return out;
}
/** 곡선 → 그림용 점 (0.01%p 간격) */
function curvePts(wc){
  const [a, b] = wc.view, out = [];
  for(let i = gIdx(a); i <= gIdx(b); i += 10) out.push({x: gX(i), y: wc.sm[i]});
  return out;
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
  ({bids: renderBidsTab, predict: renderPredictTab, watch: renderWatch, stats: renderStats, settings: renderSettings})[tab]();
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

const AMT_RANGES = [['0-1','1억 미만'], ['1-3','1~3억'], ['3-10','3~10억'], ['10-50','10~50억'], ['50-','50억 이상']];
const BID_SORTS = [['close','마감 임박순'], ['new','최신 공고순'], ['amtDesc','금액 큰 순'], ['amtAsc','금액 작은 순'], ['win','낙찰확률 높은 순'], ['value','기대 수주액 순']];
const QUICKS = [['all','진행중'], ['today','오늘 마감'], ['d3','3일 이내'], ['new','NEW'], ['watch','관심']];
const WEEKDAYS = ['일','월','화','수','목','금','토'];
let bidsQuick = LS.get('bidsFilter', {}).quick || 'all';

const kstDay = (d) => new Date(d.getTime() + 9 * 3600000).toISOString().slice(0, 10);
/** 오늘(KST) 기준 마감까지 남은 날짜 수 (달력 기준). 마감일 없으면 null */
function closeDays(b, today){
  if(!b.close) return null;
  return Math.round((Date.parse(b.close.slice(0, 10)) - Date.parse(today)) / 86400000);
}
function groupLabel(days, close){
  if(days == null) return '마감일 미정';
  const d = new Date(close.slice(0, 10) + 'T00:00:00');
  const md = `${d.getMonth() + 1}/${d.getDate()} (${WEEKDAYS[d.getDay()]})`;
  return days === 0 ? `오늘 마감 · ${md}` : days === 1 ? `내일 마감 · ${md}` : `D-${days} · ${md}`;
}

function initBidsFilters(){
  const saved = LS.get('bidsFilter', {});
  const defSido = saved.sido ?? (Data.meta.detail?.regions?.[0] || '');
  fillSelect($('bSido'), SIDOS, {all:'시·도 전체', value: defSido});
  fillSelect($('bLic'), LICENSES, {all:'면허 전체', value: saved.lic || ''});
  fillSelect($('bAmt'), AMT_RANGES, {all:'금액 전체', value: saved.amt || ''});
  fillSelect($('bSort'), BID_SORTS, {all:null, value: saved.sort || 'close'});
  fillBidsSgg(saved.sgg || '');
  const onChange = () => { saveBidsFilter(); bidsShown = PAGE_SIZE; renderBids(); };
  $('bSido').addEventListener('change', () => { fillBidsSgg(''); onChange(); });
  $('bElig').checked = saved.elig ?? Company.isSet();
  ['bSgg','bLic','bAmt','bSort','bElig'].forEach(id => $(id).addEventListener('change', onChange));
  let t; $('bQuery').addEventListener('input', () => { clearTimeout(t); t = setTimeout(() => { bidsShown = PAGE_SIZE; renderBids(); }, 200); });
  $('bidsMore').addEventListener('click', () => { bidsShown += PAGE_SIZE; renderBids(); });
  $('bidsKpis').addEventListener('click', (e) => {
    const k = e.target.closest('[data-quick]');
    if(!k) return;
    bidsQuick = k.dataset.quick; onChange();
  });
}
function saveBidsFilter(){
  LS.set('bidsFilter', {sido: $('bSido').value, sgg: $('bSgg').value, lic: $('bLic').value,
    amt: $('bAmt').value, sort: $('bSort').value, quick: bidsQuick, elig: $('bElig').checked});
}
function fillBidsSgg(value){
  const sido = $('bSido').value;
  const sggs = [...new Set((Data.bids||[]).filter(b => b.sido === sido && b.sgg).map(b => b.sgg))].sort();
  fillSelect($('bSgg'), sggs, {all:'시·군·구 전체', value});
  $('bSgg').disabled = !sido;
}

let watchIds = new Set();
async function renderBids(){
  const list = $('bidsList');
  if(!Data.bids){ list.innerHTML = loadingHtml(); await Data.loadBids(); fillBidsSgg(LS.get('bidsFilter', {}).sgg || ''); }
  const sido = $('bSido').value, sgg = $('bSgg').value, lic = $('bLic').value, sort = $('bSort').value;
  const elig = $('bElig').checked && Company.isSet();
  if(elig) await Data.loadLicMap();   // 수집된 면허제한(비건설 자격 포함)으로 판정
  const [aLo, aHi] = ($('bAmt').value || '-').split('-').map(v => v === '' ? null : +v * 1e8);
  const q = $('bQuery').value.trim().toLowerCase();
  const now = new Date(), today = kstDay(now);
  watchIds = new Set((await WatchStore.list()).map(w => w.id));
  const base = Data.bids.filter(b => {
    const amt = b.base || b.est;
    return (!sido || b.sido === sido) && (!sgg || b.sgg === sgg) &&
      (!lic || (b.lic||[]).includes(lic)) &&
      (aLo == null || (amt && amt >= aLo)) && (aHi == null || (amt && amt < aHi)) &&
      (!q || (b.nm||'').toLowerCase().includes(q) || (b.org||'').toLowerCase().includes(q) || (b.dmd||'').toLowerCase().includes(q)) &&
      (!b.close || parseKst(b.close) >= now) && (!elig || eligibility(b).ok);
  });
  const quickFn = {
    all: () => true,
    today: (b) => closeDays(b, today) === 0,
    d3: (b) => { const d = closeDays(b, today); return d != null && d <= 3; },
    new: isNew,
    watch: (b) => watchIds.has(b.id),
  };
  if(!quickFn[bidsQuick]) bidsQuick = 'all';
  $('bidsKpis').innerHTML = QUICKS.map(([k, label]) => {
    const n = base.filter(quickFn[k]).length;
    return `<button class="kpi ${k === bidsQuick ? 'on' : ''} ${k === 'today' && n ? 'red' : ''}" data-quick="${k}" type="button">
      <span class="t">${label}</span><span class="v">${fmtNum(n)}</span></button>`;
  }).join('');
  const rows = base.filter(quickFn[bidsQuick]);
  // 방문 기록
  LS.set('lastVisit', now.toISOString());
  LS.set('bidsSeenAt', Date.now());
  $('newBadge').hidden = true;

  const infoBase = Data.meta.updated_at ? `데이터 ${esc(Data.meta.updated_at.slice(0,16).replace('T',' '))} 기준` : '';
  if(!rows.length){
    $('bidsInfo').innerHTML = infoBase;
    list.innerHTML = `<div class="empty card">${Data.bids.length ? '조건에 맞는 진행중 공고가 없습니다.' : '아직 수집된 공고가 없습니다. 데이터 수집이 실행되면 표시됩니다.'}</div>`;
    $('bidsMore').hidden = true;
    return;
  }
  if(sido && Data.hasScsbid(sido) && !Data.scsbid[sido]){
    list.innerHTML = loadingHtml(`${sido} 낙찰 데이터 불러오는 중…`);
    try{ await Data.loadScsbid(sido); if(Data.hasDetail(sido)) await Data.loadOpening(sido); }catch(e){ console.warn(e); }
    if(currentTab !== 'bids' || $('bSido').value !== sido) return;
  }
  const canWin = !!(Model.m || (sido && Data.scsbid[sido]));
  const amtOf = (b) => b.base || b.est || 0;
  const cmp = {
    close: (a, b) => (a.close || '9999').localeCompare(b.close || '9999'),
    new: (a, b) => (b.ntce || b.seen || '').localeCompare(a.ntce || a.seen || ''),
    amtDesc: (a, b) => amtOf(b) - amtOf(a),
    amtAsc: (a, b) => (amtOf(a) || Infinity) - (amtOf(b) || Infinity),
    win: (a, b) => (quickPredict(b)?.winP ?? -1) - (quickPredict(a)?.winP ?? -1),
    value: (a, b) => (quickPredict(b)?.value ?? -1) - (quickPredict(a)?.value ?? -1),
  };
  const needPred = sort === 'win' || sort === 'value';
  rows.sort(cmp[needPred && !canWin ? 'close' : sort] || cmp.close);
  $('bidsInfo').innerHTML = [`${fmtNum(rows.length)}건`, infoBase,
    !sido && !Model.m ? '시·도를 고르면 예상 사정율·낙찰확률이 표시됩니다' : '',
    needPred && !canWin ? '낙찰확률·기대 수주액 정렬은 낙찰 데이터가 있는 시·도를 골라야 적용됩니다' : ''].filter(Boolean).join(' · ');

  const shown = rows.slice(0, bidsShown);
  const groupCount = {};
  if(sort === 'close') rows.forEach(x => { const g = groupLabel(closeDays(x, today), x.close); groupCount[g] = (groupCount[g] || 0) + 1; });
  let html = '', lastGroup = null;
  for(const b of shown){
    if(sort === 'close'){
      const g = groupLabel(closeDays(b, today), b.close);
      if(g !== lastGroup){
        html += `<div class="b-group"><span>${esc(g)}</span><span class="cnt">${fmtNum(groupCount[g])}건</span></div>`;
        lastGroup = g;
      }
    }
    html += bidCard(b, today);
  }
  list.innerHTML = html;
  $('bidsMore').hidden = rows.length <= bidsShown;
  $('bidsMore').textContent = `더 보기 (${fmtNum(rows.length - bidsShown)}건 남음)`;
}

/** 카드의 면허 태그: 공고가 요구하는 면허(면허제한). 실시간 공고는 수집된 값 또는 조달청 직접 조회 값 */
function licTags(b){
  const lics = licOf(b);
  if(lics.length){
    const mine = new Set(Company.isSet() ? Company.get().lics : []);
    return [`<span class="tag">요구 면허</span>`, ...lics.map(l => `<span class="tag${!mine.size ? ' lic' : mine.has(l) ? ' lic mine' : ''}">${mine.has(l) ? '✓ ' : ''}${esc(l)}</span>`)];
  }
  if(!b.live) return [];
  if(b.limOk) return ['<span class="tag">면허 제한 없음</span>'];
  return [b.limTried ? '<span class="tag">요구 면허 조회 실패</span>' : '<span class="tag">요구 면허 조회 중…</span>'];
}
function bidCard(b, today){
  const days = closeDays(b, today);
  const hh = b.close?.length > 10 ? b.close.slice(11, 16) : '';
  const dd = days == null ? {big:'-', small:'마감 미정', cls:''}
    : days < 0 || parseKst(b.close) < new Date() ? {big:'마감', small: b.close.slice(2, 10).replace(/-/g, '.'), cls:'closed'}
    : days === 0 ? {big:'오늘', small: hh ? `${hh} 마감` : '마감', cls:'today'}
    : {big:`D-${days}`, small: `${b.close.slice(5,10).replace('-','/')} ${hh}`, cls: days <= 2 ? 'soon' : ''};
  const qp = quickPredict(b);
  const amt = b.base || b.est;
  const tags = [
    `<span class="tag loc">${esc([b.sido, b.sgg].filter(Boolean).join(' ') || '지역 미상')}</span>`,
    b.sui ? '<span class="tag warn" title="수의계약(견적) — 추천값은 경쟁입찰 과거 공고 기준">수의</span>' : '',
    b.corr ? '<span class="tag warn" title="정정공고 — 바뀐 내용을 원문에서 확인">정정</span>' : '',
    ...licTags(b),
    b.rng ? `<span class="tag">예가 ${esc(rngText(b.rng))}</span>` : '',
    b.floor ? `<span class="tag">하한 ${b.floor}%</span>` : '',
    eligTag(b),
  ].join('');
  let pred = '';
  if(qp){
    pred = `<div class="b-pred">
        <div class="pv"><span>예상 사정율</span><b>${pct(qp.sr, 3)}</b></div>
        ${qp.bestSr != null ? `<div class="pv hl"><span>추천 투찰 사정률</span><b>${pct(qp.bestSr, 3)}</b></div>
        <div class="pv"><span>예상 낙찰확률</span><b>${(qp.winP * 100).toFixed(2)}%${qp.lift ? ` <small class="lift">×${qp.lift.toFixed(1)}</small>` : ''}</b></div>` : ''}
        <div class="pv"><span>추천 투찰가</span><b>${qp.bid ? won(qp.bid) : '기초금액 미공개'}</b></div>
        ${qp.cnt ? `<div class="pv"><span>예상 참가</span><b>~${fmtNum(qp.cnt)}개사</b></div>` : ''}
      </div>
      <div class="b-note">${sampleText(qp.n)}${qp.note ? ` · ${esc(qp.note)}` : ''}${qp.model ? ' · 추천값·낙찰확률은 전국의 경쟁 규모가 비슷한 공고 기준 · ×는 평균 업체 대비' : qp.wc ? ` · 낙찰확률은 과거 ${fmtNum(qp.wc.n)}건 재생, 예상 참가 수 반영 · ×는 무작위 대비` : ''}${qp.value ? ` · 기대 수주액 ${eok(qp.value)}` : ''}</div>`;
  }else if(b.sido && !Data.hasScsbid(b.sido)){
    pred = '<div class="b-note">이 지역 낙찰 데이터 없음</div>';
  }
  const res = b.sido ? Data.scsbid[b.sido]?.byId.get(b.id) : null;
  if(res?.amt) pred += `<div class="b-note result">개찰 결과 · ${res.sr != null ? `사정율 <b>${pct(res.sr, 3)}</b> · ` : ''}1위 ${esc(res.win || '-')} ${won(res.amt)}${res.rate ? ` (${pct(res.rate)})` : ''}${res.cnt ? ` · ${fmtNum(res.cnt)}개사 참가` : ''}</div>`;
  const open = b.open ? `개찰 ${b.open.slice(5, 16).replace('-', '/')}` : '';
  return `<article class="bcard ${dd.cls}">
    <div class="b-dday"><b>${dd.big}</b><span>${esc(dd.small)}</span></div>
    <div class="b-main">
      <div class="b-title">${isNew(b) ? '<span class="new">NEW</span>' : ''}${esc(b.nm)}</div>
      <div class="b-org">${esc(b.org || b.dmd || '')}${b.dmd && b.org && b.dmd !== b.org ? ` <span class="faint">· 수요 ${esc(b.dmd)}</span>` : ''}</div>
      <div class="b-tags">${tags}</div>
    </div>
    <div class="b-side">
      <button class="star ${watchIds.has(b.id) ? 'on' : ''}" data-watch="${esc(b.id)}" title="관심공고" type="button">${watchIds.has(b.id) ? '★' : '☆'}</button>
      <div class="b-amt"><span>${b.base ? '기초금액' : b.est ? '추정가격' : ''}</span><b>${amt ? eok(amt) : '미공개'}</b></div>
    </div>
    ${pred}
    <div class="b-actions">
      <button class="btn sm" data-predict="${esc(b.id)}" type="button">이 공고로 예측</button>
      ${b.url ? `<a class="btn line sm" href="${esc(b.url)}" target="_blank" rel="noopener">공고 원문</a>` : ''}
      <span class="b-meta">${esc([b.no ? `${b.no}-${b.ord}` : '', open].filter(Boolean).join(' · '))}</span>
    </div>
  </article>`;
}

// ============================================================ 실시간 공고 검색 (조달청 API 를 브라우저에서 직접 호출, 서비스키는 이 기기에만 저장)
const LIVE_BASE = 'https://apis.data.go.kr/1230000/ad/BidPublicInfoService';
const LIVE_ROWS = 100;
// 업무구분별 오퍼레이션: [나라장터 검색조건 조회, 기본 목록 조회(검색조건이 안 될 때 대신), 기초금액 조회]
const LIVE_KINDS = {
  '공사': ['getBidPblancListInfoCnstwkPPSSrch', 'getBidPblancListInfoCnstwk', 'getBidPblancListInfoCnstwkBsisAmount'],
  '용역': ['getBidPblancListInfoServcPPSSrch', 'getBidPblancListInfoServc', 'getBidPblancListInfoServcBsisAmount'],
  '물품': ['getBidPblancListInfoThngPPSSrch', 'getBidPblancListInfoThng', 'getBidPblancListInfoThngBsisAmount'],
  '외자': ['getBidPblancListInfoFrgcptPPSSrch', 'getBidPblancListInfoFrgcpt', null],
  '기타': ['getBidPblancListInfoEtcPPSSrch', 'getBidPblancListInfoEtc', null],
};
const Live = {items: [], raw: 0, page: 0, params: null, token: 0, kind: '공사', fallback: false, wins: [], wi: 0, totals: []};
let bidsMode = LS.get('bidsMode', null);
function apiKey(){
  const k = String(LS.get('apiKey', '') || '').trim();
  try{ return k.includes('%') ? decodeURIComponent(k) : k; }catch(e){ return k; }   // 인코딩 키를 넣어도 동작
}
const findNotice = (id) => Data.bids?.find(x => x.id === id) || Live.items.find(x => x.id === id);

async function liveCall(op, params, base=LIVE_BASE){
  const q = new URLSearchParams({serviceKey: apiKey(), type: 'json', ...params});
  const r = await fetch(`${base}/${op}?${q}`);
  const text = await r.text();
  let j;
  try{ j = JSON.parse(text); }
  catch(e){
    const m = text.match(/<returnAuthMsg>(.*?)<\/returnAuthMsg>|<resultMsg>(.*?)<\/resultMsg>/);
    throw new Error(m ? (m[1] || m[2]) : `HTTP ${r.status}`);
  }
  const auth = j.OpenAPI_ServiceResponse?.cmmMsgHeader;
  if(auth) throw new Error(auth.returnAuthMsg || auth.errMsg || '서비스키 오류');
  const root = j.response || j, code = String(root.header?.resultCode ?? '00');
  if(['03', 'INFO-200'].includes(code)) return {items: [], total: 0};
  if(!['00', '0', '000', 'INFO-000'].includes(code)) throw new Error(`${root.header?.resultMsg || '조회 오류'} (${code})`);
  let items = root.body?.items;
  if(items && !Array.isArray(items)) items = items.item ?? items;
  if(items && !Array.isArray(items)) items = [items];
  return {items: (items || []).filter(x => x && typeof x === 'object'), total: +(root.body?.totalCount || 0)};
}

// API 응답 → 공고 항목 (scripts/collect.py 의 F_* 후보와 같은 이름들)
const pickF = (it, ...ks) => { for(const k of ks){ const v = it[k]; if(v != null && String(v).trim() !== '') return v; } return null; };
const numF = (v) => { if(v == null || String(v).trim() === '') return null; const n = +String(v).replace(/[,%원\s]/g, ''); return isFinite(n) ? n : null; };
function normDt(v){
  const d = String(v || '').replace(/\D/g, '');
  return d.length >= 12 ? `${d.slice(0,4)}-${d.slice(4,6)}-${d.slice(6,8)} ${d.slice(8,10)}:${d.slice(10,12)}`
    : d.length >= 8 ? `${d.slice(0,4)}-${d.slice(4,6)}-${d.slice(6,8)}` : null;
}
const SIDO_PREFIX = [['서울','서울'],['부산','부산'],['대구','대구'],['인천','인천'],['광주','광주'],['대전','대전'],['울산','울산'],['세종','세종'],
  ['경기','경기'],['강원','강원'],['충청북','충북'],['충북','충북'],['충청남','충남'],['충남','충남'],['전라북','전북'],['전북','전북'],
  ['전라남','전남'],['전남','전남'],['경상북','경북'],['경북','경북'],['경상남','경남'],['경남','경남'],['제주','제주']];
function parseRegion(...texts){
  for(const t of texts){
    const s = String(t || '').trim();
    const hit = SIDO_PREFIX.find(([k]) => s.startsWith(k));
    if(hit) return {sido: hit[1], sgg: (s.split(/\s+/)[1] || '').match(/^\S+[시군구]$/)?.[0]};
  }
  return {};
}
// 면허 이름 맞추기: 코드(/4991)·구분점(· ㆍ .)·"공사업/사업/업"을 떼고 비교. 나라장터는 "금속창호ㆍ지붕건축물조립공사업"으로 쓴다
const licKey = (s) => String(s).split('/')[0].replace(/[·ㆍ.,\s]/g, '').replace('지붕건축물조립', '지붕건축물조성');
const LIC_BY_KEY = new Map(LICENSES.map(l => [licKey(l), l]));
function normLic(raw){
  if(!raw) return undefined;
  const k = licKey(raw);
  const hit = [k, k.replace(/공사업$/, ''), k.replace(/사업$/, ''), k.replace(/업$/, '')].map(x => LIC_BY_KEY.get(x)).find(Boolean);
  return [hit || String(raw).split('/')[0].trim()];
}
function liveNotice(it){
  const no = String(pickF(it, 'bidNtceNo') || '').trim(), ord = String(pickF(it, 'bidNtceOrd') ?? '000').trim();
  const org = pickF(it, 'ntceInsttNm'), dmd = pickF(it, 'dminsttNm');
  const {sido, sgg} = parseRegion(pickF(it, 'cnstrtsiteRgnNm', 'cnstrtSiteRgnNm', 'cnstwkSiteRgnNm'), dmd, org);
  const b = {id: `${no}-${ord}`, no, ord, nm: String(pickF(it, 'bidNtceNm', 'cnstwkNm') || '').trim(), org, dmd, sido, sgg,
    lic: [...new Set(['mainCnsttyNm', ...Array.from({length: 9}, (_, i) => `subsiCnsttyNm${i + 1}`)].map(k => pickF(it, k)).filter(Boolean).flatMap(normLic))], est: numF(pickF(it, 'presmptPrce', 'presmptPrc')), base: numF(pickF(it, 'bssamt', 'bsisAmt')),
    floor: numF(pickF(it, 'sucsfbidLwltRate', 'scsbdLwltRate')), ntce: normDt(pickF(it, 'bidNtceDt', 'rgstDt')),
    close: normDt(pickF(it, 'bidClseDt')), open: normDt(pickF(it, 'opengDt', 'rlOpengDt')),
    url: pickF(it, 'bidNtceDtlUrl', 'bidNtceUrl'), cancel: /취소/.test(pickF(it, 'ntceKindNm') || ''), live: true, kind: Live.kind,
    corr: /정정/.test(pickF(it, 'ntceKindNm') || ''), sui: /수의/.test(pickF(it, 'cntrctCnclsMthdNm') || '')};
  Object.keys(b).forEach(k => { if(b[k] == null || b[k] === '') delete b[k]; });
  return b;
}
/** 목록 API 에는 기초금액·A값이 없어서, 예측할 때 공고번호로 한 번 더 조회해 채운다 */
async function enrichLive(b){
  const op = LIVE_KINDS[b.kind || '공사']?.[2];
  if(!b.live || b.bsisTried || !apiKey() || !op) return;
  b.bsisTried = true;
  try{
    const {items} = await liveCall(op, {inqryDiv: '2', bidNtceNo: b.no, numOfRows: 10, pageNo: 1});
    const it = items.find(x => String(x.bidNtceOrd ?? '') === b.ord) || items[0];
    if(!it) return;
    const base = numF(pickF(it, 'bssamt', 'bsisAmt', 'bssAmt'));
    if(base) b.base = base;
    const aTotal = numF(pickF(it, 'aValue', 'aVal', 'aAmt'));
    const parts = ['npnInsrprm', 'mrfnHealthInsrprm', 'odsnLngtrmrcprInsrprm', 'rtrfundNon', 'sftyMngcst', 'sftyChckMngcst', 'qltyMngcst'].map(k => numF(it[k])).filter(v => v != null);
    if(aTotal != null || parts.length) b.a = aTotal ?? parts.reduce((x, y) => x + y, 0);
    const net = numF(pickF(it, 'pureCnstrctCst', 'pureCnstrtnCst', 'netCnstrctCst', 'cnstrtnAbsltPrc', 'pureCnstcst'));
    if(net) b.net = net;
    let lo = numF(pickF(it, 'rsrvtnPrceRngBgnRate', 'rsrvtnPrceRngBgnRt')), hi = numF(pickF(it, 'rsrvtnPrceRngEndRate', 'rsrvtnPrceRngEndRt'));
    if(lo != null || hi != null){ if(lo > 0) lo = -lo; b.rng = [lo, hi]; }
  }catch(e){ console.warn('기초금액 조회 실패', e); }
}

function initLive(){
  fillSelect($('lRgn'), SIDOS, {all:'시·도 전체'});
  const fillLSgg = () => { fillSelect($('lSgg'), sggsOf($('lRgn').value), {all:'시·군 전체'}); $('lSgg').disabled = !$('lRgn').value; };
  fillLSgg();
  Data.loadBids().then(fillLSgg);
  $('lRgn').addEventListener('change', fillLSgg);
  $('lLic').addEventListener('change', async () => { if(!Live.params || $('lLic').disabled) return; await Data.loadLicMap(); renderLive(); if(liveRows().length < 30 && !liveDone()) await liveSearch(true); });
  $('lSgg').addEventListener('change', async () => { if(!Live.params) return; renderLive(); if(liveRows().length < 30 && !liveDone()) await liveSearch(true); });
  fillSelect($('lLic'), LICENSES, {all:'업종 전체'});
  fillSelect($('lAmt'), AMT_RANGES, {all:'추정가격 전체'});
  const setPeriod = (days) => { const t = new Date(); $('lTo').value = kstDay(t); $('lFrom').value = kstDay(new Date(t - days * 86400000)); };
  setPeriod(7);
  $('lPeriod').addEventListener('click', (e) => {
    const d = e.target.dataset?.d; if(!d) return;
    setPeriod(+d);
    $('lPeriod').querySelectorAll('button').forEach(b => b.classList.toggle('on', b === e.target));
  });
  ['lFrom', 'lTo'].forEach(id => $(id).addEventListener('change', () => $('lPeriod').querySelectorAll('button').forEach(b => b.classList.remove('on'))));
  fillSelect($('lKind'), Object.keys(LIVE_KINDS), {all:null, value:'공사'});
  $('lKind').addEventListener('change', () => { $('lLic').disabled = $('lKind').value !== '공사'; if($('lLic').disabled) $('lLic').value = ''; });
  $('lSearch').addEventListener('click', () => liveSearch());
  ['lQuery', 'lOrg', 'lDmd'].forEach(id => $(id).addEventListener('keydown', (e) => { if(e.key === 'Enter') liveSearch(); }));
  $('liveMore').addEventListener('click', () => liveSearch(true));
  $('lElig').checked = LS.get('bidsFilter', {}).elig ?? Company.isSet();
  $('lElig').addEventListener('change', async () => { if(!Live.params) return; await Data.loadLicMap(); renderLive(); if(liveRows().length < 30 && !liveDone()) await liveSearch(true); });
  $('bMode').addEventListener('click', (e) => {
    const v = e.target.dataset?.v; if(!v) return;
    bidsMode = v; LS.set('bidsMode', v); renderBidsTab();
  });
}
function renderBidsTab(){
  const mode = bidsMode || (apiKey() ? 'live' : 'saved');
  $('bMode').querySelectorAll('button').forEach(b => b.classList.toggle('on', b.dataset.v === mode));
  $('bidsSaved').hidden = mode !== 'saved';
  $('bidsLive').hidden = mode !== 'live';
  if(mode === 'saved') return renderBids();
  $('liveKeyHint').hidden = !!apiKey();
  if(apiKey() && !Live.params) liveSearch();
}

function liveParams(){
  const [aLo, aHi] = ($('lAmt').value || '-').split('-').map(v => v === '' ? null : +v * 1e8);
  const p = {inqryDiv: $('lDiv').value};   // 기간은 liveWindows 구간마다 넣는다
  const set = (k, v) => { if(v != null && v !== '') p[k] = v; };
  set('bidNtceNm', $('lQuery').value.trim());
  set('ntceInsttNm', $('lOrg').value.trim());
  set('dminsttNm', $('lDmd').value.trim());
  set('prtcptLmtRgnNm', $('lRgn').value);
  // 업종은 보내지 않는다: 조달청 검색조건의 업종 필터는 맞는 공고가 있어도 0건을 돌려주는 일이 있어(다른 개발자도 같은 보고),
  // 주공종·부대공종 + 수집된 면허제한(lic_map.json)으로 앱에서 거른다 → liveRows
  set('presmptPrceBgn', aLo);
  set('presmptPrceEnd', aHi);
  if($('lOpen').checked) p.bidClseExcpYn = 'Y';
  return p;
}
// 조달청 API 는 한 번에 약 1개월까지만 조회된다 → 기간을 30일씩 나눠 최신 구간부터 차례로 받는다
const LIVE_SPAN_DAYS = 30;
const ymd8 = (d) => `${d.getFullYear()}${String(d.getMonth() + 1).padStart(2, '0')}${String(d.getDate()).padStart(2, '0')}`;
const date8 = (s) => new Date(`${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6, 8)}T00:00:00`);
function liveWindows(from, to){
  const out = [], start = new Date(from + 'T00:00:00');
  let end = new Date(to + 'T00:00:00');
  while(end >= start){
    let bgn = new Date(end); bgn.setDate(bgn.getDate() - (LIVE_SPAN_DAYS - 1));
    if(bgn < start) bgn = new Date(start);
    out.push([ymd8(bgn), ymd8(end)]);
    end = new Date(bgn); end.setDate(end.getDate() - 1);
  }
  return out;
}
const liveDone = () => Live.wi >= Live.wins.length;
/** 앱에서 거르는 조건(시·군, 참가 가능, 기본 목록 대체 조회)이 있나 — 있으면 충분히 모일 때까지 더 받는다 */
const liveClientFilter = () => !!($('lSgg').value || $('lLic').value || ($('lElig').checked && Company.isSet()) || Live.fallback);
/** 공고에 이 면허가 걸려 있나: 주공종·부대공종 또는 수집된 면허제한 */
const liveHasLic = (b, lic) => (b.lic || []).includes(lic) || licOf(b).includes(lic);
function liveRows(){
  const maxOrd = {};
  Live.items.forEach(b => { if(!maxOrd[b.no] || b.ord > maxOrd[b.no]) maxOrd[b.no] = b.ord; });   // 변경공고는 마지막 차수만
  const elig = $('lElig').checked && Company.isSet();
  const sgg = $('lSgg').value;
  const inSgg = (b) => !sgg || b.sgg === sgg || (b.rgn || []).some(t => parseRegion(t).sgg === sgg);
  const lic = $('lLic').value;
  // 참가 가능만: 요구 면허를 끝내 알 수 없는 공고(조회 실패)는 뺀다. 아직 조회 전인 공고는 조회되도록 잠깐 남긴다
  const eligOk = (b) => { const e = eligibility(b); return e.ok && !(e.lic === 'unknown' && b.limTried); };
  return Live.items.filter(b => b.ord === maxOrd[b.no] && !b.cancel && (!elig || eligOk(b)) && inSgg(b) && (!lic || liveHasLic(b, lic)));
}
/** 현재 구간의 다음 쪽 1번 호출 */
async function liveFetchOne(){
  const [bgn, end] = Live.wins[Live.wi];
  const [srchOp, listOp] = LIVE_KINDS[Live.kind];
  const rows = Live.rows || LIVE_ROWS;
  const q = {inqryBgnDt: bgn + '0000', inqryEndDt: end + '2359', numOfRows: rows, pageNo: Live.page + 1};
  const rangeErr = (m) => /범위|\(07\)/.test(m);
  let res;
  try{
    if(!Live.fallback){
      try{ res = await liveCall(srchOp, {...Live.params, ...q}); }
      catch(e){
        if(/서비스키|SERVICE_KEY|SERVICE ACCESS|ACCESS_DENIED|UNREGISTERED|등록되지/.test(e.message) || rangeErr(e.message)) throw e;
        Live.fallback = true;   // 검색조건 조회가 안 되면 기본 목록 조회 + 앱에서 거르기
      }
    }
    if(Live.fallback) res = await liveCall(listOp, {inqryDiv: Live.params.inqryDiv, ...q});
  }catch(e){
    // 그래도 기간이 길다고 하면 구간을 반으로 나눠 다시
    if(rangeErr(e.message) && bgn < end){
      const mid = new Date((date8(bgn).getTime() + date8(end).getTime()) / 2);
      const midNext = new Date(mid); midNext.setDate(midNext.getDate() + 1);
      Live.wins.splice(Live.wi, 1, [ymd8(midNext), end], [bgn, ymd8(mid)]);
      return;
    }
    throw e;
  }
  Live.totals[Live.wi] = res.total;
  Live.page++;
  Live.raw += res.items.length;
  const have = new Set(Live.items.map(b => b.id));
  const P2 = Live.params, low = (v) => String(v || '').toLowerCase();
  const keep = (n) => !Live.fallback || (
    (!P2.bidNtceNm || low(n.nm).includes(low(P2.bidNtceNm))) &&
    (!P2.ntceInsttNm || low(n.org).includes(low(P2.ntceInsttNm))) &&
    (!P2.dminsttNm || low(n.dmd).includes(low(P2.dminsttNm))) &&
    (!P2.prtcptLmtRgnNm || n.sido === P2.prtcptLmtRgnNm) &&
    (!P2.presmptPrceBgn || (n.est || 0) >= P2.presmptPrceBgn) &&
    (!P2.presmptPrceEnd || (n.est || Infinity) < P2.presmptPrceEnd) &&
    (!P2.bidClseExcpYn || !n.close || parseKst(n.close) >= new Date()));
  for(const it of res.items){
    const n = liveNotice(it);
    if(n.no && !have.has(n.id) && keep(n)){ have.add(n.id); Live.items.push(n); }
  }
  if(!res.items.length || Live.page * rows >= res.total){ Live.wi++; Live.page = 0; }
}
async function liveSearch(more=false){
  const list = $('liveList');
  if(!apiKey()){ $('liveKeyHint').hidden = false; return; }
  if(!$('lFrom').value || !$('lTo').value || $('lFrom').value > $('lTo').value){
    $('liveInfo').textContent = '조회 기간을 확인하세요.'; return;
  }
  const token = ++Live.token;
  if(!more){
    Object.assign(Live, {params: liveParams(), items: [], raw: 0, kind: $('lKind').value, fallback: false,
      wins: liveWindows($('lFrom').value, $('lTo').value), wi: 0, page: 0, totals: [], rows: liveClientFilter() ? 999 : LIVE_ROWS});
    if($('lLic').value || ($('lElig').checked && Company.isSet())) await Data.loadLicMap();
    list.innerHTML = loadingHtml('나라장터에서 조회 중…');
    $('liveMore').hidden = true;
  }
  $('liveMore').disabled = true;
  // 한 번에: 앱에서 거르는 조건이 없으면 100건, 있으면 조건에 맞는 30건이 모일 때까지 (최대 12번 호출)
  const startRaw = Live.raw, startRows = liveRows().length;
  try{
    for(let calls = 0; calls < 12 && !liveDone(); calls++){
      await liveFetchOne();
      if(token !== Live.token) return;
      if(liveClientFilter() ? liveRows().length - startRows >= 30 : Live.raw - startRaw >= LIVE_ROWS) break;
      if(!more) list.innerHTML = loadingHtml(`나라장터에서 조회 중… (${Math.min(Live.wi + 1, Live.wins.length)}/${Live.wins.length}구간, ${fmtNum(Live.raw)}건 받음)`);
    }
  }catch(e){
    if(token !== Live.token) return;
    $('liveInfo').textContent = '';
    list.innerHTML = `<div class="empty card">조회하지 못했습니다: ${esc(e.message)}<br><span class="faint">서비스키 오류라면 설정 탭에서 키를 확인하세요 (활용 승인 직후에는 1~2시간 걸릴 수 있습니다).</span></div>`;
    return;
  }finally{ $('liveMore').disabled = false; }
  renderLive();
}
async function renderLive(){
  const list = $('liveList'), sido = $('lRgn').value;
  if(sido){
    Live.items.forEach(b => { if(!b.sido) b.sido = sido; });
    if(Data.hasScsbid(sido) && !Data.scsbid[sido]){ try{ await Data.loadScsbid(sido); }catch(e){ console.warn(e); } }
  }
  watchIds = new Set((await WatchStore.list()).map(w => w.id));
  const rows = liveRows(), sgg = $('lSgg').value;
  const today = kstDay(new Date());
  const known = Live.totals.reduce((a, b) => a + (b || 0), 0);
  const nW = Live.wins.length, doneW = Math.min(Live.wi, nW);
  $('liveInfo').innerHTML = [`나라장터 실시간 ${esc(Live.kind)} ${fmtNum(Live.raw)}건 받음${liveDone() ? ` (전체 ${fmtNum(known)}건)` : ` · 전체 ${fmtNum(known)}건 이상`}${liveClientFilter() ? ` → 조건에 맞는 ${fmtNum(rows.length)}건` : ''}`,
    nW > 1 ? `기간을 1개월씩 ${nW}구간으로 나눠 최신부터 조회 (${doneW}/${nW}구간 완료)` : '',
    sgg ? `시·군(${esc(sgg)})은 공사 현장·참가가능지역 기준으로 앱에서 거름` : '',
    $('lLic').value ? `업종(${esc($('lLic').value)})은 주공종·부대공종·면허제한 기준으로 앱에서 거름` : '',
    Live.kind !== '공사' ? '예측은 공사만 제공' : '',
    !sido && !Model.m ? '지역을 고르면 예상 사정율·낙찰확률도 표시됩니다' : ''].filter(Boolean).join(' · ');
  list.innerHTML = rows.length ? rows.map(b => bidCard(b, today)).join('') : `<div class="empty card">${liveDone() ? '조건에 맞는 공고가 없습니다.' : '아직 조건에 맞는 공고를 못 찾았습니다. "더 보기"로 이전 기간을 이어서 조회하세요.'}</div>`;
  $('liveMore').hidden = liveDone();
  $('liveMore').textContent = '더 보기 (이어서 조회)';
  const token = Live.token;
  if(await fetchLiveLimits(rows) && token === Live.token) renderLive();   // 요구 면허·지역을 받은 뒤 다시 거르고 그림
}

async function toggleWatch(id, btn){
  const b = findNotice(id);
  if(!b) return;
  if(watchIds.has(id)){
    await WatchStore.remove(id); watchIds.delete(id);
    btn.classList.remove('on'); btn.textContent = '☆';
  }else{
    const qp = quickPredict(b);
    await WatchStore.save({...pickNotice(b), savedAt: new Date().toISOString(),
      pred: qp ? {sr: qp.sr, bid: qp.bid, n: qp.n, by: '자동'} : null});
    watchIds.add(id);
    btn.classList.add('on'); btn.textContent = '★';
  }
}
const pickNotice = (b) => ({id:b.id, no:b.no, ord:b.ord, nm:b.nm, org:b.org, dmd:b.dmd, sido:b.sido, sgg:b.sgg,
  lic:licOf(b).length ? licOf(b) : b.lic, rgn:b.rgn, base:b.base, est:b.est, a:b.a, floor:b.floor, net:b.net, rng:b.rng, close:b.close, open:b.open, url:b.url});

// ============================================================ 우리 업체 (소재지·보유 면허·사업자번호 — 이 기기에만 저장)
const Company = {
  get(){ return {sido: '', sgg: '', lics: [], biz: '', caps: {}, ...LS.get('company', {})}; },
  set(c){ LS.set('company', c); qpCache.clear(); },
  isSet(){ const c = this.get(); return !!(c.sido || c.lics.length); },
};
/** 이 공고에 우리 업체가 참가할 수 있나. 참가가능지역(rgn)·면허(lic) 기준. 정보가 없으면 '확인 필요' */
/** 공고의 면허제한: 수집된 면허제한(lic_map) 우선. 실시간 공고의 b.lic 는 주공종·부대공종이라 제한 면허가 아니다 */
function licOf(b){
  const m = Data.licMap?.get(b.id);
  return m?.length ? m : b.reqLic?.length ? b.reqLic : (b.live ? [] : b.lic || []);
}
/** 목록에 보이는 실시간 공사 공고의 빠진 정보를 채운다 (공고당 한 번, 10건씩):
 *  면허제한·참가가능지역 = 수집된 lic_map 에 없으면 조달청 조회, 기초금액·A값·예가범위 = 수집된 진행중 공고 → 없으면 조달청 조회(enrichLive).
 *  기초금액이 있어야 카드에 추천 투찰가가 나온다 */
async function fetchLiveLimits(rows){
  if(!apiKey()) return false;
  const bsis = rows.filter(b => b.live && b.kind === '공사' && !b.base && !b.bsisTried).slice(0, 10);
  bsis.forEach(b => {
    const c = Data.bids?.find(x => x.id === b.id);
    if(c?.base) ['base', 'a', 'net', 'rng', 'floor'].forEach(k => { if(c[k] != null && b[k] == null) b[k] = c[k]; });
  });
  const need = rows.filter(b => b.live && b.kind === '공사' && !b.limTried && !Data.licMap?.get(b.id)?.length).slice(0, 10);
  const todo = bsis.filter(b => !b.base);
  if(!need.length && !todo.length) return bsis.length > 0;
  await Promise.all([...todo.map(b => enrichLive(b)), ...need.map(async b => {
    b.limTried = true;
    const setLim = (lics, rgns) => {
      b.reqLic = [...new Set(lics.flatMap(normLic))];
      b.rgn = [...new Set(rgns)];   // [] = 지역 제한 없음
      b.limOk = true;
    };
    // 1) 공고일 기준 하루치 조회(수집기와 같은 방식, 여러 공고가 한 번에 채워짐)
    const day = (b.ntce || '').slice(0, 10);
    if(day){
      try{
        const d = await limitsForDay(day);
        if(d.lic.has(b.id)){ setLim(d.lic.get(b.id), d.rgn.get(b.id) || []); return; }   // 없으면 제한 없음인지 누락인지 몰라 공고번호로 다시
      }
      catch(e){ console.warn('면허제한 하루 조회', day, e); }
    }
    // 2) 공고번호 조회
    try{
      const q = {inqryDiv: '2', bidNtceNo: b.no, numOfRows: 100, pageNo: 1};
      const [l, r] = await Promise.all([liveCall('getBidPblancListInfoLicenseLimit', q), liveCall('getBidPblancListInfoPrtcptPsblRgn', q)]);
      const mine = (it) => String(it.bidNtceOrd ?? b.ord) === b.ord;
      setLim(l.items.filter(mine).map(it => pickF(it, 'lcnsLmtNm')).filter(Boolean), r.items.filter(mine).map(it => pickF(it, 'prtcptPsblRgnNm')).filter(Boolean));
    }catch(e){ console.warn('면허제한 조회', b.no, e); }
  })]);
  return true;
}
/** 공고 게시일(+다음날) 에 등록된 면허제한·참가가능지역 전부 → {lic: Map(공고ID → [원문]), rgn: Map(공고ID → [원문])}. 날짜별로 한 번만 받는다 */
const limDays = new Map();
function limitsForDay(day){
  if(!limDays.has(day)){
    const p = (async () => {
      const d0 = day.replace(/-/g, ''), d1 = kstDay(new Date(Date.parse(day) + 86400000)).replace(/-/g, '');
      const out = {lic: new Map(), rgn: new Map()};
      for(const [op, field, map] of [['getBidPblancListInfoLicenseLimit', 'lcnsLmtNm', out.lic], ['getBidPblancListInfoPrtcptPsblRgn', 'prtcptPsblRgnNm', out.rgn]]){
        for(let page = 1; page <= 15; page++){
          const {items, total} = await liveCall(op, {inqryDiv: '1', inqryBgnDt: d0 + '0000', inqryEndDt: d1 + '2359', numOfRows: 999, pageNo: page});
          for(const it of items){
            const v = pickF(it, field);
            if(!v) continue;
            const id = `${String(it.bidNtceNo || '').trim()}-${String(it.bidNtceOrd ?? '000').trim()}`;
            if(!map.has(id)) map.set(id, []);
            if(!map.get(id).includes(v)) map.get(id).push(v);
          }
          if(!items.length || page * 999 >= total) break;
        }
      }
      return out;
    })();
    p.catch(() => limDays.delete(day));
    limDays.set(day, p);
  }
  return limDays.get(day);
}
// 참여가능금액(면허별 적격심사 실적 한도) 칸: j3·j5 = 지자체 3년·5년, g = 조달청·그 외 기관, k = 한국수력원자력(다를 때만)
const CAP_FIELDS = [['j3', '지자체 3년'], ['j5', '지자체 5년'], ['g', '조달청·그 외'], ['k', '한수원 (다를 때만)']];
/** 발주기관 종류: 지자체(행안부 기준) / 한수원 / 그 외(조달청 기준) */
function orgKind(b){
  const t = `${b.org || ''} ${b.dmd || ''}`;
  if(/한국수력원자력/.test(t)) return 'k';
  if(/특별시|광역시|특별자치시|특별자치도|도청|시청|군청|구청|교육청|교육지원청/.test(t) || /(^|\s)\S+[도시군구](\s|$)/.test(t)) return 'j';
  return 'g';
}
/** 공고 금액(추정가격, 없으면 기초금액)이 우리 참여가능금액 안인가. 'ok' | 'check'(지자체 3~5년 사이) | 'no' | null(검사 안 함) */
function capCheck(b){
  const c = Company.get(), amt = b.est || b.base;
  if(!amt || !c.caps) return null;
  const kind = orgKind(b);
  let best = null;
  for(const l of licOf(b).filter(l => c.lics.includes(l))){
    const v = c.caps[l];
    if(!v) continue;
    let r = null;
    if(kind === 'j' && (v.j3 || v.j5)) r = v.j3 && amt <= v.j3 ? 'ok' : v.j5 && amt <= v.j5 ? (v.j3 ? 'check' : 'ok') : 'no';
    else if(v.g || v.k) r = amt <= ((kind === 'k' && v.k) || v.g || v.k) ? 'ok' : 'no';
    if(r) best = best === 'ok' || r === 'ok' ? 'ok' : best === 'check' || r === 'check' ? 'check' : 'no';
  }
  return best;
}
function eligibility(b){
  const c = Company.get();
  const out = {ok: true, lic: null, rgn: null, cap: null};
  const lics = licOf(b), rgn = rgnOf(b);
  if(c.lics.length){
    if(!lics.length) out.lic = b.limOk ? 'ok' : 'unknown';   // limOk: 조달청 조회 결과 면허 제한 없음
    else if(lics.some(l => c.lics.includes(l))) out.lic = 'ok';
    else { out.lic = 'no'; out.ok = false; }
  }
  if(c.sido && rgn?.length){
    const hit = rgn.some(t => {
      if(/전국/.test(t)) return true;
      const r = parseRegion(t);
      return r.sido === c.sido && (!r.sgg || !c.sgg || r.sgg === c.sgg);
    });
    out.rgn = hit ? 'ok' : 'no';
    if(!hit) out.ok = false;
  }
  if(out.ok){
    out.cap = capCheck(b);
    if(out.cap === 'no') out.ok = false;
  }
  return out;
}
function eligTag(b){
  if(!Company.isSet()) return '';
  const e = eligibility(b);
  if(!e.ok) return `<span class="tag bad">참가 불가 · ${e.rgn === 'no' ? '지역 제한' : e.cap === 'no' ? '실적 한도 초과' : '면허 불일치'}</span>`;
  if(e.cap === 'check') return '<span class="tag warn" title="추정가격이 지자체 3년 실적 한도는 넘고 5년 한도 안 — 공고문의 실적 기간 확인">실적 확인 (지자체 5년 기준만 가능)</span>';
  if(e.lic === 'unknown') return '<span class="tag warn">면허 확인 필요</span>';
  return '<span class="tag okc">참가 가능</span>';
}

/** 이 시·도의 시·군 목록 — 진행중 공고의 현장 지역과 참가가능지역에서 모은다 */
function sggsOf(sido){
  const set = new Set();
  (Data.bids || []).forEach(b => {
    if(b.sido === sido && b.sgg) set.add(b.sgg);
    (b.rgn || []).forEach(t => { const r = parseRegion(t); if(r.sido === sido && r.sgg) set.add(r.sgg); });
  });
  return [...set].sort();
}
function initCompany(){
  const c = Company.get();
  fillSelect($('coSido'), SIDOS, {all: '선택 안 함', value: c.sido});
  const fillSgg = (value) => {
    const sido = $('coSido').value;
    fillSelect($('coSgg'), sggsOf(sido), {all: '시·군 전체', value});
    $('coSgg').disabled = !sido;
  };
  Data.loadBids().then(() => fillSgg(c.sgg));
  $('coSido').addEventListener('change', () => fillSgg(''));
  $('coBiz').value = c.biz || '';
  const lics = new Set(c.lics);
  const caps = {...c.caps};
  const readCaps = () => $('coCaps').querySelectorAll('input[data-cap]').forEach(el => {
    const [l, f] = el.dataset.cap.split('|'), v = numOf(el);
    caps[l] = {...caps[l]};
    if(v) caps[l][f] = v; else delete caps[l][f];
  });
  const renderCaps = () => {
    readCaps();
    $('coCaps').innerHTML = [...lics].map(l => `<div class="cap-row"><b>${esc(l)}</b><div class="inline-inputs" style="margin-top:4px;">${CAP_FIELDS.map(([f, t]) =>
      `<div><label>${t}</label><input class="money" type="text" inputmode="numeric" data-cap="${esc(l)}|${f}" placeholder="원"></div>`).join('')}</div></div>`).join('')
      || '<p class="faint">보유 면허를 먼저 고르세요.</p>';
    $('coCaps').querySelectorAll('input[data-cap]').forEach(el => { const [l, f] = el.dataset.cap.split('|'); if(caps[l]?.[f]) setMoney(el, caps[l][f]); });
  };
  buildChips($('coLics'), LICENSES, lics, renderCaps);
  renderCaps();
  $('coSave').addEventListener('click', () => {
    readCaps();
    const keep = Object.fromEntries(Object.entries(caps).filter(([l, v]) => lics.has(l) && Object.keys(v).length));
    Company.set({sido: $('coSido').value, sgg: $('coSgg').value, lics: [...lics], biz: $('coBiz').value.replace(/\D/g, ''), caps: keep});
    $('coMsg').innerHTML = '<span class="badge ok">저장됨</span> 입찰공고에서 "참가 가능한 공고만"을 켜면 적용됩니다';
    LS.set('bidsFilter', {...LS.get('bidsFilter', {}), elig: true});
    $('bElig').checked = true; $('lElig').checked = true;
  });
}

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
  // ② 이번 공고 정보 → 투찰가 계산기로 같이 채움
  [['inBase', 'cBase'], ['inA', 'cA'], ['inFloor', 'cFloor']].forEach(([from, to]) =>
    $(from).addEventListener('input', () => { $(to).value = $(from).value || (to === 'cFloor' ? DEFAULT_FLOOR : ''); renderCalc(); }));
  // 사용법은 처음엔 펼쳐 두고, 한 번 닫으면 닫힌 채로 기억
  $('pGuide').open = !LS.get('guideClosed', false);
  $('pGuide').addEventListener('toggle', () => LS.set('guideClosed', !$('pGuide').open));
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
        <div class="meta-line">기초금액 ${won(n.base)} · A값 ${won(n.a)} · 낙찰하한율 ${n.floor ? pct(n.floor) : `미확인(${DEFAULT_FLOOR}% 적용)`} · 순공사원가 ${won(n.net)}${n.rng ? ` · 예가범위 ${esc(rngText(n.rng))}` : ''}</div>
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

async function predictWithNotice(id){
  const b = findNotice(id);
  if(!b) return;
  if(b.live) await enrichLive(b);
  P.notice = b;
  if(b.sido){ P.sidos.clear(); P.sidos.add(b.sido); LS.set('pSidos', [...P.sidos]); }
  P.sggs.clear();
  P.lics = new Set(b.lic || []); LS.set('pLics', [...P.lics]);
  syncChips($('pSidoChips'), P.sidos); syncChips($('pLicChips'), P.lics);
  setMoney($('inBase'), b.base || b.est);
  setMoney($('inA'), b.a);
  $('inFloor').value = b.floor || '';
  $('inCnt').value = '';
  $('pRng').value = b.rng && [2, 3].includes(b.rng[1]) && b.rng[0] === -b.rng[1] ? `${b.rng[0]},${b.rng[1]}` : '';
  $('fBase').checked = !!(b.base || b.est);
  setMoney($('cBase'), b.base);
  setMoney($('cA'), b.a);
  $('cFloor').value = b.floor || DEFAULT_FLOOR;
  setMoney($('cNet'), b.net);
  $('cManual').value = '';
  $('fRng').checked = !!b.rng;
  if(b.rng?.[1]) $('sRange').value = Math.abs(b.rng[1]);
  if(b.sido){ $('orgSido').value = b.sido; }
  orgLoaded = true;
  switchTab('predict');
  renderSggChips();
  loadOrgOptions(recOrg(b));
  runPredict();
}

/** 설정 → 백테스트 결과(시·도별). 추천값 옆에 "검증됨/우위 없음/검증 전" 표시에 쓴다 */
function btBadge(sido){
  const r = LS.get('btResult', {})[sido];
  if(!r) return `<span class="badge gray">백테스트 전</span>`;
  return r.adopt ? `<span class="badge ok">백테스트 우위 확인 · 무작위 대비 ${r.lift.toFixed(2)}배 (${fmtNum(r.n)}건)</span>`
    : `<span class="badge warn">백테스트: 뚜렷한 우위 없음 (${fmtNum(r.n)}건, ${r.lift ? r.lift.toFixed(2) + '배' : '-'})</span>`;
}

async function runPredict(){
  const out = $('predictResult');
  const sidos = [...P.sidos];
  if(!sidos.length){ out.innerHTML = '<div class="card"><div class="empty">지역을 하나 이상 선택하세요.</div></div>'; return; }
  const missing = sidos.filter(s => !Data.hasScsbid(s));
  out.innerHTML = `<div class="card">${loadingHtml()}</div>`;
  let recs;
  const opening = new Map();
  try{
    recs = await Data.loadScsbidMany(sidos);
    for(const s of sidos.filter(s => Data.hasDetail(s))) for(const [id, b] of (await Data.loadOpening(s)).bids) opening.set(id, b);
  }catch(e){ out.innerHTML = `<div class="card"><div class="empty">데이터를 불러오지 못했습니다. (${esc(e.message)})</div></div>`; return; }
  const selRng = $('pRng').value ? $('pRng').value.split(',').map(Number) : (P.notice?.rng || null);
  const o = {sggs: P.sggs, lics: P.lics, recent: $('fRecent').checked,
    useBase: $('fBase').checked, base: numOf($('inBase')),
    useCnt: $('fCnt').checked, cnt: numOf($('inCnt')),
    rng: $('fRng').checked ? selRng : null,
    org: $('fOrg').checked && P.notice ? recOrg(P.notice) : ''};
  let rows = filterRecords(recs, o);
  const notes = [];
  if(o.rng && rows.length < MIN_SAMPLE){ rows = filterRecords(recs, {...o, rng: null}); notes.push('예가범위 일치 표본 부족 → 예가범위 조건 제외'); }
  $('matchCount').textContent = `${fmtNum(rows.length)}건의 과거 낙찰로 계산${notes.length ? ' · ' + notes.join(' · ') : ''}`;
  const pred = predictFrom(rows);
  const homeSido = P.notice?.sido || (sidos.length === 1 ? sidos[0] : null);
  // 곡선은 넓은 표본으로: 선택 지역 최근 24개월 + 예가범위 같음(30건↑). 면허·금액으로 쪼개면 우연한 봉우리가 생긴다(설계 문서 3-4)
  const recent24 = recs.filter(r => (r.date || '') >= monthsAgo(24));
  const rngPool = selRng ? recent24.filter(r => sameRng(r.rng, selRng)) : [];
  const curveRows = rngPool.length >= MIN_SAMPLE ? rngPool : recent24;
  const wc = pred ? winCurve(curveRows, {sido: homeSido, opening}) : null;
  // 이 공고의 예상 참가업체 수: 입력값 → 비슷한 과거 공고 중앙값
  const ec = numOf($('inCnt')) ? {n: numOf($('inCnt')), k: 0} : expectedCnt(recs.filter(r => (r.date || '') >= monthsAgo(24)), P.notice || {lic: [...P.lics], base: o.base});
  P.last = pred ? {pred, rows, wc, ec} : null;
  if(!pred || rows.length < 3){
    out.innerHTML = `<div class="card"><div class="empty">조건에 맞는 과거 데이터가 너무 적습니다 (${rows.length}건). 조건을 넓혀 보세요.${missing.length ? `<br>데이터 없는 지역: ${esc(missing.join(', '))}` : ''}</div></div>`;
    return;
  }
  $('cRate').value = (wc ? wc.best.x : pred.mean).toFixed(4);
  renderCalc();
  const base = numOf($('cBase')), a = numOf($('cA')), floor = +$('cFloor').value || DEFAULT_FLOOR;
  const amtAt = (x) => base ? bidAmount(base, x, a, floor) : null;

  // ---- 1) 추천 요약: 전국 모델(비슷한 경쟁 규모)이 있으면 우선, 없으면 이 지역 곡선
  const nIn = numOf($('inCnt'));
  const noticeLike = {...(P.notice || {}), sido: homeSido || P.notice?.sido, base: o.base || P.notice?.base || P.notice?.est,
    rng: selRng, floor: +$('inFloor').value || P.notice?.floor};
  const mp = modelPredict(noticeLike, nIn);
  const rec = mp ? recFromModel(mp) : wc ? recFromLocal(wc, ec) : null;
  P.last.rec = rec;
  if(rec){ $('cRate').value = rec.x.toFixed(4); renderCalc(); }
  let hero, curveCard = '', tips = '';
  if(rec){
    const vw = rec.src === 'model' ? valWinP(rec.random, rec.nExp) : null;   // 역검증 기준 (없으면 과거 곡선값)
    const lift = vw ? vw.lift : rec.random ? rec.p / rec.random : null;
    const meanX = rec.meanS ?? pred.mean;
    const V = Model.m?.validation, T = V?.total, seg = rec.nExp ? valSegment(rec.nExp) : null;
    const net = numOf($('cNet')) || P.notice?.net || 0;
    const recAmt = amtAt(rec.x);
    const level = !rec.nExp ? '' : rec.nExp < 20 ? '<b style="color:var(--ok)">경쟁 적음 — 유리</b>' : rec.nExp < 80 ? '보통' : '<b style="color:var(--warn)">경쟁 많음</b>';
    const check = [
      rec.nExp ? `<li class="ok"><b>경쟁 규모</b>: 예상 참가 <b>~${fmtNum(rec.nExp)}곳</b>${rec.byInput ? '(직접 입력)' : ''} → ${level}${seg ? ` · 이런 공고의 역검증 낙찰률 <b>${(seg.near / seg.n * 100).toFixed(1)}%</b> (${fmtNum(seg.n)}건)` : ''}</li>` : '',
      `<li class="ok"><b>추천 위치</b>: ${rec.src === 'model' ? '경쟁 규모가 비슷한 전국 과거 공고에서 가장 자주 1순위였던 투찰 사정률' : '이 지역 과거 공고에서 가장 자주 1순위였던 투찰 사정률'} <b>${pct(rec.x, 3)}</b>${T?.n ? ` · 새 달 역검증 ${fmtNum(T.n)}건에서 <b>${fmtNum(T.near)}건</b> 낙찰 (평균 사정율 방식 ${fmtNum(T.mean)}건 · 평균 업체 기대 ${fmtNum(Math.round(T.rand))}건)` : ''}</li>`,
      rec.sb != null ? `<li class="ok"><b>하한 미달은 정상</b>: 비슷한 과거 공고에서 이 값은 <b>${Math.round(rec.sb * 100)}%</b>가 낙찰하한가 미달이었습니다. 미달이 잦아도 1순위가 될 확률은 위 예상 낙찰확률 그대로입니다 — 하한 이상에 넣어도 1순위가 아니면 똑같이 떨어지고, 사정율보다 한참 높게 넣는 값(예: 100.4~100.8%)은 역검증에서 평균 업체보다 18~34% 덜 낙찰됐습니다.</li>` : '',
      `<li class="ok"><b>금액</b>: ${recAmt ? `<b>${won(recAmt)}</b>을 ` : ''}원 단위까지 그대로. 안전 범위 ${recAmt ? `${won(amtAt(rec.lo))} ~ ${won(amtAt(rec.hi))}` : `${pct(rec.lo, 3)} ~ ${pct(rec.hi, 3)}`} 안이면 확률 비슷</li>`,
      !base ? `<li class="warn"><b>기초금액</b>을 넣어야 추천 금액이 계산됩니다</li>` : '',
      !rec.rngKnown && rec.src === 'model' ? `<li class="warn"><b>예가범위</b>를 모르면 ±3% 기준으로 계산합니다. 공고문에서 확인해 ② 칸에 고르세요</li>` : '',
      net && recAmt ? (recAmt < net * 0.98 ? `<li class="bad"><b>순공사원가 98% 미만</b> (${won(Math.ceil(net * 0.98))}) — 입찰 무효 위험, 금액을 올리세요</li>` : `<li class="ok"><b>순공사원가 98% 이상</b> 확인</li>`) : '',
      `<li class="ok"><b>기록</b>: ☆ 관심공고에 저장 → 넣은 금액을 적어 두면 개찰 뒤 판정과 다음 보정값</li>`,
    ].filter(Boolean).join('');
    hero = `<div class="card hero">
      <div class="hero-top"><span class="hero-tag">🎯 추천 투찰</span>${valBadge()}</div>
      <div class="hero-main">
        <div>
          <div class="hero-label">추천 투찰금액</div>
          <div class="hero-amt">${recAmt ? won(recAmt) : '<span class="faint" style="font-size:18px;">기초금액을 넣으면 금액이 나옵니다</span>'}</div>
          <div class="hero-sub">투찰 사정률 <b>${pct(rec.x, 3)}</b>${recAmt ? ` · 안전 범위 ${won(amtAt(rec.lo))} ~ ${won(amtAt(rec.hi))}` : ` · 안전 범위 ${pct(rec.lo, 3)} ~ ${pct(rec.hi, 3)}`}</div>
        </div>
        <div class="hero-stats">
          <div class="stat hl"><div class="t">예상 낙찰확률</div><div class="v">${((vw ? vw.p : rec.p) * 100).toFixed(2)}%</div></div>
          <div class="stat"><div class="t">평균 업체 대비 ${vw ? '(역검증)' : '(과거)'}</div><div class="v">${lift ? '×' + lift.toFixed(2) : '-'}</div></div>
          <div class="stat"><div class="t">예상 참가</div><div class="v">${rec.nExp ? '~' + fmtNum(rec.nExp) + '곳' : '-'}</div></div>
          <div class="stat"><div class="t">평균 사정율로 넣으면</div><div class="v">${(rec.at(meanX) * 100).toFixed(2)}%</div></div>
        </div>
      </div>
      <h3>✅ 추천 체크</h3>
      <ul class="checklist">${check}</ul>
      <div class="meta-line">${esc(rec.note)} · 참고용이며 낙찰을 보장하지 않습니다.</div>
      <div class="btn-row" style="margin-top:10px;"><button class="btn sm" data-use-sr="${rec.x}" type="button">계산기에 적용</button></div>
    </div>`;

    // ---- 2) 곡선 + 후보
    const [vMin, vMax] = rec.view;
    const sBins = binPts(rows.map(r => [r.sr, 1]), vMin, vMax, (vMax - vMin) > 3 ? 0.05 : 0.02);
    curveCard = `<div class="card">
      <h2>투찰 사정률별 과거 낙찰확률</h2>
      <p class="sub">선 = 그 값으로 넣었을 때 과거 낙찰확률, 옅은 막대 = 이 지역 실제 사정율 분포. 사정율이 자주 떨어지면서 경쟁사가 덜 몰린 곳이 높게 나옵니다.</p>
      ${plot([{pts: sBins, color: 'var(--text-sub)', label: '실제 사정율 분포', bars: true},
              {pts: rec.pts(), color: 'var(--primary)', label: '과거 낙찰확률', fill: true}],
        {min: vMin, max: vMax, marks: [{x: rec.x, color: 'var(--target)', label: `추천 ${rec.x.toFixed(3)}`}, {x: meanX, color: 'var(--text-faint)', label: `평균 ${meanX.toFixed(2)}`}]})}
      <div class="table-wrap" style="margin-top:10px; max-height:none;"><table>
        <thead><tr><th>후보</th><th class="num">투찰 사정률</th><th class="num">과거 낙찰확률</th><th class="num">평균 업체 대비</th><th class="num">투찰금액</th><th></th></tr></thead>
        <tbody>${rec.peaks.map((c, i) => `<tr${i ? '' : ' class="hl-row"'}><td>${i + 1}${i ? '' : ' ★'}</td><td class="num">${pct(c.x, 3)}</td><td class="num">${(c.p * 100).toFixed(2)}%</td>
          <td class="num">${rec.random ? '×' + (c.p / rec.random).toFixed(2) : '-'}</td><td class="num">${base ? won(amtAt(c.x)) : '-'}</td>
          <td><button class="btn sm line" data-use-sr="${c.x}" type="button">적용</button></td></tr>`).join('')}</tbody>
      </table></div>
      <div class="meta-line">곡선 표본: ${esc(rec.note)} · 곡선 폭 ±${rec.src === 'model' && Model.m?.smooth ? Model.m.smooth : curveSmooth()}%p · 과거 낙찰확률은 과거에 맞춘 값이라 새 공고에선 더 낮음(위 예상 낙찰확률은 역검증 기준) · 평균 업체 = 1 ÷ 참가업체 수</div>
    </div>`;

    // ---- 3) 금액·숫자 팁
    const cal = LS.get('myCal', null);
    tips = `<div class="card">
      <h2>💡 어떤 금액·숫자를 넣을까</h2>
      <ul class="tips">
        <li><b>금액</b>: 추천 금액을 <b>원 단위까지 그대로</b> 넣으세요. 만원·천원 단위로 반올림하면 투찰 사정률이 옮겨가 확률 구간을 벗어날 수 있습니다${base ? ` (이 공고에서 1만원 ≈ 사정률 ${(1e4 / (floor / 100) / base * 100).toFixed(4)}%p)` : ''}.</li>
        <li><b>범위</b>: 안전 범위 안이면 과거 확률이 비슷했습니다. 다른 사람과 같은 금액(동가)을 피하려면 범위 안에서 끝자리를 조금 바꿔도 됩니다.</li>
        <li><b>공고 고르기</b>: 같은 노력이면 <b>예상 참가가 적은 공고</b>에 넣으세요. 역검증에서 예상 20곳 미만 공고의 낙찰률은 150곳 이상 공고의 10배 이상이었습니다. 입찰공고 탭 → "낙찰확률 높은 순".</li>
        <li><b>복수예가 번호(15개 중 2개)</b>: 수백 개사가 함께 고르기 때문에 내 선택 2개가 예정가격에 주는 영향은 거의 없습니다. 통계 탭의 번호 빈도가 무작위(26.7%)와 크게 다를 때만 참고하세요.</li>
        ${cal && cal.n ? `<li><b>내 투찰 기록 보정</b>: 개찰된 내 투찰 ${fmtNum(cal.n)}건 기준, 투찰 사정률을 <b>${cal.shift >= 0 ? '+' : ''}${cal.shift.toFixed(3)}%p</b> 옮겼다면 낙찰권이 ${cal.wins}건 → ${cal.best}건이었습니다${cal.n < MIN_SAMPLE ? ' <span class="badge warn">참고 부족</span>' : ''}.</li>` : '<li><b>내 기록</b>: 관심공고 탭에서 실제로 넣은 금액을 기록하면, 개찰 뒤 결과와 비교해 다음에 얼마나 올리거나 내릴지 알려드립니다.</li>'}
      </ul>
    </div>`;
  }else{
    hero = `<div class="card"><h2>🎯 추천 투찰</h2><div class="empty">추천을 계산할 과거 데이터가 부족합니다. 조건을 넓혀 보세요.</div></div>`;
  }

  // ---- 4) 참고: 사정율 분포 (기존 평균 방식)
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
  out.innerHTML = hero + curveCard + tips + `
    <details class="card fold">
      <summary><h2>참고: 사정율 분포 (평균·공격·추천·보수)</h2><span class="meta-line" style="margin:0;">평균 ${pred.mean.toFixed(3)}% · ${sampleText(pred.n)}</span></summary>
      <p class="sub" style="margin-top:12px;">선택한 조건의 과거 사정율(예정가격 ÷ 기초금액) 분포입니다. 평균 사정율은 가장 흔한 값이지만 경쟁사도 가장 많이 몰려 있어 낙찰확률 최대값과 다를 수 있습니다.</p>
      <div class="result-grid">
        <div>
          <div class="big-number">${pred.mean.toFixed(3)}<small>% 평균 사정율</small></div>
          <div class="meta-line">표준편차 ±${pred.std.toFixed(3)}%p · ${sampleText(pred.n)}</div>
          <div class="meta-line">신뢰도 <b>${pred.conf.label}</b> (${pred.conf.score}점 · 표본 수와 분산 기준)</div>
          <div class="meta-line">중앙값 ${quantile(sorted,.5).toFixed(3)}% · 범위 ${sorted[0].toFixed(2)}~${sorted.at(-1).toFixed(2)}%</div>
          <div class="bands">${band('aggressive','공격')}${band('recommend','평균','rec')}${band('conservative','보수')}</div>
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
    </details>
    <details class="card fold">
      <summary><h2>근거 과거 공고 (${fmtNum(rows.length)}건)</h2><span class="meta-line" style="margin:0;">예측에 쓰인 공고 · 최근 순</span></summary>
      <div class="btn-row" style="margin:12px 0 8px;"><button class="btn sm ghost" id="csvBtn" type="button">CSV 내보내기</button></div>
      <div class="table-wrap"><table>
        <thead><tr><th>공고명</th><th>기관</th><th class="num">낙찰율</th><th class="num">사정율</th><th class="num">기초금액</th><th class="num">참가</th><th>개찰일</th></tr></thead>
        <tbody>${rows.slice(0, 1000).map(r => `<tr><td class="wrap">${esc(r.nm)}</td><td>${esc(recOrg(r))}</td><td class="num">${pct(r.rate)}</td><td class="num">${pct(r.sr)}</td><td class="num">${eok(r.base)}</td><td class="num">${r.cnt ?? '-'}</td><td>${esc(r.date)}</td></tr>`).join('')}</tbody>
      </table></div>
      ${rows.length > 1000 ? `<div class="meta-line">화면에는 최근 1,000건만 표시합니다. 전체는 CSV로 받으세요.</div>` : ''}
    </details>`;
  out.querySelectorAll('[data-use-sr]').forEach(btn => btn.addEventListener('click', () => {
    $('cRate').value = (+btn.dataset.useSr).toFixed(4);
    renderCalc();
    $('calcResult').scrollIntoView({behavior:'smooth', block:'center'});
  }));
  $('csvBtn').onclick = () => downloadCSV('예측근거_과거공고.csv',
    ['공고번호','공고명','발주기관','수요기관','시도','시군','면허','기초금액','예정가격','낙찰금액','낙찰율','사정율','참가업체수','낙찰하한율','A값','개찰일'],
    rows.map(r => [r.no, r.nm, r.org, r.dmd, r.sido, r.sgg, (r.lic||[]).join(' '), r.base, r.plan, r.amt, r.rate, r.sr, r.cnt, r.floor, r.a, r.date]));
}

function calcValues(){
  const base = numOf($('cBase')), a = numOf($('cA'));
  const floor = +$('cFloor').value || DEFAULT_FLOOR, sr = +$('cRate').value || 0;
  const net = numOf($('cNet')), manual = numOf($('cManual'));
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
  const rng = $('pRng').value ? $('pRng').value.split(',').map(Number) : P.notice?.rng;
  const info = [];
  if(rng && rng[0] != null && rng[1] != null && (c.sr < 100 + rng[0] || c.sr > 100 + rng[1])) info.push(`적용 사정율이 공고 예가범위(${100+rng[0]}~${100+rng[1]}%) 밖입니다.`);
  if(P.last?.rec){
    const r = P.last.rec;
    info.push(`이 투찰 사정률(${c.sr}%)의 과거 낙찰확률 <b>${(r.at(c.sr)*100).toFixed(2)}%</b> · 추천 ${pct(r.x, 3)}에서 ${(r.p*100).toFixed(2)}%`);
  }
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
  const base = numOf($('cBase'));
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

// ---------- 개찰 결과 실시간 조회 (조달청 낙찰정보서비스, 같은 서비스키)
const SCS_BASE = () => Data.meta?.api?.base?.scsbid || 'https://apis.data.go.kr/1230000/as/ScsbidInfoService';
/** 공고 1건의 개찰 순위 + 예정가격. 아직 개찰 전이면 null */
async function fetchOpeningResult(w){
  const ord = w.ord || '000';
  const [ranks, prices] = await Promise.all([
    liveCall('getOpengResultListInfoOpengCompt', {bidNtceNo: w.no, bidNtceOrd: ord, numOfRows: 999, pageNo: 1}, SCS_BASE()).then(r => r.items),
    liveCall('getOpengResultListInfoCnstwkPreparPcDetail', {inqryDiv: '2', bidNtceNo: w.no, numOfRows: 100, pageNo: 1}, SCS_BASE()).then(r => r.items).catch(() => []),
  ]);
  const lastRebid = (lst) => { const rb = lst.map(i => +(i.rbidNo || 0)); const mx = Math.max(0, ...rb); return lst.filter((_, k) => rb[k] === mx); };
  const R = lastRebid(ranks).map(it => ({
    rank: +(pickF(it, 'opengRank', 'rank') || 0), name: String(pickF(it, 'prcbdrNm', 'bidprcCorpNm', 'corpNm') || '').trim(),
    biz: String(pickF(it, 'prcbdrBizno', 'bizno', 'bizNo') || '').replace(/\D/g, ''), amt: numF(pickF(it, 'bidprcAmt', 'bidAmt')),
    rate: numF(pickF(it, 'bidprcrt', 'bidprcRt', 'bidRate')), note: String(pickF(it, 'rmrk', 'rmk') || '').trim(),
  })).filter(x => x.amt).sort((a, b) => (a.rank || 1e9) - (b.rank || 1e9));
  const pr = lastRebid(prices);
  const prOrd = pr.filter(it => String(pickF(it, 'bidNtceOrd') ?? ord) === ord);
  const P2 = prOrd.length ? prOrd : pr;
  const plan = P2.map(it => numF(pickF(it, 'plnprc', 'plnPrc'))).find(Boolean) || null;
  const base = P2.map(it => numF(pickF(it, 'bssamt', 'bsisAmt', 'bssAmt'))).find(Boolean) || w.base || null;
  if(!R.length && !plan) return null;
  const biz = String(Company.get().biz || '').replace(/\D/g, '');
  const mine = biz ? R.find(x => x.biz === biz) || null : null;
  return {at: new Date().toISOString(), n: R.length, plan, base, win: R[0] || null, mine,
    top: R.slice(0, 10), xs: R.map(x => x.amt)};   // xs = 전체 투찰금액 (순위 계산용)
}
const opened = (w) => { const t = parseKst(w.open || w.close); return t && t < new Date(); };

// ============================================================ 관심공고 (내 투찰 기록 → 개찰 뒤 검증 → 다음 투찰 보정)
/** 내 투찰금액을 승리 구간과 비교. 개찰 상세가 있으면 내 예상 순위도 센다. */
function judgeBid(amt, r, op){
  const w = winWindow(r, op);
  if(!amt || !w) return null;
  const base = op?.base || r.base, a = r.a || 0, floor = r.floor || DEFAULT_FLOOR;
  const x = ((amt - a) / (floor / 100) + a) / base * 100;
  const floorPrice = Math.ceil((w.S * base / 100 - a) * floor / 100 + a);
  const winAmt = bidAmount(base, w.W, a, floor);
  let rank = null;
  if(op?.r?.length && x >= w.S){
    rank = 1 + op.r.filter(row => { const y = bidToSr(row[2], base, a, floor); return y != null && y >= w.S && y < x; }).length;
  }
  const res = x < w.S ? {cls: 'below', label: '하한 미달', gap: floorPrice - amt, gapText: `낙찰하한가보다 ${won(floorPrice - amt)} 낮음`}
    : x < w.W ? {cls: 'win', label: '낙찰권 (1순위)', gap: 0, gapText: `1위보다 ${won(winAmt - amt)} 낮게 씀`}
    : {cls: 'high', label: '1위보다 높음', gap: amt - winAmt, gapText: `1위보다 ${won(amt - winAmt)} 높음`};
  return {...res, x, S: w.S, W: w.W, rank, floorPrice};
}

/** 내 투찰 기록들로 "투찰 사정률을 얼마나 옮겼으면 가장 많이 낙찰권이었나" (−1 ~ +1%p, 0.001 간격) */
function calibrate(list){
  if(!list.length) return null;
  const wins0 = list.filter(j => j.S <= j.x && j.x < j.W).length;
  let best = {shift: 0, k: wins0};
  for(let i = -1000; i <= 1000; i++){
    const d = i / 1000;
    const k = list.filter(j => j.S <= j.x + d && j.x + d < j.W).length;
    if(k > best.k || (k === best.k && Math.abs(d) < Math.abs(best.shift))) best = {shift: d, k};
  }
  return {n: list.length, wins: wins0, best: best.k, shift: best.shift};
}

let watchMode = LS.get('watchMode', 'watch');   // 'watch' 관심공고 | 'joined' 참여한 공고
const isJoined = (w) => !!(w.joined || w.myBid || w.res?.mine);

/** 수집된 개찰 상세(전체 순위)에서 우리 사업자번호가 있는 공고를 찾는다 (regions.json 지역만) */
async function findMyBidsInOpening(skipIds){
  const biz = String(Company.get().biz || '').replace(/\D/g, '');
  if(!biz) return [];
  const out = [];
  for(const sido of (Data.meta.detail?.regions || []).filter(s => Data.hasDetail(s))){
    let op, sc = null;
    try{ op = await Data.loadOpening(sido); if(Data.hasScsbid(sido)) sc = await Data.loadScsbid(sido); }catch(e){ continue; }
    for(const [id, b] of op.bids){
      if(skipIds.has(id) || !b.r?.length) continue;
      const ci = (b.corps || []).findIndex(c => c[1] === biz);
      if(ci < 0) continue;
      const row = b.r.find(x => x[1] === ci);
      if(!row) continue;
      const rec = sc?.byId.get(id) || {};
      const cut = id.lastIndexOf('-');
      const top = b.r.slice(0, 10).map(x => ({rank: x[0], name: b.corps[x[1]]?.[0] || '', biz: b.corps[x[1]]?.[1] || '', amt: x[2], rate: x[3], note: x[4] || ''}));
      out.push({id, no: id.slice(0, cut), ord: id.slice(cut + 1), nm: rec.nm || id, org: rec.org, dmd: rec.dmd, sido, sgg: rec.sgg,
        lic: rec.lic, base: b.base || rec.base, a: rec.a, floor: rec.floor, rng: rec.rng, open: b.date, close: b.date,
        joined: true, auto: true, myBid: row[2],
        res: {n: b.r.length, plan: b.plan || rec.plan, base: b.base || rec.base, win: top[0] || null,
              mine: {rank: row[0], amt: row[2], biz, name: b.corps[ci][0]}, top, xs: b.r.map(x => x[2])}});
    }
  }
  return out.sort((x, y) => (y.open || '').localeCompare(x.open || '')).slice(0, 200);
}

/** 공고번호로 참여 공고 추가: 진행중·실시간·수집 데이터에서 정보를 찾고, 개찰됐으면 결과까지 조회 */
async function addJoinedByNo(text){
  const m = String(text).trim().toUpperCase().match(/^([A-Z0-9]+?)(?:-(\d{1,3}))?$/);
  if(!m) throw new Error('공고번호 형식을 확인하세요 (예: R26BK01735101-000)');
  const no = m[1], ord = (m[2] || '000').padStart(3, '0'), id = `${no}-${ord}`;
  let info = Data.bids?.find(b => b.no === no) || Live.items.find(b => b.no === no);
  if(!info) for(const s of Object.values(Data.scsbid)){ const r = s.byId.get(id) || s.recs.find(r => r.no === no); if(r){ info = r; break; } }
  const w = {...(info ? pickNotice(info) : {}), id, no, ord, nm: info?.nm || id, joined: true, savedAt: new Date().toISOString()};
  if(info?.date && !w.open) w.open = info.date;
  if(apiKey()){
    try{
      const res = await fetchOpeningResult(w);
      if(res){ w.res = res; w.myBid = res.mine?.amt || null; if(!w.open) w.open = new Date().toISOString().slice(0, 10); }
      w.resTried = new Date().toISOString();
    }catch(e){ console.warn(e); }
  }
  await WatchStore.save(w);
  return w;
}

async function renderWatch(){
  const el = $('watchList');
  let items = await WatchStore.list();
  el.innerHTML = loadingHtml();
  const sidos = [...new Set(items.map(i => i.sido).filter(s => s && Data.hasScsbid(s)))];
  try{
    await Data.loadScsbidMany(sidos);
    await Promise.all(sidos.filter(s => Data.hasDetail(s)).map(s => Data.loadOpening(s)));
  }catch(e){ console.warn(e); }
  // 개찰 시각이 지난 공고는 조달청에서 결과를 바로 가져온다 (한 번에 최대 5건, 결과 없으면 1시간 뒤 다시)
  if(apiKey()){
    const due = items.filter(w => opened(w) && !w.res?.n && (!w.resTried || Date.now() - Date.parse(w.resTried) > 3600000)).slice(0, 5);
    if(due.length){
      el.innerHTML = loadingHtml(`개찰 결과 조회 중… (${due.length}건)`);
      for(const w of due){
        try{
          const res = await fetchOpeningResult(w);
          await WatchStore.save({...w, resTried: new Date().toISOString(), ...(res ? {res, myBid: w.myBid || res.mine?.amt || null} : {})});
        }catch(e){ console.warn('개찰 조회', w.id, e); await WatchStore.save({...w, resTried: new Date().toISOString()}); }
      }
      items = await WatchStore.list();
    }
  }
  const joinedMode = watchMode === 'joined';
  const auto = joinedMode ? await findMyBidsInOpening(new Set(items.map(w => w.id))) : [];
  const pseudo = new Map(auto.map(w => [w.id, w]));
  const list = joinedMode ? [...items.filter(isJoined), ...auto] : items;
  const modeSeg = `<div class="seg mode-seg" id="wMode">
      <button data-v="watch" class="${joinedMode ? '' : 'on'}" type="button">⭐ 관심공고 (${fmtNum(items.length)})</button>
      <button data-v="joined" class="${joinedMode ? 'on' : ''}" type="button">📝 참여한 공고 (${fmtNum(items.filter(isJoined).length + auto.length)})</button>
    </div>`;
  const hasBiz = !!Company.get().biz;
  const joinForm = joinedMode ? `<div class="join-add">
      <input type="text" id="joinNo" placeholder="공고번호로 추가 (예: R26BK01735101-000)" autocomplete="off">
      <button class="btn sm" id="joinAdd" type="button">참여 공고 추가</button>
      <span class="meta-line" id="joinMsg" style="margin:0;"></span>
    </div>
    <div class="meta-line" style="margin:0 0 12px;">${hasBiz ? `사업자번호로 <b>강원 개찰 상세</b>에서 우리가 넣은 공고를 자동으로 찾고(${fmtNum(auto.length)}건), 개찰되면 조달청에서 순위·금액을 바로 가져옵니다.` : '설정 → 우리 업체에 <b>사업자번호</b>를 넣으면 우리 순위·금액을 자동으로 찾고, 수집된 개찰 상세에서 참여 공고도 자동으로 모읍니다.'} 조달청 API에 "사업자번호로 찾기"가 없어서, 다른 지역 공고는 공고번호로 추가하거나 ☆ 저장 뒤 "참여 표시"를 눌러 주세요.</div>` : '';
  if(!list.length){
    el.innerHTML = modeSeg + joinForm + `<div class="empty">${joinedMode ? '아직 참여한 공고가 없습니다. 공고번호로 추가하거나, 관심공고에서 "참여 표시"를 누르세요.' : '아직 저장한 관심공고가 없습니다. 입찰공고 탭에서 ☆를 눌러 보세요.'}</div>`;
    bindWatch(el, pseudo);
    return;
  }
  const findRec = (w) => {
    const s = Data.scsbid[w.sido];
    if(!s) return null;
    return s.byId.get(w.id) || s.recs.find(r => r.no === w.no) || null;
  };
  const judged = [];
  let nBid = 0, nOpen = 0, nFirst = 0; const ranks = [];
  const cards = list.map(w => {
    // 판정 재료: 조달청 실시간/개찰 상세 결과(res) 우선, 없으면 수집된 낙찰 기록
    const r0 = findRec(w);
    const res = w.res?.plan ? w.res : null;
    const r = res ? {base: res.base || r0?.base || w.base, plan: res.plan, amt: res.win?.amt || r0?.amt, a: w.a ?? r0?.a, floor: w.floor || r0?.floor,
                     cnt: res.n || r0?.cnt, win: res.win?.name || r0?.win, date: (w.open || '').slice(0, 10) || r0?.date}
      : r0 ? {...r0} : null;
    if(r && r.base && r.plan) r.sr = r.plan / r.base * 100;
    const op = res ? {base: r.base, plan: r.plan, r: res.xs.map((amt, i) => [i + 1, 0, amt])} : r0 ? Data.opening[w.sido]?.bids.get(r0.id) : null;
    const dd = ddayLabel(w.close);
    const mine = r ? judgeBid(w.myBid, r, op) : null;
    const rec = r ? judgeBid(w.pred?.bid, r, op) : null;
    if(isJoined(w)) nBid++;
    if(r) nOpen++;
    const rank = w.res?.mine?.rank || mine?.rank;
    if(rank) ranks.push(rank);
    if(rank === 1) nFirst++;
    if(mine) judged.push(mine);
    let body;
    if(r && r.sr != null){
      body = `<div class="stat-grid" style="margin-top:8px;">
          <div class="stat ${rank === 1 ? 'hl' : ''}"><div class="t">우리 순위</div><div class="v">${rank ? fmtNum(rank) + '위' + (r.cnt ? ` <small class="faint">/ ${fmtNum(r.cnt)}곳</small>` : '') : w.myBid ? '-' : '기록 없음'}</div></div>
          <div class="stat"><div class="t">1위</div><div class="v" style="font-size:14px;">${esc(r.win || '-')}<br>${won(r.amt)}</div></div>
          <div class="stat"><div class="t">실제 사정율</div><div class="v">${pct(r.sr)}</div></div>
          <div class="stat"><div class="t">예정가격</div><div class="v" style="font-size:14px;">${won(r.plan)}</div></div>
        </div>
        <div class="meta-line">개찰 ${esc(r.date || '')}${res ? (w.auto ? ' · 수집된 개찰 상세' : ' · 조달청 실시간 조회') : ' · 수집 데이터'}</div>
        ${mine ? `<div class="verdict ${mine.cls}"><b>내 투찰 ${won(w.myBid)}</b> → ${rank === 1 ? '🏆 1순위' : mine.label}${rank && rank !== 1 ? ` · ${fmtNum(rank)}순위` : ''} · ${mine.gapText} <span class="faint">(투찰 사정률 ${mine.x.toFixed(3)}%)</span></div>` : ''}
        ${rec ? `<div class="verdict ${rec.cls} soft">앱 추천 ${won(w.pred.bid)} → ${rec.label} · ${rec.gapText}</div>` : ''}
        ${res?.top?.length ? `<details class="ranks"><summary>개찰 순위 상위 ${res.top.length}곳 보기</summary><div class="table-wrap" style="max-height:none;margin-top:6px;"><table>
          <thead><tr><th>순위</th><th>업체</th><th class="num">투찰금액</th><th class="num">투찰률</th><th>비고</th></tr></thead>
          <tbody>${res.top.map(x => `<tr${res.mine && x.biz === res.mine.biz ? ' class="hl-row"' : ''}><td>${x.rank || '-'}</td><td>${esc(x.name)}</td><td class="num">${won(x.amt)}</td><td class="num">${x.rate != null ? pct(x.rate) : '-'}</td><td>${esc(x.note)}</td></tr>`).join('')}</tbody></table></div></details>` : ''}`;
    }else{
      const st = opened(w) ? (apiKey() ? '개찰 시각이 지났지만 아직 결과가 올라오지 않았습니다' : '개찰됨 · 설정에서 서비스키를 넣으면 결과를 바로 조회합니다') : `개찰 전${w.open ? ' · 개찰 ' + esc(w.open.slice(5, 16).replace('-', '/')) : ''}`;
      body = `<div class="meta-line">${st} · 앱 추천 ${w.pred?.bid ? won(w.pred.bid) : '없음'}</div>`;
    }
    const star = w.auto
      ? `<button class="btn sm ghost" data-join-save="${esc(w.id)}" type="button">＋ 저장</button>`
      : `<button class="star on" data-unwatch="${esc(w.id)}" title="관심 해제" type="button">★</button>`;
    return `<div class="bid">
      <div class="bid-top"><div>
        <div class="bid-title">${isJoined(w) ? '<span class="badge blue" style="margin-right:4px;">참여</span>' : ''}${esc(w.nm)}</div>
        <div class="bid-sub">${esc(w.org || '')}${w.sido ? ' · ' + esc([w.sido, w.sgg].filter(Boolean).join(' ')) : ''} · ${esc(w.no ? `${w.no}-${w.ord}` : '')}${w.close ? ` · <span class="dday ${dd.urgent ? 'urgent' : ''}">${esc(dd.text)}</span>` : ''}</div>
      </div>${star}</div>
      ${body}
      <div class="mybid-row">
        <label>내가 넣은 투찰금액</label>
        <input type="text" class="money" inputmode="numeric" autocomplete="off" data-mybid-in="${esc(w.id)}" value="${moneyText(w.myBid)}" placeholder="${w.pred?.bid ? '예: ' + moneyText(w.pred.bid) : '원 단위'}">
        <button class="btn sm ghost" data-mybid-save="${esc(w.id)}" type="button">기록</button>
        ${opened(w) && apiKey() ? `<button class="btn sm line" data-res-fetch="${esc(w.id)}" type="button">개찰 결과 조회</button>` : ''}
        ${!w.auto ? `<button class="btn sm line" data-join-toggle="${esc(w.id)}" type="button">${w.joined ? '참여 표시 해제' : '참여 표시'}</button>` : ''}
      </div>
      <div class="bid-actions">${findNotice(w.id) ? `<button class="btn sm" data-predict="${esc(w.id)}" type="button">이 공고로 예측</button>` : ''}
        ${w.url ? `<a class="btn line sm" href="${esc(w.url)}" target="_blank" rel="noopener">공고 원문</a>` : ''}</div>
    </div>`;
  });
  const cal = calibrate(judged);
  LS.set('myCal', cal);
  const cnt = (c) => judged.filter(j => j.cls === c).length;
  const head = joinedMode ? `<div class="kpis" style="grid-template-columns:repeat(4,minmax(0,1fr));">
      <div class="kpi" style="cursor:default;"><span class="t">참여</span><span class="v">${fmtNum(list.length)}</span></div>
      <div class="kpi" style="cursor:default;"><span class="t">개찰됨</span><span class="v">${fmtNum(nOpen)}</span></div>
      <div class="kpi ${nFirst ? 'on' : ''}" style="cursor:default;"><span class="t">🏆 1순위</span><span class="v">${fmtNum(nFirst)}</span></div>
      <div class="kpi" style="cursor:default;"><span class="t">평균 순위</span><span class="v">${ranks.length ? fmtNum(stats(ranks).mean, 1) + '위' : '-'}</span></div>
    </div>` : `<div class="kpis" style="grid-template-columns:repeat(3,minmax(0,1fr));">
      <div class="kpi" style="cursor:default;"><span class="t">관심공고</span><span class="v">${fmtNum(items.length)}</span></div>
      <div class="kpi" style="cursor:default;"><span class="t">참여</span><span class="v">${fmtNum(nBid)}</span></div>
      <div class="kpi" style="cursor:default;"><span class="t">개찰됨</span><span class="v">${fmtNum(nOpen)}</span></div>
    </div>`;
  const summary = cal ? `<div class="card-inner my-score">
      <h3 style="margin-top:0;">내 투찰 성적 (개찰된 ${fmtNum(cal.n)}건)${sampleBadge(cal.n)}</h3>
      <div class="stat-grid">
        <div class="stat"><div class="t">낙찰권</div><div class="v" style="color:var(--ok);">${cnt('win')}</div></div>
        <div class="stat"><div class="t">하한 미달</div><div class="v" style="color:var(--target);">${cnt('below')}</div></div>
        <div class="stat"><div class="t">1위보다 높음</div><div class="v">${cnt('high')}</div></div>
        <div class="stat hl"><div class="t">다음 투찰 보정</div><div class="v">${cal.shift >= 0 ? '+' : ''}${cal.shift.toFixed(3)}%p</div></div>
      </div>
      <div class="meta-line">내 투찰 사정률을 ${cal.shift >= 0 ? '+' : ''}${cal.shift.toFixed(3)}%p 옮겼다면 낙찰권이 ${cal.wins}건 → ${cal.best}건이었습니다. ${cnt('high') > cnt('below') ? '대체로 1위보다 높게 쓰는 편입니다.' : cnt('below') > cnt('high') ? '대체로 하한 미달이 많은 편입니다.' : ''} 표본이 적을수록 우연일 수 있습니다.</div>
    </div>` : '';
  el.innerHTML = modeSeg + joinForm + head + summary + `<div class="bid-list" style="margin-top:0;">${cards.join('')}</div>`;
  bindWatch(el, pseudo);
}

function bindWatch(el, pseudo){
  const getW = async (id) => (await WatchStore.list()).find(x => x.id === id) || pseudo.get(id);
  $('wMode')?.addEventListener('click', (e) => {
    const v = e.target.closest('[data-v]')?.dataset.v; if(!v) return;
    watchMode = v; LS.set('watchMode', v); renderWatch();
  });
  $('joinAdd')?.addEventListener('click', async () => {
    const msg = $('joinMsg');
    msg.innerHTML = loadingHtml('추가하고 결과 조회 중…');
    try{
      const w = await addJoinedByNo($('joinNo').value);
      msg.textContent = w.res ? '추가됨 · 개찰 결과를 가져왔습니다' : `추가됨${apiKey() ? ' · 아직 개찰 결과가 없습니다' : ' · 서비스키를 넣으면 결과를 조회합니다'}`;
      setTimeout(renderWatch, 600);
    }catch(e){ msg.textContent = e.message; }
  });
  el.querySelectorAll('[data-mybid-save]').forEach(btn => btn.addEventListener('click', async () => {
    const id = btn.dataset.mybidSave;
    const inp = el.querySelector(`[data-mybid-in="${CSS.escape(id)}"]`);
    const w = await getW(id);
    if(!w) return;
    const {auto, ...rest} = w;
    await WatchStore.save({...rest, myBid: numOf(inp) || null});
    renderWatch();
  }));
  el.querySelectorAll('[data-join-save]').forEach(btn => btn.addEventListener('click', async () => {
    const w = pseudo.get(btn.dataset.joinSave); if(!w) return;
    const {auto, ...rest} = w;
    await WatchStore.save({...rest, savedAt: new Date().toISOString()});
    renderWatch();
  }));
  el.querySelectorAll('[data-join-toggle]').forEach(btn => btn.addEventListener('click', async () => {
    const w = await getW(btn.dataset.joinToggle); if(!w) return;
    await WatchStore.save({...w, joined: !w.joined});
    renderWatch();
  }));
  el.querySelectorAll('[data-res-fetch]').forEach(btn => btn.addEventListener('click', async () => {
    const w = await getW(btn.dataset.resFetch);
    if(!w) return;
    btn.disabled = true; btn.textContent = '조회 중…';
    try{
      const res = await fetchOpeningResult(w);
      const {auto, ...rest} = w;
      await WatchStore.save({...rest, resTried: new Date().toISOString(), ...(res ? {res, myBid: w.myBid || res.mine?.amt || null} : {})});
      if(!res){ btn.textContent = '아직 결과 없음'; return; }
    }catch(e){ btn.textContent = '조회 실패'; btn.title = e.message; return; }
    renderWatch();
  }));
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
  const allX = [], allS = [];   // 경쟁사 투찰 사정률 · 실제 사정율 (겹쳐 그리기용)
  for(const [id, b] of op.bids){
    const r = sc.byId.get(id);
    if(!r || (cut && r.date < cut) || (lic && !(r.lic||[]).includes(lic))) continue;
    const drawn = (b.p||[]).filter(x => x[2]);
    if(drawn.length){ nDraw++; drawn.forEach(x => { if(x[0] >= 1 && x[0] <= 15) freq[x[0]]++; }); }
    const base = b.base || r.base, floor = r.floor || DEFAULT_FLOOR, a = r.a || 0;
    if(!b.r?.length) continue;
    nRank++;
    const ww = winWindow(r, b);
    if(ww) allS.push([ww.S, 1]);
    for(const row of b.r){
      const [rank, ci, amt] = row;
      const [name, biz] = b.corps[ci] || ['?', ''];
      const key = biz || name;
      let c = corps.get(key);
      if(!c) corps.set(key, c = {name, biz, n:0, wins:0, srs:[]});
      c.n++;
      if(r.winBiz ? biz === r.winBiz : rank === 1) c.wins++;
      const sr = bidToSr(amt, base, a, floor);
      if(sr != null){ c.srs.push(sr); allX.push([sr, 1 / b.r.length]); }
    }
  }
  const freqItems = freq.slice(1).map((c, i) => ({label: String(i+1), v: nDraw ? c / nDraw : 0}));
  const top = [...corps.values()].sort((a,b) => b.n - a.n).slice(0, 20);
  top.forEach(c => c.st = stats(c.srs));
  const sSorted = allS.map(v => v[0]).sort((x, y) => x - y);
  const dMin = sSorted.length ? Math.floor(quantile(sSorted, .01) * 10) / 10 - 0.2 : 98, dMax = sSorted.length ? Math.ceil(quantile(sSorted, .99) * 10) / 10 + 0.2 : 102;
  const densCard = allS.length >= 5 ? `<div class="card">
      <h2>경쟁사 투찰 사정률 vs 실제 사정율</h2>
      <p class="sub">파란 선 = 경쟁사들이 투찰한 위치(공고마다 같은 비중), 옅은 막대 = 실제 사정율이 떨어진 위치. 막대는 높은데 선이 낮은 곳이 "사정율은 자주 오는데 경쟁사가 덜 몰린" 빈틈입니다.</p>
      ${plot([{pts: binPts(allS, dMin, dMax, 0.02), color: 'var(--text-sub)', label: '실제 사정율', bars: true},
              {pts: binPts(allX, dMin, dMax, 0.02), color: 'var(--primary)', label: '경쟁사 투찰 사정률'}], {min: dMin, max: dMax})}
      <div class="meta-line">개찰 순위 ${sampleText(nRank)} · 투찰 ${fmtNum(allX.length)}건</div>
    </div>` : '';
  el.innerHTML = html + densCard + `
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
  $('setVal').innerHTML = renderValidation();
  $('appVersion').textContent = await getAppVersion();
  const theme = LS.get('theme', 'auto');
  $('themeSeg').querySelectorAll('button').forEach(b => b.classList.toggle('on', b.dataset.v === theme));
}

/** 매일 자동 역검증 표 (model.json 의 validation) */
function renderValidation(){
  const V = Model.m?.validation;
  if(!V?.total?.n) return '<div class="empty">아직 역검증 결과가 없습니다. 데이터가 두 달 이상 쌓이면 수집 때 자동으로 계산됩니다.</div>';
  const T = V.total, rate = (k, n) => n ? (k / n * 100).toFixed(2) + '%' : '-';
  const d = T.mean ? (T.near / T.mean - 1) * 100 : 0;
  return `<div class="stat-grid">
      <div class="stat hl"><div class="t">추천값(비슷한 경쟁 규모)</div><div class="v">${fmtNum(T.near)}건 · ${rate(T.near, T.n)}</div></div>
      <div class="stat"><div class="t">평균 사정율로 넣었다면</div><div class="v">${fmtNum(T.mean)}건 · ${rate(T.mean, T.n)}</div></div>
      <div class="stat"><div class="t">평균 업체 기대</div><div class="v">${fmtNum(T.rand, 1)}건</div></div>
      <div class="stat"><div class="t">추천값 vs 평균 사정율</div><div class="v" style="color:${d > 0 ? 'var(--ok)' : 'var(--warn)'}">${d >= 0 ? '+' : ''}${d.toFixed(1)}%</div></div>
    </div>
    <div class="meta-line">시험 공고 ${fmtNum(T.n)}건 · 추천값 95% 신뢰구간 ${(T.near_ci[0]*100).toFixed(2)}~${(T.near_ci[1]*100).toFixed(2)}% · 하한 미달 추천값 ${(T.below_near*100).toFixed(0)}% / 평균 ${(T.below_mean*100).toFixed(0)}% · ${esc(V.rule)}</div>
    <h3>달별</h3>
    <div class="table-wrap" style="max-height:none;"><table>
      <thead><tr><th>시험 달</th><th class="num">공고</th><th class="num">추천값 낙찰</th><th class="num">평균 사정율 낙찰</th><th class="num">평균 업체 기대</th></tr></thead>
      <tbody>${V.months.map(m => `<tr><td>${esc(m.m)}</td><td class="num">${fmtNum(m.n)}</td><td class="num"><b>${m.near}</b></td><td class="num">${m.mean}</td><td class="num">${fmtNum(m.rand, 1)}</td></tr>`).join('')}</tbody>
    </table></div>
    <h3>예상 참가 규모별 — 공고 고르기 효과</h3>
    <div class="table-wrap" style="max-height:none;"><table>
      <thead><tr><th>예상 참가</th><th class="num">공고</th><th class="num">추천값 낙찰률</th><th class="num">평균 사정율 낙찰률</th><th class="num">평균 업체</th></tr></thead>
      <tbody>${V.segments.map(s => `<tr><td>${s.k[0]}~${s.k[1] ?? ''}곳</td><td class="num">${fmtNum(s.n)}</td><td class="num"><b>${rate(s.near, s.n)}</b></td><td class="num">${rate(s.mean, s.n)}</td><td class="num">${rate(s.rand, s.n)}</td></tr>`).join('')}</tbody>
    </table></div>
    <div class="meta-line">예상 참가가 적은 공고일수록 낙찰률이 크게 높습니다 → 입찰공고 탭 "낙찰확률 높은 순"으로 공고를 고르세요. 데이터 ${esc(Model.m.data.from)} ~ ${esc(Model.m.data.to)} · 모델 갱신 ${esc(Model.m.updated_at.slice(0, 16).replace('T', ' '))}</div>`;
}

// ---------- 조달청 대조 점검: 수집 데이터가 조달청과 맞는지 무작위로 골라 다시 조회해 비교
async function runVerify(){
  const out = $('verifyResult');
  if(!apiKey()){ out.innerHTML = '<div class="alert warn">먼저 위 "실시간 공고 검색 키"에 서비스키를 넣어 주세요.</div>'; return; }
  const sido = $('vSido').value;
  if(!sido || !Data.hasScsbid(sido)){ out.innerHTML = '<div class="empty">낙찰 데이터가 있는 시·도를 고르세요.</div>'; return; }
  out.innerHTML = loadingHtml('조달청에서 다시 조회하는 중…');
  const sc = await Data.loadScsbid(sido);
  const cut = monthsAgo(1);
  const pool = sc.recs.filter(r => r.amt && r.base && (r.date || '') >= cut);
  const N = Math.min(10, pool.length);
  const pick = [...pool].sort(() => Math.random() - 0.5).slice(0, N);
  const rows = [];
  for(const r of pick){
    let res = null, err = '';
    try{ res = await fetchOpeningResult({no: r.no, ord: r.ord, base: r.base}); }catch(e){ err = e.message; }
    const near = (a, b, tol) => a && b ? Math.abs(a - b) <= Math.max(1, b * tol) : null;
    const amtOk = res ? res.xs.includes(r.amt) : null;                  // 우리 1위 금액이 조달청 투찰 목록에 그대로 있나
    const planOk = res?.plan ? near(r.plan, res.plan, 0.0001) : null;    // 예정가격 (낙찰률로 역산한 값이면 ±0.01%)
    const cntOk = res?.n ? r.cnt === res.n : null;
    rows.push({r, res, err, amtOk, planOk, cntOk});
  }
  const mark = (v) => v === true ? '<span class="badge ok">일치</span>' : v === false ? '<span class="badge">다름</span>' : '<span class="badge gray">확인 불가</span>';
  const tally = (k) => ({ok: rows.filter(x => x[k] === true).length, bad: rows.filter(x => x[k] === false).length});
  const ta = tally('amtOk'), tp = tally('planOk'), tc = tally('cntOk');
  out.innerHTML = `<div class="stat-grid">
      <div class="stat"><div class="t">1위(낙찰) 금액</div><div class="v">${ta.ok}/${ta.ok + ta.bad} 일치</div></div>
      <div class="stat"><div class="t">예정가격</div><div class="v">${tp.ok}/${tp.ok + tp.bad} 일치</div></div>
      <div class="stat"><div class="t">참가 업체 수</div><div class="v">${tc.ok}/${tc.ok + tc.bad} 일치</div></div>
    </div>
    <div class="table-wrap" style="margin-top:10px; max-height:none;"><table>
      <thead><tr><th>공고</th><th class="num">낙찰금액 (앱 / 조달청 1위)</th><th>금액</th><th class="num">예정가격 (앱 / 조달청)</th><th>예가</th><th class="num">참가 (앱 / 조달청)</th><th>참가</th></tr></thead>
      <tbody>${rows.map(x => `<tr><td class="wrap">${esc(x.r.nm)}<br><span class="faint">${esc(x.r.no)}-${esc(x.r.ord)} · ${esc(x.r.date)}</span>${x.err ? `<br><span class="faint">조회 실패: ${esc(x.err)}</span>` : !x.res ? '<br><span class="faint">조달청에 개찰 결과 없음</span>' : ''}</td>
        <td class="num">${won(x.r.amt)}<br><span class="faint">${won(x.res?.win?.amt)}</span></td><td>${mark(x.amtOk)}</td>
        <td class="num">${won(x.r.plan)}<br><span class="faint">${won(x.res?.plan)}</span></td><td>${mark(x.planOk)}</td>
        <td class="num">${x.r.cnt ?? '-'}<br><span class="faint">${x.res?.n ?? '-'}</span></td><td>${mark(x.cntOk)}</td></tr>`).join('')}</tbody>
    </table></div>
    <div class="meta-line">${esc(sido)} 최근 1개월 낙찰 중 무작위 ${N}건을 조달청 낙찰정보서비스(개찰 순위·예정가격)로 다시 조회해 비교했습니다. 낙찰금액은 조달청 투찰 목록에 같은 금액이 있으면 일치(1순위가 적격심사에서 떨어지면 낙찰자는 2순위 이하일 수 있음). 예정가격을 낙찰률로 역산한 공고는 ±0.01% 안이면 일치. 조달청 호출 ${N * 2}회를 씁니다.</div>`;
}

function initSettings(){
  fillSelect($('vSido'), SIDOS, {all: '시·도 선택', value: Company.get().sido || Data.meta.detail?.regions?.[0] || ''});
  $('vRun').addEventListener('click', runVerify);

  const keyMsg = (t) => { $('keyMsg').innerHTML = t; };
  keyMsg(LS.get('apiKey', '') ? '저장된 키 있음 (이 기기)' : '저장된 키 없음');
  $('keySave').addEventListener('click', async () => {
    const v = $('keyIn').value.trim();
    if(!v){ keyMsg('서비스키를 붙여넣으세요.'); return; }
    LS.set('apiKey', v); $('keyIn').value = '';
    keyMsg(loadingHtml('연결 확인 중…'));
    try{
      const d = kstDay(new Date()).replace(/-/g, '');
      const {total} = await liveCall('getBidPblancListInfoCnstwkPPSSrch', {inqryDiv: '1', inqryBgnDt: d + '0000', inqryEndDt: d + '2359', numOfRows: 1, pageNo: 1});
      keyMsg(`<span class="badge ok">연결 성공</span> 오늘 공사 공고 ${fmtNum(total)}건 확인`);
      Live.params = null;
    }catch(e){ keyMsg(`<span class="badge">연결 실패</span> ${esc(e.message)} (키는 저장됨)`); }
  });
  $('keyDel').addEventListener('click', () => { LS.set('apiKey', ''); Live.params = null; keyMsg('키를 지웠습니다.'); });
  // 이 기기의 키를 QR 로 보여줘 폰 카메라로 찍으면 폰 앱에 저장되게 한다 (키는 주소의 # 뒤에만 실려 서버로 가지 않음)
  $('keyQr').addEventListener('click', async () => {
    const box = $('keyQrBox');
    if(!box.hidden){ box.hidden = true; box.innerHTML = ''; return; }
    const k = String(LS.get('apiKey', '') || '').trim();
    if(!k){ keyMsg('먼저 이 기기에 서비스키를 저장하세요.'); return; }
    const url = location.origin + location.pathname + '#key=' + encodeURIComponent(k);
    box.hidden = false;
    box.innerHTML = loadingHtml('QR 만드는 중…');
    try{
      if(!window.QRCode) await new Promise((res, rej) => {
        const sc = document.createElement('script');
        sc.src = 'https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js';
        sc.onload = res; sc.onerror = () => rej(new Error('QR 라이브러리를 불러오지 못했습니다'));
        document.head.appendChild(sc);
      });
      box.innerHTML = '<div id="keyQrImg"></div><div class="meta-line">폰 카메라로 찍어 열면 폰 앱에 키가 저장됩니다. 다 쓰면 이 버튼을 다시 눌러 QR을 닫으세요. 이 QR·링크를 다른 사람에게 보내지 마세요.</div>';
      new QRCode($('keyQrImg'), {text: url, width: 200, height: 200, correctLevel: QRCode.CorrectLevel.M});
    }catch(e){ box.innerHTML = `<div class="alert warn">${esc(e.message)}</div>`; }
  });
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

/** Wilson 95% 신뢰구간 */
function wilson(k, n, z=1.96){
  if(!n) return [0, 0];
  const p = k / n, d = 1 + z*z/n, c = (p + z*z/(2*n)) / d, h = z * Math.sqrt(p*(1-p)/n + z*z/(4*n*n)) / d;
  return [Math.max(0, c - h), Math.min(1, c + h)];
}
const BT_SMOOTHS = [0.005, 0.01, 0.03, 0.05];

/** 롤링 표본외 백테스트: 최근 12개월 각 달 m 에 대해 m 이전 24개월로만 추천값을 정하고(예가범위별),
 *  m 달의 실제 공고에서 S ≤ x < W 이면 성공. 무작위 기준(1/참가수)·평균 전략·곡선 전략(폭별)을 비교한다. */
async function runBacktest(){
  const out = $('btResult');
  const sido = $('btSido').value;
  const months = Math.max(3, Math.min(24, +$('btN').value || 12));
  if(!sido || !Data.hasScsbid(sido)){ out.innerHTML = '<div class="empty">낙찰 데이터가 있는 시·도를 선택하세요.</div>'; return; }
  out.innerHTML = loadingHtml('과거를 한 달씩 다시 재생하는 중…');
  const sc = await Data.loadScsbid(sido);
  const op = Data.hasDetail(sido) ? (await Data.loadOpening(sido)).bids : null;
  await new Promise(r => setTimeout(r, 20));
  const usable = sc.recs.filter(r => r.date && winWindow(r, op?.get(r.id)));
  if(!usable.length){ out.innerHTML = '<div class="empty">예정가격·낙찰금액이 있는 공고가 없습니다.</div>'; return; }
  const last = usable[0].date.slice(0, 7);
  const monthList = [];
  for(let i = 0; i < months; i++) monthList.push(addMonths(last + '-01', -i).slice(0, 7));
  const strat = {random: {k: 0, n: 0}, mean: {k: 0, n: 0, below: 0}};
  BT_SMOOTHS.forEach(s => strat['c' + s] = {k: 0, n: 0, below: 0});
  let randSum = 0, nTest = 0;
  const monthly = [];
  for(const m of monthList.reverse()){
    const mStart = m + '-01', mEnd = addMonths(mStart, 1), tStart = addMonths(mStart, -24);
    const test = usable.filter(r => r.date >= mStart && r.date < mEnd);
    const train = usable.filter(r => r.date >= tStart && r.date < mStart);
    if(!test.length || train.length < MIN_SAMPLE) continue;
    const row = {m, n: 0, mean: 0, curve: 0};
    const groups = new Map();
    test.forEach(t => { const k = (t.rng || []).join(','); if(!groups.has(k)) groups.set(k, []); groups.get(k).push(t); });
    for(const [k, tests] of groups){
      const same = k ? train.filter(r => (r.rng || []).join(',') === k) : [];
      const tr = same.length >= MIN_SAMPLE ? same : train;
      const now = parseKst(mStart).getTime();
      const meanX = stats(tr.map(r => winWindow(r, op?.get(r.id)).S)).mean;
      const xs = {};
      for(const s of BT_SMOOTHS) xs[s] = winCurve(tr, {now, sido, opening: op, smooth: s})?.best.x ?? meanX;
      for(const t of tests){
        const w = winWindow(t, op?.get(t.id));
        const hit = (x) => w.S <= x && x < w.W;
        nTest++; row.n++;
        if(t.cnt){ strat.random.n++; randSum += 1 / t.cnt; }
        strat.mean.n++; if(hit(meanX)){ strat.mean.k++; row.mean++; } else if(meanX < w.S) strat.mean.below++;
        for(const s of BT_SMOOTHS){
          const st = strat['c' + s]; st.n++;
          if(hit(xs[s])){ st.k++; if(s === 0.01) row.curve++; } else if(xs[s] < w.S) st.below++;
        }
      }
    }
    monthly.push(row);
  }
  if(!nTest){ out.innerHTML = '<div class="empty">시험할 수 있는 공고가 부족합니다 (각 달 이전 24개월에 30건 이상 필요).</div>'; return; }
  const randP = strat.random.n ? randSum / strat.random.n : null;
  const line = (name, st, note='') => {
    const rate = st.k / st.n, [lo, hi] = wilson(st.k, st.n);
    const lift = randP ? rate / randP : null;
    return {name, note, rate, lo, hi, lift, liftLo: randP ? lo / randP : null, liftHi: randP ? hi / randP : null, below: st.below / st.n, n: st.n, k: st.k};
  };
  const curves = BT_SMOOTHS.map(s => ({s, ...line(`곡선 전략 (폭 ±${s}%p)`, strat['c' + s])}));
  const bestC = curves.reduce((b, c) => c.rate > b.rate ? c : b, curves[0]);
  const meanL = line('평균 전략 (평균 사정율)', strat.mean);
  const adopt = bestC.n >= 2000 && bestC.liftLo != null && bestC.liftLo > 1;
  // 가장 좋았던 폭을 앱 전체 곡선에 쓰고, 결과를 시·도별로 기억
  LS.set('curveSmooth', bestC.s);
  qpCache.clear();
  const saved = LS.get('btResult', {});
  saved[sido] = {n: bestC.n, rate: bestC.rate, lift: bestC.lift, liftLo: bestC.liftLo, adopt, smooth: bestC.s, at: new Date().toISOString()};
  LS.set('btResult', saved);

  const tr = (l, cls='') => `<tr class="${cls}"><td>${l.name}${l.note}</td><td class="num">${(l.rate*100).toFixed(2)}%</td><td class="num">${(l.lo*100).toFixed(2)}~${(l.hi*100).toFixed(2)}%</td>
    <td class="num">${l.lift ? '×' + l.lift.toFixed(2) : '-'}${l.liftLo ? ` <span class="faint">(${l.liftLo.toFixed(2)}~${l.liftHi.toFixed(2)})</span>` : ''}</td>
    <td class="num">${(l.rate*100).toFixed(1)}건</td><td class="num">${l.below != null ? (l.below*100).toFixed(1) + '%' : '-'}</td></tr>`;
  out.innerHTML = `
    <div class="alert ${adopt ? 'ok' : 'warn'}">${adopt
      ? `✅ <b>우위 있음</b> — 곡선 추천값이 무작위보다 ${bestC.lift.toFixed(2)}배(신뢰구간 하한 ${bestC.liftLo.toFixed(2)}배) 자주 낙찰권이었습니다. 추천값으로 사용합니다.`
      : `⚠️ <b>뚜렷한 우위 없음</b> — ${bestC.n < 2000 ? `시험 공고가 ${fmtNum(bestC.n)}건으로 2,000건 미만입니다. ` : ''}${bestC.liftLo != null ? `무작위 대비 배수 신뢰구간 하한 ${bestC.liftLo.toFixed(2)} ${bestC.liftLo > 1 ? '> 1' : '≤ 1'}. ` : ''}추천값은 참고로만 보세요.`}</div>
    <div class="table-wrap" style="max-height:none; margin-top:10px;"><table>
      <thead><tr><th>전략</th><th class="num">성공률</th><th class="num">95% 신뢰구간</th><th class="num">무작위 대비</th><th class="num">100건당 낙찰</th><th class="num">하한 미달</th></tr></thead>
      <tbody>
        <tr><td>무작위 기준 (평균 업체, 1÷참가수)</td><td class="num">${randP ? (randP*100).toFixed(2) + '%' : '-'}</td><td class="num">-</td><td class="num">×1.00</td><td class="num">${randP ? (randP*100).toFixed(1) + '건' : '-'}</td><td class="num">-</td></tr>
        ${tr(meanL)}
        ${curves.map(c => tr({...c, note: c.s === bestC.s ? ' <span class="badge blue">채택</span>' : ''}, c.s === bestC.s ? 'hl-row' : '')).join('')}
      </tbody></table></div>
    <div class="meta-line">${esc(sido)} · 시험 ${fmtNum(nTest)}건(최근 ${monthly.length}개월) · 각 달은 그 이전 24개월 데이터만으로 추천값을 정함(예가범위별, 같은 범위 30건 미만이면 전체) · 성공 = 실제 사정율 ≤ 추천 투찰 사정률 &lt; 실제 낙찰자 투찰 사정률</div>
    <div class="meta-line">채택 기준: 시험 2,000건 이상 그리고 무작위 대비 배수 신뢰구간 하한 &gt; 1. 곡선 폭은 이 결과에서 가장 좋았던 값을 골라 앱 전체에 적용했으므로(현재 ±${bestC.s}%p) 실제보다 약간 낙관적일 수 있습니다.</div>
    <h3>달별 결과 (곡선 폭 ±0.01%p 기준)</h3>
    <div class="table-wrap" style="max-height:none;"><table>
      <thead><tr><th>시험 달</th><th class="num">공고</th><th class="num">평균 전략 성공</th><th class="num">곡선 전략 성공</th></tr></thead>
      <tbody>${monthly.map(r => `<tr><td>${r.m}</td><td class="num">${fmtNum(r.n)}</td><td class="num">${r.mean}</td><td class="num">${r.curve}</td></tr>`).join('')}</tbody>
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
  // QR/링크로 받은 서비스키 저장 (#key=…) 후 주소에서 바로 지운다
  const km = location.hash.match(/^#key=(.+)$/);
  if(km){
    try{ LS.set('apiKey', decodeURIComponent(km[1])); LS.set('bidsMode', 'live'); }catch(e){}
    history.replaceState(null, '', location.pathname + '#bids');
    setTimeout(() => alert('이 기기에 조달청 서비스키를 저장했습니다. 입찰공고 탭에서 실시간 검색을 쓸 수 있어요.'), 300);
  }
  applyTheme();
  $('todayLabel').textContent = new Date().toLocaleDateString('ko-KR', {year:'numeric', month:'long', day:'numeric', weekday:'short'});
  const lv = LS.get('lastVisit', null);
  prevVisit = lv ? new Date(lv) : null;
  initServiceWorker();
  await Data.loadMeta();
  await Data.loadModel();

  document.querySelectorAll('.nav-item').forEach(b => b.addEventListener('click', () => switchTab(b.dataset.tab)));
  window.addEventListener('popstate', () => switchTab(location.hash.slice(1), false));
  document.addEventListener('click', (e) => {
    const w = e.target.closest('[data-watch]');
    if(w){ toggleWatch(w.dataset.watch, w); return; }
    const u = e.target.closest('[data-unwatch]');
    if(u){ WatchStore.remove(u.dataset.unwatch).then(renderWatch); return; }
    const p = e.target.closest('[data-predict]');
    if(p){ predictWithNotice(p.dataset.predict); return; }
    const g = e.target.closest('[data-goto]');
    if(g) switchTab(g.dataset.goto);
  });

  initBidsFilters();
  initLive();
  initCompany();
  initPredict();
  initStats();
  initSettings();
  switchTab(location.hash.slice(1) || 'bids', false);
  Data.loadBids().then(() => { if(currentTab !== 'bids') updateNewBadge(); });
}

init();
