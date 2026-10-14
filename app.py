"""Perp Farming Radar: read-only, data-availability-aware research MVP."""
from __future__ import annotations

import io
import html
import os
import base64
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont
import re
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import plotly.express as px
import requests
import streamlit as st

st.set_page_config(page_title='빈집털이', page_icon='🤔', layout='wide', initial_sidebar_state='collapsed')

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
        result.append({'name':name, 'category':p.get('category'), 'chains': ', '.join(p.get('chains') or []), 'tvl_usd': number(p.get('tvl')), 'listed_date':listed, 'source':'DefiLlama catalog','logo_url':p.get('logo')})
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
    volumes=[number(m.get('volume24hNotional')) for m in perpetuals]
    volumes=[v for v in volumes if v is not None and v>=0]
    return {'markets':len(perpetuals),'oi_usd':sum(values) if values else None,
            'volume_24h_usd':sum(volumes) if volumes else None,
            'volume_source':'Arcus official /v1/markets · sum perpetual volume24hNotional',
            'oi_source':'Arcus markets API · perpetual one-sided OI × mark price'}

@st.cache_data(ttl=300,show_spinner=False)
def fetch_lighter_mainnet():
    """Generic Lighter mainnet only: NOT Robinhood Chain Lighter."""
    payload=api_get('https://mainnet.zklighter.elliot.ai/api/v1/exchangeStats')
    if not isinstance(payload,dict) or payload.get('code')!=200:
        raise ValueError('Lighter exchangeStats response invalid')
    volume=number(payload.get('daily_usd_volume'))
    if volume is None:
        stats=payload.get('order_book_stats') or []
        values=[number(item.get('daily_quote_token_volume')) for item in stats if isinstance(item,dict)]
        values=[v for v in values if v is not None and v>=0]
        volume=sum(values) if values else None
    if volume is None:raise ValueError('Lighter has no daily USD volume')
    return {'volume_24h_usd':volume,'volume_source':'Lighter mainnet official exchangeStats',
            'market_data_time':datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}

@st.cache_data(ttl=900,show_spinner=False)
def fetch_pacifica_ccxt():
    """Market OI from published CCXT Pacifica adapter. Treat failures as missing."""
    import ccxt
    client=ccxt.pacifica({'enableRateLimit':True,'timeout':16000})
    records=client.fetch_open_interests()
    if isinstance(records,dict):records=list(records.values())
    if not isinstance(records,list):raise ValueError('Expected CCXT OI records')
    values=[]
    for record in records:
        if not isinstance(record,dict):continue
        usd=number(record.get('openInterestValue'))
        if usd is not None and usd>=0:values.append(usd)
    if not values:raise ValueError('CCXT response lacks USD-denominated openInterestValue')
    return {'oi_usd':sum(values),'oi_source':'Pacifica via CCXT fetchOpenInterests, USD notional',
            'market_data_time':datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}

@st.cache_data(ttl=900,show_spinner=False)
def fetch_ccxt_perp_volume(venue):
    """Aggregate only USD quoteVolume tickers for perpetual swap markets."""
    import ccxt
    exchange=getattr(ccxt,venue)({'enableRateLimit':True,'timeout':16000})
    markets=exchange.load_markets()
    tickers=exchange.fetch_tickers()
    total=0.0
    counted=0
    for symbol,ticker in tickers.items():
        market=markets.get(symbol)
        if not isinstance(market,dict) or not market.get('swap') or market.get('active') is False:
            continue
        quote=str(market.get('quote') or '').upper()
        settle=str(market.get('settle') or '').upper()
        if quote not in ('USD','USDC','USDT') or settle not in ('USD','USDC','USDT'):
            continue
        vol=number(ticker.get('quoteVolume'))
        if vol is None:
            base=number(ticker.get('baseVolume'))
            last=number(ticker.get('last'))
            if base is not None and last is not None:vol=base*last
        if vol is not None and vol>=0:
            total+=vol
            counted+=1
    if counted==0:raise ValueError('No USD-quoted perpetual tickers with 24h volume')
    return {'volume_24h_usd':total,'volume_source':venue+' CCXT perpetual 24h quote volume',
            'market_data_time':datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}

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
/* Independent fixed settings control: does not depend on native sidebar DOM. */
[data-testid="stSidebarCollapsedControl"],
[data-testid="collapsedControl"],
[data-testid="stSidebarCollapseButton"],
[data-testid="stSidebarNavCollapseButton"]{display:none!important;}
[data-testid="stHeader"]{background:transparent!important;}
[data-testid="stPopover"]{
    position:fixed!important;
    left:0!important;
    top:6px!important;
    z-index:2147483000!important;
    width:auto!important;
    margin:0!important;
    padding:0!important;
}
[data-testid="stPopover"] > button,
[data-testid="stPopover"] > div > button {
    width:32px!important;
    min-width:32px!important;
    height:34px!important;
    min-height:34px!important;
    padding:0!important;
    margin:0!important;
    display:flex!important;
    align-items:center!important;
    justify-content:center!important;
    border:0!important;
    border-radius:0!important;
    background:transparent!important;
    color:#5e7180!important;
    font-size:24px!important;
    font-weight:400!important;
    box-shadow:none!important;
}
/* Popover renders its own trailing chevron: keep only the custom » label. */
[data-testid="stPopover"] > button [data-testid="stIconMaterial"],
[data-testid="stPopover"] > div > button [data-testid="stIconMaterial"],
[data-testid="stPopover"] > button svg,
[data-testid="stPopover"] > div > button svg {
    display:none!important;
}
[data-testid="stPopover"] > button:hover,
[data-testid="stPopover"] > div > button:hover {
    background:#edf3f6!important;
    color:#344f61!important;
}
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
.block-container{padding-top:1.25rem!important;padding-bottom:2rem!important}
[data-testid="stHeader"]{height:2.25rem;background:transparent}
.radar-banner{background:linear-gradient(115deg,#edf5f9,#f7f7f3 67%,#eaf0f2);padding:10px 18px;border:1px solid #d9e3e7;border-radius:12px;margin-bottom:8px}
.radar-overline{font-weight:700;letter-spacing:.17em;color:#7594a6;font-size:11px}
.radar-banner h1{color:#586771!important;font-size:1.42rem!important;font-weight:700!important;line-height:1.15!important}
.radar-description{font-size:12px;color:#7b8992;margin-top:3px}
.radar-chip{display:none}
.radar-hint{padding:14px 18px;background:#ecf3f7;border-left:3px solid #a5bfd0;border-radius:9px;color:#607685;font-size:13px}
.radar-hint b{color:#566b79}
[data-testid="stSidebar"]{min-width:230px!important;max-width:230px!important;width:230px!important}
[data-testid="stSidebar"] [data-testid="stVerticalBlock"]{gap:.35rem}
[data-testid="stSidebar"] h2{font-size:1.08rem!important}
[data-testid="stSidebar"] [data-testid="stFileUploaderDropzone"]{padding:8px!important}
</style>""",unsafe_allow_html=True)
st.markdown("""<div class="radar-banner">
<h1 style="margin:8px 0 2px">빈집털이</h1>
<div class="radar-description">참여자 밀집도와 포인트 파밍 효율을 추적하는 리서치 대시보드</div>
<div class="radar-chip">● LIVE MARKET DATA · CLOUD DANCER EDITION</div></div>""",unsafe_allow_html=True)

# Independent left-edge settings control replaces the fragile native sidebar opener.
with st.popover('»', help='데이터 연결 및 설정 열기'):
    st.header('데이터 연결')
    pro_key=st.text_input('DefiLlama Pro API key (선택)', type='password', help='유료 derivatives 거래량 API용. 키는 저장하지 않습니다.')
    use_hl=st.checkbox('Hyperliquid 시장 데이터 불러오기', value=True)
    uploaded=st.file_uploader('프로젝트별 포인트·참여자 CSV', type='csv')
    st.download_button('CSV 템플릿 다운로드', (DATA_DIR/'manual_metrics.csv').read_bytes(), 'manual_metrics.csv', mime='text/csv')
    if st.button('API 캐시 새로고침'):
        fetch_catalog.clear();fetch_oi.clear();fetch_hyperliquid.clear();fetch_standx.clear();fetch_variational.clear();fetch_pacifica_markets.clear();fetch_arcus_markets.clear();fetch_nado_ccxt.clear();fetch_pacifica_ccxt.clear();fetch_pro_derivatives.clear();st.rerun()


errors=[]
with st.spinner('공개 데이터 수집 중…'):
    try: catalog=fetch_catalog()
    except Exception as e: catalog=pd.DataFrame();errors.append(f'프로젝트 목록: {e}')
    try: oi=fetch_oi()
    except Exception as e: oi=pd.DataFrame();errors.append(f'OI: {e}')
    try: hl=fetch_hyperliquid() if use_hl else None
    except Exception as e: hl=None;errors.append(f'Hyperliquid: {e}')
    try: lighter_mainnet=fetch_lighter_mainnet()
    except Exception as e: lighter_mainnet=None;errors.append(f'Lighter mainnet: {e}')
    try: sx=fetch_standx()
    except Exception as e: sx=None;errors.append(f'StandX: {e}')
    try: va=fetch_variational()
    except Exception as e: va=None;errors.append(f'Variational: {e}')
    try: pacifica_markets=fetch_pacifica_markets()
    except Exception as e: pacifica_markets=None;errors.append(f'Pacifica markets: {e}')
    try: arcus_stats=fetch_arcus_markets()
    except Exception as e: arcus_stats=None;errors.append(f'Arcus markets: {e}')
    try: pacifica_oi=fetch_pacifica_ccxt()
    except Exception as e: pacifica_oi=None;errors.append(f'Pacifica CCXT OI: {e}')
    try: pacifica_volume=fetch_ccxt_perp_volume('pacifica')
    except Exception as e: pacifica_volume=None;errors.append(f'Pacifica 24h volume: {e}')
    try: nado_volume=fetch_ccxt_perp_volume('nado')
    except Exception as e: nado_volume=None;errors.append(f'Nado 24h volume: {e}')
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
if pacifica_oi:
    pacifica_mask=df['watch_name'].eq('Pacifica')
    if pacifica_mask.any():
        for field,value in pacifica_oi.items():
            if field not in df.columns:df[field]=None
            df.loc[pacifica_mask,field]=value
for venue_name,venue_data in [('Pacifica',pacifica_volume),('Nado',nado_volume)]:
    if venue_data:
        venue_mask=df['watch_name'].eq(venue_name)
        if venue_mask.any():
            for field,value in venue_data.items():
                if field not in df.columns:df[field]=None
                df.loc[venue_mask,field]=value
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
if lighter_mainnet:
    key=norm('Lighter')
    existing=df['_id'].eq(key)
    if existing.any():
        for field,value in lighter_mainnet.items():
            if field not in df.columns:df[field]=None
            df.loc[existing,field]=value
    else:
        df=pd.concat([df,pd.DataFrame([{'name':'Lighter','_id':key,**lighter_mainnet}])],ignore_index=True)
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

# Recent collector values backfill only missing public market metrics.
try:
    archive=pd.read_csv('https://raw.githubusercontent.com/krwdigger/perp-farming-radar/main/data/market_snapshots.csv')
    archive['captured_at_utc']=pd.to_datetime(archive['captured_at_utc'],utc=True,errors='coerce')
    for metric in ['volume_24h_usd','oi_usd']:
        archive[metric]=pd.to_numeric(archive[metric],errors='coerce')
    archive=archive[(archive['status'].eq('ok')) & archive['captured_at_utc'].notna()]
    archive=archive[archive['captured_at_utc']>=pd.Timestamp.now(tz='UTC')-pd.Timedelta(hours=48)]
    archive=archive[archive['venue'].isin(watch['name'])].sort_values('captured_at_utc',ascending=False)
    for metric,source in [('volume_24h_usd','volume_source'),('oi_usd','oi_source')]:
        recent=archive.dropna(subset=[metric]).drop_duplicates('venue')
        if recent.empty:continue
        look=recent.set_index('venue')
        fill=df['watch_name'].map(look[metric])
        missing=df[metric].isna() & fill.notna()
        df.loc[missing,metric]=fill[missing]
        df.loc[missing,source]='GitHub public snapshot (48h maximum age)'
except Exception as archive_error:
    errors.append('Historical snapshot fallback: '+str(archive_error)[:140])
df['volume_oi_ratio_24h']=df['volume_24h_usd']/df['oi_usd'].replace(0,float('nan'))
df.loc[df['oi_usd']<=0,'volume_oi_ratio_24h']=float('nan')

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

radar_tabs=st.tabs(['⭐ 관심 거래소','📈 경쟁 비교','🔎 전체 스캐너','🔌 데이터 출처','🧮 포인트 계산기'])
@st.cache_data(ttl=300,show_spinner=False)
def load_public_market_history():
    url='https://raw.githubusercontent.com/krwdigger/perp-farming-radar/main/data/market_snapshots.csv'
    data=pd.read_csv(url)
    for key in ['oi_usd','volume_24h_usd']:
        data[key]=pd.to_numeric(data[key],errors='coerce')
    data['captured_at_utc']=pd.to_datetime(data['captured_at_utc'],utc=True,errors='coerce')
    data=data[(data['status']=='ok') & data['captured_at_utc'].notna()]
    return data

def latest_oi_weekly_change(data,venue):
    match=data[(data['venue']==venue)&data['oi_usd'].notna()].sort_values('captured_at_utc')
    if match.empty:return float('nan')
    now=match.iloc[-1]
    previous=match[match['captured_at_utc']<=now['captured_at_utc']-pd.Timedelta(days=7)]
    if previous.empty or previous.iloc[-1]['oi_usd']<=0:return float('nan')
    return (now['oi_usd']/previous.iloc[-1]['oi_usd']-1)*100

@st.cache_data(ttl=3600,show_spinner=False)
def round_exchange_logo(name, url):
    """Produce a true circular PNG: the image corners are transparent, not merely outlined."""
    from PIL import Image, ImageDraw, ImageOps
    size=192
    # Supersampling makes the actual clipping edge smooth at small chart sizes.
    mask=Image.new('L',(size,size),0)
    ImageDraw.Draw(mask).ellipse((3,3,size-4,size-4),fill=255)
    avatar=None
    if url and url.startswith('https://'):
        try:
            response=requests.get(url,timeout=8,headers=HEADERS)
            response.raise_for_status()
            image=Image.open(BytesIO(response.content)).convert('RGBA')
            # Cover (rather than contain) the circle so square logos cannot remain as
            # visibly square images surrounded by a decorative ring.
            avatar=ImageOps.fit(image,(size,size),method=Image.Resampling.LANCZOS,
                                centering=(0.5,0.5))
            # Preserve transparent source artwork while giving it a circular backing.
            backing=Image.new('RGBA',(size,size),(248,251,253,255))
            backing.alpha_composite(avatar)
            avatar=backing
        except Exception:
            avatar=None
    if avatar is None:
        avatar=Image.new('RGBA',(size,size),(227,239,246,255))
        drawer=ImageDraw.Draw(avatar)
        drawer.text((size//2,size//2),str(name)[:1].upper(),
                    fill=(44,79,100,255),anchor='mm',stroke_width=0)
    avatar.putalpha(mask)
    output=BytesIO()
    avatar.save(output,format='PNG')
    return 'data:image/png;base64,'+base64.b64encode(output.getvalue()).decode('ascii')

def exchange_logo_url(name):
    # Official-site favicons for projects absent or mismatched in DefiLlama.
    official={'StandX':'standx.com','Entropy':'entropy.io','hello.trade':'hello.trade'}
    if name in official:
        return 'https://www.google.com/s2/favicons?domain='+official[name]+'&sz=128'
    candidates = {
        'Lighter': ['lighter'], 'Hyperliquid': ['hyperliquid'],
        'StandX': ['standx', 'standx perps'], 'Nado': ['nado'],
        'Variational': ['variational'], 'Pacifica': ['pacifica'],
        'Arcus': ['arcus'], 'QFEX': ['qfex'], 'N1': ['n1'],
        'Entropy': ['entropy'], 'TxFlow': ['txflow'],
        'robinhoodchain.lighter': [], 'hello.trade': ['hello.trade'],
    }
    logos = {}
    if 'logo_url' in catalog.columns and 'name' in catalog.columns:
        logos = {
            norm(row['name']): row['logo_url']
            for _, row in catalog.iterrows()
            if isinstance(row.get('logo_url'), str) and row['logo_url'].startswith('https://')
        }
    url = next(
        (logos.get(norm(n)) for n in candidates.get(name, [name]) if logos.get(norm(n))),
        None
    )
    domains = {
        'Hyperliquid': 'hyperliquid.xyz', 'Lighter': 'lighter.xyz',
        'Variational': 'omni.variational.io', 'Nado': 'nado.finance',
        'StandX': 'standx.com', 'Arcus': 'arcus.xyz',
        'QFEX': 'qfex.com', 'TxFlow': 'txflow.com',
        'Pacifica': 'pacifica.fi', 'hello.trade': 'hello.trade',
        'robinhoodchain.lighter': 'robinhood.com', 'N1': 'n1.xyz',
    }
    if not url and name in domains:
        url = f"https://www.google.com/s2/favicons?domain={domains[name]}&sz=64"
    return url

def exchange_logo_html(name):
    # DefiLlama catalog logos are actual exchange assets, never guessed.
    candidates={'Lighter':['lighter'],'Hyperliquid':['hyperliquid'],'StandX':['standx','standx perps'],'Nado':['nado'],'Variational':['variational'],
                'Pacifica':['pacifica'],'Arcus':['arcus'],'QFEX':['qfex'],'N1':['n1'],'Entropy':['entropy'],'TxFlow':['txflow'],
                'robinhoodchain.lighter':[],'hello.trade':['hello.trade']}
    logos={}
    if 'logo_url' in catalog.columns and 'name' in catalog.columns:
        logos={norm(row['name']):row['logo_url'] for _,row in catalog.iterrows() if isinstance(row.get('logo_url'),str) and row['logo_url'].startswith('https://')}
    url=exchange_logo_url(name)
    # Verified official web domains are used for favicon lookup when not in the catalog.
    domains={
        'Hyperliquid':'hyperliquid.xyz','Lighter':'lighter.xyz',
        'Variational':'omni.variational.io','Nado':'nado.finance',
        'StandX':'standx.com','Arcus':'arcus.xyz','QFEX':'qfex.com',
        'TxFlow':'txflow.com','Pacifica':'pacifica.fi','hello.trade':'hello.trade','N1':'n1.xyz',
        'robinhoodchain.lighter':'robinhood.com'
    }
    if not url and name in domains:
        url='https://www.google.com/s2/favicons?domain='+domains[name]+'&sz=64'
    label=html.escape(str(name))
    if url:
        return '<span class="exchange-cell"><img class="exchange-logo" src="'+html.escape(url,quote=True)+'" alt="" loading="lazy" referrerpolicy="no-referrer"><span>'+label+'</span></span>'
    return '<span class="exchange-cell"><span class="exchange-monogram">'+html.escape(str(name)[:1].upper())+'</span><span>'+label+'</span></span>'

def market_volume_html(value,scale):
    if pd.isna(value):return '<span class="radar-missing">—</span>'
    portion=max(0,min(100,float(value)/max(float(scale),1)*100))
    return '<div class="radar-volume-cell"><div class="radar-bar-track"><div class="radar-bar-fill" style="width:'+f'{portion:.2f}'+'%"></div></div><span>'+html.escape(compact_usd(value))+'</span></div>'

with radar_tabs[0]:
    comparison=df[df['_id'].isin([norm('Hyperliquid'),norm('Lighter')])].copy()
    comparison['watch_name']=comparison['_id'].map({norm('Hyperliquid'):'Hyperliquid',norm('Lighter'):'Lighter'})
    comparison=comparison.drop_duplicates('watch_name')
    watched=df[df['is_watchlist'].fillna(False).astype(bool)].copy()
    # Preserve all 11 named watchlist projects, even when no market metrics exist.
    # Benchmark exchanges are displayed separately and never consume rank numbers.
    watched=watched.drop_duplicates(subset=['watch_name'],keep='first').copy()
    watched=watched[watched['watch_name'].notna() & watched['watch_name'].astype(str).str.strip().ne('')].copy()
    benchmarks=comparison.copy()
    # Both tables share the same volume scale, including reference markets.
    combined_volumes=pd.concat([watched['volume_24h_usd'],benchmarks['volume_24h_usd']]).dropna()
    shared_volume_max=max(1.0,float(combined_volumes.max())) if not combined_volumes.empty else 1.0
    try:
        history=load_public_market_history()
        watched['oi_weekly_change_pct']=watched['watch_name'].map(lambda n:latest_oi_weekly_change(history,n))
    except Exception:
        history=pd.DataFrame()
        watched['oi_weekly_change_pct']=float('nan')
    watched['volume_oi_ratio']=watched['volume_24h_usd']/watched['oi_usd'].replace(0,float('nan'))
    watched.loc[watched['oi_usd']<=0,'volume_oi_ratio']=float('nan')
    st.subheader('거래소별 거래량 · OI')
    # Two compact reference rows, deliberately not part of the farming rankings.
    if not benchmarks.empty:
        st.caption('시장 규모 비교군 · Hyperliquid / Lighter')
        benchmark_scale=shared_volume_max
        benchmark_rows=[]
        for _,b in benchmarks.iterrows():
            v=b['volume_24h_usd'];benchmark_oi=b['oi_usd']
            ratio=f'{v/benchmark_oi:.2f}×' if pd.notna(v) and pd.notna(benchmark_oi) and benchmark_oi>0 else '—'
            benchmark_rows.append('<tr><td class="rank-name">'+exchange_logo_html(b['watch_name'])+'</td>'
                                  '<td>'+market_volume_html(v,benchmark_scale)+'</td>'
                                  '<td>'+html.escape(compact_usd(benchmark_oi))+'</td><td>'+html.escape(ratio)+'</td></tr>')
        st.markdown('<div class="radar-table-wrap benchmark-wrap"><table class="radar-rank-table"><thead><tr>'
                    '<th>비교군</th><th>24H Volume</th><th>Open Interest</th><th>Vol / OI</th>'
                    '</tr></thead><tbody>'+''.join(benchmark_rows)+'</tbody></table></div>',unsafe_allow_html=True)
    control1,control2,control3=st.columns([2,1,1])
    search=control1.text_input('거래소 검색',placeholder='Variational / StandX / Pacifica',key='market_search')
    display_mode=control2.selectbox('지표',['전체','Volume 확보','OI 확보','Volume + OI 확보'],key='market_metric')
    sort=control3.selectbox('정렬',['24H Volume ↓','OI ↓','Volume/OI ↓','Volume/OI ↑','거래소명'],key='market_sort')
    rank=watched.copy()
    if search:rank=rank[rank['watch_name'].astype(str).str.contains(search,case=False,regex=False)]
    if display_mode=='Volume 확보':rank=rank[rank['volume_24h_usd'].notna()]
    elif display_mode=='OI 확보':rank=rank[rank['oi_usd'].notna()]
    elif display_mode=='Volume + OI 확보':rank=rank[rank['volume_24h_usd'].notna()&rank['oi_usd'].notna()]
    col,ascending={'24H Volume ↓':('volume_24h_usd',False),'OI ↓':('oi_usd',False),'Volume/OI ↓':('volume_oi_ratio',False),'Volume/OI ↑':('volume_oi_ratio',True),'거래소명':('watch_name',True)}[sort]
    rank=rank.sort_values(col,ascending=ascending,na_position='last')
    rank=rank.reset_index(drop=True)
    rank.insert(0,'#',range(1,len(rank)+1))
    max_volume=shared_volume_max
    def table_cell(value):
        return html.escape(str(value))
    def volume_bar(value):
        if pd.isna(value):return '<span class="radar-missing">—</span>'
        percentage=max(0.0,min(100.0,float(value)/max_volume*100))
        return ('<div class="radar-volume-cell"><div class="radar-bar-track">'
                '<div class="radar-bar-fill" style="width:'+f'{percentage:.2f}'+'%"></div>'
                '</div><span>'+table_cell(compact_usd(value))+'</span></div>')
    body=[]
    for _,entry in rank.iterrows():
        row_oi_text=compact_usd(entry['oi_usd'])
        ratio=f"{entry['volume_oi_ratio']:.2f}×" if pd.notna(entry['volume_oi_ratio']) else '—'
        oi_delta=f"{entry['oi_weekly_change_pct']:+.1f}%" if pd.notna(entry['oi_weekly_change_pct']) else '—'
        traders=integer_fmt(entry['active_wallets_7d'])
        body.append('<tr><td class="rank-no">'+table_cell(int(entry['#']))+'</td>'
                    '<td class="rank-name">'+exchange_logo_html(entry['watch_name'])+'</td>'
                    '<td>'+volume_bar(entry['volume_24h_usd'])+'</td>'
                    '<td>'+table_cell(row_oi_text)+'</td>'
                    '<td>'+table_cell(ratio)+'</td>'
                    '<td>'+table_cell(oi_delta)+'</td>'
                    '<td>'+table_cell(traders)+'</td></tr>')
    st.markdown("""<style>
    .radar-table-wrap{width:100%;overflow-x:auto;border:1px solid #d8e2e6;border-radius:14px;background:#fffefa}
    .radar-rank-table{width:100%;border-collapse:collapse;min-width:920px;font-size:16px;color:#344f61}
    .radar-rank-table thead th{background:#edf3f6;color:#6d8190;text-align:left;
        font-size:14px;font-weight:700;padding:16px 14px;white-space:nowrap}
    .radar-rank-table tbody td{border-top:1px solid #e6ecee;padding:18px 14px;
        font-variant-numeric:tabular-nums;white-space:nowrap;font-size:16px}
    .radar-rank-table tbody tr:hover{background:#f1f7fa}
    .radar-rank-table .rank-no{width:50px;color:#8799a4;font-weight:650}
    .radar-rank-table .rank-name{font-weight:730;color:#36586c;min-width:155px}
    .radar-rank-table th:nth-child(3),.radar-rank-table td:nth-child(3){min-width:230px;width:30%}
    .radar-volume-cell{display:flex;gap:12px;align-items:center;justify-content:space-between}
    .radar-bar-track{min-width:105px;flex:1;height:11px;background:#e3edf2;border-radius:8px;overflow:hidden}
    .radar-bar-fill{height:100%;background:#76a9c1;border-radius:8px}
    .radar-volume-cell span{min-width:80px;text-align:right;font-weight:750;font-size:15px;color:#35556a}
    .radar-missing{color:#a5afb6}
    .exchange-cell{display:inline-flex;align-items:center;gap:10px;vertical-align:middle}
    .exchange-logo,.exchange-monogram{width:30px;height:30px;min-width:30px;border-radius:50%;object-fit:contain}
    .exchange-monogram{display:inline-flex;align-items:center;justify-content:center;background:#e4edf2;color:#57788c;font-size:14px;font-weight:700}
    .benchmark-wrap{margin:5px 0 18px}
    .benchmark-wrap .radar-rank-table tbody td{padding:12px 14px}
    .benchmark-wrap .radar-rank-table th:nth-child(2),.benchmark-wrap .radar-rank-table td:nth-child(2){min-width:230px;width:30%}

    </style>""",unsafe_allow_html=True)
    table_header='<div class="radar-table-wrap"><table class="radar-rank-table"><thead><tr><th>#</th><th>Exchange</th><th>24H Volume</th><th>Open Interest</th><th>Vol / OI</th><th>OI 7D</th><th>7D Traders</th></tr></thead><tbody>'
    st.markdown(table_header+''.join(body)+'</tbody></table></div>',unsafe_allow_html=True)
    st.caption('Vol/OI는 거래 회전율에 가까운 시장 활동 지표입니다. 파밍 수익률이나 활성 지갑 수의 대체치는 아닙니다. OI 7D는 일주일 전 기록이 있는 경우에만 표시합니다.')
    chart_a,chart_b=st.columns(2)
    with chart_a:
        st.markdown('#### 24H Volume Rankings')
        v=watched.dropna(subset=['volume_24h_usd']).sort_values('volume_24h_usd')
        if not v.empty:
            fig=px.bar(v,x='volume_24h_usd',y='watch_name',orientation='h',text='volume_24h_usd',
                       color_discrete_sequence=['#75aabe'],labels={'volume_24h_usd':'USD','watch_name':''})
            fig.update_traces(texttemplate='%{text:$,.3s}',textposition='outside',cliponaxis=False)
            fig.update_layout(height=max(260,len(v)*52+70),margin=dict(l=0,r=90,t=8,b=20),
                              paper_bgcolor='rgba(0,0,0,0)',plot_bgcolor='rgba(0,0,0,0)',showlegend=False,
                              font_color='#647682',xaxis=dict(tickformat='~s',gridcolor='#e4e9e9'))
            st.plotly_chart(fig,use_container_width=True)
    with chart_b:
        st.markdown('#### Open Interest Rankings')
        v=watched.dropna(subset=['oi_usd']).sort_values('oi_usd')
        if not v.empty:
            fig=px.bar(v,x='oi_usd',y='watch_name',orientation='h',text='oi_usd',
                       color_discrete_sequence=['#a1a9ba'],labels={'oi_usd':'USD','watch_name':''})
            fig.update_traces(texttemplate='%{text:$,.3s}',textposition='outside',cliponaxis=False)
            fig.update_layout(height=max(260,len(v)*52+70),margin=dict(l=0,r=90,t=8,b=20),
                              paper_bgcolor='rgba(0,0,0,0)',plot_bgcolor='rgba(0,0,0,0)',showlegend=False,
                              font_color='#647682',xaxis=dict(tickformat='~s',gridcolor='#e4e9e9'))
            st.plotly_chart(fig,use_container_width=True)
    st.markdown('#### 시장 위치 분석')
    both=watched.dropna(subset=['volume_24h_usd','oi_usd']).copy()
    both=both[(both['volume_24h_usd']>0)&(both['oi_usd']>0)]
    if not both.empty:
        import math
        volumes=both['volume_24h_usd']
        low,high=float(volumes.min()),float(volumes.max())
        def marker_size(volume):
            # Circle area scales with 24H volume; perceptible without overpowering labels.
            fraction=(float(volume)-low)/(high-low) if high>low else 0.5
            return 43+29*math.sqrt(fraction)
        lx=both['oi_usd'].map(lambda v:math.log10(v))
        ly=both['volume_24h_usd'].map(lambda v:math.log10(v))
        xmin,xmax=float(lx.min()),float(lx.max())
        ymin,ymax=float(ly.min()),float(ly.max())
        xpad=max((xmax-xmin)*0.23,0.24)
        ypad=max((ymax-ymin)*0.26,0.26)
        xlo,xhi=xmin-xpad,xmax+xpad
        ylo,yhi=ymin-ypad,ymax+ypad
        fig=px.scatter(both,x='oi_usd',y='volume_24h_usd',hover_name='watch_name',
                       log_x=True,log_y=True,
                       labels={'oi_usd':'Open Interest (USD)','volume_24h_usd':'24H Volume (USD)'})
        fig.update_traces(marker=dict(size=45,color='rgba(0,0,0,0)'),
                          hovertemplate='<b>%{hovertext}</b><br>OI: $%{x:,.0f}<br>24H Volume: $%{y:,.0f}<extra></extra>')
        for _,entry in both.iterrows():
            xpos=(math.log10(float(entry['oi_usd']))-xlo)/(xhi-xlo)
            ypos=(math.log10(float(entry['volume_24h_usd']))-ylo)/(yhi-ylo)
            diameter=marker_size(entry['volume_24h_usd'])
            # Domain coordinates permit identically sized circular icons on log scales.
            fig.add_layout_image(dict(
                source=round_exchange_logo(str(entry['watch_name']),exchange_logo_url(entry['watch_name'])),
                xref='x domain',yref='y domain',x=xpos,y=ypos,
                sizex=diameter/900,sizey=diameter/430,
                xanchor='center',yanchor='middle',sizing='contain',layer='above'))
            fig.add_annotation(x=xpos,y=min(0.985,ypos+diameter/760+0.018),
                               xref='x domain',yref='y domain',showarrow=False,
                               text=html.escape(str(entry['watch_name'])),
                               font=dict(size=14,color='#2f4658',family='Arial'),
                               bgcolor='rgba(255,255,255,0.92)',borderpad=3)
        def axis_ticks(lo,hi):
            candidates=[1,2,5]
            ticks=[]
            for power in range(math.floor(lo)-1,math.ceil(hi)+2):
                for unit in candidates:
                    logvalue=power+math.log10(unit)
                    if lo<=logvalue<=hi:
                        value=unit*10**power
                        label=f'${value/1e9:g}B' if value>=1e9 else (f'${value/1e6:g}M' if value>=1e6 else f'${value:,.0f}')
                        ticks.append((value,label))
            return ticks
        xticks=axis_ticks(xlo,xhi)
        yticks=axis_ticks(ylo,yhi)
        fig.update_layout(height=515,margin=dict(t=24,l=22,r=35,b=24),
                          paper_bgcolor='rgba(0,0,0,0)',plot_bgcolor='rgba(0,0,0,0)',
                          font=dict(family='Arial',size=14,color='#3f5565'),showlegend=False,
                          xaxis=dict(range=[xlo,xhi],tickmode='array',
                                     tickvals=[v for v,_ in xticks],ticktext=[t for _,t in xticks],
                                     title_font=dict(size=15),tickfont=dict(size=13),
                                     showgrid=True,gridcolor='#e2e9ed',minor=dict(showgrid=False),zeroline=False),
                          yaxis=dict(range=[ylo,yhi],tickmode='array',
                                     tickvals=[v for v,_ in yticks],ticktext=[t for _,t in yticks],
                                     title_font=dict(size=15),tickfont=dict(size=13),
                                     showgrid=True,gridcolor='#e2e9ed',minor=dict(showgrid=False),zeroline=False))
        st.plotly_chart(fig,use_container_width=True)
        st.caption('원형 로고의 크기는 24시간 거래량에 비례합니다. 가로축은 OI, 세로축은 거래량이며 두 축 모두 로그 눈금입니다.')
    with st.expander('데이터 제공 범위 및 미확보 항목'):
        coverage=watched[['watch_name','volume_source','oi_source']].copy()
        coverage['Volume']=watched['volume_24h_usd'].map(lambda x:'확보' if pd.notna(x) else '미확보')
        coverage['OI']=watched['oi_usd'].map(lambda x:'확보' if pd.notna(x) else '미확보')
        st.dataframe(coverage.rename(columns={'watch_name':'거래소','volume_source':'거래량 출처','oi_source':'OI 출처'}),hide_index=True,use_container_width=True)
    st.download_button('시장 데이터 CSV',watched.to_csv(index=False).encode('utf-8-sig'),'perp_market_radar.csv','text/csv')

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
    if errors:
        with st.expander('API 오류 상세',expanded=False):
            for error in errors:st.write(error)
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
      {'소스':'DefiLlama OI 개요','항목':'OI','상태':'연결 성공' if isinstance(oi,pd.DataFrame) and not oi.empty else '실패','캐시':'15분'},
      {'소스':'Variational Omni 공식 API','항목':'24H Volume / OI','상태':'연결 성공' if va else '실패','캐시':'5분'},
      {'소스':'Pacifica / CCXT','항목':'현재 OI','상태':'연결 성공' if pacifica_oi else '실패/응답 확인 필요','캐시':'15분'},
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
