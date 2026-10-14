"""Perp Farming Radar: read-only, data-availability-aware research MVP."""
from __future__ import annotations

import io
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import plotly.express as px
import requests
import streamlit as st

st.set_page_config(page_title='Perp Farming Radar', page_icon='📡', layout='wide')
DATA_DIR = Path(__file__).resolve().parent
SNAPSHOT_FILE = DATA_DIR / 'radar_snapshots.csv'
WATCHLIST_FILE = DATA_DIR / 'watchlist.csv'
HEADERS = {'User-Agent': 'PerpFarmingRadar-MVP/0.3'}
FETCHED_AT = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')

def norm(name):
    return re.sub(r'[^a-z0-9]', '', str(name).lower())

def number(value):
    try:
        val = float(value)
        return val if pd.notna(val) else None
    except (ValueError, TypeError):
        return None

def api_get(url, params=None):
    resp = requests.get(url, params=params, headers=HEADERS, timeout=18)
    resp.raise_for_status()
    return resp.json()

@st.cache_data(ttl=900, show_spinner=False)
def fetch_catalog():
    arr = api_get('https://api.llama.fi/protocols')
    result=[]
    for p in arr:
        category = str(p.get('category') or '').lower()
        if not any(word in category for word in ('derivative', 'perp')):
            continue
        name=p.get('name')
        if not name: continue
        stamp=number(p.get('listedAt'))
        if stamp and stamp > 10**12: stamp/=1000
        listed=datetime.fromtimestamp(stamp, tz=timezone.utc).date().isoformat() if stamp and 1e9 < stamp < 3e9 else None
        result.append({'name':name, 'category':p.get('category'), 'chains': ', '.join(p.get('chains') or []), 'tvl_usd': number(p.get('tvl')), 'listed_date':listed, 'source':'DefiLlama catalog'})
    return pd.DataFrame(result).drop_duplicates('name')

@st.cache_data(ttl=900, show_spinner=False)
def fetch_oi():
    payload=api_get('https://api.llama.fi/overview/open-interest')
    rows=[]
    for p in payload.get('protocols', []):
        rows.append({'name':p.get('name'), 'oi_usd':number(p.get('total24h')),
                     'oi_source':'DefiLlama open-interest overview'})
    return pd.DataFrame(rows)

@st.cache_data(ttl=120, show_spinner=False)
def fetch_hyperliquid():
    r=requests.post('https://api.hyperliquid.xyz/info', json={'type':'metaAndAssetCtxs'}, timeout=18, headers=HEADERS)
    r.raise_for_status()
    meta, contexts=r.json()
    volume=0.0
    oi=0.0
    pairs=0
    for asset, context in zip(meta.get('universe', []), contexts):
        px=number(context.get('markPx'))
        sz=number(context.get('openInterest'))
        day=number(context.get('dayNtlVlm'))
        if day is not None: volume+=day
        if px is not None and sz is not None: oi+=px*sz
        pairs+=1
    return {'name':'Hyperliquid','volume_24h_usd':volume,'oi_usd':oi, 'markets':pairs,
            'volume_source':'Hyperliquid API, 24h market context', 'oi_source':'Hyperliquid API, current estimate'}

@st.cache_data(ttl=900, show_spinner=False)
def fetch_standx():
    """Official StandX public market overview: 24h quote volume and current OI."""
    data = api_get('https://perps.standx.com/api/query_market_overview')
    if not isinstance(data, dict) or not isinstance(data.get('summary'), dict):
        raise ValueError('StandX API response shape changed')
    summary = data['summary']
    volume = number(summary.get('volume_quote_24h'))
    oi = number(summary.get('open_interest_notional'))
    if volume is None and oi is None:
        raise ValueError('StandX summary contains no supported market figures')
    return {'volume_24h_usd': volume, 'oi_usd': oi,
            'volume_source': 'StandX official public market overview (24h USD quote volume)',
            'oi_source': 'StandX official public market overview (current OI notional)',
            'market_data_time': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}

@st.cache_data(ttl=300, show_spinner=False)
def fetch_variational():
    """Official Variational Omni public platform stats (USDC denominated)."""
    data = api_get('https://omni-client-api.prod.ap-northeast-1.variational.io/metadata/stats')
    if not isinstance(data, dict):
        raise ValueError('Variational market statistics response is not an object')
    volume = number(data.get('total_volume_24h'))
    oi = number(data.get('open_interest'))
    if volume is None and oi is None:
        raise ValueError('Variational statistics contain no supported market figures')
    return {'volume_24h_usd': volume, 'oi_usd': oi,
            'volume_source': 'Variational Omni official public API (24h USDC volume)',
            'oi_source': 'Variational Omni official public API (current USDC OI)',
            'market_data_time': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}

@st.cache_data(ttl=900, show_spinner=False)
def fetch_pacifica_markets():
    """Public metadata only. Never convert market listing into volume or active users."""
    data=api_get('https://api.pacifica.fi/api/v1/info')
    if not isinstance(data,dict) or data.get('success') is not True or not isinstance(data.get('data'),list):
        raise ValueError('Pacifica public market metadata response changed')
    return len(data['data'])

@st.cache_data(ttl=900, show_spinner=False)
def fetch_arcus_markets():
    """Public market discovery only; schema checked conservatively."""
    data=api_get('https://api.arcus.xyz/v1/markets')
    rows=data if isinstance(data,list) else data.get('markets') if isinstance(data,dict) else None
    if not isinstance(rows,list):
        raise ValueError('Arcus API market-list schema unverified')
    perpetuals=[r for r in rows if isinstance(r,dict) and r.get('type')=='PERPETUAL']
    values=[]
    for m in perpetuals:
        size=number(m.get('openInterest'))
        price=number(m.get('markPrice'))
        if size is not None and price is not None:
            values.append(size*price)
    return {'markets':len(perpetuals),'oi_usd':sum(values) if values else None,
            'oi_source':'Arcus markets API · perpetual one-sided OI × mark price'}

@st.cache_data(ttl=900,show_spinner=False)
def fetch_nado_ccxt():
    """CCXT official Nado adapter. Only aggregate explicitly reported OI USD fields."""
    import ccxt
    client=ccxt.nado({'enableRateLimit':True,'timeout':16000})
    try:
        market_data=client.fetch_open_interests()
        if isinstance(market_data,dict): market_data=list(market_data.values())
        if not isinstance(market_data,list):raise ValueError('Nado OI response is not a list')
        oi_values=[]
        for item in market_data:
            if not isinstance(item,dict):continue
            amt=number(item.get('openInterestValue'))
            if amt is None:
                info=item.get('info')
                if isinstance(info,dict):amt=number(info.get('open_interest_usd'))
            if amt is not None:oi_values.append(amt)
        if not oi_values:raise ValueError('No reported USD open interest fields')
        return {'oi_usd':sum(oi_values),'oi_source':'Nado via CCXT fetchOpenInterests (reported USD OI)',
                'market_data_time':datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}
    finally:
        close=getattr(client,'close',None)
        if callable(close):close()

@st.cache_data(ttl=900, show_spinner=False)
def fetch_pro_derivatives(key):
    payload=api_get('https://pro-api.llama.fi/' + key.strip() + '/api/overview/derivatives',
                    params={'excludeTotalDataChart':'true','excludeTotalDataChartBreakdown':'true'})
    return pd.DataFrame([{
        'name':p.get('name'), 'volume_24h_usd':number(p.get('total24h')),
        'volume_7d_usd':number(p.get('total7d')),
        'volume_prev_7d_usd':number(p.get('total14dto7d')),
        'volume_source':'DefiLlama Pro derivatives'
    } for p in payload.get('protocols',[])])

def merge_exact(left, right, suffix):
    if right is None or right.empty or 'name' not in right: return left
    right=right.copy();right['_id']=right['name'].map(norm)
    right=right.drop(columns=['name']).drop_duplicates('_id')
    combined=left.merge(right,on='_id',how='left',suffixes=('',suffix))
    for field in ['oi_usd','volume_24h_usd','volume_7d_usd','volume_prev_7d_usd','oi_source','volume_source','market_data_time']:
        other=f'{field}{suffix}'
        if other in combined:
            if field in combined: combined[field]=combined[other].combine_first(combined[field])
            else: combined[field]=combined[other]
            combined=combined.drop(columns=other)
    return combined

def fmt_money(x):
    if pd.isna(x):return 'N/A'
    x=float(x)
    if abs(x)>=1e9:return f'${x/1e9:,.2f}B'
    if abs(x)>=1e6:return f'${x/1e6:,.2f}M'
    if abs(x)>=1e3:return f'${x/1e3:,.1f}K'
    return f'${x:,.0f}'


st.markdown("""<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=Noto+Sans+KR:wght@400;500;600;700&display=swap');
html,body,[data-testid="stApp"],[class*="css"]{font-family:'DM Sans','Noto Sans KR',sans-serif}
[data-testid="stAppViewContainer"]{background:linear-gradient(160deg,#f0f5f7 0%,#f8f7f3 38%,#f5f3ee 100%);color:#505c66}
[data-testid="stSidebar"]{background:#eff3f3;border-right:1px solid #d8e0e3}
.block-container{max-width:1500px;padding-top:1.8rem;padding-bottom:4rem}
h1,h2,h3{color:#485760!important;letter-spacing:-.025em;font-weight:700!important}
h1{font-size:2.35rem!important}
p,li,[data-testid="stCaptionContainer"]{color:#65737d}
[data-testid="stMetric"]{background:#ffffffdf;border:1px solid #dce4e7;border-radius:16px;padding:18px 20px;box-shadow:0 5px 24px #a7b8c313}
[data-testid="stMetricLabel"]{color:#78858d}
[data-testid="stMetricValue"]{color:#4a606d}
[data-testid="stDataFrame"]{border:1px solid #d7e0e4;border-radius:13px;overflow:hidden}
.stTabs [data-baseweb="tab-list"]{gap:12px;border-bottom:1px solid #d7e0e3}
.stTabs [data-baseweb="tab"]{color:#65737d;border-radius:10px}
.stTabs [aria-selected="true"]{color:#557f97!important}
[data-testid="stSidebar"] label,[data-testid="stSidebar"] p{color:#647580}
.radar-banner{background:linear-gradient(115deg,#edf5f9,#f7f7f3 67%,#eaf0f2);padding:28px 32px;border:1px solid #d9e3e7;border-radius:18px;margin-bottom:22px}
.radar-overline{font-weight:700;letter-spacing:.17em;color:#7594a6;font-size:11px}
.radar-banner h1{color:#586771!important;font-size:2.4rem!important;font-weight:700!important}
.radar-description{font-size:14px;color:#7b8992;margin-top:11px}
.radar-chip{display:inline-block;background:#e5eff5;color:#7194a8;padding:6px 12px;border-radius:30px;margin-top:15px;font-size:11px;font-weight:700}
.radar-hint{padding:14px 18px;background:#ecf3f7;border-left:3px solid #a5bfd0;border-radius:9px;color:#607685;font-size:13px}
.radar-hint b{color:#566b79}
</style>""",unsafe_allow_html=True)
st.markdown("""<div class="radar-banner"><div class="radar-overline">PERP DEX INTELLIGENCE / WEEKLY RESEARCH</div>
<h1 style="margin:8px 0 2px">Perp Farming Radar</h1>
<div class="radar-description">참여자 밀집도와 포인트 파밍 효율을 추적하는 리서치 대시보드</div>
<div class="radar-chip">● LIVE MARKET DATA · CLOUD DANCER EDITION</div></div>""",unsafe_allow_html=True)

with st.sidebar:
    st.header('데이터 연결')
    pro_key=st.text_input('DefiLlama Pro API key (선택)', type='password', help='유료 derivatives 거래량 API용. 키는 저장하지 않습니다.')
    use_hl=st.checkbox('Hyperliquid 시장 데이터 불러오기', value=True)
    uploaded=st.file_uploader('프로젝트별 포인트·참여자 CSV', type='csv')
    st.download_button('CSV 템플릿 다운로드', (DATA_DIR/'manual_metrics.csv').read_bytes(), 'manual_metrics.csv', mime='text/csv')
    if st.button('API 캐시 새로고침'):
        fetch_catalog.clear();fetch_oi.clear();fetch_hyperliquid.clear();fetch_standx.clear();fetch_variational.clear();fetch_pacifica_markets.clear();fetch_arcus_markets.clear();fetch_nado_ccxt.clear();fetch_pro_derivatives.clear();st.rerun()
    st.divider()
    st.caption('관심 프로젝트 11개는 기본 등록됩니다. 이름 중복은 거래소별 실제 엔드포인트 확인 전까지 자동 통합하지 않습니다.')
    st.caption('활성 사용자 수는 현재 범용 무료 API에서 자동으로 가져오지 못합니다. 확인 가능한 프로젝트별 데이터를 CSV로 연결해야 합니다.')
    st.caption('v0.4: StandX·Variational 공식 공개 시장 API 연동. 24h 거래량 및 현재 OI이며 7일 지표나 포인트 경쟁률은 아닙니다.')

errors=[]
with st.spinner('공개 데이터 수집 중…'):
    try: catalog=fetch_catalog()
    except Exception as e: catalog=pd.DataFrame();errors.append(f'프로젝트 목록: {e}')
    try: oi=fetch_oi()
    except Exception as e: oi=pd.DataFrame();errors.append(f'OI: {e}')
    try: hl=fetch_hyperliquid() if use_hl else None
    except Exception as e: hl=None;errors.append(f'Hyperliquid: {e}')
    try: sx=fetch_standx()
    except Exception as e: sx=None;errors.append(f'StandX: {e}')
    try: va=fetch_variational()
    except Exception as e: va=None;errors.append(f'Variational: {e}')
    try: pacifica_markets=fetch_pacifica_markets()
    except Exception as e: pacifica_markets=None;errors.append(f'Pacifica markets: {e}')
    try: arcus_stats=fetch_arcus_markets()
    except Exception as e: arcus_stats=None;errors.append(f'Arcus markets: {e}')
    try: nado_stats=fetch_nado_ccxt()
    except Exception as e: nado_stats=None;errors.append(f'Nado CCXT: {e}')
    try: pro=fetch_pro_derivatives(pro_key) if pro_key else pd.DataFrame()
    except Exception as e: pro=pd.DataFrame();errors.append(f'DefiLlama Pro: {e}')

if catalog.empty:
    st.warning('외부 목록 API를 불러오지 못했습니다. 관심 목록을 오프라인으로 표시합니다.')
    catalog=pd.DataFrame(columns=['name','category','chains','tvl_usd','listed_date','source'])

df=catalog.copy();df['_id']=df['name'].map(norm)
watch=pd.read_csv(WATCHLIST_FILE).fillna('')
# Alias match is explicit and intentionally excludes Robinhood Chain Lighter from generic Lighter.
watch_aliases={norm(alias):row['name'] for _,row in watch.iterrows() for alias in str(row['aliases']).split('|') if alias.strip()}
df['watch_name']=df['_id'].map(watch_aliases)
# Do not associate generic 'Lighter' venue activity with the Robinhood Chain points market.
existing_watch=set(df['watch_name'].dropna())
missing=watch.loc[~watch['name'].isin(existing_watch)]
if not missing.empty:
    records=[{'name':r['name'],'_id':norm(r['name']),'watch_name':r['name'],'source':'Watchlist only — venue mapping pending'} for _,r in missing.iterrows()]
    df=pd.concat([df,pd.DataFrame(records)],ignore_index=True)
df['is_watchlist']=df['watch_name'].notna().fillna(False).astype(bool)

df=merge_exact(df,oi,'_oi')
df=merge_exact(df,pro,'_pro')
# StandX market figures are only applied to the explicitly labelled StandX watchlist row.
# Do not merge ambiguously named venues or infer points data from the market API.
if sx:
    standx_mask = df['watch_name'].eq('StandX')
    if standx_mask.any():
        for field, value in sx.items():
            if field not in df.columns: df[field] = None
            df.loc[standx_mask, field] = value
# Scope Variational market stats to the explicitly identified Variational watchlist row.
if va:
    va_mask = df['watch_name'].eq('Variational')
    if va_mask.any():
        for field, value in va.items():
            if field not in df.columns: df[field] = None
            df.loc[va_mask, field] = value
if arcus_stats:
    arcus_mask=df['watch_name'].eq('Arcus')
    if arcus_mask.any():
        for field,value in arcus_stats.items():
            if field not in df.columns:df[field]=None
            df.loc[arcus_mask,field]=value
if nado_stats:
    nado_mask=df['watch_name'].eq('Nado')
    if nado_mask.any():
        for field,value in nado_stats.items():
            if field not in df.columns:df[field]=None
            df.loc[nado_mask,field]=value
if hl:
    hk=norm(hl['name'])
    mask=df['_id']==hk
    if mask.any():
        for col,val in hl.items():
            if col!='name':df.loc[mask,col]=val
    else:
        df=pd.concat([df,pd.DataFrame([dict(hl, _id=hk)])],ignore_index=True)
manual=pd.DataFrame()
if uploaded is not None:
    try:
        manual=pd.read_csv(uploaded)
        if 'name' not in manual.columns: raise ValueError('name 열이 필요합니다.')
        manual['name']=manual['name'].astype(str).str.strip()
        manual['_id']=manual['name'].map(norm)
        if manual['_id'].duplicated().any(): raise ValueError('중복된 프로젝트명이 있습니다.')
        extra=manual.loc[~manual['_id'].isin(df['_id']), ['name','_id']]
        if len(extra):df=pd.concat([df,extra], ignore_index=True)
        manual=manual.drop(columns=['name'])
        df=df.merge(manual,on='_id',how='left',suffixes=('','_manual'))
        for col in list(df.columns):
            if col.endswith('_manual'):
                original=col[:-7]
                if original in df:df[original]=df[col].combine_first(df[original]);df=df.drop(columns=[col])
                else:df=df.rename(columns={col:original})
    except Exception as e: st.error(f'수동 CSV 오류: {e}')

numeric_fields=['volume_24h_usd','volume_7d_usd','volume_prev_7d_usd','oi_usd','active_wallets_7d','new_wallets_7d','active_wallets_prev_7d','weekly_points','eligible_volume_7d','eligible_oi_time_usd_hours']
for c in numeric_fields:
    if c not in df:df[c]=float('nan')
    df[c]=pd.to_numeric(df[c],errors='coerce')
for c in ['point_model','notes','oi_source','volume_source','market_data_time']:
    if c not in df:df[c]=None

df['is_watchlist']=df['is_watchlist'].fillna(False).astype(bool)
df['volume_per_active_wallet_7d_usd']=df['volume_7d_usd']/df['active_wallets_7d'].replace(0,float('nan'))
df['wallet_change_pct']=(df['active_wallets_7d']/df['active_wallets_prev_7d']-1)*100
df.loc[df['active_wallets_prev_7d']<=0,'wallet_change_pct']=float('nan')
df['volume_change_pct']=(df['volume_7d_usd']/df['volume_prev_7d_usd']-1)*100
df.loc[df['volume_prev_7d_usd']<=0,'volume_change_pct']=float('nan')
# Relative competition (0 low -> 100 high): calculated only for projects with validated wallet inputs.
mask=df['active_wallets_7d'].ge(0)
df['wallet_competition_percentile']=float('nan')
if mask.any():df.loc[mask,'wallet_competition_percentile']=df.loc[mask,'active_wallets_7d'].rank(pct=True,method='average')*100


def compact_usd(x):
    if pd.isna(x): return '—'
    x=float(x)
    if abs(x)>=1e9:return f'${x/1e9:,.2f}B'
    if abs(x)>=1e6:return f'${x/1e6:,.2f}M'
    if abs(x)>=1e3:return f'${x/1e3:,.1f}K'
    return f'${x:,.0f}'

def integer_fmt(x):
    return '—' if pd.isna(x) else f'{float(x):,.0f}'

def readable_metrics(frame, rename):
    result=frame.copy()
    for field in ['volume_24h_usd','volume_7d_usd','oi_usd']:
        if field in result:result[field]=result[field].map(compact_usd)
    for field in ['active_wallets_7d','weekly_points']:
        if field in result:result[field]=result[field].map(integer_fmt)
    return result.rename(columns=rename)

st.caption('API 갱신 시각: '+datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')+' · 무료 데이터의 범위와 실제 갱신 간격은 거래소마다 다릅니다')
st.markdown('<div class="radar-hint"><b>빈집 탐지 원칙</b> · 거래량/OI가 적은 것과 포인트 경쟁률이 낮은 것은 다릅니다. 활성 지갑·배분 풀 미확보 프로젝트에는 임의 점수를 부여하지 않습니다.</div>',unsafe_allow_html=True)
st.write('')
k1,k2,k3,k4,k5=st.columns(5)
k1.metric('추적 프로젝트',f'{len(df):,}')
k2.metric('거래량 데이터',f"{df[['volume_24h_usd','volume_7d_usd']].notna().any(axis=1).sum():,}")
k3.metric('OI 데이터',f"{df['oi_usd'].notna().sum():,}")
k4.metric('7D 활성 지갑',f"{df['active_wallets_7d'].notna().sum():,}")
k5.metric('주간 포인트',f"{df['weekly_points'].notna().sum():,}")
if errors:
    with st.expander('⚠️ 일부 API 조회 오류'):
        for e in errors:st.write(e)
radar_tabs=st.tabs(['⭐ 관심 거래소','📈 경쟁 비교','🔎 전체 스캐너','🔌 데이터 출처','🧮 포인트 계산기'])
with radar_tabs[0]:
    st.subheader('관심 프로젝트 · Watchlist')
    watched=df[df['is_watchlist'].fillna(False).astype(bool)].copy()
    watched['상태']=watched.apply(lambda r:'참여자 데이터' if pd.notna(r['active_wallets_7d']) else ('시장 데이터' if pd.notna(r['oi_usd']) or pd.notna(r['volume_24h_usd']) or pd.notna(r['volume_7d_usd']) else '미연동'),axis=1)
    # User-facing home prioritizes actionable comparisons; diagnostics live in Data Sources.
    st.markdown('### Perp DEX Rankings')
    st.caption('거래량·OI·7일 활성 지갑 비교 | 지갑 변화율 확인 시에만 혼잡도 색상을 표시합니다.')
    f1,f2,f3=st.columns([2,1,1])
    search=f1.text_input('거래소 검색',placeholder='Variational, StandX, Nado…',key='watch_search')
    choice=f2.selectbox('데이터',['전체','24H Volume 확보','OI 확보','7D 지갑 확보'],key='watch_data_filter')
    order=f3.selectbox('정렬',['24H Volume ↓','OI ↓','OI ↑','7D 지갑 변화 ↑','7D 지갑 변화 ↓'],key='watch_order')
    display=watched.copy()
    if search:display=display[display['watch_name'].astype(str).str.contains(search,case=False,regex=False)]
    if choice=='24H Volume 확보':display=display[display['volume_24h_usd'].notna()]
    elif choice=='OI 확보':display=display[display['oi_usd'].notna()]
    elif choice=='7D 지갑 확보':display=display[display['active_wallets_7d'].notna()]
    sort_column,ascending={
      '24H Volume ↓':('volume_24h_usd',False),'OI ↓':('oi_usd',False),
      'OI ↑':('oi_usd',True),'7D 지갑 변화 ↑':('wallet_change_pct',False),
      '7D 지갑 변화 ↓':('wallet_change_pct',True)}[order]
    display=display.sort_values(sort_column,ascending=ascending,na_position='last').reset_index(drop=True)
    def crowd_label(change):
        if pd.isna(change):return ''
        if change>=25:return '🔴 유입 급증'
        if change>=5:return '🟠 유입 증가'
        if change<=-20:return '🟢 유입 크게 감소'
        if change<=-5:return '🟢 유입 감소'
        return '🔵 변화 작음'
    display.insert(0,'순위',[i+1 for i in range(len(display))])
    display['혼잡 변화']=display['wallet_change_pct'].apply(crowd_label)
    display['Volume']=display['volume_24h_usd'].map(compact_usd)
    display['OI']=display['oi_usd'].map(compact_usd)
    display['활성 지갑']=display['active_wallets_7d'].map(integer_fmt)
    display['지갑당 7D Volume']=display['volume_per_active_wallet_7d_usd'].map(compact_usd)
    display['7D 변화']=display['wallet_change_pct'].apply(lambda x:f'{x:+.1f}%' if pd.notna(x) else '—')
    st.dataframe(display[['순위','watch_name','volume_24h_usd','Volume','OI','활성 지갑','지갑당 7D Volume','7D 변화','혼잡 변화']].rename(
        columns={'watch_name':'프로젝트','volume_24h_usd':'거래량 바 (24H)'}),
        hide_index=True,use_container_width=True,height=min(570,max(245,42*(len(display)+1)+10)),
        column_config={
          '순위':st.column_config.NumberColumn('순위',width='small',format='%d'),
          '프로젝트':st.column_config.TextColumn('프로젝트',width='medium'),
          '거래량 바 (24H)':st.column_config.ProgressColumn('거래량 바 (24H)',width='medium',
              min_value=0,max_value=max(1,float(display['volume_24h_usd'].max())) if display['volume_24h_usd'].notna().any() else 1,format='$%.0f'),
          '혼잡 변화':st.column_config.TextColumn('혼잡 변화',width='medium')
        })
    movers=display[display['wallet_change_pct'].notna()]
    if not movers.empty:
        falling=movers.sort_values('wallet_change_pct').iloc[0]
        rising=movers.sort_values('wallet_change_pct',ascending=False).iloc[0]
        x,y=st.columns(2)
        x.metric('이번 주 유입 감소 최상위',str(falling['watch_name']),f"{falling['wallet_change_pct']:+.1f}% (7D 지갑)")
        y.metric('이번 주 유입 증가 최상위',str(rising['watch_name']),f"{rising['wallet_change_pct']:+.1f}% (7D 지갑)")
    else:
        st.caption('')
    st.caption('색상은 **파밍 효율 등급이 아니라** 거래 지갑 유입 변화 구분입니다. 미확보 지갑 수를 거래량으로 추정하지 않습니다.')
    st.markdown('#### 📊 관심 거래소 시장 규모 비교')
    chart_left, chart_right=st.columns(2)
    with chart_left:
        vol=watched.dropna(subset=['volume_24h_usd']).sort_values('volume_24h_usd')
        if vol.empty:st.info('24시간 거래량 데이터 미확보')
        else:
            fig=px.bar(vol,x='volume_24h_usd',y='watch_name',orientation='h',
                       color_discrete_sequence=['#8cb6c9'],
                       labels={'volume_24h_usd':'24H 거래량 (USD)','watch_name':''})
            fig.update_layout(height=max(240,110+len(vol)*46),margin=dict(l=0,r=8,t=5,b=0),
                              paper_bgcolor='rgba(0,0,0,0)',plot_bgcolor='rgba(0,0,0,0)',
                              font_color='#647682',showlegend=False)
            fig.update_xaxes(tickformat='~s',gridcolor='#e2e7e9')
            st.plotly_chart(fig,use_container_width=True)
    with chart_right:
        oi_chart=watched.dropna(subset=['oi_usd']).sort_values('oi_usd')
        if oi_chart.empty:st.info('OI 데이터 미확보')
        else:
            fig=px.bar(oi_chart,x='oi_usd',y='watch_name',orientation='h',
                       color_discrete_sequence=['#b7abb7'],
                       labels={'oi_usd':'현재 OI (USD)','watch_name':''})
            fig.update_layout(height=max(240,110+len(oi_chart)*46),margin=dict(l=0,r=8,t=5,b=0),
                              paper_bgcolor='rgba(0,0,0,0)',plot_bgcolor='rgba(0,0,0,0)',
                              font_color='#647682',showlegend=False)
            fig.update_xaxes(tickformat='~s',gridcolor='#e2e7e9')
            st.plotly_chart(fig,use_container_width=True)
    st.caption('그래프는 데이터가 확보된 거래소만 보여주며, Volume과 OI는 파밍 경쟁률 점수가 아닙니다.')
    with st.expander('관심 거래소별 데이터 출처 및 수집 상태'):
        st.dataframe(watched[['watch_name','volume_source','oi_source','market_data_time']].rename(columns={'watch_name':'거래소','volume_source':'거래량 출처','oi_source':'OI 출처','market_data_time':'데이터 조회 시각'}),hide_index=True,use_container_width=True)
    st.download_button('관심 프로젝트 데이터 CSV',watched.to_csv(index=False).encode('utf-8-sig'),'watchlist_metrics.csv','text/csv')
    st.markdown('#### 최근 자동 수집 추이')
    @st.cache_data(ttl=300,show_spinner=False)
    def load_snapshot_history():
        url='https://raw.githubusercontent.com/krwdigger/perp-farming-radar/main/data/market_snapshots.csv'
        hist=pd.read_csv(url)
        hist['captured_at_utc']=pd.to_datetime(hist['captured_at_utc'],utc=True,errors='coerce')
        for field in ['volume_24h_usd','oi_usd','markets']:
            hist[field]=pd.to_numeric(hist[field],errors='coerce')
        return hist.dropna(subset=['captured_at_utc'])
    try:
        hist=load_snapshot_history()
        selected_venue=st.selectbox('시계열 프로젝트',['Variational','StandX','Pacifica','Arcus','Hyperliquid'],key='snap_venue')
        h=hist[(hist['venue']==selected_venue)&(hist['status']=='ok')].sort_values('captured_at_utc').tail(250)
        if h.empty:st.info('해당 거래소의 성공한 자동 수집 기록이 아직 없습니다.')
        else:
            leftchart,rightchart=st.columns(2)
            with leftchart:
                if h['oi_usd'].notna().any():
                    fig=px.line(h,x='captured_at_utc',y='oi_usd',markers=True,title='시점별 OI ($)')
                    fig.update_traces(line_color='#8caec1')
                    fig.update_layout(paper_bgcolor='rgba(0,0,0,0)',plot_bgcolor='rgba(0,0,0,0)')
                    st.plotly_chart(fig,use_container_width=True)
                else:st.info('OI 시계열 미제공 (시장목록만 조회)')
            with rightchart:
                if h['volume_24h_usd'].notna().any():
                    fig=px.line(h,x='captured_at_utc',y='volume_24h_usd',markers=True,title='24시간 롤링 거래량 ($)')
                    fig.update_traces(line_color='#b3a6bf')
                    fig.update_layout(paper_bgcolor='rgba(0,0,0,0)',plot_bgcolor='rgba(0,0,0,0)')
                    st.plotly_chart(fig,use_container_width=True)
                else:st.info('24시간 거래량 시계열 미제공')
            if h['oi_usd'].notna().any():
                latest=h[h['oi_usd'].notna()].iloc[-1]
                previous=h[(h['captured_at_utc']<=latest['captured_at_utc']-pd.Timedelta(days=7))&h['oi_usd'].notna()]
                if not previous.empty and previous.iloc[-1]['oi_usd']>0:
                    delta=(latest['oi_usd']/previous.iloc[-1]['oi_usd']-1)*100
                    st.metric('7일 이전 스냅샷 대비 OI 변화',f'{delta:+.2f}%')
                else:st.caption('7일 OI 변화율: 7일 전 기록이 쌓여야 계산됩니다.')
        st.download_button('자동 수집 원본 CSV',hist.to_csv(index=False).encode('utf-8-sig'),'radar_history.csv','text/csv')
    except Exception as e:
        st.info('자동 수집 이력이 아직 생성되지 않았습니다. GitHub Actions 최초 실행 후 시계열이 표시됩니다.')

with radar_tabs[1]:
    st.subheader('참여자 경쟁도 vs 시장 OI')
    ca,cb=st.columns(2)
    with ca:
        st.markdown('#### A · 7일 활성 지갑')
        v=df.dropna(subset=['active_wallets_7d']).sort_values('active_wallets_7d').tail(20)
        if v.empty:st.info('활성 지갑 데이터 미확보 · 온체인 지갑 분석 연동 필요')
        else:st.plotly_chart(px.bar(v,x='active_wallets_7d',y='name',orientation='h'),use_container_width=True)
    with cb:
        st.markdown('#### B · OI 규모 상위 20개')
        v=df.dropna(subset=['oi_usd']).nlargest(20,'oi_usd').sort_values('oi_usd')
        if v.empty:st.info('OI 데이터 미확보')
        else:
            fig=px.bar(v,x='oi_usd',y='name',orientation='h',labels={'oi_usd':'OI USD','name':'DEX'})
            fig.update_layout(height=560,paper_bgcolor='rgba(0,0,0,0)',plot_bgcolor='rgba(0,0,0,0)',font_color='#647682')
            st.plotly_chart(fig,use_container_width=True)
    st.info('현재 OI는 시간가중 OI 포인트와 다릅니다. 낮은 OI = 저경쟁 파밍이라고 가정하지 않습니다.')
with radar_tabs[2]:
    st.subheader('전체 거래소 탐색')
    q1,q2,q3=st.columns([2,1,1])
    query=q1.text_input('검색',placeholder='Perp DEX 이름')
    flt=q2.selectbox('데이터 필터',['전체','OI 확보','거래량 확보','지갑 확보'])
    starred=q3.checkbox('관심 목록만')
    v=df.copy()
    if query:v=v[v['name'].astype(str).str.contains(query,case=False,na=False,regex=False)]
    if starred:v=v[v['is_watchlist'].fillna(False).astype(bool)]
    if flt=='OI 확보':v=v[v['oi_usd'].notna()]
    elif flt=='거래량 확보':v=v[v[['volume_24h_usd','volume_7d_usd']].notna().any(axis=1)]
    elif flt=='지갑 확보':v=v[v['active_wallets_7d'].notna()]
    sc=['name','chains','listed_date','volume_24h_usd','volume_7d_usd','oi_usd','active_wallets_7d','weekly_points']
    for col in sc:
        if col not in v:v[col]=None
    st.caption(f'검색 결과 {len(v):,}개 · 목록 등록일은 실제 서비스 출시일과 다를 수 있음')
    st.dataframe(readable_metrics(v[sc], rename={'name':'거래소','chains':'체인','listed_date':'목록 등록일','volume_24h_usd':'24H Volume','volume_7d_usd':'7D Volume','oi_usd':'OI','active_wallets_7d':'7D 지갑','weekly_points':'주간 포인트'}),hide_index=True,use_container_width=True,height=570)
    st.download_button('스캐너 CSV 내보내기',v[sc].to_csv(index=False).encode('utf-8-sig'),'perp_scanner.csv','text/csv')
with radar_tabs[3]:
    st.subheader('데이터 연결 현황')
    st.markdown('### 관심 프로젝트 11개 · 미연동 사유')
    st.caption('공개 문서 검토 기준 · 프로젝트 전체 사용자 수/포인트 정보는 API 접근 가능성과 별개입니다. Dune에서 확인 가능한 것은 인덱싱된 체인·컨트랙트 이벤트로 한정됩니다.')
    investigation=[
      {'프로젝트':'Variational','시장 데이터':'공식 API 일부 연결','미확보 데이터':'7일 활성 거래자·실제 포인트','원인 구분':'API 세부 지표 미확보 / 포인트 산식 별도 검증','Dune':'입출금/컨트랙트 활동은 후보 · 실제 체결은 별도 검증','자료':'https://omni.variational.io/'},
      {'프로젝트':'Nado','시장 데이터':'SDK/인덱서 조사 단계','미확보 데이터':'거래량·OI·활성 지갑·포인트','원인 구분':'무료 불가 확정 아님 · 어댑터 미작성','Dune':'인덱서/컨트랙트 이벤트 식별 필요','자료':'https://docs.nado.xyz/'},
      {'프로젝트':'StandX','시장 데이터':'공식 API 일부 연결','미확보 데이터':'7일 활성 거래자·실제 포인트','원인 구분':'API에 파밍 전체 활동 데이터 없음/미확인','Dune':'거래 엔진과 온체인 자금 이동 분리 필요','자료':'https://docs.standx.com/'},
      {'프로젝트':'N1','시장 데이터':'거래소 식별 미확정','미확보 데이터':'전체','원인 구분':'프로젝트·체인 식별 먼저 필요','Dune':'정확한 컨트랙트/체인 필요','자료':'https://dune.com/'},
      {'프로젝트':'Arcus','시장 데이터':'공식 공개 시장목록 조사','미확보 데이터':'거래량·OI·활성 지갑·포인트','원인 구분':'REST 일부 공개 · 집계 어댑터 미작성','Dune':'Robinhood Chain 컨트랙트 이벤트 확인 필요','자료':'https://api.arcus.xyz/v1/markets'},
      {'프로젝트':'Entropy','시장 데이터':'공식 소스 미확인','미확보 데이터':'전체','원인 구분':'자료 유료/비공개 여부도 미확인','Dune':'프로젝트/컨트랙트 식별 필요','자료':'https://dune.com/'},
      {'프로젝트':'QFEX','시장 데이터':'공개 market CLI 확인','미확보 데이터':'거래소 통합 거래량·OI·지갑·포인트','원인 구분':'무료 시장 명령 제공 · 직접 집계 구현 필요','Dune':'거래 엔진 거래 내역이 온체인인지 확인 필요','자료':'https://github.com/QFEX-org/cli'},
      {'프로젝트':'TxFlow','시장 데이터':'컨트랙트 매핑 대기','미확보 데이터':'전체','원인 구분':'이벤트/체인/주소 검증 필요','Dune':'트레이드 이벤트 있으면 SQL 집계 가능','자료':'https://dune.com/'},
      {'프로젝트':'robinhoodchain.lighter','시장 데이터':'전용 API 별도 조사','미확보 데이터':'거래량·OI·지갑·포인트','원인 구분':'일반 Lighter와 별도 시장 · 어댑터 미작성','Dune':'체인 입출금만으로 롤업 체결 복원 불가','자료':'https://docs.robinhood.com/chain/'},
      {'프로젝트':'Pacifica','시장 데이터':'공식 무료 시장목록 조회','미확보 데이터':'집계 거래량·OI·활성 지갑·포인트','원인 구분':'무료 공개 API 있음 · 지표별 검증/집계 필요','Dune':'거래가 오프체인이면 실제 거래지갑 집계 한계','자료':'https://pacifica.gitbook.io/docs/api-documentation/api/rest-api/markets/get-market-info'},
      {'프로젝트':'hello.trade','시장 데이터':'공식 소스 미확인','미확보 데이터':'전체','원인 구분':'자료 유료/비공개 여부도 미확인','Dune':'프로젝트/컨트랙트 식별 필요','자료':'https://dune.com/'}
    ]
    st.dataframe(pd.DataFrame(investigation),hide_index=True,use_container_width=True,height=435,
                 column_config={'자료':st.column_config.LinkColumn('검증 참고 링크')})
    st.download_button('미연동 조사표 CSV',pd.DataFrame(investigation).to_csv(index=False).encode('utf-8-sig'),'integration_audit.csv','text/csv')
    st.markdown('### 신규 공개 API 확인')
    checks=[
      {'거래소':'Pacifica','상태':'시장 목록 조회 성공' if pacifica_markets is not None else '연결 실패/응답 변경',
       '확인된 시장 수':pacifica_markets,'의미':'상장된 마켓 개수만 의미. 거래량·OI·지갑 수 아님'},
      {'거래소':'Arcus','상태':'시장/OI 조회 성공' if arcus_stats is not None else '연결 실패/응답 변경',
       '확인된 시장 수':arcus_stats.get('markets') if arcus_stats else None,'의미':'PERPETUAL 한쪽 OI×가격만 집계. 거래량·포인트 아님'}
    ]
    st.dataframe(pd.DataFrame(checks),hide_index=True,use_container_width=True)
    st.info('정확한 빈집 순위는 활성 거래자와 포인트 배분 데이터가 충분히 확보될 때만 제공할 수 있습니다. Dune 무료 API도 쿼리 실행 한도·체인 지원 여부를 확인해야 합니다.')

    status_rows=[
      {'소스':'DefiLlama 프로토콜 목록','항목':'Perp DEX 목록','상태':'연결 성공' if not catalog.empty else '실패','캐시':'15분'},
      {'소스':'DefiLlama OI 개요','항목':'OI','상태':'연결 성공' if not oi.empty else '실패','캐시':'15분'},
      {'소스':'Variational Omni 공식 API','항목':'24H Volume / OI','상태':'연결 성공' if va else '실패','캐시':'5분'},
      {'소스':'Nado / CCXT','항목':'현재 OI','상태':'연결 성공' if nado_stats else '실패/응답 확인 필요','캐시':'15분'},
      {'소스':'StandX 공식 API','항목':'24H Volume / OI','상태':'연결 성공' if sx else '실패','캐시':'15분'},
      {'소스':'Hyperliquid 공식 API','항목':'24H Volume / OI','상태':'연결 성공' if hl else '미사용/실패','캐시':'2분'},
      {'소스':'DefiLlama Pro','항목':'7D Volume','상태':'연결 성공' if not pro.empty else '유료 키 필요' if not pro_key else '실패','캐시':'15분'},
      {'소스':'참여자/포인트','항목':'7D 지갑 / 포인트','상태':'사용자 CSV' if uploaded else '미연동','캐시':'수동'}]
    st.dataframe(pd.DataFrame(status_rows),hide_index=True,use_container_width=True)
    st.caption('조회 성공은 전체 시장의 데이터가 제공되었다는 보장이 아닙니다. 개별 거래소와 체인 식별을 먼저 확인해야 합니다.')
with radar_tabs[4]:
    st.subheader('포인트 효율 시뮬레이터')
    st.caption('입력값은 계산 예시이며 실거래소의 실제 포인트 산식으로 간주하지 않습니다.')
    x,y=st.columns(2)
    with x:
        method=st.selectbox('가정',['거래량 비례','시간가중 OI 비례'])
        pool=st.number_input('주간 포인트 풀',min_value=0.,value=100000.,step=10000.)
        whole=st.number_input('전체 적격 활동량',min_value=0.,value=10000000.,step=100000.)
    with y:
        mine=st.number_input('나의 적격 활동량',min_value=0.,value=100000.,step=10000.)
        fee=st.number_input('실효 수수료 bps',min_value=0.,value=5.,step=.5)
        extra=st.number_input('추가 비용 USD',min_value=0.,value=0.,step=1.)
    if pool>0 and whole>0 and mine<=whole:
        pts=pool*mine/whole
        cost=(mine*fee/10000 if method=='거래량 비례' else 0)+extra
        c1,c2,c3=st.columns(3)
        c1.metric('예상 포인트',f'{pts:,.2f}')
        c2.metric('포인트당 비용',f'{cost/pts:.6f} USD' if pts else 'N/A')
        c3.metric('활동 점유율',f'{mine/whole*100:.4f}%')
    else:st.warning('전체 활동량과 포인트 풀을 확인하세요.')
st.divider()
st.caption('v1.2 Rankings Cloud Dancer · Read-only research dashboard · 에어드랍 수익 보장 없음 · 주문/지갑 서명 기능 없음')
