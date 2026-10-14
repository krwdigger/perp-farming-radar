"""Scheduled public-market snapshots. No keys, wallets or trading actions."""
import csv
import datetime as dt
import json
import os
import pathlib
import urllib.request

OUT=pathlib.Path(__file__).resolve().parent/"data"/"market_snapshots.csv"
FIELDS=["captured_at_utc","venue","volume_24h_usd","oi_usd","markets","source","status","error"]
HEADERS={"User-Agent":"Perp-Farming-Radar/1.0","Accept":"application/json"}

def get(url):
    req=urllib.request.Request(url,headers=HEADERS)
    with urllib.request.urlopen(req,timeout=22) as res:
        return json.load(res)

def numeric(x):
    try: return float(x) if x is not None else None
    except (ValueError,TypeError):return None

def pull(venue,source,fn):
    try:
        d=fn()
        return {"venue":venue,"source":source,"volume_24h_usd":d.get("volume_24h_usd"),
                "oi_usd":d.get("oi_usd"),"markets":d.get("markets"),"status":"ok","error":""}
    except Exception as e:
        return {"venue":venue,"source":source,"volume_24h_usd":None,"oi_usd":None,
                "markets":None,"status":"error","error":str(e)[:180]}

def variational():
    d=get("https://omni-client-api.prod.ap-northeast-1.variational.io/metadata/stats")
    return dict(volume_24h_usd=numeric(d.get("total_volume_24h")),oi_usd=numeric(d.get("open_interest")))

def standx():
    d=get("https://perps.standx.com/api/query_market_overview")["summary"]
    return dict(volume_24h_usd=numeric(d.get("volume_quote_24h")),oi_usd=numeric(d.get("open_interest_notional")))

def pacifica():
    d=get("https://api.pacifica.fi/api/v1/info")
    if not isinstance(d,dict) or d.get("success") is not True or not isinstance(d.get("data"),list):
        raise ValueError("Unexpected market-info schema")
    return dict(markets=len(d["data"]))

def arcus():
    d=get("https://api.arcus.xyz/v1/markets")
    markets=d if isinstance(d,list) else d.get("markets") if isinstance(d,dict) else None
    if not isinstance(markets,list):raise ValueError("Unexpected market-list schema")
    perps=[m for m in markets if isinstance(m,dict) and m.get("type")=="PERPETUAL"]
    notionals=[]
    for m in perps:
        size=numeric(m.get("openInterest"));price=numeric(m.get("markPrice"))
        if size is not None and price is not None:notionals.append(size*price)
    volume=[numeric(m.get("volume24hNotional")) for m in perps]
    volume=[v for v in volume if v is not None and v>=0]
    return dict(markets=len(perps),oi_usd=sum(notionals) if notionals else None,
                volume_24h_usd=sum(volume) if volume else None)

def pacifica_oi():
    import ccxt
    exchange=ccxt.pacifica({"enableRateLimit":True,"timeout":16000})
    records=exchange.fetch_open_interests()
    if isinstance(records,dict):records=list(records.values())
    if not isinstance(records,list):raise ValueError("OI response not a list")
    values=[]
    for record in records:
        if not isinstance(record,dict):continue
        val=numeric(record.get("openInterestValue"))
        if val is not None and val>=0:values.append(val)
    if not values:raise ValueError("No USD-denominated open interest values")
    return {"oi_usd":sum(values),"markets":len(values)}

def ccxt_perp_volume(venue):
    import ccxt
    exchange=getattr(ccxt,venue)({"enableRateLimit":True,"timeout":16000})
    markets=exchange.load_markets()
    tickers=exchange.fetch_tickers()
    volume=0.0
    count=0
    for symbol,ticker in tickers.items():
        market=markets.get(symbol)
        if not isinstance(market,dict) or not market.get("swap") or market.get("active") is False:
            continue
        if str(market.get("quote") or "").upper() not in ("USD","USDC","USDT"):
            continue
        if str(market.get("settle") or "").upper() not in ("USD","USDC","USDT"):
            continue
        q=numeric(ticker.get("quoteVolume"))
        if q is None:
            base=numeric(ticker.get("baseVolume"))
            price=numeric(ticker.get("last"))
            if base is not None and price is not None:q=base*price
        if q is not None and q>=0:
            volume+=q
            count+=1
    if not count:raise ValueError("No USD perpetual markets with public 24h turnover")
    return {"volume_24h_usd":volume,"markets":count}

def pacifica_volume():
    return ccxt_perp_volume("pacifica")

def nado_volume():
    return ccxt_perp_volume("nado")

def nado_oi():
    import ccxt
    exchange=ccxt.nado({"enableRateLimit":True,"timeout":16000})
    records=exchange.fetch_open_interests()
    if isinstance(records,dict):records=list(records.values())
    if not isinstance(records,list):raise ValueError("Expected CCXT open interest records")
    values=[]
    for record in records:
        if not isinstance(record,dict):continue
        amount=numeric(record.get("openInterestValue"))
        if amount is None:
            info=record.get("info")
            if isinstance(info,dict):amount=numeric(info.get("open_interest_usd"))
        if amount is not None and amount>=0:values.append(amount)
    if not values:raise ValueError("No explicit USD open interest notional in Nado records")
    return {"oi_usd":sum(values),"markets":len(values)}

def hyperliquid():
    payload=json.dumps({"type":"metaAndAssetCtxs"}).encode()
    req=urllib.request.Request("https://api.hyperliquid.xyz/info",headers={**HEADERS,"Content-Type":"application/json"},data=payload)
    with urllib.request.urlopen(req,timeout=22) as res:meta,contexts=json.load(res)
    volume=sum((numeric(x.get("dayNtlVlm")) or 0) for x in contexts)
    oi=sum((numeric(x.get("markPx")) or 0)*(numeric(x.get("openInterest")) or 0) for x in contexts)
    return dict(volume_24h_usd=volume,oi_usd=oi,markets=len(contexts))

def main():
    now=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    probes=[
       ("Variational","Official Omni API",variational),
       ("StandX","Official public overview",standx),
       ("Pacifica","Official public market info (market count only)",pacifica),
       ("Pacifica","CCXT unified fetchOpenInterests",pacifica_oi),
       ("Nado","CCXT unified fetchOpenInterests",nado_oi),
       ("Pacifica","CCXT public perpetual 24h ticker turnover",pacifica_volume),
       ("Nado","CCXT public perpetual 24h ticker turnover",nado_volume),
       ("Arcus","Official /v1/markets perpetual 24h notional",arcus),
       ("Hyperliquid","Official info API",hyperliquid)]
    rows=[]
    for venue,source,fn in probes:
        row=pull(venue,source,fn);row["captured_at_utc"]=now;rows.append(row)
    OUT.parent.mkdir(parents=True,exist_ok=True)
    exists=OUT.exists()
    with OUT.open("a",newline="",encoding="utf-8") as f:
        writer=csv.DictWriter(f,fieldnames=FIELDS)
        if not exists:writer.writeheader()
        writer.writerows(rows)
    print(json.dumps({"captured_at":now,"results":[{"venue":r["venue"],"status":r["status"],"error":r["error"]} for r in rows]}))
if __name__=="__main__":main()
