import os, re, time, threading, csv, io, hashlib
from datetime import datetime, timezone
import requests
from bs4 import BeautifulSoup
import psycopg
from flask import Flask, Response, jsonify

TARGET_URL=os.getenv('TARGET_URL','https://e-rang.kr/api/coin.php')
INTERVAL=max(60,int(os.getenv('INTERVAL_SECONDS','60')))
DATABASE_URL=os.environ['DATABASE_URL']
UA=os.getenv('USER_AGENT','CoinMonitor/1.0 (+public-page-observation)')
app=Flask(__name__)

def conn(): return psycopg.connect(DATABASE_URL)

def init_db():
    with conn() as c, c.cursor() as cur:
        cur.execute('''CREATE TABLE IF NOT EXISTS observations(
          id BIGSERIAL PRIMARY KEY, observed_at TIMESTAMPTZ NOT NULL,
          http_status INT, success BOOLEAN NOT NULL DEFAULT FALSE,
          current_price NUMERIC, parsed_json JSONB, raw_text TEXT,
          raw_html TEXT, content_sha256 TEXT, error TEXT,
          long_signal BOOLEAN, short_signal BOOLEAN, long_color TEXT, short_color TEXT, entry_message BOOLEAN)''')
        cur.execute('CREATE INDEX IF NOT EXISTS idx_obs_time ON observations(observed_at DESC)')
        for ddl in [
            'ALTER TABLE observations ADD COLUMN IF NOT EXISTS long_signal BOOLEAN',
            'ALTER TABLE observations ADD COLUMN IF NOT EXISTS short_signal BOOLEAN',
            'ALTER TABLE observations ADD COLUMN IF NOT EXISTS long_color TEXT',
            'ALTER TABLE observations ADD COLUMN IF NOT EXISTS short_color TEXT',
            'ALTER TABLE observations ADD COLUMN IF NOT EXISTS entry_message BOOLEAN']:
            cur.execute(ddl)

from parser import parse_page

def collect_once():
    ts=datetime.now(timezone.utc)
    status=None; html=''; error=None; parsed={}; success=False; cp=None
    try:
        r=requests.get(TARGET_URL,headers={'User-Agent':UA,'Accept':'text/html,*/*'},timeout=20)
        status=r.status_code; html=r.text
        r.raise_for_status()
        parsed=parse_page(html)
        raw=parsed.get('current_price_raw')
        if raw: cp=float(raw.replace(',',''))
        success=True
    except Exception as e: error=f'{type(e).__name__}: {e}'
    sha=hashlib.sha256(html.encode('utf-8','replace')).hexdigest() if html else None
    with conn() as c, c.cursor() as cur:
        sig=parsed.get('signals',{})
        ls=sig.get('long',{}); ss=sig.get('short',{})
        cur.execute('''INSERT INTO observations(observed_at,http_status,success,current_price,parsed_json,raw_text,raw_html,content_sha256,error,long_signal,short_signal,long_color,short_color,entry_message)
          VALUES(%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
          (ts,status,success,cp,__import__('json').dumps(parsed,ensure_ascii=False),parsed.get('page_text'),html,sha,error,ls.get('active'),ss.get('active'),ls.get('detected_color'),ss.get('detected_color'),parsed.get('entry_message')))

def loop():
    while True:
        started=time.monotonic()
        collect_once()
        time.sleep(max(1,INTERVAL-(time.monotonic()-started)))

@app.get('/')
def home(): return jsonify(service='coin-monitor',target=TARGET_URL,interval_seconds=INTERVAL,endpoints=['/status','/export.csv'])

@app.get('/status')
def status():
    with conn() as c, c.cursor() as cur:
        cur.execute('SELECT count(*),count(*) FILTER(WHERE success) FROM observations'); total,ok=cur.fetchone()
        cur.execute('SELECT observed_at,http_status,success,current_price,error,parsed_json FROM observations ORDER BY id DESC LIMIT 1'); last=cur.fetchone()
    return jsonify(total=total,successful=ok,last={'observed_at':last[0].isoformat(),'http_status':last[1],'success':last[2],'current_price':str(last[3]) if last[3] is not None else None,'error':last[4],'parsed':last[5]} if last else None)

@app.get('/export.csv')
def export_csv():
    out=io.StringIO(); w=csv.writer(out)
    w.writerow(['id','observed_at','http_status','success','current_price','long_signal','short_signal','long_color','short_color','entry_message','parsed_json','content_sha256','error'])
    with conn() as c, c.cursor() as cur:
        cur.execute('SELECT id,observed_at,http_status,success,current_price,long_signal,short_signal,long_color,short_color,entry_message,parsed_json,content_sha256,error FROM observations ORDER BY id')
        for row in cur: w.writerow(row)
    return Response(out.getvalue(),mimetype='text/csv',headers={'Content-Disposition':'attachment; filename=coin_observations.csv'})

if __name__=='__main__':
    init_db()
    threading.Thread(target=loop,daemon=True).start()
    app.run(host='0.0.0.0',port=int(os.getenv('PORT','8080')))
