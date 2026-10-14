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
import plotly.io as pio
import plotly.graph_objects as go
import requests
import streamlit as st

st.set_page_config(page_title='빈집털이', page_icon='🤔', layout='wide', initial_sidebar_state='collapsed')

# Default Plotly palette for graphs without a chart-specific color scale.
# Explicitly pass theme=None to Streamlit charts so its default cool palette
# cannot override this warm template.
pio.templates["autumn_radar"] = go.layout.Template(
    layout=go.Layout(
        font=dict(family="Arial, Noto Sans KR, sans-serif", color="#5E402B", size=13),
        colorway=["#AC592C", "#CD8540", "#D6A85B", "#7C6244", "#BA6D48"],
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(255,248,233,0.58)",
        xaxis=dict(gridcolor="#E2CBAA", zerolinecolor="#D6B88A", linecolor="#BA9870"),
        yaxis=dict(gridcolor="#E2CBAA", zerolinecolor="#D6B88A", linecolor="#BA9870"),
    )
)
pio.templates.default = "autumn_radar"


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

@st.cache_data(ttl=300, show_spinner=False)
def fetch_entropy_hip3():
    """Entropy's isolated Hyperliquid HIP-3 'io' dex, not global Hyperliquid."""
    response=requests.post(
        'https://api.hyperliquid.xyz/info',
        json={'type':'metaAndAssetCtxs','dex':'io'},
        headers=HEADERS,timeout=15)
    response.raise_for_status()
    data=response.json()
    if not isinstance(data,list) or len(data)!=2:
        raise ValueError('Unexpected HIP-3 market metadata format')
    metadata,contexts=data
    markets=metadata.get('universe') if isinstance(metadata,dict) else None
    if not isinstance(markets,list) or not isinstance(contexts,list) or len(markets)!=len(contexts):
        raise ValueError('HIP-3 market list and asset contexts disagree')
    volume=[]
    open_interest=[]
    for item,market in zip(contexts,markets):
        if not isinstance(item,dict) or not isinstance(market,dict):
            continue
        daily=number(item.get('dayNtlVlm'))
        price=number(item.get('markPx'))
        size=number(item.get('openInterest'))
        if daily is not None and daily>=0:volume.append(daily)
        if price is not None and size is not None and price>=0 and size>=0:
            open_interest.append(price*size)
    if not volume and not open_interest:
        raise ValueError('No usable Entropy HIP-3 volume or OI')
    return {
        'volume_24h_usd':sum(volume) if volume else None,
        'oi_usd':sum(open_interest) if open_interest else None,
        'volume_source':'Hyperliquid public HIP-3 io dex metaAndAssetCtxs, dayNtlVlm USD',
        'oi_source':'Hyperliquid public HIP-3 io dex, OI contracts × markPx USD',
        'market_data_time':datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    }

@st.cache_data(ttl=300, show_spinner=False)
def fetch_robinhood_lighter():
    """Robinhood Chain Lighter ONLY, never general Lighter mainnet.

    Public order-book details report per-market OI as contracts. Require a
    clearly identified mark price and market size metadata before USD conversion.
    """
    url='https://api.rh.lighter.xyz/api/v1/exchangeStats'
    stats=api_get(url)
    if not isinstance(stats,dict):raise ValueError('Lighter RH exchangeStats invalid')
    payload=stats.get('data',stats)
    if not isinstance(payload,dict):raise ValueError('Lighter RH metrics payload invalid')
    volume=number(payload.get('daily_usd_volume'))
    if volume is None:
        quotes=payload.get('order_book_stats')
        if isinstance(quotes,list):
            parts=[number(x.get('daily_quote_token_volume')) for x in quotes if isinstance(x,dict)]
            parts=[p for p in parts if p is not None and p>=0]
            if parts:volume=sum(parts)
    # Quote volume is always 24-hour US dollar notional, not number of trades.
    if volume is None or volume<0:raise ValueError('No explicit Lighter RH 24h USD volume')
    return {'volume_24h_usd':volume,
            'volume_source':'Robinhood Chain-only Lighter official exchangeStats',
            'market_data_time':datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}

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


# Hide Streamlit's built-in app toolbar while keeping the custom
# theme and data-connection controls unchanged.
st.markdown("""
<style>
[data-testid="stToolbar"],
[data-testid="stMainMenu"],
[data-testid="stStatusWidget"],
[data-testid="stDecoration"],
[data-testid="stAppDeployButton"]{
 display:none!important;
}
</style>
""", unsafe_allow_html=True)

st.markdown("""<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=Noto+Sans+KR:wght@400;500;600;700&display=swap');
html,body,[data-testid="stApp"],[class*="css"]{font-family:'DM Sans','Noto Sans KR',sans-serif}
[data-testid="stAppViewContainer"]{background:linear-gradient(160deg,#f8efdf 0%,#fbf5e9 38%,#f7edd8 100%);color:#573d2d}
[data-testid="stSidebar"]{background:#f7e9d1;border-right:1px solid #dfc39d}
/* The only visible settings toggle lives directly under document.body.
   The native Streamlit popover is retained solely as a working settings trigger. */
[data-testid="stSidebarCollapsedControl"],
[data-testid="collapsedControl"],
[data-testid="stSidebarCollapseButton"],
[data-testid="stSidebarNavCollapseButton"]{display:none!important}
[data-testid="stHeader"]{background:transparent!important}
/* Keep the actual trigger mounted and clickable via script, but invisible. */
[data-testid="stPopover"]{
  width:1px!important;min-width:1px!important;height:1px!important;
  padding:0!important;margin:0!important;opacity:0!important;
  overflow:hidden!important;pointer-events:none!important;
}
[data-testid="stPopover"] button{
  width:1px!important;height:1px!important;min-height:1px!important;
  min-width:1px!important;padding:0!important;margin:0!important;
}
#radar-edge-toggle{
  position:fixed!important;top:8px!important;left:0!important;right:auto!important;
  display:flex!important;align-items:center!important;justify-content:center!important;
  width:38px!important;height:40px!important;padding:0!important;margin:0!important;
  border:1px solid #d2ac7e!important;border-left:0!important;
  border-radius:0 10px 10px 0!important;background:#fff6e7!important;
  box-shadow:0 3px 12px rgba(70,39,17,.16)!important;color:#754622!important;
  font:700 24px/1 Arial,sans-serif!important;cursor:pointer!important;
  transform:none!important;z-index:2147483640!important;box-sizing:border-box!important;
}
#radar-edge-toggle:hover{background:#f2dbb5!important}
#radar-edge-toggle:focus-visible{outline:2px solid #ba743c!important;outline-offset:2px!important}
.block-container{max-width:1500px;padding-top:1.8rem;padding-bottom:4rem}
h1,h2,h3{color:#573d2d!important;letter-spacing:-.025em;font-weight:700!important}
h1{font-size:2.35rem!important}
p,li,[data-testid="stCaptionContainer"]{color:#785a41}
[data-testid="stMetric"]{background:#fff8eb;border:1px solid #e4cba5;border-radius:16px;padding:18px 20px;box-shadow:0 5px 24px #a77b4c13}
[data-testid="stMetricLabel"]{color:#8e6a48}
[data-testid="stMetricValue"]{color:#63462d}
[data-testid="stDataFrame"]{border:1px solid #dec6a4;border-radius:13px;overflow:hidden}
.stTabs [data-baseweb="tab-list"]{gap:12px;border-bottom:1px solid #dec6a4}
.stTabs [data-baseweb="tab"]{color:#785a41;border-radius:10px}
.stTabs [aria-selected="true"]{color:#ac612d!important}
[data-testid="stSidebar"] label,[data-testid="stSidebar"] p{color:#745435}
.block-container{padding-top:1.25rem!important;padding-bottom:2rem!important}
[data-testid="stHeader"]{height:2.25rem;background:transparent}
.radar-banner{background:transparent!important;padding:0!important;border:0!important;border-radius:0!important;box-shadow:none!important;margin:10px 0 22px!important}
.radar-overline{font-weight:700;letter-spacing:.17em;color:#9a632f;font-size:11px}
.radar-banner h1{color:#563b27!important;font-size:2.5rem!important;font-weight:800!important;line-height:1.15!important;letter-spacing:-.035em!important;margin:0!important}
.radar-description{font-size:12px;color:#826244;margin-top:3px}
.radar-chip{display:none}
.radar-hint{padding:14px 18px;background:#f9ead1;border-left:3px solid #cc9e63;border-radius:9px;color:#735036;font-size:13px}
.radar-hint b{color:#67442b}
[data-testid="stSidebar"]{min-width:230px!important;max-width:230px!important;width:230px!important}
[data-testid="stSidebar"] [data-testid="stVerticalBlock"]{gap:.35rem}
[data-testid="stSidebar"] h2{font-size:1.08rem!important}
[data-testid="stSidebar"] [data-testid="stFileUploaderDropzone"]{padding:8px!important}

/* Large cutout cat face immediately to the right of the dashboard title. */
.radar-banner{
    display:flex!important;align-items:center!important;justify-content:flex-start!important;
    gap:22px!important;flex-wrap:nowrap!important;min-height:158px!important;
    padding:0!important;border:0!important;background:transparent!important;
}
.radar-banner h1{flex:0 0 auto!important;white-space:nowrap!important}
.radar-cat-head{
    display:block;width:clamp(145px,16vw,195px)!important;height:auto!important;
    max-height:178px!important;object-fit:contain!important;
    flex-shrink:0!important;pointer-events:none;
}
@media(max-width:650px){
    .radar-banner{gap:12px!important;min-height:110px!important}
    .radar-banner h1{font-size:2.45rem!important}
    .radar-cat-head{width:120px!important;max-height:116px!important}
}
/* Harvest palette inspired by the supplied autumn pumpkin photo. */
:root{--harvest-ink:#493323;--harvest-rust:#ae5727;--harvest-pumpkin:#cb782d;--harvest-straw:#f2dfb5;--harvest-cream:#fff9ee;}
[data-testid="stAppViewContainer"]{
  background:linear-gradient(90deg,rgba(255,250,239,.93),rgba(252,244,225,.88))!important;
  color:var(--harvest-ink)!important;
}
[data-testid="stAppViewContainer"] .block-container{
  background:rgba(255,249,238,.89)!important;
  border:1px solid rgba(144,101,62,.16)!important;
  border-radius:19px!important;
  box-shadow:0 10px 34px rgba(78,46,17,.08)!important;
  padding:28px 33px 48px!important;
}
h1,h2,h3,.radar-banner h1{color:#57361f!important}
.radar-banner h1{font-size:3.15rem!important;font-weight:850!important}
p,li,[data-testid="stCaptionContainer"]{color:#695545!important}
[data-testid="stSidebar"]{background:#f9eed7!important;border-right:1px solid #dbbf95!important}
[data-testid="stTabs"] [role="tablist"]{border-color:#e1caa9!important}
[data-testid="stTabs"] button{color:#765637!important}
[data-testid="stTabs"] button[aria-selected="true"]{color:#ae5727!important;border-bottom-color:#bd692e!important}
.radar-table-wrap,.benchmark-wrap{background:rgba(255,253,247,.96)!important;border:1px solid #e0cba8!important}
.radar-rank-table{color:#503927!important}
.radar-rank-table thead th{background:#edddbf!important;color:#634a30!important}
.radar-rank-table tbody td{border-top:1px solid #ecdec8!important;color:#503927!important}
.radar-rank-table tbody tr:hover{background:#fff0d7!important}
.radar-rank-table .rank-name{color:#5b3920!important}
.radar-bar-track{background:#eddbc0!important}
.radar-bar-fill{background:linear-gradient(90deg,#a84e27,#d78930)!important}
.radar-volume-cell span{color:#6b3c24!important}
[data-testid="stDataFrame"],[data-testid="stMetric"],[data-testid="stExpander"]{
  border-color:#e2cfaf!important;
}
[data-testid="stMetric"]{background:#fff6e5e8!important}
[data-testid="stMetricLabel"],[data-testid="stMetricValue"]{color:#63442d!important}
[data-testid="stPopover"] button{color:#704826!important}

/* Unified autumn UI for all Streamlit control surfaces and tables. */
[data-testid="stApp"]{color:#4c3323!important}
[data-testid="stTabs"] [role="tab"]{color:#65482e!important;font-weight:650!important;font-size:15px!important}
[data-testid="stTabs"] [role="tab"][aria-selected="true"]{color:#a45729!important;border-bottom:3px solid #c77737!important}
[data-testid="stWidgetLabel"] p,[data-testid="stSelectbox"] label p,[data-testid="stTextInput"] label p{color:#634329!important}
div[data-baseweb="select"]>div,div[data-baseweb="input"]>div,input,textarea{
  background:#fff8ec!important;border-color:#ddc39d!important;color:#503723!important;
}
div[data-baseweb="popover"],[data-baseweb="menu"],[role="listbox"],[role="option"]{
  background:#fff7e9!important;color:#513923!important;
}
div[data-testid="stAlert"]{background:#fff1d8!important;color:#68482e!important;border:1px solid #e6c899!important;border-radius:10px!important}
div[data-testid="stAlert"] p,div[data-testid="stAlert"] div{color:#67462a!important}
[data-testid="stExpander"] details,[data-testid="stExpander"] summary{background:#fff6e6!important;color:#563c27!important}
[data-testid="stDataFrame"]{background:#fffbf3!important;border-color:#dec59e!important}
[data-testid="stPlotlyChart"]{background:rgba(255,250,241,.78)!important;border-radius:12px!important}
[data-testid="stButton"] button,[data-testid="stDownloadButton"] button{background:#f2dfbc!important;color:#684227!important;border:1px solid #dabb88!important}
.radar-hint{background:#fff0d5!important;border-left-color:#bd713b!important;color:#704926!important}
.radar-hint b{color:#663b22!important}
.radar-table-wrap{width:100%;overflow-x:auto;border:1px solid #dfc6a1!important;border-radius:14px!important;background:#fffbf2!important}
.radar-rank-table{width:100%;border-collapse:collapse;min-width:920px;font-size:16px;color:#543922!important}
.radar-rank-table thead th{background:#efddb9!important;color:#644329!important;text-align:left;font-size:14px;font-weight:750;padding:16px 14px;white-space:nowrap;border-bottom:1px solid #d7b687!important}
.radar-rank-table tbody td{border-top:1px solid #e9d9bd!important;padding:18px 14px;font-variant-numeric:tabular-nums;white-space:nowrap;font-size:16px;color:#4f3827!important}
.radar-rank-table tbody tr:hover{background:#fff1d9!important}
.radar-rank-table .rank-no{width:50px;color:#957859!important;font-weight:650}
.radar-rank-table .rank-name{font-weight:750;color:#624128!important;min-width:155px}
.radar-rank-table th:nth-child(3),.radar-rank-table td:nth-child(3){min-width:230px;width:30%}
.radar-volume-cell{display:flex;gap:12px;align-items:center;justify-content:space-between}
.radar-bar-track{min-width:105px;flex:1;height:12px;background:#edd9b9!important;border-radius:8px;overflow:hidden}
.radar-bar-fill{height:100%;background:linear-gradient(90deg,#a54f24,#df8d39)!important;border-radius:8px}
.radar-volume-cell span{min-width:80px;text-align:right;font-weight:750;font-size:15px;color:#674028!important}
.radar-missing{color:#987f6a}
.exchange-cell{display:inline-flex;align-items:center;gap:10px;vertical-align:middle}
.exchange-logo,.exchange-monogram{width:32px;height:32px;min-width:32px;border-radius:50%;object-fit:cover;background:#fff7e8}
.exchange-monogram{display:inline-flex;align-items:center;justify-content:center;background:#f2dfbf;color:#80512c;font-size:14px;font-weight:700}
.benchmark-wrap{margin:5px 0 18px}
.benchmark-wrap .radar-rank-table tbody td{padding:12px 14px}
.benchmark-wrap .radar-rank-table th:nth-child(2),.benchmark-wrap .radar-rank-table td:nth-child(2){min-width:230px;width:30%}

/* Finish the native Streamlit/third-party components left in blue. */
[data-testid="stApp"] {
  --harvest-accent:#A95728;--harvest-paper:#FFF8EA;
}
[data-testid="stApp"] a,[data-testid="stApp"] a:visited{color:#99502D}
[data-testid="stApp"] a:hover{color:#703A23}
[data-testid="stTabs"] [role="tab"]:hover{background:#F5E3C9!important;color:#793F25!important}
[data-testid="stTabs"] [role="tab"][aria-selected="true"]{
  color:#9E5028!important;
  background:rgba(255,245,227,.9)!important;
  border-bottom-color:#C47531!important;
}
div[data-testid="stAlert"],div[data-testid="stAlert"]>div,
[data-testid="stNotificationContentInfo"],[data-testid="stNotificationContentWarning"]{
  background:#FFF1D9!important;
  border-color:#DFC096!important;
  color:#66452D!important;
}
div[data-testid="stAlert"] svg,[data-testid="stAlert"] [data-testid="stIconMaterial"]{
  color:#A75B2F!important;fill:#A75B2F!important;
}
[data-testid="stDataFrame"] [role="columnheader"],
[data-testid="stDataFrame"] .ag-header,
[data-testid="stDataFrame"] .ag-header-cell,
[data-testid="stDataFrame"] th{
  background:#F1DEBE!important;color:#5F412B!important;
}
[data-testid="stDataFrame"] [role="gridcell"],
[data-testid="stDataFrame"] .ag-row,
[data-testid="stDataFrame"] td{
  background:#FFFBF2!important;color:#503826!important;
  border-color:#E9D6B5!important;
}
[data-testid="stDataFrame"] [role="row"]:hover [role="gridcell"]{
  background:#FBEBCD!important;
}
[data-testid="stDataFrame"] [role="columnheader"] *,
[data-testid="stDataFrame"] [role="gridcell"] *{
  color:#563A28!important;
}
[data-testid="stApp"] input:focus-visible,
[data-testid="stApp"] textarea:focus-visible,
[data-testid="stApp"] button:focus-visible{
  outline:2px solid #B86D35!important;outline-offset:2px!important;
}
[data-testid="stApp"] [role="progressbar"]{
  accent-color:#B96831!important;
}

/* Remove residual blue backgrounds, focus rings and select menus from Streamlit. */
:root{accent-color:#b76931}
[data-testid="stApp"] input,[data-testid="stApp"] textarea,
[data-testid="stApp"] [data-baseweb="input"]>div,
[data-testid="stApp"] [data-baseweb="select"]>div,
[data-testid="stApp"] [data-testid="stFileUploaderDropzone"],
[data-testid="stApp"] [data-testid="stNumberInput"]>div{
  background:#fff8e9!important;background-color:#fff8e9!important;
  border-color:#d5b486!important;color:#533723!important;box-shadow:none!important}
[data-testid="stApp"] input::placeholder{color:#93745a!important;opacity:1!important}
[data-testid="stApp"] input:focus,[data-testid="stApp"] textarea:focus,
[data-testid="stApp"] [data-baseweb="input"]:focus-within,
[data-testid="stApp"] [data-baseweb="select"]:focus-within{
 outline-color:#b76931!important;border-color:#b76931!important;
 box-shadow:0 0 0 1px #b76931!important}
[data-testid="stApp"] [data-testid="stTextInput"] svg,
[data-testid="stApp"] [data-testid="stSelectbox"] svg{fill:#815330!important;color:#815330!important}
[data-baseweb="popover"] [role="listbox"],[data-baseweb="popover"] [role="option"]{
 background:#fff8e9!important;color:#5c3e29!important}
[data-baseweb="popover"] [role="option"]:hover,
[data-baseweb="popover"] [role="option"][aria-selected="true"]{
 background:#eddbbd!important;color:#5c3e29!important}
[data-testid="stApp"] [data-testid="stSlider"] [role="slider"],
[data-testid="stApp"] [role="progressbar"]{accent-color:#b76931!important}
[data-testid="stTabs"] [role="tablist"]{border-bottom-color:#d6b589!important}
[data-testid="stTabs"] [role="tab"]{font-size:15px!important;color:#69442b!important;font-weight:700!important}
[data-testid="stTabs"] [role="tab"][aria-selected="true"]{
 color:#a25129!important;border-bottom:3px solid #b76d36!important}
[data-testid="stButton"] button,[data-testid="stDownloadButton"] button,
[data-testid="stFileUploaderDropzone"] button{
 background:#f0ddbd!important;border-color:#d0a876!important;color:#684229!important}
[data-testid="stDataFrame"]{background:#fff9ef!important;color:#573b28!important}
[data-testid="stApp"] [data-testid="stPlotlyChart"]{
 background:rgba(255,248,236,.55)!important}

/* Tabs: no traveling indicator, no overlapping borders, no reflow animation.
   Streamlit/BaseWeb draws its own moving tab-highlight separate from tab buttons. */
[data-testid="stTabs"] [data-baseweb="tab-highlight"],
[data-testid="stTabs"] [data-baseweb="tab-border"],
.stTabs [data-baseweb="tab-highlight"],
.stTabs [data-baseweb="tab-border"]{
 display:none!important;visibility:hidden!important;opacity:0!important;
 width:0!important;height:0!important;transition:none!important;animation:none!important;
}
[data-testid="stTabs"] [data-baseweb="tab-list"],
.stTabs [data-baseweb="tab-list"]{
 display:flex!important;gap:18px!important;
 border:0!important;border-bottom:1px solid #d9bc95!important;
 background:transparent!important;overflow:visible!important;
}
[data-testid="stTabs"] [data-baseweb="tab"],
[data-testid="stTabs"] [role="tab"]{
 position:relative!important;box-sizing:border-box!important;
 padding:12px 5px 13px!important;margin:0!important;
 border:0!important;border-bottom:0!important;border-radius:0!important;
 background:transparent!important;background-color:transparent!important;
 color:#68442e!important;font-weight:700!important;font-size:15px!important;
 box-shadow:none!important;outline:0!important;
 transition:none!important;animation:none!important;transform:none!important;
 will-change:auto!important;white-space:nowrap!important;
}
[data-testid="stTabs"] [role="tab"]::after{
 content:""!important;display:block!important;position:absolute!important;
 left:0!important;right:0!important;bottom:-1px!important;height:3px!important;
 width:auto!important;background:transparent!important;
 border:0!important;opacity:0!important;pointer-events:none!important;
 transition:none!important;animation:none!important;transform:none!important;
}
[data-testid="stTabs"] [role="tab"][aria-selected="true"]{
 color:#a2592d!important;background:transparent!important;
 border:0!important;box-shadow:none!important;
}
[data-testid="stTabs"] [role="tab"][aria-selected="true"]::after{
 background:#b76c34!important;opacity:1!important;
}
[data-testid="stTabs"] [role="tab"]:hover,
[data-testid="stTabs"] [role="tab"]:focus-visible{
 color:#9d552e!important;background:transparent!important;
 box-shadow:none!important;border:0!important;
}
[data-testid="stTabs"] [role="tab"] *,
[data-testid="stTabs"] [role="tab"]::before{
 transition:none!important;animation:none!important;
}

/* Consistent typography across Streamlit, custom ranking tables and plots. */
:root{
 --radar-font:'Noto Sans KR','DM Sans',sans-serif;
 --radar-body:15px;
 --radar-small:13px;
 --radar-section:22px;
 --radar-project:15px;
}
html,body,[data-testid="stApp"],[data-testid="stApp"] *,
[data-testid="stPopover"] *,[data-baseweb="popover"] *,
[data-testid="stDataFrame"] *,[data-testid="stTabs"] *,
.radar-rank-table,.radar-rank-table *{
 font-family:var(--radar-font)!important;
}
[data-testid="stApp"] p,[data-testid="stApp"] li,
[data-testid="stApp"] label,[data-testid="stApp"] input,
[data-testid="stApp"] button,[data-testid="stApp"] textarea,
[data-testid="stApp"] [data-baseweb="select"],
[data-testid="stApp"] [data-testid="stWidgetLabel"]{
 font-size:var(--radar-body)!important;
 line-height:1.5!important;
}
[data-testid="stApp"] h2,[data-testid="stApp"] h3{
 font-family:var(--radar-font)!important;
 font-size:var(--radar-section)!important;
 line-height:1.35!important;
 font-weight:750!important;
}
[data-testid="stApp"] h4{
 font-family:var(--radar-font)!important;
 font-size:18px!important;line-height:1.4!important;font-weight:700!important;
}
[data-testid="stApp"] [data-testid="stCaptionContainer"],
[data-testid="stApp"] [data-testid="stCaptionContainer"] p{
 font-size:var(--radar-small)!important;line-height:1.55!important;
}
.radar-banner h1{font-family:var(--radar-font)!important;font-size:50px!important;line-height:1.15!important}
.radar-rank-table thead th{font-size:14px!important;font-weight:700!important}
.radar-rank-table tbody td{font-size:var(--radar-body)!important;line-height:1.4!important}
.radar-rank-table .rank-name,
.radar-rank-table .rank-name .exchange-cell,
.radar-rank-table .rank-name .exchange-cell > span:last-child,
.exchange-cell > span:last-child{
 font-family:var(--radar-font)!important;font-size:var(--radar-project)!important;
 line-height:1.4!important;font-weight:700!important;letter-spacing:0!important;
}
.radar-volume-cell span{font-size:var(--radar-body)!important}
[data-testid="stTabs"] [role="tab"]{
 font-family:var(--radar-font)!important;font-size:var(--radar-body)!important;
 font-weight:700!important;
}
[data-testid="stMetricLabel"] p{font-size:var(--radar-small)!important}
[data-testid="stMetricValue"]{font-size:28px!important;font-weight:700!important}
@media(max-width:650px){
 .radar-banner h1{font-size:38px!important}
 [data-testid="stApp"] h2,[data-testid="stApp"] h3{font-size:20px!important}
}

/* Market-activity proxy: compact two-line congestion cell. */
.radar-table-wrap:not(.benchmark-wrap) .radar-rank-table th:last-child,
.radar-table-wrap:not(.benchmark-wrap) .radar-rank-table td:last-child{
 min-width:135px;text-align:center;vertical-align:middle}
.congestion-cell{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:4px}
.congestion-cell strong{font-size:15px!important;font-weight:700!important;color:#6b432a}
.pumpkin-meter{font-size:19px!important;letter-spacing:1px;white-space:nowrap;line-height:1.2}
.pumpkin-empty{color:#cdb58b;font-size:15px}

/* The 9-column research matrix scrolls horizontally on small screens. */
.fundraising-ranking .radar-rank-table{min-width:1390px!important}
.fundraising-ranking .funding-col{min-width:145px;font-weight:700!important}
.fundraising-ranking .investor-col{min-width:170px;max-width:240px;white-space:normal!important}
.fundraising-ranking .tier-col{text-align:center;min-width:110px}
.fundraising-ranking th:nth-child(7),.fundraising-ranking th:nth-child(8),
.fundraising-ranking th:nth-child(9){white-space:nowrap}
.fundraising-ranking .funding-col small{font-size:11px!important;color:#927654}
.funding-source{color:#965222!important;text-decoration:underline;text-underline-offset:3px}
.farming-tier{display:inline-flex;align-items:center;justify-content:center;
 min-width:34px;border-radius:6px;padding:4px 9px;
 font-size:15px!important;font-weight:800!important}
.tier-s{background:#ecd2a1;color:#6d3b16}.tier-a{background:#eddfbd;color:#825021}
.tier-b{background:#f0e8d3;color:#6b563a}.tier-c{background:#f0eee6;color:#706d62}
.tier-d{background:#eee8e5;color:#7a635c}.tier-unknown{color:#947b69}

/* Stable accordion layout: prevent label/icon collisions and stray text bleed. */
[data-testid="stExpander"]{position:relative!important;isolation:isolate!important;
 overflow:hidden!important;border:1px solid #d8b98c!important;
 border-radius:10px!important;background:#fff8eb!important}
[data-testid="stExpander"] details{width:100%!important;max-width:100%!important}
[data-testid="stExpander"] summary,
[data-testid="stExpander"] [data-testid="stExpanderToggleIcon"]{
 box-sizing:border-box!important}
[data-testid="stExpander"] summary{
 position:relative!important;display:flex!important;align-items:center!important;
 justify-content:flex-start!important;gap:12px!important;
 min-height:49px!important;height:auto!important;padding:11px 17px!important;
 white-space:normal!important;overflow:hidden!important;
 background:#fff7e8!important;color:#65442d!important
}
[data-testid="stExpander"] summary p,
[data-testid="stExpander"] summary span:not([data-testid="stIconMaterial"]){
 position:relative!important;display:inline-block!important;max-width:100%!important;
 margin:0!important;white-space:normal!important;line-height:1.5!important;
 font:600 15px/1.5 'Noto Sans KR','DM Sans',sans-serif!important;
 letter-spacing:0!important;color:#65442d!important;overflow-wrap:anywhere!important
}
[data-testid="stExpander"] summary svg,
[data-testid="stExpander"] [data-testid="stExpanderToggleIcon"]{
 position:static!important;transform:none;
 flex:0 0 16px!important;width:16px!important;height:16px!important;
 margin:0!important;color:#9c643d!important
}
[data-testid="stExpander"] [data-testid="stExpanderDetails"]{
 padding:12px 18px!important;overflow-wrap:anywhere!important
}
[data-testid="stExpander"] summary:hover{background:#f4e3c8!important}

/* Reliable bar-chart layout with a dedicated non-overlapping venue column. */
.venue-rank-chart{background:rgba(255,249,237,.72);border-radius:12px;padding:16px 15px;overflow:hidden}
.venue-rank-row{display:grid;grid-template-columns:152px minmax(0,1fr);gap:12px;align-items:center;min-height:47px}
.venue-rank-name{display:flex;justify-content:flex-end;align-items:center;min-width:0;white-space:nowrap}
.venue-rank-name .exchange-cell{font-size:15px!important;font-weight:700!important;gap:8px}
.venue-rank-name .exchange-logo,.venue-rank-name .exchange-monogram{width:29px;height:29px;min-width:29px}
.venue-rank-measure{display:flex;position:relative;align-items:center;height:33px;min-width:0;
 background:repeating-linear-gradient(to right,transparent 0,transparent calc(25% - 1px),#ddc4a2 calc(25% - 1px),#ddc4a2 25%)}
.venue-bar{height:30px;min-width:2px;border-radius:1px}
.venue-rank-value{font-size:13px!important;color:#6d4b33!important;white-space:nowrap;margin-left:5px}
.venue-rank-foot{display:grid;grid-template-columns:152px minmax(0,1fr);gap:12px;margin-top:7px}
.venue-rank-ticks{display:flex;justify-content:space-between;gap:0;color:#6e503a;font-size:12px!important}
.venue-rank-ticks span{white-space:nowrap}
.venue-rank-axis{text-align:center;margin-top:4px;color:#6e503a;font-size:13px!important}
@media(max-width:740px){.venue-rank-row,.venue-rank-foot{grid-template-columns:125px minmax(0,1fr);gap:7px}
.venue-rank-chart{padding:11px 8px}.venue-rank-name .exchange-cell{font-size:13px!important}}
/* Material icon ligatures must not inherit the global Noto font. */
[data-testid="stExpander"] summary [data-testid="stIconMaterial"],
[data-testid="stExpander"] summary span[class*="material"],
[data-testid="stExpander"] summary [data-testid="stExpanderToggleIcon"] span{
 font-family:'Material Symbols Rounded','Material Symbols Outlined','Material Icons'!important;
 font-size:22px!important;font-weight:normal!important;letter-spacing:normal!important;
 line-height:1!important;white-space:nowrap!important;display:inline-flex!important;
 font-feature-settings:'liga' 1!important;
}
[data-testid="stExpander"] summary [data-testid="stExpanderToggleIcon"]{
 flex:0 0 24px!important;min-width:24px!important;width:24px!important;
 display:inline-flex!important;align-items:center!important;justify-content:center!important}

/* Replace the faulty arrow_right ligature with a real CSS disclosure triangle. */
[data-testid="stExpander"] summary [data-testid="stExpanderToggleIcon"],
[data-testid="stExpander"] summary [data-testid="stIconMaterial"],
[data-testid="stExpander"] summary span[class*="material"]{
 display:none!important;visibility:hidden!important;width:0!important;
 min-width:0!important;height:0!important;overflow:hidden!important;
}
[data-testid="stExpander"] summary{position:relative!important;padding-left:43px!important}
[data-testid="stExpander"] summary::before{
 content:"▸"!important;position:absolute!important;display:block!important;
 top:50%!important;left:17px!important;transform:translateY(-50%)!important;
 color:#90603b!important;font:700 16px/1 Arial,sans-serif!important;
 width:14px!important;height:16px!important;line-height:16px!important;
}
[data-testid="stExpander"] details[open] summary::before{content:"▾"!important}

/* Hide Streamlit's decorative heading permalink icons. */
[data-testid="stHeaderActionElements"],
[data-testid="stMarkdownContainer"] a.header-anchor,
[data-testid="stMarkdownContainer"] .header-anchor,
[data-testid="stMarkdownContainer"] a[href^="#"]:has(svg),
[data-testid="stHeadingWithActionElements"] [data-testid="stHeaderActionElements"]{
 display:none!important;visibility:hidden!important;
}
</style>""",unsafe_allow_html=True)
# Keep the exact uploaded 1200×1600 JPEG in the repository (no lossy re-encoding).
# Scale to the viewport height rather than covering its width, which greatly
# enlarged the vertical photo on wide screens.
_pumpkin_asset = DATA_DIR / 'assets' / 'pumpkin_original.jpg'
if _pumpkin_asset.exists():
    _pumpkin_b64 = base64.b64encode(_pumpkin_asset.read_bytes()).decode('ascii')
    st.markdown(
        '<style>'
        '[data-testid="stAppViewContainer"]{'
        'background-color:#c49d6d!important;'
        '--radar-pumpkin-image:url("data:image/jpeg;base64,'+_pumpkin_b64+'")!important;'
        'background-image:linear-gradient(rgba(69,40,19,.20),rgba(74,44,24,.23)),'
        'url("data:image/jpeg;base64,'+_pumpkin_b64+'")!important;'
        'background-position:center top!important;'
        'background-size:auto min(100vh, 1600px)!important;'
        'background-attachment:scroll!important;'
        'background-repeat:repeat-x!important;}'
        '[data-testid="stAppViewContainer"] .block-container{'
        'background:rgba(255,248,234,.88)!important;'
        'backdrop-filter:none!important;}'
        '</style>',unsafe_allow_html=True
    )

# Embedded photo-derived cursor: original transparent paw, scaled to browser-friendly dimensions.
_cursor_file = DATA_DIR / 'assets' / 'cat_paw_cursor_hq.b64'
_cursor_backup = DATA_DIR / 'assets' / 'cat_paw_cursor.b64'
_paw_base64 = ''
for _candidate in (_cursor_file, _cursor_backup):
    if not _candidate.is_file():
        continue
    try:
        _candidate_bytes = base64.b64decode(
            _candidate.read_text(encoding='utf-8').strip(),validate=True)
        with Image.open(BytesIO(_candidate_bytes)) as _cursor_img:
            _cursor_img.load()
            if _cursor_img.format != 'PNG':
                raise ValueError('Cursor is not a PNG')
            if _cursor_img.width < 12 or _cursor_img.height < 12:
                raise ValueError('Cursor image too small')
        if _candidate == _cursor_backup:
            with Image.open(BytesIO(_candidate_bytes)) as _small_paw:
                _paw_upscaled = _small_paw.convert('RGBA').resize(
                    (48,51),Image.Resampling.LANCZOS)
                _paw_output = BytesIO()
                _paw_upscaled.save(_paw_output,format='PNG')
                _candidate_bytes = _paw_output.getvalue()
        _paw_base64 = base64.b64encode(_candidate_bytes).decode('ascii')
        break
    except Exception:
        continue
if _paw_base64:
    st.markdown(
        '<style>'
        'html,body,[data-testid="stApp"],[data-testid="stApp"] *{'
        'cursor:url("data:image/png;base64,' + _paw_base64 + '") 15 42,auto!important;}'
        'input[type="text"],input[type="password"],input[type="number"],textarea,'
        '[contenteditable="true"]{cursor:text!important;}'
        '</style>',unsafe_allow_html=True
    )

_cat_asset = DATA_DIR / 'assets' / 'cat_head.b64'
_cat_head_b64 = _cat_asset.read_text(encoding='utf-8').strip() if _cat_asset.exists() else ''
_dark_cat_asset=DATA_DIR/'assets'/'dark_cat_head.b64'
_dark_cat_b64=_dark_cat_asset.read_text(encoding='utf-8').strip() if _dark_cat_asset.exists() else ''
# Render both images; CSS swaps visibility on the root dark-mode class.
# Unlike updating img.src from JavaScript, this is resilient to Streamlit
# re-rendering the banner and to cached image node replacements.
_cat_head_html = (
    f'<img class="radar-cat-head radar-cat-light" src="data:image/webp;base64,{_cat_head_b64}" alt="고양이 얼굴">'
    if _cat_head_b64 else ''
)
if _dark_cat_b64:
    _cat_head_html += (
        f'<img class="radar-cat-head radar-cat-dark" src="data:image/webp;base64,{_dark_cat_b64}" alt="잭오랜턴 고양이">'
    )
st.markdown(
    '<div class="radar-banner"><h1>빈집털이</h1><span class="radar-cat-stack">'
    + _cat_head_html + '</span></div>',
    unsafe_allow_html=True
)
_theme_widget=r"""
<style id="radar-theme-css">
.radar-cat-stack{display:inline-grid;grid-template-areas:"cat";align-items:center;justify-items:center;flex-shrink:0}
.radar-cat-stack .radar-cat-head{grid-area:cat}
.radar-cat-stack .radar-cat-dark{display:none!important}
html.radar-dark .radar-cat-stack .radar-cat-light{display:none!important}
html.radar-dark .radar-cat-stack .radar-cat-dark{display:block!important}
#radar-dark-toggle{position:fixed!important;top:18px!important;right:max(24px,calc((100vw - 1450px)/2 + 30px))!important;z-index:2147483645!important;cursor:pointer!important;border:1px solid #b9844f!important;border-radius:999px!important;padding:9px 16px!important;font:700 14px/1.4 'Noto Sans KR',sans-serif!important;background:#fff4e2!important;color:#583920!important;box-shadow:0 3px 15px rgba(36,19,7,.2)!important;}
html.radar-dark body,html.radar-dark [data-testid="stApp"]{background:#120f14!important;color:#f1e3d5!important}
html.radar-dark [data-testid="stAppViewContainer"]{background-color:#17121a!important;background-blend-mode:multiply,multiply!important}
html.radar-dark [data-testid="stAppViewContainer"] .block-container{background:rgba(23,19,27,.94)!important;backdrop-filter:none!important}
html.radar-dark #radar-dark-toggle{background:#372536!important;border-color:#ac6d42!important;color:#fff0df!important}
html.radar-dark .radar-banner h1,html.radar-dark [data-testid="stApp"] h1,html.radar-dark [data-testid="stApp"] h2,html.radar-dark [data-testid="stApp"] h3,html.radar-dark [data-testid="stApp"] h4,html.radar-dark [data-testid="stApp"] p,html.radar-dark [data-testid="stApp"] li,html.radar-dark [data-testid="stApp"] label,html.radar-dark [data-testid="stApp"] span{color:#e9d7c5!important}
html.radar-dark [data-testid="stCaptionContainer"] *{color:#bdad9d!important}
html.radar-dark .radar-table-wrap,html.radar-dark .venue-rank-chart,html.radar-dark .radar-rank-table,html.radar-dark .market-chart-panel,html.radar-dark [data-testid="stExpander"],html.radar-dark [data-testid="stExpander"] details,html.radar-dark [data-testid="stExpander"] summary,html.radar-dark [data-testid="stMetric"],html.radar-dark [data-testid="stDataFrame"]{background:#241d28!important;color:#f2e4d6!important;border-color:#594535!important}
html.radar-dark .radar-rank-table thead th,html.radar-dark .radar-rank-table th,html.radar-dark .fundraising-ranking thead th{background:#3c2a2a!important;color:#f3dfbf!important;border-color:#684e38!important}
html.radar-dark .radar-rank-table tbody td,html.radar-dark .radar-rank-table tbody tr,html.radar-dark .radar-rank-table .rank-name{background:#241d28!important;color:#f0dfce!important;border-color:#514038!important}
html.radar-dark .radar-rank-table tbody tr:hover td{background:#362733!important}
html.radar-dark .radar-bar-track,html.radar-dark .venue-rank-measure{background-color:#4e3a37!important}
html.radar-dark .venue-rank-value,html.radar-dark .venue-rank-axis,html.radar-dark .venue-rank-ticks,html.radar-dark .exchange-cell,html.radar-dark .congestion-cell strong,html.radar-dark .funding-source{color:#f0d7b7!important}
html.radar-dark input,html.radar-dark textarea,html.radar-dark [data-baseweb="select"] > div,html.radar-dark [data-testid="stTextInput"] > div > div{background:#30252c!important;color:#f5e7d5!important;border-color:#78563f!important}
html.radar-dark [data-testid="stTabs"] [role="tab"],html.radar-dark [data-testid="stTabs"] [role="tab"] *{color:#e7c7a2!important}
html.radar-dark [data-testid="stTabs"] [role="tab"][aria-selected="true"]{border-bottom-color:#f1a558!important}
html.radar-dark [data-testid="stExpander"] summary{background:#2b202c!important}
html.radar-dark [data-testid="stApp"] a{color:#ffbd7e!important}
/* Let Plotly own SVG geometry, backgrounds and axes.
   Previous !important SVG fill rules interfered with chart rendering. */
#radar-dark-toggle .radar-pumpkin-unlit{display:inline-block;filter:grayscale(1) brightness(.7);margin-right:5px;font-size:18px;vertical-align:-2px}
#radar-dark-toggle .radar-pumpkin-lit{display:inline-block;filter:drop-shadow(0 0 5px #ffb647);margin-right:5px;font-size:18px;vertical-align:-2px}

/* Dark v3: final component overrides; avoid native light-themed portals. */
html.radar-dark [data-testid="stAppViewContainer"],
html.radar-dark [data-testid="stMain"],
html.radar-dark [data-testid="stMainBlockContainer"]{background-color:#130f17!important;color:#f3e5d6!important}
html.radar-dark [data-testid="stApp"] .block-container{background:#17131c!important}
html.radar-dark [data-testid="stPopover"],
html.radar-dark [data-baseweb="popover"],
html.radar-dark [data-baseweb="popover"] > div,
html.radar-dark [data-baseweb="popover"] > div > div,
html.radar-dark [data-testid="stPopoverContent"],
html.radar-dark [role="dialog"],
html.radar-dark [role="dialog"] > div{
 background-color:#231c29!important;color:#f4e4d2!important;
 border-color:#70513c!important;box-shadow:0 14px 36px #0008!important}
html.radar-dark [data-baseweb="popover"] [data-testid="stVerticalBlock"],
html.radar-dark [data-baseweb="popover"] [data-testid="stElementContainer"],
html.radar-dark [data-baseweb="popover"] [data-testid="stFileUploader"],
html.radar-dark [data-baseweb="popover"] [data-testid="stFileUploaderDropzone"],
html.radar-dark [data-testid="stFileUploaderDropzone"] section,
html.radar-dark [data-testid="stFileUploader"] section{
 background:#2c2330!important;color:#f4e4d2!important;border-color:#76513b!important}
html.radar-dark [data-baseweb="popover"] h1,
html.radar-dark [data-baseweb="popover"] h2,
html.radar-dark [data-baseweb="popover"] h3,
html.radar-dark [data-baseweb="popover"] label,
html.radar-dark [data-baseweb="popover"] p,
html.radar-dark [data-baseweb="popover"] span,
html.radar-dark [data-testid="stFileUploader"] label,
html.radar-dark [data-testid="stFileUploader"] small{color:#f0ddca!important}
html.radar-dark [data-baseweb="select"],
html.radar-dark [data-baseweb="select"] > div,
html.radar-dark [data-baseweb="select"] > div > div,
html.radar-dark [data-baseweb="input"] > div,
html.radar-dark [data-testid="stApp"] input,
html.radar-dark [data-baseweb="menu"],
html.radar-dark [role="listbox"],
html.radar-dark [role="option"],
html.radar-dark [data-baseweb="popover"] [role="listbox"],
html.radar-dark [data-baseweb="popover"] [role="option"]{
 background-color:#302634!important;color:#f6e7d8!important;border-color:#765b49!important}
html.radar-dark [role="option"] *,
html.radar-dark [data-baseweb="select"] span{color:#f6e7d8!important}
html.radar-dark [role="option"]:hover,
html.radar-dark [role="option"][aria-selected="true"]{background:#493342!important}
html.radar-dark [data-testid="stApp"] button:not(#radar-dark-toggle):not(#radar-edge-toggle),
html.radar-dark [data-baseweb="popover"] button,
html.radar-dark [data-testid="stFileUploader"] button{
 background:#3b2937!important;border:1px solid #aa7750!important;
 color:#ffead5!important;opacity:1!important}
html.radar-dark [data-testid="stApp"] button:disabled,
html.radar-dark [data-baseweb="popover"] button:disabled{
 background:#302834!important;color:#b6a194!important;border-color:#615044!important;opacity:1!important}
html.radar-dark .radar-rank-table thead th,
html.radar-dark table thead th{background:#3b2b32!important;color:#f6dfc5!important;border-color:#6d5040!important}
html.radar-dark .radar-rank-table td,
html.radar-dark table tbody td{background:#211b27!important;color:#f4e6d8!important;border-color:#59453e!important}
html.radar-dark .radar-rank-table tbody tr:hover td{background:#332736!important}
html.radar-dark .farming-tier,
html.radar-dark .farming-tier span{font-weight:800!important}
html.radar-dark .farming-tier.tier-s{background:#875022!important;color:#fff0d6!important}
html.radar-dark .farming-tier.tier-a{background:#6c4929!important;color:#ffe6be!important}
html.radar-dark .farming-tier.tier-b{background:#514137!important;color:#f7e2cc!important}
html.radar-dark .farming-tier.tier-c,
html.radar-dark .farming-tier.tier-d{background:#3c3537!important;color:#e5cdbb!important}
html.radar-dark [data-testid="stDataFrame"],
html.radar-dark [data-testid="stDataFrame"]>div,
html.radar-dark [data-testid="stDataFrame"] [data-testid="stDataFrameResizable"],
html.radar-dark [data-testid="stTable"]{background-color:#231c29!important;color:#f0ddca!important}
html.radar-dark [data-testid="stDataFrame"] canvas{filter:none!important}
html.radar-dark [data-testid="stAlert"],
html.radar-dark [role="alert"]{background:#302632!important;color:#f0dcc6!important;border:1px solid #79563f!important}
html.radar-dark [data-testid="stAlert"] *,
html.radar-dark [role="alert"] *{color:#e9d1b8!important}
html.radar-dark [data-testid="stPlotlyChart"]{background:#241e2a!important;border-radius:12px!important}
html.radar-dark [data-testid="stWidgetLabel"] *,
html.radar-dark [data-testid="stCaptionContainer"] *,
html.radar-dark [data-testid="stDownloadButton"] *,
html.radar-dark .farming-ranking *{color:#dfc2aa!important}
</style>
<script>
(() => {
  const key='radar.theme.v1';
  let button=document.getElementById('radar-dark-toggle');
  if(!button){
    button=document.createElement('button');
    button.id='radar-dark-toggle';
    button.type='button';
    button.setAttribute('aria-label','다크모드 전환');
    document.body.appendChild(button);
  }
  const darkCat='data:image/webp;base64,{{DARK_CAT_B64}}';
  const lightCat='data:image/webp;base64,{{LIGHT_CAT_B64}}';
  function apply(isDark){
    document.documentElement.classList.toggle('radar-dark',isDark);
    button.innerHTML=isDark?'<span class="radar-pumpkin-lit" aria-hidden="true">🎃</span> DARK':'<span class="radar-pumpkin-unlit" aria-hidden="true">🎃</span> LIGHT';
    button.setAttribute('aria-pressed',String(isDark));
    // Banner images switch through CSS bound to html.radar-dark.
  }
  button.onclick=()=>{
    const next=!document.documentElement.classList.contains('radar-dark');
    apply(next);
    try{localStorage.setItem(key,next?'dark':'light')}catch(_e){}
  };
  let saved='light';
  try{saved=localStorage.getItem(key)||'light'}catch(_e){}
  apply(saved==='dark');

})();
</script>
"""
# Restore the original fixed top-right pumpkin toggle. The theme
# stylesheet and button script must be emitted together for the DOM class
# selectors and click handling to work.
# Theme selector stays in Streamlit's normal document flow so the native
# button receives pointer events. Fixed positioning of the wrapper previously
# displayed a button but made it unclickable in the deployed page.
# The original browser-side toggle changes only the DOM class: no Streamlit
# widget event, rerun, network refetch or Plotly recreation on theme clicks.
_dark_now = False
_theme_css = _theme_widget.split('</style>', 1)[0] + '</style>'
_theme_css += "\n<style>\nhtml.radar-dark [data-testid=\"stAppViewContainer\"]{\n background-color:#130f17!important;\n}\nhtml.radar-dark [data-testid=\"stAppViewContainer\"] .block-container{\n background:#17131c!important;backdrop-filter:none!important;\n}\n</style>"
_theme_css += "\n<style>\n/* Reference: rankings table plum background, muted bronze separators. */\nhtml.radar-dark [data-testid=\"stAppViewContainer\"] .block-container{background:rgba(29,22,33,.80)!important}\nhtml.radar-dark [data-testid=\"stMetric\"],html.radar-dark [data-testid=\"stExpander\"],\nhtml.radar-dark [data-testid=\"stDataFrame\"],html.radar-dark .radar-table-wrap,\nhtml.radar-dark .venue-rank-chart,html.radar-dark .market-chart-panel{\n background-color:#241d28!important;border-color:#594535!important;\n color:#f0dfce!important\n}\nhtml.radar-dark [data-testid=\"stExpander\"] summary{\n background:#3c2a32!important;color:#f3dfbf!important\n}\nhtml.radar-dark .radar-rank-table thead th,html.radar-dark .radar-rank-table th{\n background:#3c2a32!important;color:#f3dfbf!important;border-color:#684e38!important\n}\nhtml.radar-dark .radar-rank-table tbody td{background:#241d28!important;color:#f0dfce!important;border-color:#514038!important}\nhtml.radar-dark [data-baseweb=\"input\"]>div,html.radar-dark [data-baseweb=\"select\"]>div,\nhtml.radar-dark [data-testid=\"stTextInput\"] input,html.radar-dark [data-testid=\"stApp\"] textarea{\n background-color:#30252c!important;border-color:#78563f!important;color:#f5e7d5!important\n}\nhtml.radar-dark [role=\"listbox\"],html.radar-dark [role=\"option\"],html.radar-dark [data-baseweb=\"menu\"],\nhtml.radar-dark [data-baseweb=\"popover\"],html.radar-dark [data-testid=\"stPopoverContent\"]{\n background-color:#2c2330!important;color:#f4e4d2!important;border-color:#76513b!important\n}\nhtml.radar-dark [data-testid=\"stButton\"] button,html.radar-dark [data-testid=\"stDownloadButton\"] button{\n background-color:#3b2937!important;border-color:#aa7750!important;color:#ffead5!important\n}\nhtml.radar-dark [data-testid=\"stButton\"] button:disabled,html.radar-dark [data-testid=\"stDownloadButton\"] button:disabled{\n background-color:#302834!important;color:#b6a194!important;opacity:1!important\n}\nhtml.radar-dark [data-testid=\"stAlert\"]{background-color:#302632!important;border-color:#79563f!important}\nhtml.radar-dark [data-testid=\"stAlert\"] p{color:#f0dcc6!important}\nhtml.radar-dark [data-testid=\"stCaptionContainer\"] p{color:#cbb29e!important}\nhtml.radar-dark [data-testid=\"stDataFrame\"] canvas{filter:none!important}\n/* Data connection popover: override inherited light-theme brown typography. */\nhtml.radar-dark [data-baseweb=\"popover\"]:has([data-testid=\"stFileUploader\"]),\nhtml.radar-dark [data-testid=\"stPopoverContent\"]:has([data-testid=\"stFileUploader\"]),\nhtml.radar-dark [role=\"dialog\"]:has([data-testid=\"stFileUploader\"]){\n background:#241d28!important;color:#f0dfce!important;\n}\nhtml.radar-dark [data-baseweb=\"popover\"]:has([data-testid=\"stFileUploader\"]) :is(h1,h2,h3,h4,p,span,label,small),\nhtml.radar-dark [data-testid=\"stPopoverContent\"]:has([data-testid=\"stFileUploader\"]) :is(h1,h2,h3,h4,p,span,label,small),\nhtml.radar-dark [role=\"dialog\"]:has([data-testid=\"stFileUploader\"]) :is(h1,h2,h3,h4,p,span,label,small){\n color:#f0dfce!important;\n}\nhtml.radar-dark [data-baseweb=\"popover\"]:has([data-testid=\"stFileUploader\"]) :is([data-testid=\"stCaptionContainer\"],[data-testid=\"stWidgetLabel\"]) *,\nhtml.radar-dark [data-testid=\"stPopoverContent\"]:has([data-testid=\"stFileUploader\"]) :is([data-testid=\"stCaptionContainer\"],[data-testid=\"stWidgetLabel\"]) *{\n color:#d8baa3!important;\n}\nhtml.radar-dark [data-testid=\"stFileUploader\"] section,\nhtml.radar-dark [data-testid=\"stFileUploaderDropzone\"]{\n background:#30252c!important;border-color:#78563f!important;\n}\nhtml.radar-dark [data-baseweb=\"popover\"]:has([data-testid=\"stFileUploader\"]) button:disabled,\nhtml.radar-dark [data-testid=\"stPopoverContent\"]:has([data-testid=\"stFileUploader\"]) button:disabled{\n background:#302834!important;color:#bba89a!important;border-color:#615044!important;opacity:1!important;\n}\n/* Preserve dark surfaces on hover, focus and expanded states.\n   In particular, Streamlit's expander summary has a light hover default. */\nhtml.radar-dark [data-testid=\"stExpander\"] summary,\nhtml.radar-dark [data-testid=\"stExpander\"] summary:hover,\nhtml.radar-dark [data-testid=\"stExpander\"] summary:focus,\nhtml.radar-dark [data-testid=\"stExpander\"] summary:focus-visible,\nhtml.radar-dark [data-testid=\"stExpander\"] details[open] summary,\nhtml.radar-dark [data-testid=\"stExpander\"] details[open] summary:hover,\nhtml.radar-dark [data-testid=\"stExpander\"] summary[aria-expanded=\"true\"]{\n background:#3c2a32!important;background-color:#3c2a32!important;\n color:#f3dfbf!important;border-color:#684e38!important;\n}\nhtml.radar-dark [data-testid=\"stExpander\"] summary :is(p,span,svg),\nhtml.radar-dark [data-testid=\"stExpander\"] summary:hover :is(p,span,svg){\n color:#f3dfbf!important;fill:currentColor;\n}\nhtml.radar-dark [data-baseweb=\"select\"] > div:hover,\nhtml.radar-dark [data-baseweb=\"select\"] > div:focus-within,\nhtml.radar-dark [data-baseweb=\"input\"] > div:hover,\nhtml.radar-dark [data-baseweb=\"input\"] > div:focus-within{\n background-color:#30252c!important;color:#f5e7d5!important;\n border-color:#aa7750!important;\n}\nhtml.radar-dark [role=\"option\"]:hover,html.radar-dark [role=\"option\"]:focus,\nhtml.radar-dark [role=\"option\"][aria-selected=\"true\"]{\n background:#493342!important;color:#f4e4d2!important;\n}\n/* Override the old light alert CSS, including its inner panels. */\nhtml.radar-dark div[data-testid=\"stAlert\"],\nhtml.radar-dark div[data-testid=\"stAlert\"] > div,\nhtml.radar-dark [data-testid=\"stAlert\"] [data-testid=\"stMarkdownContainer\"]{\n background:#302632!important;\n color:#f0dcc6!important;\n border-color:#79563f!important;\n}\nhtml.radar-dark div[data-testid=\"stAlert\"] :is(p,span,div,svg){\n color:#f0dcc6!important;\n}\n</style>\n"
_theme_css += "<style>\n#radar-dark-toggle::before{\n content:\"\";display:inline-block;width:19px;height:19px;flex:0 0 19px;\n margin-right:5px;background:center/contain no-repeat url(\"data:image/svg+xml,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%2064%2064%22%3E%3Cpath%20d%3D%22M30%2010Q32%203%2038%204L37%209Q34%207%2033%2012C49%205%2059%2018%2059%2033C59%2050%2048%2060%2032%2060S5%2050%205%2033C5%2018%2015%205%2030%2010Z%22%20fill%3D%22%23da7c30%22%20stroke%3D%22%23a75023%22%20stroke-width%3D%222%22%2F%3E%3Cpath%20d%3D%22M22%2014Q13%2031%2022%2053M42%2014Q51%2031%2042%2053%22%20fill%3D%22none%22%20stroke%3D%22%23b65c24%22%20stroke-width%3D%222%22%2F%3E%3Cpath%20d%3D%22M30%2011L31%203L37%203L34%2012%22%20fill%3D%22%23557b32%22%2F%3E%3Cg%20fill%3D%22%234a2b1d%22%3E%3Cpath%20d%3D%22M20%2025L29%2034H16Z%22%2F%3E%3Cpath%20d%3D%22M44%2025L48%2034H35Z%22%2F%3E%3Cpath%20d%3D%22M29%2034L35%2034L32%2039Z%22%2F%3E%3Cpath%20d%3D%22M17%2043Q32%2056%2048%2043L45%2050L39%2048L34%2053L28%2049L22%2051Z%22%2F%3E%3C%2Fg%3E%3C%2Fsvg%3E\");\n}\nhtml.radar-dark #radar-dark-toggle::before{\n background-image:url(\"data:image/svg+xml,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%2064%2064%22%3E%3Cdefs%3E%3Cfilter%20id%3D%22g%22%20x%3D%22-100%25%22%20y%3D%22-100%25%22%20width%3D%22300%25%22%20height%3D%22300%25%22%3E%3CfeGaussianBlur%20stdDeviation%3D%222%22%20result%3D%22b%22%2F%3E%3CfeMerge%3E%3CfeMergeNode%20in%3D%22b%22%2F%3E%3CfeMergeNode%20in%3D%22SourceGraphic%22%2F%3E%3C%2FfeMerge%3E%3C%2Ffilter%3E%3C%2Fdefs%3E%3Cpath%20d%3D%22M30%2010Q32%203%2038%204L37%209Q34%207%2033%2012C49%205%2059%2018%2059%2033C59%2050%2048%2060%2032%2060S5%2050%205%2033C5%2018%2015%205%2030%2010Z%22%20fill%3D%22%23da7c30%22%20stroke%3D%22%23a75023%22%20stroke-width%3D%222%22%2F%3E%3Cpath%20d%3D%22M22%2014Q13%2031%2022%2053M42%2014Q51%2031%2042%2053%22%20fill%3D%22none%22%20stroke%3D%22%23b65c24%22%20stroke-width%3D%222%22%2F%3E%3Cpath%20d%3D%22M30%2011L31%203L37%203L34%2012%22%20fill%3D%22%23557b32%22%2F%3E%3Cg%20fill%3D%22%23ffe391%22%20filter%3D%22url(%23g)%22%3E%3Cpath%20d%3D%22M20%2025L29%2034H16Z%22%2F%3E%3Cpath%20d%3D%22M44%2025L48%2034H35Z%22%2F%3E%3Cpath%20d%3D%22M29%2034L35%2034L32%2039Z%22%2F%3E%3Cpath%20d%3D%22M17%2043Q32%2056%2048%2043L45%2050L39%2048L34%2053L28%2049L22%2051Z%22%2F%3E%3C%2Fg%3E%3C%2Fsvg%3E\")!important;\n}\nhtml:not(.radar-dark) [class*=\"st-key-radar-dark-table-\"]{display:none!important}\nhtml.radar-dark [class*=\"st-key-radar-light-table-\"]{display:none!important}\nhtml.radar-dark [data-testid=\"stPlotlyChart\"] .js-plotly-plot,\nhtml.radar-dark [data-testid=\"stPlotlyChart\"] .plot-container,\nhtml.radar-dark [data-testid=\"stPlotlyChart\"] .svg-container{\n background:#241d28!important;background-color:#241d28!important;\n}\nhtml.radar-dark [data-testid=\"stPlotlyChart\"] svg.main-svg{background:transparent!important;background-color:transparent!important}\nhtml.radar-dark [data-testid=\"stPlotlyChart\"] .bglayer rect{fill:#241d28!important}\nhtml.radar-dark [data-testid=\"stPlotlyChart\"] rect.bg{fill:#241d28!important}\nhtml.radar-dark [data-testid=\"stPlotlyChart\"] :is(.xtick,.ytick,.g-xtitle,.g-ytitle,.annotation,.legend) text{\n fill:#f5e6d8!important\n}\nhtml.radar-dark [data-testid=\"stPlotlyChart\"] .gridlayer path{stroke:#5c4b4e!important}\n</style>"
_theme_css += """<style>
html.radar-dark [data-testid="stAppViewContainer"]{
 background-image:linear-gradient(rgba(19,14,23,.75),rgba(19,14,23,.75)),
 var(--radar-pumpkin-image)!important;
 background-color:#17121a!important;
 background-blend-mode:normal!important
}
html.radar-dark [data-testid="stMain"],
html.radar-dark [data-testid="stMainBlockContainer"]{background-color:transparent!important}
</style>"""
# Dark-mode background: use the uploaded photo directly. Keep the same
# background sizing/positioning as LIGHT, with exactly one 75% overlay.
_dark_pumpkin_asset = DATA_DIR / 'assets' / 'pumpkin_dark.webp'
if _dark_pumpkin_asset.is_file():
    _dark_pumpkin_b64 = base64.b64encode(_dark_pumpkin_asset.read_bytes()).decode('ascii')
    _theme_css += (
        '<style>'
        'html.radar-dark body [data-testid="stAppViewContainer"]{'
        'background-image:linear-gradient(rgba(19,14,23,.75),rgba(19,14,23,.75)),'
        'url("data:image/webp;base64,'+_dark_pumpkin_b64+'")!important;'
        'background-color:#17121a!important;'
        'background-position:center top!important;'
        'background-size:auto min(100vh, 1600px)!important;'
        'background-repeat:repeat-x!important;'
        'background-attachment:scroll!important;'
        'background-blend-mode:normal!important;'
        '}'
        'html.radar-dark body [data-testid="stAppViewContainer"] [data-testid="stMain"],'
        'html.radar-dark body [data-testid="stAppViewContainer"] [data-testid="stMainBlockContainer"]{'
        'background:transparent!important;'
        '}'
        'html.radar-dark body [data-testid="stAppViewContainer"] .block-container{'
        'background:rgba(23,18,26,.88)!important;'
        '}'
        '</style>'
    )
st.markdown(_theme_css, unsafe_allow_html=True)

st.components.v1.html("""
<script>
(() => {
 const doc=window.parent.document;
 const win=window.parent;
 const storage='radar.theme.v1';
 let button=doc.getElementById('radar-dark-toggle');
 if(!button){
   button=doc.createElement('button');
   button.id='radar-dark-toggle';
   button.type='button';
   button.setAttribute('aria-label','라이트 및 다크 모드 전환');
   doc.body.appendChild(button);
 }
 function apply(dark){
   doc.documentElement.classList.toggle('radar-dark',dark);
   button.textContent=dark?'DARK':'LIGHT';
   button.setAttribute('aria-pressed',String(dark));
 }
 let saved='light';
 try{saved=win.localStorage.getItem(storage)||'light'}catch(e){}
 apply(saved==='dark');
 button.onclick=()=>{
   const next=!doc.documentElement.classList.contains('radar-dark');
   apply(next);
   try{win.localStorage.setItem(storage,next?'dark':'light')}catch(e){}
 };
})();
</script>
""", height=0)

_radar_table_seq = 0

def radar_dataframe(frame, **kwargs):
    """Keep native editable-free Streamlit grids in LIGHT; use the reference
    rankings-table colors for DARK, including cells rendered by canvas."""
    global _radar_table_seq
    _radar_table_seq += 1
    _table_id = _radar_table_seq
    with st.container(key=f'radar-light-table-{_table_id}'):
        st.dataframe(frame, **kwargs)
    if not isinstance(frame, pd.DataFrame):
        frame = pd.DataFrame(frame)
    headers = ''.join(
        '<th style="position:sticky;top:0;background:#3c2a32;color:#f3dfbf;'
        'border:1px solid #684e38;padding:9px 12px;text-align:left;'
        'white-space:nowrap;z-index:1">' + html.escape(str(column)) + '</th>'
        for column in frame.columns
    )
    rows=[]
    for values in frame.itertuples(index=False,name=None):
        cells=[]
        for value in values:
            if pd.isna(value):
                label='—'
            else:
                label=str(value)
            safe=html.escape(label)
            if label.startswith(('https://','http://')):
                safe='<a href="'+html.escape(label,quote=True)+'" target="_blank" rel="noopener noreferrer" style="color:#edac71;text-decoration:underline">'+safe+'</a>'
            cells.append(
                '<td style="background:#241d28;color:#f0dfce;'
                'border:1px solid #514038;padding:9px 12px;'
                'white-space:nowrap;max-width:390px;overflow:hidden;text-overflow:ellipsis"'
                ' title="'+html.escape(label,quote=True)+'">'+safe+'</td>'
            )
        rows.append('<tr>'+''.join(cells)+'</tr>')
    height=kwargs.get('height',None)
    maxheight='max-height:'+str(int(height))+'px;' if isinstance(height,(int,float)) else ''
    with st.container(key=f'radar-dark-table-{_table_id}'):
        st.markdown(
        '<div style="width:100%;overflow:auto;'+maxheight
        +'border:1px solid #594535;border-radius:10px;background:#241d28">'
        '<table style="width:100%;border-collapse:collapse;font-size:13px">'
        '<thead><tr>'+headers+'</tr></thead><tbody>'
        +''.join(rows)+'</tbody></table></div>',
        unsafe_allow_html=True
    )

# Override chart colors at the Plotly figure layer rather than SVG CSS.
def readable_plotly(fig):
    bg = '#241d28' if _dark_now else '#fff8ed'
    fg = '#f5e6d8' if _dark_now else '#573d2d'
    grid = '#5c4b4e' if _dark_now else '#dfc6a5'
    fig.update_layout(
        paper_bgcolor=bg, plot_bgcolor=bg,
        font=dict(color=fg, family='Noto Sans KR, sans-serif'),
        xaxis=dict(tickfont=dict(color=fg), title_font=dict(color=fg),
                   gridcolor=grid, linecolor=grid, zerolinecolor=grid),
        yaxis=dict(tickfont=dict(color=fg), title_font=dict(color=fg),
                   gridcolor=grid, linecolor=grid, zerolinecolor=grid),
        hoverlabel=dict(bgcolor=bg, font=dict(color=fg)),
    )
    for annotation in fig.layout.annotations:
        annotation.font = dict(color=fg, size=14)
    return fig


st.markdown("""
<style>
/* Native theme button in normal layout; no fixed/z-index overlay. */
.st-key-theme_action_v3 button {
 min-width:110px!important;min-height:40px!important;
 border-radius:999px!important;padding:8px 12px!important;
 border:1px solid #b9844f!important;background:#fff4e2!important;
 color:#583920!important;font:700 14px/1.4 'Noto Sans KR',sans-serif!important;
 box-shadow:0 3px 15px rgba(36,19,7,.2)!important;
 cursor:pointer!important;opacity:1!important;
}
.st-key-theme_action_v3 button p {
 color:inherit!important;font-weight:700!important;white-space:nowrap!important;
}
.st-key-theme_action_v3 button::before{
 content:"";display:inline-block;width:19px;height:19px;flex:0 0 19px;
 margin-right:5px;background:center/contain no-repeat url("data:image/svg+xml,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%2064%2064%22%3E%3Cpath%20d%3D%22M30%2010Q32%203%2038%204L37%209Q34%207%2033%2012C49%205%2059%2018%2059%2033C59%2050%2048%2060%2032%2060S5%2050%205%2033C5%2018%2015%205%2030%2010Z%22%20fill%3D%22%23da7c30%22%20stroke%3D%22%23a75023%22%20stroke-width%3D%222%22%2F%3E%3Cpath%20d%3D%22M22%2014Q13%2031%2022%2053M42%2014Q51%2031%2042%2053%22%20fill%3D%22none%22%20stroke%3D%22%23b65c24%22%20stroke-width%3D%222%22%2F%3E%3Cpath%20d%3D%22M30%2011L31%203L37%203L34%2012%22%20fill%3D%22%23557b32%22%2F%3E%3Cg%20fill%3D%22%234a2b1d%22%3E%3Cpath%20d%3D%22M20%2025L29%2034H16Z%22%2F%3E%3Cpath%20d%3D%22M44%2025L48%2034H35Z%22%2F%3E%3Cpath%20d%3D%22M29%2034L35%2034L32%2039Z%22%2F%3E%3Cpath%20d%3D%22M17%2043Q32%2056%2048%2043L45%2050L39%2048L34%2053L28%2049L22%2051Z%22%2F%3E%3C%2Fg%3E%3C%2Fsvg%3E");
}
.st-key-theme_action_v3 button:hover{background:#ffebca!important}
</style>
""" + ("""
<style>
.st-key-theme_action_v3 button {
 background:#372536!important;border-color:#ac6d42!important;color:#fff0df!important;
}
.st-key-theme_action_v3 button::before{
 background-image:url("data:image/svg+xml,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%2064%2064%22%3E%3Cdefs%3E%3Cfilter%20id%3D%22g%22%20x%3D%22-100%25%22%20y%3D%22-100%25%22%20width%3D%22300%25%22%20height%3D%22300%25%22%3E%3CfeGaussianBlur%20stdDeviation%3D%222%22%20result%3D%22b%22%2F%3E%3CfeMerge%3E%3CfeMergeNode%20in%3D%22b%22%2F%3E%3CfeMergeNode%20in%3D%22SourceGraphic%22%2F%3E%3C%2FfeMerge%3E%3C%2Ffilter%3E%3C%2Fdefs%3E%3Cpath%20d%3D%22M30%2010Q32%203%2038%204L37%209Q34%207%2033%2012C49%205%2059%2018%2059%2033C59%2050%2048%2060%2032%2060S5%2050%205%2033C5%2018%2015%205%2030%2010Z%22%20fill%3D%22%23da7c30%22%20stroke%3D%22%23a75023%22%20stroke-width%3D%222%22%2F%3E%3Cpath%20d%3D%22M22%2014Q13%2031%2022%2053M42%2014Q51%2031%2042%2053%22%20fill%3D%22none%22%20stroke%3D%22%23b65c24%22%20stroke-width%3D%222%22%2F%3E%3Cpath%20d%3D%22M30%2011L31%203L37%203L34%2012%22%20fill%3D%22%23557b32%22%2F%3E%3Cg%20fill%3D%22%23ffe391%22%20filter%3D%22url(%23g)%22%3E%3Cpath%20d%3D%22M20%2025L29%2034H16Z%22%2F%3E%3Cpath%20d%3D%22M44%2025L48%2034H35Z%22%2F%3E%3Cpath%20d%3D%22M29%2034L35%2034L32%2039Z%22%2F%3E%3Cpath%20d%3D%22M17%2043Q32%2056%2048%2043L45%2050L39%2048L34%2053L28%2049L22%2051Z%22%2F%3E%3C%2Fg%3E%3C%2Fsvg%3E")!important;
}
.st-key-theme_action_v3 button:hover{background:#4c3445!important}
</style>
""" if _dark_now else ""), unsafe_allow_html=True)

# Independent left-edge settings control replaces the fragile native sidebar opener.
with st.popover(' ', help='데이터 연결 및 설정 열기'):
    st.header('데이터 연결')
    pro_key=st.text_input('DefiLlama Pro API key (선택)', type='password', help='유료 derivatives 거래량 API용. 키는 저장하지 않습니다.')
    use_hl=st.checkbox('Hyperliquid 시장 데이터 불러오기', value=True)
    uploaded=st.file_uploader('프로젝트별 포인트·참여자 CSV', type='csv')
    st.download_button('CSV 템플릿 다운로드', (DATA_DIR/'manual_metrics.csv').read_bytes(), 'manual_metrics.csv', mime='text/csv')
    if st.button('API 캐시 새로고침'):
        fetch_catalog.clear();fetch_oi.clear();fetch_hyperliquid.clear();fetch_standx.clear();fetch_variational.clear();fetch_pacifica_markets.clear();fetch_arcus_markets.clear();fetch_nado_ccxt.clear();fetch_pacifica_ccxt.clear();fetch_pro_derivatives.clear();st.rerun()




# Browser-level opener.  This only operates on our own static DOM; it never
# interpolates user inputs.  Streamlit's popover remains the functional backend.
st.html("""
<script>
(() => {
  const id = 'radar-edge-toggle';
  let button = document.getElementById(id);
  if (!button) {
    button = document.createElement('button');
    button.id = id;
    button.type = 'button';
    button.textContent = '❯';
    button.setAttribute('aria-label', '데이터 연결 설정 열기');
    button.title = '데이터 연결 설정 열기';
    document.body.appendChild(button);
  }
  // Override BaseWeb's computed floating placement after the popover opens.
  // Its trigger remains in the Streamlit content column, whereas our actual
  // button sits at the viewport's left edge.
  function positionSettingsPanel() {
    const trigger = document.querySelector('[data-testid="stPopover"] button');
    if (!trigger) return;
    const candidates = [...document.querySelectorAll(
      '[data-baseweb="popover"],[data-testid="stPopoverContent"],[role="dialog"]'
    )];
    for (const panel of candidates) {
      const text = panel.textContent || '';
      if (!text.includes('데이터 연결') || !text.includes('API')) continue;
      // The portal's floating wrapper commonly holds the computed transform.
      // Also position the panel for Streamlit versions whose content is positioned.
      const floating = panel.closest('[data-baseweb="popover"]') || panel;
      for (const node of [floating, panel]) {
        node.style.setProperty('position', 'fixed', 'important');
        node.style.setProperty('left', '8px', 'important');
        node.style.setProperty('top', '55px', 'important');
        node.style.setProperty('right', 'auto', 'important');
        node.style.setProperty('bottom', 'auto', 'important');
        node.style.setProperty('transform', 'none', 'important');
        node.style.setProperty('margin', '0', 'important');
        node.style.setProperty('max-height', 'calc(100vh - 65px)', 'important');
      }
    }
  }
  button.onclick = () => {
    const trigger = document.querySelector('[data-testid="stPopover"] button');
    if (trigger) trigger.click();
    requestAnimationFrame(positionSettingsPanel);
  };
  // Streamlit can mount the floating panel a frame after the click.
  if (!window.__radarSettingsPanelObserver) {
    const watcher = new MutationObserver(() => {
      requestAnimationFrame(positionSettingsPanel);
    });
    watcher.observe(document.body, {childList:true,subtree:true});
    window.__radarSettingsPanelObserver = watcher;
  }
})();
</script>
""", unsafe_allow_javascript=True)

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
    try: entropy_stats=fetch_entropy_hip3()
    except Exception as e: entropy_stats=None;errors.append(f'Entropy HIP-3: {e}')
    try: robinhood_lighter_stats=fetch_robinhood_lighter()
    except Exception as e: robinhood_lighter_stats=None;errors.append(f'Robinhood Lighter: {e}')
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
# Keep Robinhood Chain Lighter isolated from general Lighter.
if robinhood_lighter_stats:
    rh_mask=df['watch_name'].eq('robinhoodchain.lighter')
    if rh_mask.any():
        for field,value in robinhood_lighter_stats.items():
            if value is not None:
                if field not in df.columns:df[field]=None
                df.loc[rh_mask,field]=value
# Entropy io is a distinct HIP-3 venue. Never merge Hyperliquid's default market.
if entropy_stats:
    entropy_mask=df['watch_name'].eq('Entropy')
    if entropy_mask.any():
        for field,value in entropy_stats.items():
            if value is not None:
                if field not in df.columns:df[field]=None
                df.loc[entropy_mask,field]=value
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
    # Prefer the bundled collector output. GitHub Raw may lag behind the
    # deployed revision or be unavailable from Streamlit Cloud.
    archive_path=DATA_DIR/'data'/'market_snapshots.csv'
    if archive_path.is_file():
        archive=pd.read_csv(archive_path)
    else:
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
        df.loc[missing,source]='Official collector snapshot (48h maximum age)'
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

def harvest_linear_bar(fig, values, height=360, money=True):
    """Explicit linear USD ticks: equal value differences have equal pixel spacing."""
    import math
    valid=[float(v) for v in values if pd.notna(v) and float(v)>=0]
    maximum=max(valid) if valid else 1.0
    rough=max(maximum/5.0,1.0)
    decade=10**math.floor(math.log10(rough))
    increment=next((k*decade for k in (1,2,2.5,5,10) if k*decade>=rough),10*decade)
    axis_end=max(4*increment,math.ceil(maximum/increment+0.35)*increment)
    tick_values=[i*increment for i in range(int(axis_end/increment)+1)]
    fig.update_layout(
        height=height,margin=dict(l=178,r=105,t=18,b=62),
        showlegend=False,font=dict(family='Noto Sans KR, DM Sans, sans-serif',size=14,color='#61432d'),
        paper_bgcolor='rgba(0,0,0,0)',plot_bgcolor='rgba(255,248,233,.28)',
        xaxis=dict(type='linear',range=[0,axis_end],tickmode='array',
                   tickvals=tick_values,ticktext=[(f'${n/1e9:g}B' if n>=1e9 else f'${n/1e6:g}M' if n>=1e6 else f'${n/1e3:g}K' if n>=1e3 else f'${n:,.0f}') if money else f'{n:,.0f}' for n in tick_values],
                   ticks='outside',tickcolor='#a07750',showticklabels=True,
                   showgrid=True,gridcolor='#e2cda9',zeroline=False,
                   tickfont=dict(family='Noto Sans KR, DM Sans, sans-serif',size=13,color='#62432c'),title_font=dict(size=14,color='#62432c')),
        yaxis=dict(tickfont=dict(family='Noto Sans KR, DM Sans, sans-serif',size=15,color='#62432c'),showgrid=False,automargin=True,title=None))
    return fig

radar_tabs=st.tabs(['🎃 관심 거래소','🐱 경쟁 비교','🌰 전체 스캐너','🍁 데이터 출처'])
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
    if url and (url.startswith('https://') or url.startswith('data:image/')):
        try:
            if url.startswith('data:image/'):
                content=base64.b64decode(url.split(',',1)[1])
            else:
                response=requests.get(url,timeout=8,headers=HEADERS)
                response.raise_for_status()
                content=response.content
            image=Image.open(BytesIO(content)).convert('RGBA')
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

# HelloTrade mark cross-checked against hello.trade and its verified project profile.
# Always deliver a data URI to browsers; this avoids broken third-party image links.
HELLOTRADE_LOGO_URL = 'https://pbs.twimg.com/profile_images/1990455362042933250/zGFe38sh.jpg'

@st.cache_data(ttl=86400,show_spinner=False)
def hello_trade_logo_embedded():
    from PIL import Image, ImageDraw
    # Prefer the actual project-branded bitmap, validated before embedding.
    try:
        response=requests.get(HELLOTRADE_LOGO_URL,timeout=3,
                              headers={'User-Agent':'Mozilla/5.0'})
        response.raise_for_status()
        if not 1000 <= len(response.content) <= 3000000:
            raise ValueError('logo image byte length not plausible')
        image=Image.open(BytesIO(response.content))
        image.verify()
        if image.width<90 or image.height<90:
            raise ValueError('logo image is too small')
        return ('data:image/jpeg;base64,'+
                base64.b64encode(response.content).decode('ascii'))
    except Exception:
        # Self-contained vector-traced emergency fallback of the same mark.
        # Rasterize with Pillow so Streamlit's image sanitizer reliably accepts it.
        board=Image.new('RGBA',(512,512),(255,91,4,255))
        pencil=ImageDraw.Draw(board)
        edges=[
            ((143,362),(123,354),(127,322),(140,285)),
            ((140,285),(163,219),(222,144),(259,151)),
            ((259,151),(280,155),(280,179),(281,186)),
            ((281,186),(305,147),(338,130),(357,144)),
            ((357,144),(385,165),(379,213),(348,284)),
            ((348,284),(323,340),(281,372),(256,351)),
            ((256,351),(242,339),(242,315),(244,304)),
            ((244,304),(224,342),(169,373),(143,362))
        ]
        outline=[]
        for p0,p1,p2,p3 in edges:
            for n in range(48):
                t=n/48.0
                x=(1-t)**3*p0[0]+3*(1-t)**2*t*p1[0]+3*(1-t)*t*t*p2[0]+t**3*p3[0]
                y=(1-t)**3*p0[1]+3*(1-t)**2*t*p1[1]+3*(1-t)*t*t*p2[1]+t**3*p3[1]
                outline.append((x,y))
        pencil.polygon(outline,fill=(255,255,255,255))
        board=board.resize((128,128),Image.Resampling.LANCZOS)
        output=BytesIO()
        board.save(output,format='PNG',optimize=True)
        return ('data:image/png;base64,'+
                base64.b64encode(output.getvalue()).decode('ascii'))

def exchange_logo_url(name):
    if str(name).strip().lower() in ('hello.trade','hellotrade','hello trade'):
        return hello_trade_logo_embedded()
    # Official-site favicons for projects absent or mismatched in DefiLlama.
    official={'StandX':'standx.com','Entropy':'entropy.io'}
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

def display_venue_name(name):
    return 'Lighter RH' if str(name)=='robinhoodchain.lighter' else str(name)

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
    label=html.escape(display_venue_name(name))
    if url:
        return '<span class="exchange-cell"><img class="exchange-logo" src="'+html.escape(url,quote=True)+'" alt="" loading="lazy" referrerpolicy="no-referrer"><span>'+label+'</span></span>'
    return '<span class="exchange-cell"><span class="exchange-monogram">'+html.escape(display_venue_name(name)[:1].upper())+'</span><span>'+label+'</span></span>'

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
    # This is a *relative market-activity proxy*, not a trader count or a
    # prediction of rewards. Never assign a score to markets with missing data.
    valid_market=(watched['volume_24h_usd']>0)&(watched['oi_usd']>0)
    watched['activity_congestion']=float('nan')
    if valid_market.sum()>=2:
        group=watched.loc[valid_market]
        vol_rank=group['volume_24h_usd'].rank(pct=True,method='average')
        oi_rank=group['oi_usd'].rank(pct=True,method='average')
        watched.loc[valid_market,'activity_congestion']=100*(0.6*vol_rank+0.4*oi_rank)
    def congestion_pumpkins(value):
        import math
        if pd.isna(value):return '<span class="radar-missing">자료 부족</span>'
        score=float(value)
        if score>=80:label='매우 혼잡'
        elif score>=60:label='혼잡'
        elif score>=40:label='보통'
        elif score>=20:label='낮음'
        else:label='매우 낮음'
        count=max(1,min(5,6-math.ceil(score/20)))
        return ('<div class="congestion-cell"><strong>'+label+'</strong>'
                '<div class="pumpkin-meter" aria-label="호박 '+str(count)+'개 / 5개">'
                +'🎃'*count+'<span class="pumpkin-empty">'+'·'*(5-count)+'</span>'
                '</div></div>')

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
    # Fundraising research is curated with source links and explicit scope.
    # The S/A/B/C/D tier is our editorial *research priority*, not a
    # standardized crypto-industry grade or an expected token return.
    research_file=DATA_DIR/'data'/'fundraising_research.csv'
    try:
        funding=pd.read_csv(research_file,keep_default_na=False)
        funding_by_name={str(rec['project']):rec for rec in funding.to_dict('records')}
    except Exception:
        funding_by_name={}

    def research_funding_grade(record):
        if not record or record.get('verified')=='no':
            return ('미평가','근거 부족')
        name=str(record.get('project',''))
        amount=pd.to_numeric(record.get('amount_usd',''),errors='coerce')
        if record.get('scope')=='parent':
            return ('B','별도 시장·포인트 프로그램의 직접 투자금이 아님')
        if record.get('scope')=='ecosystem':
            return ('B','블록체인 생태계 펀딩: 해당 앱의 보상과 별개')
        if name=='StandX':
            return ('B','자체 조달 운영: VC가 없다는 이유로 감점하지 않음')
        if pd.isna(amount):
            return ('B','금액 미공개: 주요 투자사만 확인됨')
        lead=str(record.get('lead_investor','')).lower()
        strong=any(term in lead for term in (
            'dragonfly','founders fund','general catalyst','a16z',
            'paradigm','coinbase','bain capital','ribbit','robinhood'))
        # Published funding thresholds are common screening shortcuts.
        # Grade cutoffs/weights are explicitly project-specific heuristics.
        if amount>=30000000 and strong:return ('S','대형 투자 라운드 + 주요 투자사')
        if amount>=8000000 and strong:return ('A','투자 규모와 투자사 모두 우수')
        if amount>=3000000 and strong:return ('A','유의미한 투자 + 주요 투자사')
        if amount>=3000000:return ('B','공개 펀딩 확보')
        if amount>=1000000:return ('C','초기 투자 확인')
        return ('D','초기 투자 소규모 확인')

    def fundraising_cells(project):
        record=funding_by_name.get(str(project))
        if not record:
            return ('<span class="radar-missing">미확인</span>',
                    '<span class="radar-missing">—</span>',
                    '<span class="radar-missing">미평가</span>')
        source=str(record.get('source_url','')).strip()
        amount=str(record.get('display_amount','')).strip() or '미확인'
        investors=str(record.get('investors','')).strip() or '미확인'
        lead=str(record.get('lead_investor','')).strip()
        scope=str(record.get('scope',''))
        status=str(record.get('verified',''))
        grade,why=research_funding_grade(record)
        grade_class=grade.lower() if grade in ('S','A','B','C','D') else 'unknown'
        if source.startswith('https://'):
            amount_html=('<a class="funding-source" href="'+html.escape(source,quote=True)
                         +'" target="_blank" rel="noopener noreferrer" title="투자 자료 출처">'
                         +html.escape(amount)+'</a>')
        else:
            amount_html=html.escape(amount)
        qualifier=(' <small>· 모기업</small>' if scope=='parent' else
                   ' <small>· 생태계</small>' if scope=='ecosystem' else
                   ' <small>· 자체 조달</small>' if project=='StandX' else '')
        if status=='partial':
            qualifier+=' <small>· 추정</small>'
        investor_html=html.escape(lead or investors)
        investor_html='<span title="'+html.escape(investors,quote=True)+'">'+investor_html+'</span>'
        grade_html=('<span class="farming-tier tier-'+grade_class+'" title="'
                    +html.escape(why,quote=True)+'">'+html.escape(grade)+'</span>')
        return (amount_html+qualifier,investor_html,grade_html)

    body=[]
    for _,entry in rank.iterrows():
        row_oi_text=compact_usd(entry['oi_usd'])
        ratio=f"{entry['volume_oi_ratio']:.2f}×" if pd.notna(entry['volume_oi_ratio']) else '—'
        raised_html,investor_html,grade_html=fundraising_cells(entry['watch_name'])
        body.append('<tr><td class="rank-no">'+table_cell(int(entry['#']))+'</td>'
                    '<td class="rank-name">'+exchange_logo_html(entry['watch_name'])+'</td>'
                    '<td>'+volume_bar(entry['volume_24h_usd'])+'</td>'
                    '<td>'+table_cell(row_oi_text)+'</td>'
                    '<td>'+table_cell(ratio)+'</td>'
                    '<td>'+congestion_pumpkins(entry['activity_congestion'])+'</td>'
                    '<td class="funding-col">'+raised_html+'</td>'
                    '<td class="investor-col">'+investor_html+'</td>'
                    '<td class="tier-col">'+grade_html+'</td></tr>')
    table_header=('<div class="radar-table-wrap fundraising-ranking"><table class="radar-rank-table">'
                  '<thead><tr><th>#</th><th>Exchange</th><th>24H Volume</th>'
                  '<th>Open Interest</th><th>Vol / OI</th><th>혼잡도</th>'
                  '<th>투자유치</th><th>주요 투자사</th><th>검토 등급</th>'
                  '</tr></thead><tbody>')
    st.markdown(table_header+''.join(body)+'</tbody></table></div>',unsafe_allow_html=True)
    st.caption('추정 혼잡도: 관측된 관심 거래소의 24H Volume 순위 60% + OI 순위 40%. 투자/등급은 실제 보상액이나 파밍 성공확률이 아닌 리서치 우선순위이며, 투자 미공개 또는 별도 모기업·생태계 투자금은 구분합니다.')
    with st.expander('펀드레이징 출처 · S~D 등급 산정 기준',expanded=False):
        st.markdown(
            '**등급은 업계의 공식 공통 규격이 아니라 공개 투자 라운드 분석에서 쓰는 '
            '투자 규모·리드 투자사·투자 대상 일치 여부를 활용한 자체 리서치 등급입니다.** '
            'S: 3천만 달러 이상 및 검증된 주요 리드 투자사, '
            'A: 8백만 달러 이상+주요 리드 또는 3백만 달러 이상+주요 리드, '
            'B: 공개 투자 확인 또는 유명 전략 투자이나 금액 비공개·자체 운영·연결된 모기업 투자, '
            'C: 공개 투자 1백만 달러 이상, D: 1백만 달러 미만. '
            '검증할 수 없는 경우 미평가. 자금 조달 규모가 작다는 사실만으로 파밍 기회가 낮다는 뜻은 아닙니다.'
        )
        st.warning('주의: VC 투자만으로 에어드랍 시행·포인트 전환·토큰 발행이 보장되지 않습니다. 특히 Lighter 모기업 투자금과 Robinhood Chain 전용 포인트를 혼동하지 않습니다.')
        if not funding_by_name:
            st.info('투자 자료 파일을 불러오지 못했습니다.')
        else:
            radar_dataframe(
                funding[['project','display_amount','investors','round','scope','note','source_url']]
                .rename(columns={'project':'거래소','display_amount':'공개 투자금','investors':'투자사',
                                 'round':'라운드','scope':'투자 대상','note':'검증 메모','source_url':'출처'}),
                hide_index=True,use_container_width=True,
                column_config={'출처':st.column_config.LinkColumn('근거 링크')}
            )
    def ranking_with_logos(frame, field, accent, axis_title):
        # Native CSS grid: venue and bar are separate columns, never overlays.
        import math
        v=frame.dropna(subset=[field]).sort_values(field,ascending=False)
        if v.empty:
            st.info('해당 시장 데이터가 확보되면 차트가 표시됩니다.')
            return
        maximum=max(float(v[field].max()),1.0)
        magnitude=10**math.floor(math.log10(maximum/4))
        step=next((i*magnitude for i in (1,2,2.5,5,10) if i*magnitude>=maximum/4),10*magnitude)
        end=math.ceil(maximum/step)*step
        rows=[]
        for _,entry in v.iterrows():
            value=float(entry[field])
            name=str(entry['watch_name'])
            percentage=max(0,min(100,100*value/end))
            bar=('<span class="venue-bar" style="width:'+f'{percentage:.2f}'
                 +'%;background:'+accent+'"></span>')
            rows.append('<div class="venue-rank-row">'
                        '<div class="venue-rank-name">'+exchange_logo_html(name)+'</div>'
                        '<div class="venue-rank-measure">'+bar
                        +'<span class="venue-rank-value">'+html.escape(compact_usd(value))+'</span>'
                        '</div></div>')
        def tick(v):
            if v>=1e9:return '$'+f'{v/1e9:g}'+'B'
            if v>=1e6:return '$'+f'{v/1e6:g}'+'M'
            if v>=1e3:return '$'+f'{v/1e3:g}'+'K'
            return '$'+f'{v:,.0f}'
        ticks=''.join('<span>'+html.escape(tick(k*step))+'</span>'
                      for k in range(int(end/step)+1))
        st.markdown('<div class="venue-rank-chart">'+''.join(rows)
                    +'<div class="venue-rank-foot"><span></span><div class="venue-rank-ticks">'
                    +ticks+'</div></div><div class="venue-rank-axis">'
                    +html.escape(axis_title)+' (USD)</div></div>',unsafe_allow_html=True)

    chart_a,chart_b=st.columns(2)
    with chart_a:
        st.markdown('#### 24H Volume Rankings')
        ranking_with_logos(watched,'volume_24h_usd','#b36732','24H Volume')
    with chart_b:
        st.markdown('#### Open Interest Rankings')
        ranking_with_logos(watched,'oi_usd','#d2944c','Open Interest')
    st.markdown('#### 시장 위치 분석')
    both=watched.dropna(subset=['volume_24h_usd','oi_usd']).copy()
    both=both[(both['volume_24h_usd']>0)&(both['oi_usd']>0)]
    if both.empty:
        st.info('현재 거래량과 OI가 모두 확보된 거래소가 없어 좌표를 표시할 수 없습니다.')
    else:
        import math
        # Keep true coordinates on linear axes; resize crowded logos in place.
        xmax=float(both['oi_usd'].max())*1.17
        ymax=float(both['volume_24h_usd'].max())*1.20
        xmin=-0.105*xmax
        ymin=-0.17*ymax

        def axis_ticks(top):
            estimate=max(float(top)/5,1)
            decade=10**math.floor(math.log10(estimate))
            step=next((n*decade for n in (1,2,2.5,5,10) if n*decade>=estimate),10*decade)
            return [i*step for i in range(int(top/step)+1)]

        def usd_tick(value):
            if value>=1e9:return '$'+f'{value/1e9:g}B'
            if value>=1e6:return '$'+f'{value/1e6:g}M'
            if value>=1e3:return '$'+f'{value/1e3:g}K'
            return '$'+f'{value:,.0f}'

        amounts=both['volume_24h_usd']
        vmin=float(amounts.min())
        vmax=float(amounts.max())

        def preferred_diameter(volume):
            # Trade-volume weighting without disproportionately large logos.
            fraction=(float(volume)-vmin)/(vmax-vmin) if vmax>vmin else 0.5
            return 29+14*math.sqrt(fraction)

        # Keep each logo centered exactly on its observed (OI, Volume) values.
        # Shrink only when neighboring data points are too close on-screen.
        markets=[]
        for _,entry in both.iterrows():
            oi_value=float(entry['oi_usd'])
            vol_value=float(entry['volume_24h_usd'])
            markets.append({
                'name':str(entry['watch_name']),
                'oi':oi_value,
                'volume':vol_value,
                'nx':(oi_value-xmin)/(xmax-xmin),
                'ny':(vol_value-ymin)/(ymax-ymin),
                'preferred':preferred_diameter(vol_value),
            })

        for index,item in enumerate(markets):
            neighbor_distance=min(
                (math.hypot((item['nx']-other['nx'])*930,
                            (item['ny']-other['ny'])*395)
                 for j,other in enumerate(markets) if j!=index),
                default=999.0
            )
            # If two preferred circles would overlap, reduce BOTH diameters.
            # 80% of nearest center distance leaves visible clearance.
            item['diameter']=max(12.0,min(item['preferred'],
                                          0.80*neighbor_distance-3.0))

        fig=go.Figure()
        for item in markets:
            name=item['name']
            actual_x=item['oi']
            actual_y=item['volume']
            diameter=item['diameter']
            tooltip=('<b>'+html.escape(name)+'</b><br>'
                     +'OI: $'+f'{actual_x:,.0f}'+'<br>'
                     +'24H Volume: $'+f'{actual_y:,.0f}'+'<extra></extra>')

            # The hover target and circular image have identical data anchors.
            fig.add_scatter(
                x=[actual_x],y=[actual_y],mode='markers',showlegend=False,
                marker=dict(size=max(18,diameter+5),
                            color='rgba(255,250,241,0.01)',line=dict(width=0)),
                hovertemplate=tooltip,name=name
            )
            try:
                logo=round_exchange_logo(name,exchange_logo_url(name))
                fig.add_layout_image(dict(
                    source=logo,xref='x domain',yref='y domain',
                    x=item['nx'],y=item['ny'],
                    sizex=diameter/930,sizey=diameter/395,
                    xanchor='center',yanchor='middle',sizing='contain',
                    layer='above'
                ))
            except Exception:
                fig.add_scatter(
                    x=[actual_x],y=[actual_y],mode='markers',showlegend=False,
                    marker=dict(size=diameter,color='#c57c3a',
                                line=dict(color='#fff4e1',width=1.5)),
                    hovertemplate=tooltip
                )

        xticks=axis_ticks(xmax)
        yticks=axis_ticks(ymax)
        fig.update_layout(
            height=575,margin=dict(t=74,l=125,r=46,b=75),
            paper_bgcolor='rgba(0,0,0,0)',
            plot_bgcolor='rgba(255,250,240,0.66)',
            showlegend=False,hovermode='closest',hoverdistance=36,
            font=dict(family='Noto Sans KR, DM Sans, sans-serif',
                      size=14,color='#60432e'),
            hoverlabel=dict(bgcolor='#fff9ec',bordercolor='#c19565',
                            font=dict(family='Noto Sans KR, DM Sans, sans-serif',
                                      size=15,color='#49311f'),align='left'),
            xaxis=dict(type='linear',range=[xmin,xmax],
                       tickmode='array',tickvals=xticks,
                       ticktext=[usd_tick(v) for v in xticks],
                       title=dict(text='Open Interest (USD)',
                                  standoff=16,
                                  font=dict(family='Noto Sans KR, DM Sans, sans-serif',size=15,color='#573a26')),
                       tickfont=dict(family='Noto Sans KR, DM Sans, sans-serif',size=13,color='#65472f'),
                       showgrid=True,gridcolor='#dfc6a5',gridwidth=1,
                       showline=True,linecolor='#ac855e',
                       zeroline=True,zerolinecolor='#cdb08b',
                       ticks='outside',automargin=True),
            yaxis=dict(type='linear',range=[ymin,ymax],
                       tickmode='array',tickvals=yticks,
                       ticktext=[usd_tick(v) for v in yticks],
                       title=None,
                       tickfont=dict(family='Noto Sans KR, DM Sans, sans-serif',size=13,color='#65472f'),
                       showgrid=True,gridcolor='#dfc6a5',gridwidth=1,
                       showline=True,linecolor='#ac855e',
                       zeroline=True,zerolinecolor='#cdb08b',
                       ticks='outside',automargin=True)
        )
        # A horizontal caption is sharper than the former rotated Y title.
        fig.add_annotation(x=0,y=1.12,xref='paper',yref='paper',
                           showarrow=False,xanchor='left',
                           text='<b>24H Volume (USD)</b>',
                           font=dict(family='Noto Sans KR, DM Sans, sans-serif',size=15,color='#573a26'))
        st.plotly_chart(
            readable_plotly(fig),use_container_width=True,theme=None,
            config={'displayModeBar':False,'scrollZoom':False,
                    'responsive':True})
        st.caption('가로축 OI · 세로축 24시간 거래량: 선형 눈금이며 로고는 실제 데이터 좌표에 표시됩니다. 가까운 거래소는 로고 크기만 자동으로 줄어듭니다.')
    with st.expander('데이터 제공 범위 및 미확보 항목'):
        coverage=watched[['watch_name','volume_source','oi_source']].copy()
        coverage['Volume']=watched['volume_24h_usd'].map(lambda x:'확보' if pd.notna(x) else '미확보')
        coverage['OI']=watched['oi_usd'].map(lambda x:'확보' if pd.notna(x) else '미확보')
        coverage_view=coverage.rename(columns={'watch_name':'거래소','volume_source':'거래량 출처','oi_source':'OI 출처'})
        radar_dataframe(coverage_view,hide_index=True,use_container_width=True)
    st.download_button('시장 데이터 CSV',watched.to_csv(index=False).encode('utf-8-sig'),'perp_market_radar.csv','text/csv')

with radar_tabs[1]:
    st.subheader('참여자 경쟁도 vs 시장 OI')
    ca,cb=st.columns(2)
    with ca:
        st.markdown('#### A · 7일 활성 지갑')
        v=df.dropna(subset=['active_wallets_7d']).sort_values('active_wallets_7d').tail(20)
        if v.empty:
            st.info('활성 지갑 데이터 미확보 · 온체인 지갑 분석 연동 필요')
        else:
            fig=px.bar(v,x='active_wallets_7d',y='name',orientation='h',
                       color_discrete_sequence=['#a36b43'],labels={'active_wallets_7d':'7D Traders','name':''})
            fig.update_traces(marker_line_width=0)
            harvest_linear_bar(fig,v['active_wallets_7d'],height=max(330,len(v)*36+90),money=False)
            st.plotly_chart(readable_plotly(fig),use_container_width=True,theme=None)
    with cb:
        st.markdown('#### B · OI 규모 상위 20개')
        v=df.dropna(subset=['oi_usd']).nlargest(20,'oi_usd').sort_values('oi_usd')
        if v.empty:
            st.info('OI 데이터 미확보')
        else:
            fig=px.bar(v,x='oi_usd',y='name',orientation='h',
                       color_discrete_sequence=['#be773e'],labels={'oi_usd':'OI (USD)','name':''})
            fig.update_traces(marker_line_width=0)
            harvest_linear_bar(fig,v['oi_usd'],height=max(330,len(v)*36+90))
            st.plotly_chart(readable_plotly(fig),use_container_width=True,theme=None)
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
    radar_dataframe(readable_metrics(v[sc], rename={'name':'거래소','chains':'체인','listed_date':'목록 등록일','volume_24h_usd':'24H Volume','volume_7d_usd':'7D Volume','oi_usd':'OI','active_wallets_7d':'7D 지갑','weekly_points':'주간 포인트'}),hide_index=True,use_container_width=True,height=570)
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
      {'프로젝트':'Nado','시장 데이터':'CCXT OI 수집 성공','미확보 데이터':'24H 거래량·활성 지갑·포인트','원인 구분':'OI는 실측 · 24H 거래량 CCXT 조회 결과 미확보','Dune':'인덱서/컨트랙트 이벤트 식별 필요','자료':'https://docs.nado.xyz/'},
      {'프로젝트':'StandX','시장 데이터':'공식 API 일부 연결','미확보 데이터':'7일 활성 거래자·실제 포인트','원인 구분':'API에 파밍 전체 활동 데이터 없음/미확인','Dune':'거래 엔진과 온체인 자금 이동 분리 필요','자료':'https://docs.standx.com/'},
      {'프로젝트':'N1','시장 데이터':'거래소 식별 미확정','미확보 데이터':'전체','원인 구분':'프로젝트·체인 식별 먼저 필요','Dune':'정확한 컨트랙트/체인 필요','자료':'https://dune.com/'},
      {'프로젝트':'Arcus','시장 데이터':'공식 공개 시장목록 조사','미확보 데이터':'거래량·OI·활성 지갑·포인트','원인 구분':'REST 일부 공개 · 집계 어댑터 미작성','Dune':'Robinhood Chain 컨트랙트 이벤트 확인 필요','자료':'https://api.arcus.xyz/v1/markets'},
      {'프로젝트':'Entropy','시장 데이터':'Hyperliquid HIP-3 io Volume·OI 수집 성공','미확보 데이터':'활성 지갑·포인트','원인 구분':'공식 io 전용 시장 24H 거래량·OI 확인','Dune':'프로젝트/컨트랙트 식별 필요','자료':'https://dune.com/'},
      {'프로젝트':'QFEX','시장 데이터':'공개 market CLI 확인','미확보 데이터':'거래소 통합 거래량·OI·지갑·포인트','원인 구분':'무료 시장 명령 제공 · 직접 집계 구현 필요','Dune':'거래 엔진 거래 내역이 온체인인지 확인 필요','자료':'https://github.com/QFEX-org/cli'},
      {'프로젝트':'TxFlow','시장 데이터':'컨트랙트 매핑 대기','미확보 데이터':'전체','원인 구분':'이벤트/체인/주소 검증 필요','Dune':'트레이드 이벤트 있으면 SQL 집계 가능','자료':'https://dune.com/'},
      {'프로젝트':'Lighter RH','시장 데이터':'Robinhood Chain 전용 24H Volume 수집 성공','미확보 데이터':'OI·활성 지갑·포인트','원인 구분':'RH 전용 endpoint에서 거래량 확인 · OI USD 산식 검증 필요','Dune':'체인 입출금만으로 롤업 체결 복원 불가','자료':'https://docs.lighter.xyz/'},
      {'프로젝트':'Pacifica','시장 데이터':'공식 무료 시장목록 조회','미확보 데이터':'집계 거래량·OI·활성 지갑·포인트','원인 구분':'무료 공개 API 있음 · 지표별 검증/집계 필요','Dune':'거래가 오프체인이면 실제 거래지갑 집계 한계','자료':'https://pacifica.gitbook.io/docs/api-documentation/api/rest-api/markets/get-market-info'},
      {'프로젝트':'hello.trade','시장 데이터':'공식 소스 미확인','미확보 데이터':'전체','원인 구분':'자료 유료/비공개 여부도 미확인','Dune':'프로젝트/컨트랙트 식별 필요','자료':'https://dune.com/'}
    ]
    radar_dataframe(pd.DataFrame(investigation),hide_index=True,use_container_width=True,height=435,
                 column_config={'자료':st.column_config.LinkColumn('검증 참고 링크')})
    st.download_button('미연동 조사표 CSV',pd.DataFrame(investigation).to_csv(index=False).encode('utf-8-sig'),'integration_audit.csv','text/csv')
    st.markdown('### 신규 공개 API 확인')
    checks=[
      {'거래소':'Pacifica','상태':'시장 목록 조회 성공' if pacifica_markets is not None else '연결 실패/응답 변경',
       '확인된 시장 수':pacifica_markets,'의미':'상장된 마켓 개수만 의미. 거래량·OI·지갑 수 아님'},
      {'거래소':'Arcus','상태':'시장/OI 조회 성공' if arcus_stats is not None else '연결 실패/응답 변경',
       '확인된 시장 수':arcus_stats.get('markets') if arcus_stats else None,'의미':'PERPETUAL 한쪽 OI×가격만 집계. 거래량·포인트 아님'}
    ]
    radar_dataframe(pd.DataFrame(checks),hide_index=True,use_container_width=True)
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
    radar_dataframe(pd.DataFrame(status_rows),hide_index=True,use_container_width=True)
    st.caption('조회 성공은 전체 시장의 데이터가 제공되었다는 보장이 아닙니다. 개별 거래소와 체인 식별을 먼저 확인해야 합니다.')
# Points efficiency simulator intentionally removed from public tabs.
# Actual point pool, eligibility and competition inputs are not yet verified.

st.divider()
st.caption('v1.2 Rankings Cloud Dancer · Read-only research dashboard · 에어드랍 수익 보장 없음 · 주문/지갑 서명 기능 없음')
