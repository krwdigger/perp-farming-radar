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
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');
html,body,[class*="css"],[data-testid="stApp"]{font-family:Inter,'Pretendard',sans-serif}
[data-testid="stAppViewContainer"]{background:radial-gradient(ellipse 980px 490px at 60% -200px,#193d4e,transparent 80%),#090f1a;color:#e9f2ff}
[data-testid="stSidebar"]{background:#0e1827;border-right:1px solid #23384d}
.block-container{max-width:1480px;padding-top:1.7rem;padding-bottom:4rem}
h1,h2,h3{color:#f1f7ff;letter-spacing:-.035em}
h1{font-size:2.3rem!important;font-weight:800!important}
[data-testid="stMetric"]{background:#122033;border:1px solid #2b4056;border-radius:15px;padding:15px 18px}
[data-testid="stMetricLabel"]{color:#9cb4c9}
[data-testid="stMetricValue"]{color:#e2f4ff}
[data-testid="stDataFrame"]{border:1px solid #2b4055;border-radius:12px;overflow:hidden}
.stTabs [data-baseweb="tab-list"]{gap:10px;border-bottom:1px solid #274054}
.stTabs [data-baseweb="tab"]{border-radius:9px;color:#bad0e3}
.radar-banner{background:linear-gradient(110deg,#123847,#132336);padding:23px 28px;border:1px solid #31566b;border-radius:18px;margin-bottom:19px}
.radar-overline{font-weight:800;letter-spacing:.17em;color:#53dbca;font-size:12px}
.radar-description{font-size:13px;color:#b9c9d9;margin-top:6px}
.radar-chip{display:inline-block;background:#163c40;color:#6fe0c7;padding:5px 10px;border-radius:40px;margin-top:12px;font-size:11px;font-weight:800}
.radar-hint{padding:11px 14px;background:#132237;border-left:3px solid #478fac;border-radius:7px;color:#b5c9dd;font-size:13px}
</style>""",unsafe_allow_html=True)
st.markdown("""<div class="radar-banner"><div class="radar-overline">PERP DEX INTELLIGENCE / MARKET RESEARCH</div>
<h1 style="margin:6px 0 0">◈ Perp Farming Radar</h1>
<div class="radar-description">관심 거래소 · 시장 지표 · 파밍 경쟁도 · 데이터 신뢰도를 한눈에</div>
<div class="radar-chip">● LIVE API · source-aware analytics</div></div>""",unsafe_allow_html=True)

with st.sidebar:
    st.header('데이터 연결')
    pro_key=st.text_input('DefiLlama Pro API key (선택)', type='password', help='유료 derivatives 거래량 API용. 키는 저장하지 않습니다.')
    use_hl=st.checkbox('Hyperliquid 시장 데이터 불러오기', value=True)
    uploaded=st.file_uploader('프로젝트별 포인트·참여자 CSV', type='csv')
    st.download_button('CSV 템플릿 다운로드', (DATA_DIR/'manual_metrics.csv').read_bytes(), 'manual_metrics.csv', mime='text/csv')
    if st.button('API 캐시 새로고침'):
        fetch_catalog.clear();fetch_oi.clear();fetch_hyperliquid.clear();fetch_standx.clear();fetch_variational.clear();fetch_pro_derivatives.clear();st.rerun()
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
    w1,w2=st.columns([2,1])
    choice=w1.selectbox('데이터 상태',['모두 보기','시장 데이터','참여자 데이터','미연동'])
    order=w2.selectbox('정렬',['관심 목록','OI 높은 순','OI 낮은 순','24H 거래량 순'])
    if choice!='모두 보기':watched=watched[watched['상태']==choice]
    if order=='OI 높은 순':watched=watched.sort_values('oi_usd',ascending=False,na_position='last')
    elif order=='OI 낮은 순':watched=watched.sort_values('oi_usd',ascending=True,na_position='last')
    elif order=='24H 거래량 순':watched=watched.sort_values('volume_24h_usd',ascending=False,na_position='last')
    viewcols=['watch_name','상태','volume_24h_usd','volume_7d_usd','oi_usd','active_wallets_7d','weekly_points']
    st.dataframe(readable_metrics(watched[viewcols], rename={'watch_name':'프로젝트','volume_24h_usd':'24H Volume ($)','volume_7d_usd':'7D Volume ($)','oi_usd':'현재 OI ($)','active_wallets_7d':'7D 지갑','weekly_points':'주간 포인트'})),hide_index=True,use_container_width=True,height=445)
    st.caption('미확보 수치는 0이 아닙니다. 거래량/OI가 있어도 포인트 효율을 계산할 수 있다는 뜻은 아닙니다.')
    st.markdown('#### 📊 관심 거래소 시장 규모 비교')
    chart_left, chart_right=st.columns(2)
    with chart_left:
        vol=watched.dropna(subset=['volume_24h_usd']).sort_values('volume_24h_usd')
        if vol.empty:st.info('24시간 거래량 데이터 미확보')
        else:
            fig=px.bar(vol,x='volume_24h_usd',y='watch_name',orientation='h',
                       color_discrete_sequence=['#35cdbf'],
                       labels={'volume_24h_usd':'24H 거래량 (USD)','watch_name':''})
            fig.update_layout(height=max(240,110+len(vol)*46),margin=dict(l=0,r=8,t=5,b=0),
                              paper_bgcolor='rgba(0,0,0,0)',plot_bgcolor='rgba(0,0,0,0)',
                              font_color='#e6edf7',showlegend=False)
            fig.update_xaxes(tickformat='~s',gridcolor='#304055')
            st.plotly_chart(fig,use_container_width=True)
    with chart_right:
        oi_chart=watched.dropna(subset=['oi_usd']).sort_values('oi_usd')
        if oi_chart.empty:st.info('OI 데이터 미확보')
        else:
            fig=px.bar(oi_chart,x='oi_usd',y='watch_name',orientation='h',
                       color_discrete_sequence=['#799bf2'],
                       labels={'oi_usd':'현재 OI (USD)','watch_name':''})
            fig.update_layout(height=max(240,110+len(oi_chart)*46),margin=dict(l=0,r=8,t=5,b=0),
                              paper_bgcolor='rgba(0,0,0,0)',plot_bgcolor='rgba(0,0,0,0)',
                              font_color='#e6edf7',showlegend=False)
            fig.update_xaxes(tickformat='~s',gridcolor='#304055')
            st.plotly_chart(fig,use_container_width=True)
    st.caption('그래프는 데이터가 확보된 거래소만 보여주며, Volume과 OI는 파밍 경쟁률 점수가 아닙니다.')
    with st.expander('관심 거래소별 데이터 출처 및 수집 상태'):
        st.dataframe(watched[['watch_name','volume_source','oi_source','market_data_time']].rename(columns={'watch_name':'거래소','volume_source':'거래량 출처','oi_source':'OI 출처','market_data_time':'데이터 조회 시각'}),hide_index=True,use_container_width=True)
    st.download_button('관심 프로젝트 데이터 CSV',watched.to_csv(index=False).encode('utf-8-sig'),'watchlist_metrics.csv','text/csv')
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
            fig.update_layout(height=560,paper_bgcolor='rgba(0,0,0,0)',plot_bgcolor='rgba(0,0,0,0)',font_color='#c5d6e7')
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
    st.dataframe(readable_metrics(v[sc], rename={'name':'거래소','chains':'체인','listed_date':'목록 등록일','volume_24h_usd':'24H Volume','volume_7d_usd':'7D Volume','oi_usd':'OI','active_wallets_7d':'7D 지갑','weekly_points':'주간 포인트'})),hide_index=True,use_container_width=True,height=570)
    st.download_button('스캐너 CSV 내보내기',v[sc].to_csv(index=False).encode('utf-8-sig'),'perp_scanner.csv','text/csv')
with radar_tabs[3]:
    st.subheader('데이터 연결 현황')
    status_rows=[
      {'소스':'DefiLlama 프로토콜 목록','항목':'Perp DEX 목록','상태':'연결 성공' if not catalog.empty else '실패','캐시':'15분'},
      {'소스':'DefiLlama OI 개요','항목':'OI','상태':'연결 성공' if not oi.empty else '실패','캐시':'15분'},
      {'소스':'Variational Omni 공식 API','항목':'24H Volume / OI','상태':'연결 성공' if va else '실패','캐시':'5분'},
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
st.caption('v0.6 · Read-only research dashboard · 에어드랍 수익 보장 없음 · 주문/지갑 서명 기능 없음')
