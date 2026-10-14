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
HEADERS = {'User-Agent': 'PerpFarmingRadar-MVP/0.1'}

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
    for field in ['oi_usd','volume_24h_usd','volume_7d_usd','volume_prev_7d_usd','oi_source','volume_source']:
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

st.title('📡 Perp Farming Radar')
st.caption('다수 Perp DEX의 경쟁 혼잡도와 포인트 효율을 분리해 보여주는 읽기 전용 MVP · 실제 데이터 / 수동 입력 / 계산된 추정치를 명확히 구분')
with st.sidebar:
    st.header('데이터 연결')
    pro_key=st.text_input('DefiLlama Pro API key (선택)', type='password', help='유료 derivatives 거래량 API용. 키는 저장하지 않습니다.')
    use_hl=st.checkbox('Hyperliquid 시장 데이터 불러오기', value=True)
    uploaded=st.file_uploader('프로젝트별 포인트·참여자 CSV', type='csv')
    st.download_button('CSV 템플릿 다운로드', (DATA_DIR/'manual_metrics.csv').read_bytes(), 'manual_metrics.csv', mime='text/csv')
    if st.button('API 캐시 새로고침'):
        fetch_catalog.clear();fetch_oi.clear();fetch_hyperliquid.clear();fetch_pro_derivatives.clear();st.rerun()
    st.divider()
    st.caption('관심 프로젝트 11개는 기본 등록됩니다. 이름 중복은 거래소별 실제 엔드포인트 확인 전까지 자동 통합하지 않습니다.')
    st.caption('활성 사용자 수는 현재 범용 무료 API에서 자동으로 가져오지 못합니다. 확인 가능한 프로젝트별 데이터를 CSV로 연결해야 합니다.')

errors=[]
with st.spinner('공개 데이터 수집 중…'):
    try: catalog=fetch_catalog()
    except Exception as e: catalog=pd.DataFrame();errors.append(f'프로젝트 목록: {e}')
    try: oi=fetch_oi()
    except Exception as e: oi=pd.DataFrame();errors.append(f'OI: {e}')
    try: hl=fetch_hyperliquid() if use_hl else None
    except Exception as e: hl=None;errors.append(f'Hyperliquid: {e}')
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
df['is_watchlist']=df['watch_name'].notna()

df=merge_exact(df,oi,'_oi')
df=merge_exact(df,pro,'_pro')
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
for c in ['point_model','notes','oi_source','volume_source']:
    if c not in df:df[c]=None

df['wallet_change_pct']=(df['active_wallets_7d']/df['active_wallets_prev_7d']-1)*100
df.loc[df['active_wallets_prev_7d']<=0,'wallet_change_pct']=float('nan')
df['volume_change_pct']=(df['volume_7d_usd']/df['volume_prev_7d_usd']-1)*100
df.loc[df['volume_prev_7d_usd']<=0,'volume_change_pct']=float('nan')
# Relative competition (0 low -> 100 high): calculated only for projects with validated wallet inputs.
mask=df['active_wallets_7d'].ge(0)
df['wallet_competition_percentile']=float('nan')
if mask.any():df.loc[mask,'wallet_competition_percentile']=df.loc[mask,'active_wallets_7d'].rank(pct=True,method='average')*100

with st.expander('수집 상태 및 해석', expanded=bool(errors)):
    st.write(f"프로토콜 목록: **{len(df):,}개** · 활성 지갑 제공: **{int(df['active_wallets_7d'].notna().sum())}개** · 주간 포인트 제공: **{int(df['weekly_points'].notna().sum())}개**")
    st.write('OI는 거래소마다 집계 시점과 방식이 다를 수 있습니다. TVL을 거래량 또는 포인트 효율로 해석하지 않습니다.')
    if errors:st.warning('일부 API를 가져오지 못했습니다: '+' | '.join(errors))

c1,c2,c3,c4=st.columns(4)
c1.metric('추적 프로젝트',f'{len(df):,}개')
c2.metric('활성 지갑 자료 확보',f"{df['active_wallets_7d'].notna().sum():,}개")
c3.metric('OI 자료 확보',f"{df['oi_usd'].notna().sum():,}개")
c4.metric('주간 포인트 자료 확보',f"{df['weekly_points'].notna().sum():,}개")

st.subheader('⭐ 관심 프로젝트 (11개)')
watch_view=df[df['is_watchlist']].copy()
watch_view['data_status']=watch_view.apply(lambda x: '활성 지갑 있음' if pd.notna(x['active_wallets_7d']) else ('거래/OI 일부 확보' if pd.notna(x['volume_7d_usd']) or pd.notna(x['oi_usd']) else '연동 필요'),axis=1)
watch_view=watch_view.merge(watch[['name','tracking_note']],left_on='watch_name',right_on='name',how='left',suffixes=('','_watch'))
st.dataframe(watch_view[['watch_name','data_status','active_wallets_7d','volume_7d_usd','oi_usd','weekly_points','tracking_note']].rename(columns={'watch_name':'관심 프로젝트','data_status':'데이터 상태','active_wallets_7d':'7d 활성 지갑','volume_7d_usd':'7d 거래량 $','oi_usd':'OI $','weekly_points':'주간 포인트','tracking_note':'추적 참고'}),hide_index=True,use_container_width=True)
st.caption('프로젝트 11개 등록 완료 ≠ 모든 프로젝트 실시간 데이터 연동 완료. 조회 불가능 항목은 N/A로 유지합니다.')
st.subheader('🔎 전체 거래소 스캐너')
f1,f2,f3=st.columns([2,1,1]); query=f1.text_input('이름 검색',placeholder='Hyperliquid, Lighter…')
only_wallet=f2.checkbox('지갑 데이터 있는 곳만')
only_points=f3.checkbox('포인트 데이터 있는 곳만')
only_watch=st.checkbox('관심 프로젝트만 보기')
view=df.copy()
if query:view=view[view['name'].str.contains(query,case=False,na=False,regex=False)]
if only_wallet:view=view[view['active_wallets_7d'].notna()]
if only_points:view=view[view['weekly_points'].notna()]
if only_watch:view=view[view['is_watchlist']]
cols=['name','chains','listed_date','active_wallets_7d','wallet_change_pct','wallet_competition_percentile','volume_24h_usd','volume_7d_usd','volume_change_pct','oi_usd','weekly_points','point_model']
for col in cols:
    if col not in view:view[col]=None
st.dataframe(view[cols].rename(columns={'name':'Perp DEX','chains':'체인','listed_date':'등록일(상장일 아님)','active_wallets_7d':'7d 활성 지갑','wallet_change_pct':'지갑 변화 %','wallet_competition_percentile':'지갑 경쟁 백분위','volume_24h_usd':'24h Volume $','volume_7d_usd':'7d Volume $','volume_change_pct':'거래량 변화 %','oi_usd':'OI $','weekly_points':'주간 포인트','point_model':'모델'}),hide_index=True,use_container_width=True,height=410)
st.download_button('현재 표 CSV 저장',view[cols].to_csv(index=False).encode('utf-8-sig'),'perp_radar_export.csv','text/csv')

left,right=st.columns(2)
with left:
    st.subheader('A · 참여자 혼잡도')
    participants=df.dropna(subset=['active_wallets_7d'])
    if participants.empty:
        st.info('지갑 데이터 없음. CSV의 active_wallets_7d, active_wallets_prev_7d 등을 연결하면 비교 차트가 표시됩니다.')
    else:
        chart=participants.sort_values('active_wallets_7d').tail(25)
        fig=px.bar(chart,x='active_wallets_7d',y='name',orientation='h',labels={'active_wallets_7d':'주간 활성 지갑','name':'DEX'})
        st.plotly_chart(fig,use_container_width=True)
with right:
    st.subheader('B · 주간 Volume / OI')
    chart=df.dropna(subset=['volume_7d_usd']).sort_values('volume_7d_usd').tail(20)
    if chart.empty: st.info('7일 거래량 데이터가 없습니다. 유료 데이터 키 또는 검증된 프로젝트별 거래량 CSV를 연결하세요.')
    else:
        fig=px.bar(chart,x='volume_7d_usd',y='name',orientation='h',labels={'volume_7d_usd':'7일 거래량 USD','name':'DEX'})
        st.plotly_chart(fig,use_container_width=True)

st.subheader('🧮 조건부 포인트 효율 시뮬레이터')
st.caption('거래량 비례·OI 시간가중 비례처럼 단일 측정치를 기준으로 고정 주간 풀을 배분하는 경우만 계산합니다. 혼합형·비공개형은 계산 불가.')
p1,p2=st.columns(2)
with p1:
    method=st.selectbox('가정하는 포인트 제도',['Volume 비례','시간가중 OI 비례'])
    pool=st.number_input('주간 배분 포인트',min_value=0.0,value=100000.0,step=10000.0)
    whole=st.number_input('프로젝트 전체 적격 활동량 (Volume USD 또는 USD·시간)',min_value=0.0,value=10000000.0,step=100000.0,format='%.2f')
    mine=st.number_input('내 적격 활동량 (동일 단위)',min_value=0.0,value=100000.0,step=10000.0,format='%.2f')
with p2:
    fee_bps=st.number_input('왕복 포함 실효 거래 수수료 (bps, Volume형)',min_value=0.0,value=5.0,step=0.5)
    extra=st.number_input('추가 비용 (USD, 펀딩비·슬리피지 등)',min_value=0.0,value=0.0,step=1.0)
    if whole>0 and pool>0 and mine<=whole:
        estimated=mine/whole*pool
        total_fee=(mine*fee_bps/10000 if method=='Volume 비례' else 0)+extra
        st.metric('가정하의 예상 포인트',f'{estimated:,.2f}')
        st.metric('포인트당 비용',f'${total_fee/estimated:.6f}' if estimated>0 else 'N/A')
        st.write(f'지분: **{mine/whole*100:.4f}%** · 단순 비용: **{fmt_money(total_fee)}**')
        if method!='Volume 비례':st.caption('OI 활동량에는 반드시 USD·시간 같은 기간 가중 값을 입력해야 합니다. 시점 OI 입력 금지.')
    else:st.warning('전체 활동량은 0보다 커야 하고 내 활동량을 포함해야 합니다.')

st.subheader('📅 주간 스냅샷 (로컬 저장)')
st.caption('저장 버튼을 누른 시점의 지표만 기록합니다. 이전 주와의 비교를 위해 정기적으로 저장하세요.')
snap_cols=['name','active_wallets_7d','new_wallets_7d','wallet_competition_percentile','volume_24h_usd','volume_7d_usd','oi_usd','weekly_points']
if st.button('현재 데이터 스냅샷 저장'):
    snap=df[snap_cols].copy();snap.insert(0,'captured_at_utc',datetime.now(timezone.utc).isoformat())
    snap.to_csv(SNAPSHOT_FILE,index=False,mode='a',header=not SNAPSHOT_FILE.exists(),encoding='utf-8-sig')
    st.success(f'{len(snap)}개 항목을 {SNAPSHOT_FILE.name}에 저장했습니다.')
if SNAPSHOT_FILE.exists():
    history=pd.read_csv(SNAPSHOT_FILE)
    st.download_button('스냅샷 기록 CSV 다운로드',SNAPSHOT_FILE.read_bytes(),'radar_snapshots.csv','text/csv')
    if len(history):
        names=history['name'].dropna().unique().tolist()
        selection=st.selectbox('히스토리 조회',names)
        h=history[history['name']==selection].copy();h['captured_at_utc']=pd.to_datetime(h['captured_at_utc'],utc=True)
        if h['oi_usd'].notna().any():st.plotly_chart(px.line(h,x='captured_at_utc',y='oi_usd',markers=True,title=f'{selection} · OI snapshots'),use_container_width=True)
        st.dataframe(h.tail(20),hide_index=True,use_container_width=True)
else:st.info('저장된 스냅샷이 없습니다.')
st.divider()
st.caption('리서치 목적 도구. API의 집계 방식·속도·누락 문제와 에어드랍 미보장 리스크를 고려하세요. 이 앱은 주문이나 지갑 서명을 수행하지 않습니다.')
