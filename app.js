/* 로또 번호 분석 — 정적 PWA. data/draws.json(전체 회차)·data/analysis.json(scripts/update.py 가 매주 계산)만 읽는다.
 * 원칙: 매 회 독립 추첨이라 분석법은 당첨 확률을 못 바꾼다. 앱은 분석을 보여 주되 표본외 역검증으로 '효과 없음'을 숫자로 같이 보인다. */
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const fmt = (n, d = 0) => Number(n).toLocaleString('ko-KR', {minimumFractionDigits: d, maximumFractionDigits: d});
const won = (n) => n ? (n >= 1e8 ? fmt(n / 1e8, 1) + '억 원' : fmt(n) + '원') : '-';
const LS = {
  get(k, d){ try{ const v = localStorage.getItem('lt.' + k); return v == null ? d : JSON.parse(v); }catch(e){ return d; } },
  set(k, v){ try{ localStorage.setItem('lt.' + k, JSON.stringify(v)); }catch(e){} },
};
const color = (n) => n <= 10 ? 'c1' : n <= 20 ? 'c2' : n <= 30 ? 'c3' : n <= 40 ? 'c4' : 'c5';
const ball = (n, cls = '') => `<span class="ball ${color(n)} ${cls}">${n}</span>`;
const PRIMES = new Set([2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43]);

let D = [], A = null, view = LS.get('view', 'pick');

function feats(s){
  s = [...s].sort((a, b) => a - b);
  const diffs = new Set();
  for(let i = 0; i < 6; i++) for(let j = i + 1; j < 6; j++) diffs.add(s[j] - s[i]);
  const ends = {};
  s.forEach(x => ends[x % 10] = (ends[x % 10] || 0) + 1);
  return {odd: s.filter(x => x % 2).length, low: s.filter(x => x <= 22).length, sum: s.reduce((a, b) => a + b, 0),
    consec: s.slice(1).filter((x, i) => x - s[i] === 1).length, ac: diffs.size - 5,
    same_end: Math.max(...Object.values(ends)), prime: s.filter(x => PRIMES.has(x)).length, low31: s.filter(x => x <= 31).length};
}
const passFilter = (f) => f.odd >= 2 && f.odd <= 4 && f.low >= 2 && f.low <= 4 && f.sum >= 100 && f.sum <= 175 && f.consec <= 1 && f.ac >= 7;
// 많이 고르는 모양(생일·연속·등차·한 줄 몰림) 피하기: 1등이 나왔을 때 나눠 가질 사람이 적은 쪽. 당첨 확률은 그대로
const crowdy = (s, f) => f.low31 === 6 || f.consec >= 2 || s.every((x, i) => i === 0 || x - s[i - 1] === s[1] - s[0]) || new Set(s.map(x => Math.ceil(x / 7))).size <= 2;

function rand6(){
  const a = Array.from({length: 45}, (_, i) => i + 1);
  for(let i = 44; i > 0; i--){ const j = (crypto.getRandomValues(new Uint32Array(1))[0] % (i + 1)); [a[i], a[j]] = [a[j], a[i]]; }
  return a.slice(0, 6).sort((x, y) => x - y);
}
function gen(opts){
  for(let t = 0; t < 5000; t++){
    const s = rand6(), f = feats(s);
    if(opts.filter && !passFilter(f)) continue;
    if(opts.crowd && crowdy(s, f)) continue;
    if(opts.noPast && D.some(d => d.n.join() === s.join())) continue;
    return s;
  }
  return rand6();
}

// ---------------------------------------------------------------- 화면
function renderPick(){
  const o = LS.get('pickOpts', {filter: false, crowd: true, noPast: true});
  const lines = LS.get('lines', []);
  $('main').innerHTML = `<div class="card">
      <h2>🎲 번호 뽑기</h2>
      <div class="row"><button class="btn" id="g5" type="button">5줄 뽑기</button><button class="btn line-b" id="g1" type="button">1줄 추가</button>${lines.length ? '<button class="btn sm line-b" id="clr" type="button">지우기</button>' : ''}</div>
      <label class="check"><input type="checkbox" id="oCrowd" ${o.crowd ? 'checked' : ''}><span>많이 고르는 모양 피하기<small>모두 31 이하(생일), 연속 2쌍↑, 등차, 한쪽 몰림 제외. 당첨 확률은 같지만 1등이면 나눠 가질 사람이 적은 쪽</small></span></label>
      <label class="check"><input type="checkbox" id="oFilter" ${o.filter ? 'checked' : ''}><span>시중 필터 적용(홀짝·고저 2~4, 합 100~175, 연속 1쌍 이하, AC 7↑)<small>역검증 결과 효과 없음. 전체 조합의 ${A ? fmt(A.filter_share, 1) : '-'}%만 남김</small></span></label>
      <label class="check"><input type="checkbox" id="oPast" ${o.noPast ? 'checked' : ''}><span>역대 1등 번호와 같은 조합 빼기</span></label>
      <div class="lines">${lines.map((s, i) => lineHtml(s, String.fromCharCode(65 + i))).join('') || '<div class="empty">버튼을 눌러 번호를 뽑으세요.</div>'}</div>
      <div class="meta">${D.length ? `다음 ${D[D.length - 1].no + 1}회 추첨용 · ` : ''}뽑은 번호는 이 기기에만 저장됩니다. 1줄 1등 확률은 1/8,145,060 (어떤 방법이든 같음).</div>
    </div>`;
  const save = () => LS.set('pickOpts', {filter: $('oFilter').checked, crowd: $('oCrowd').checked, noPast: $('oPast').checked});
  ['oFilter', 'oCrowd', 'oPast'].forEach(id => $(id).onchange = save);
  const opts = () => ({filter: $('oFilter').checked, crowd: $('oCrowd').checked, noPast: $('oPast').checked});
  $('g5').onclick = () => { LS.set('lines', Array.from({length: 5}, () => gen(opts()))); renderPick(); };
  $('g1').onclick = () => { const l = LS.get('lines', []); if(l.length >= 10) l.shift(); l.push(gen(opts())); LS.set('lines', l); renderPick(); };
  $('clr') && ($('clr').onclick = () => { LS.set('lines', []); renderPick(); });
}
function lineHtml(s, lab){
  const f = feats(s);
  return `<div class="line"><span class="lab">${lab}</span><span class="balls">${s.map(n => ball(n)).join('')}</span>
    <span class="tags">홀짝 ${f.odd}:${6 - f.odd} · 고저 ${6 - f.low}:${f.low} · 합 ${f.sum} · AC ${f.ac}${f.consec ? ` · 연속 ${f.consec}쌍` : ''}</span></div>`;
}

function renderLast(){
  const d = D[D.length - 1];
  const prev = D.slice(-11, -1).reverse();
  $('main').innerHTML = `<div class="card">
      <h2>🏁 ${d.no}회 (${esc(d.date)})</h2>
      <div class="balls">${d.n.map(n => ball(n)).join('')}<span class="plus">+</span>${ball(d.b)}</div>
      <div class="meta">1등 ${d.w1 != null ? fmt(d.w1) + '명' : '-'} · 1인당 ${won(d.p1)}${d.sales ? ` · 판매 ${won(d.sales)}` : ''}</div>
    </div>
    <div class="card"><h2>지난 10회</h2>
      ${prev.map(p => `<div class="line"><span class="lab">${p.no}</span><span class="balls">${p.n.map(n => ball(n, 'sm')).join('')}<span class="plus">+</span>${ball(p.b, 'sm')}</span><span class="tags">${esc(p.date)} · 1등 ${p.w1 != null ? fmt(p.w1) + '명' : '-'} · ${won(p.p1)}</span></div>`).join('')}
    </div>`;
}

function renderStats(){
  const sort = LS.get('statSort', 'no');
  const nums = Array.from({length: 45}, (_, i) => ({n: i + 1, f: A.freq[i], r10: A.recent10[i], r50: A.recent50[i], g: A.gap[i]}));
  const key = {no: (x) => x.n, freq: (x) => -x.f, r10: (x) => -x.r10, r50: (x) => -x.r50, gap: (x) => -x.g}[sort];
  nums.sort((a, b) => key(a) - key(b) || a.n - b.n);
  const fmax = Math.max(...A.freq), r50max = Math.max(...A.recent50, 1);
  const val = (x) => sort === 'r10' ? `최근10 ${x.r10}` : sort === 'r50' ? `최근50 ${x.r50}` : sort === 'gap' ? `${x.g}회째 안 나옴` : `${x.f}회`;
  const barv = (x) => sort === 'r50' || sort === 'r10' ? x.r50 / r50max : x.f / fmax;
  $('main').innerHTML = `<div class="verdict ${A.chi2 < A.chi2_crit ? 'ok' : 'warn'}"><b>${A.chi2 < A.chi2_crit ? '번호별 출현 횟수는 무작위와 구별되지 않습니다' : '번호별 출현에 치우침이 보입니다'}</b><br>
      ${fmt(A.n_draws)}회 동안 번호마다 평균 ${fmt(A.freq_expect, 1)}번(최소 ${Math.min(...A.freq)} · 최대 ${Math.max(...A.freq)}). 카이제곱 ${fmt(A.chi2, 1)} (무작위 95% 한계 ${fmt(A.chi2_crit, 1)}).</div>
    <div class="card"><div class="row" style="justify-content:space-between;"><h2 style="margin:0;">📊 번호별</h2>
      <select id="sSort"><option value="no">번호 순</option><option value="freq">전체 많이 나온 순</option><option value="r50">최근 50회 핫</option><option value="r10">최근 10회 핫</option><option value="gap">오래 안 나온 순</option></select></div>
      <div class="grid45" style="margin-top:12px;">${nums.map(x => `<div class="cell ${x.r10 >= 3 ? 'hot' : x.g >= 15 ? 'cold' : ''}">${ball(x.n, 'sm')}<span class="v">${val(x)}</span><span class="bar"><i style="width:${(barv(x) * 100).toFixed(0)}%"></i></span></div>`).join('')}</div>
      <div class="meta">빨간 테두리 = 최근 10회 3번 이상, 파란 테두리 = 15회 이상 안 나옴. 핫·콜드는 지난 결과일 뿐 다음 회차와 관계없습니다(🧪 역검증).</div>
    </div>
    <div class="card"><h2>👯 함께 많이 나온 쌍</h2>
      <table><thead><tr><th>번호</th><th class="num">함께 나옴</th></tr></thead><tbody>${A.pairs_top.map(([p, c]) => `<tr><td><span class="balls">${p.map(n => ball(n, 'sm')).join('')}</span></td><td class="num">${c}회</td></tr>`).join('')}</tbody></table>
      <div class="meta">쌍 하나가 무작위로 나올 기대 ${fmt(A.pair_expect, 1)}회. 쌍이 990개라 이 정도 상위값은 우연으로도 나옵니다.</div></div>`;
  $('sSort').value = sort;
  $('sSort').onchange = (e) => { LS.set('statSort', e.target.value); renderStats(); };
}

const PAT = [['odd', '홀수 개수'], ['low', '낮은 번호(1~22) 개수'], ['sum', '6개 합'], ['consec', '연속 번호 쌍'], ['ac', 'AC값(번호 간격 다양성)'], ['same_end', '같은 끝수 최대 개수'], ['prime', '소수 개수']];
function renderPattern(){
  const blocks = PAT.map(([k, t]) => {
    const p = A.patterns[k];
    const keys = Object.keys(p.random).filter(x => (p.random[x] || 0) >= 0.5 || (p.real[x] || 0) >= 0.5);
    const max = Math.max(...keys.map(x => Math.max(p.real[x] || 0, p.random[x] || 0)));
    return `<div><h3>${t}</h3>${keys.map(x => `<div class="pbar"><span>${esc(x)}</span><span class="t"><i style="width:${((p.real[x] || 0) / max * 100).toFixed(1)}%"></i><s style="left:${((p.random[x] || 0) / max * 100).toFixed(1)}%"></s></span><b>${fmt(p.real[x] || 0, 1)}%</b><span class="r">${fmt(p.random[x] || 0, 1)}%</span></div>`).join('')}</div>`;
  }).join('');
  const cr = A.crowd.groups.low31;
  $('main').innerHTML = `<div class="verdict ok"><b>모든 패턴이 무작위로 뽑았을 때와 같은 비율로 나옵니다</b><br>홀짝 3:3, 합 120~159 같은 '잘 나오는 패턴'은 원래 조합 수가 많아서 자주 나올 뿐, 그 안의 한 줄이 당첨될 확률은 다른 줄과 같습니다.</div>
    <div class="card"><h2>🧩 실제 당첨번호 vs 무작위</h2>
      <div class="meta" style="margin:0 0 6px;">막대 = 실제 ${fmt(A.n_draws)}회 비율, 검은 선 = 무작위 20만 번 비율(오른쪽 흐린 숫자)</div>
      <div class="pat">${blocks}</div></div>
    <div class="card"><h2>🔁 이월수 (직전 회차 번호가 다시 나온 개수)</h2>
      <table><tbody>${Object.entries(A.carry).map(([k, v]) => `<tr><td>${k}개</td><td class="num">${fmt(v, 1)}%</td></tr>`).join('')}</tbody></table>
      <div class="meta">무작위라도 직전 6개 중 1개 이상 겹칠 확률은 약 60%입니다.</div></div>
    <div class="card"><h2>👥 1등을 나눠 가진 정도 (번호 모양별)</h2>
      <table><thead><tr><th>31 이하 번호 개수</th><th class="num">회차</th><th class="num">1등 인원</th><th class="num">무작위 기대 대비</th></tr></thead>
      <tbody>${Object.entries(cr).map(([k, v]) => `<tr><td>${k}개</td><td class="num">${fmt(v.draws)}</td><td class="num">${fmt(v.winners)}</td><td class="num">×${(v.winners / v.expect).toFixed(2)}</td></tr>`).join('')}</tbody></table>
      <div class="meta">사람들이 생일(31 이하)·연속 번호를 많이 고르면 그런 회차엔 1등이 많아 1인당 금액이 줄어듭니다. 이 데이터에선 차이가 작고 회차별 편차가 커서 뚜렷하지 않습니다(×1.0 근처). '많이 고르는 모양 피하기'는 확률이 아니라 금액 쪽 이야기입니다.</div></div>`;
}

function renderTest(){
  const B = A.backtest, R = B.methods;
  const best = R.reduce((a, m) => m.mean > a.mean ? m : a, R[0]);
  const anyWin = R.some(m => m.ci[0] > B.theory_mean);
  $('main').innerHTML = `<div class="verdict ${anyWin ? 'ok' : 'warn'}"><b>${anyWin ? '무작위보다 확실히 나은 방법이 있습니다' : '어떤 분석법도 무작위보다 낫지 않습니다'}</b><br>
      ${fmt(B.from)}회부터 매 회차, 그 직전까지의 기록만 보고 각 방법으로 6개를 골랐다면 평균 몇 개 맞았는지(표본외). 이론값 ${B.theory_mean}개, 3개 이상 ${fmt(B.theory_ge3, 2)}%.</div>
    <div class="card"><h2>🧪 분석법별 역검증</h2>
      <table><thead><tr><th>방법</th><th class="num">평균 맞음</th><th class="num">95% 구간</th><th class="num">3개↑</th></tr></thead>
      <tbody>${R.map(m => `<tr class="${m === best ? 'best' : ''}"><td>${esc(m.name)}</td><td class="num"><b>${fmt(m.mean, 3)}</b></td><td class="num">${fmt(m.ci[0], 2)}~${fmt(m.ci[1], 2)}</td><td class="num">${fmt(m.ge3, 2)}%</td></tr>`).join('')}</tbody></table>
      <div class="meta">95% 구간이 이론값 ${B.theory_mean}을 걸치면 '잡음 범위'(무작위와 구별 안 됨). 회차 ${fmt(R[0].draws)}개 · 무작위가 섞인 방법은 20번 반복 평균. 가장 높은 줄(파랑)도 다음 주엔 순위가 바뀝니다. 9가지를 시험했으니 하나쯤 운으로 좋아 보이는 게 정상입니다.</div></div>
    <div class="card"><h2>왜 안 되나</h2><div class="meta" style="font-size:13.5px; color:var(--text);">추첨기는 지난 결과를 기억하지 않습니다. 매 회 45개 공 중 6개가 새로 뽑히니 '많이 나온 번호', '오래 안 나온 번호', '잘 나오는 패턴' 모두 다음 회차 확률과 관계가 없습니다. 바꿀 수 있는 건 당첨됐을 때 나눠 가질 사람 수(번호 모양) 정도입니다.</div></div>`;
}

function renderMine(){
  const sel = new Set(LS.get('mySel', []));
  let res = '';
  if(sel.size === 6){
    const s = [...sel];
    const hit = {3: 0, 4: 0, 5: 0, '5b': 0, 6: 0}, best = [];
    D.forEach(d => {
      const m = d.n.filter(x => sel.has(x)).length;
      const k = m === 5 && sel.has(d.b) ? '5b' : m;
      if(hit[k] != null){ hit[k]++; if(m >= 4) best.push({d, m, b: k === '5b'}); }
    });
    res = `<h3>역대 ${fmt(D.length)}회에 넣었다면</h3>
      <table><tbody><tr><td>1등 (6개)</td><td class="num"><b>${hit[6]}</b>회</td></tr><tr><td>2등 (5개+보너스)</td><td class="num">${hit['5b']}회</td></tr><tr><td>3등 (5개)</td><td class="num">${hit[5]}회</td></tr><tr><td>4등 (4개)</td><td class="num">${hit[4]}회</td></tr><tr><td>5등 (3개)</td><td class="num">${hit[3]}회</td></tr></tbody></table>
      <div class="meta">무작위 한 줄이라도 ${fmt(D.length)}회면 5등 약 ${fmt(D.length * 0.0177, 0)}회, 4등 약 ${fmt(D.length * 0.000969, 1)}회가 기대값입니다.</div>
      ${best.length ? `<h3>4개 이상 맞은 회차</h3>${best.slice(-10).reverse().map(x => `<div class="line"><span class="lab">${x.d.no}</span><span class="balls">${x.d.n.map(n => ball(n, 'sm' + (sel.has(n) ? '' : ' dim'))).join('')}</span><span class="tags">${esc(x.d.date)} · ${x.m}개${x.b ? '+보너스' : ''}</span></div>`).join('')}` : ''}`;
  }
  $('main').innerHTML = `<div class="card"><h2>🔍 내 번호 맞춰보기</h2>
      <div class="meta" style="margin:0;">번호 6개를 누르세요 (${sel.size}/6)</div>
      <div class="pick6">${Array.from({length: 45}, (_, i) => `<button type="button" data-n="${i + 1}" class="${sel.has(i + 1) ? 'on' : ''}">${i + 1}</button>`).join('')}</div>
      <div class="row" style="margin-top:10px;">${sel.size ? '<button class="btn sm line-b" id="mClr" type="button">다시 고르기</button>' : ''}${LS.get('lines', []).length ? '<button class="btn sm line-b" id="mFromPick" type="button">뽑은 첫 줄 넣기</button>' : ''}</div>
      ${res}</div>`;
  $('main').querySelector('.pick6').onclick = (e) => {
    const b = e.target.closest('[data-n]'); if(!b) return;
    const n = +b.dataset.n;
    sel.has(n) ? sel.delete(n) : sel.size < 6 && sel.add(n);
    LS.set('mySel', [...sel].sort((a, b) => a - b)); renderMine();
  };
  $('mClr') && ($('mClr').onclick = () => { LS.set('mySel', []); renderMine(); });
  $('mFromPick') && ($('mFromPick').onclick = () => { LS.set('mySel', LS.get('lines', [])[0]); renderMine(); });
}

const VIEWS = {pick: renderPick, last: renderLast, stats: renderStats, pattern: renderPattern, test: renderTest, mine: renderMine};
function show(v){
  view = VIEWS[v] ? v : 'pick'; LS.set('view', view);
  document.querySelectorAll('#tabs button').forEach(b => b.classList.toggle('on', b.dataset.v === view));
  if(!A){ $('main').innerHTML = '<div class="empty">데이터를 불러오지 못했습니다.</div>'; return; }
  VIEWS[view]();
}
$('tabs').onclick = (e) => { const b = e.target.closest('[data-v]'); if(b){ show(b.dataset.v); window.scrollTo(0, 0); } };

(async () => {
  try{
    const [d, a] = await Promise.all(['data/draws.json', 'data/analysis.json'].map(u => fetch(u, {cache: 'no-cache'}).then(r => { if(!r.ok) throw new Error(u); return r.json(); })));
    D = d; A = a;
    $('dataInfo').textContent = `${fmt(A.n_draws)}회까지 · ${A.last.date} 추첨 · 갱신 ${A.updated_at.slice(5, 16).replace('T', ' ')}`;
  }catch(e){ $('dataInfo').textContent = '데이터 불러오기 실패'; }
  show(view);
})();
if('serviceWorker' in navigator) navigator.serviceWorker.register('sw.js').catch(() => {});
