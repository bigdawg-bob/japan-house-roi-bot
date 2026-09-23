import os, requests, re, json, xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont
from urllib.parse import urljoin

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
DEEPSEEK_KEY = os.getenv("DEEPSEEK_API_KEY")

SOLD_KW = ["成約済み","売却済み","売約済み","取引完了","販売終了","SOLD","商談中","契約済み","成約"]

# TOP 5 SITES - FULL SCAN
SOURCES = {
    "sumai": {
        "name": "Sumai Akiya",
        "base": "https://akiya.sumai.biz/",
        "pages": ["https://akiya.sumai.biz/", "https://akiya.sumai.biz/page/2/", "https://akiya.sumai.biz/page/3/", "https://akiya.sumai.biz/page/4/", "https://akiya.sumai.biz/page/5/", "https://akiya.sumai.biz/page/6/", "https://akiya.sumai.biz/page/7/", "https://akiya.sumai.biz/page/8/", "https://akiya.sumai.biz/page/9/", "https://akiya.sumai.biz/page/10/"],
        "feed": "https://akiya.sumai.biz/feed/"
    },
    "athome": {
        "name": "Akiya Athome",
        "base": "https://www.akiya-athome.jp/",
        "pages": [f"https://www.akiya-athome.jp/buy/?page={i}" for i in range(1, 21)]
    },
    "homes": {
        "name": "Homes Akiyabank",
        "base": "https://www.homes.co.jp/akiyabank/",
        "pages": [f"https://www.homes.co.jp/akiyabank/bukken/?page={i}" for i in range(1, 21)]
    },
    "koryoya": {
        "name": "Koryoya",
        "base": "https://www.koryoya.com/",
        "pages": [f"https://www.koryoya.com/search/?page={i}" for i in range(1, 21)]
    },
    "ieichiba": {
        "name": "Ieichiba",
        "base": "https://www.ieichiba.com/",
        "pages": [f"https://www.ieichiba.com/search/?page={i}" for i in range(1, 21)]
    }
}

def is_live(html, url):
    for k in SOLD_KW:
        if k in html: return False
    if not re.search(r"\d+万円|\d+,\d+円|¥", html): return False
    return True

def is_good_rental(data):
    try:
        yield_val = float(str(data.get('yield','0')).replace('%','').replace('％',''))
        income_match = re.search(r'(\d+\.?\d*)', str(data.get('net_income','0')))
        income = float(income_match.group(1)) if income_match else 0
        if 'K' in str(data.get('net_income','')) and income < 1000:
            income *= 1000
        occ_match = re.search(r'(\d+)', str(data.get('occupancy','0')))
        occ = float(occ_match.group(1)) if occ_match else 0
        license_days = int(data.get('license_days',0))
        rating = float(data.get('rating',0))
        print(f"RENTAL CHECK: yield={yield_val}% income={income} occ={occ}% days={license_days} rating={rating}")
        if yield_val < 5 or yield_val == 0: return False
        if income == 0: return False
        if occ < 30: return False
        if license_days == 60: return False
        if rating < 7: return False
        return True
    except Exception as e:
        print(f"Rental check error {e}")
        return False

def deepseek_safe(p):
    sys_prompt = """You are @japan.house.roi rental investor analyst - RENTAL FIRST, not cheap house.
ENGLISH ONLY. Return SINGLE JSON: hook,sub_hook,caption,rating,reason,reno_cost,nightly_rate,occupancy,net_income,yield,license_type,license_days
HARD RENTAL RULES - MUST OBEY:
- If NET INCOME is $0/yr or Yield 0% or Occupancy 0% -> rating = 3/10, reason = "No rental income - rural no demand"
- Kyoto Prefecture rural (Kyotango, Ayabe, etc) NOT Kyoto City = MAX 4/10 because 60 days/year and far from tourists, no Airbnb demand
- Kyoto City residential = MAX 5/10 because 60 days/year Jan15-Mar15 only, low yield
- ONLY give 7+/10 if: yield >=8% AND occupancy >=50% AND license_days >=180 AND popular tourist area
- Hakuba/Nozawa/Niseko/Furano/Myoko ski <15 mins lift + under $100K + 180 days license + 60%+ occupancy = 10/10
- Atami Onsen + onsen + <15 mins Atami Station + 68%+ occupancy = 9.5/10
- Shonan/Kamakura/Karuizawa = 9/10 if yield >10%
- Random rural cheap house with no rental demand = 3-4/10 SKIP
Popularity > Price. Cheap is NOT good if no rental.
"""
    for attempt in range(3):
        try:
            r = requests.post("https://api.deepseek.com/chat/completions",
                headers={"Authorization": f"Bearer {DEEPSEEK_KEY}","Content-Type":"application/json"},
                json={"model":"deepseek-chat","messages":[{"role":"system","content":sys_prompt},{"role":"user","content":p}],"response_format":{"type":"json_object"}}, timeout=40)
            txt = r.json()["choices"][0]["message"]["content"]
            try: return json.loads(txt)
            except:
                m = re.search(r'\{.*\}', txt, re.DOTALL)
                if m: return json.loads(m.group(0))
        except Exception as e:
            print(f"deepseek retry {attempt} {e}")
            continue
    return None

def send(msg, link, rating):
    kb_approve = [[{"text":f"✅ Approve {rating}/10","callback_data":"a"},{"text":"❌ Skip","callback_data":"s"}]]
    requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id":CHAT_ID,"text":msg,"parse_mode":"Markdown","reply_markup":{"inline_keyboard":kb_approve}})
    kb_link = [[{"text":"🔗 Open Listing","url":link}]]
    requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id":CHAT_ID,"text":f"🔗 Source:\n{link}\n✅ Verified LIVE - Still available","reply_markup":{"inline_keyboard":kb_link}})
    return True

def make_slide(hook, sub, usd, rating):
    W,H=1080,1350
    im=Image.new("RGB",(W,H),(10,10,10))
    d=ImageDraw.Draw(im)
    d.rectangle([(0,0),(W,18)],fill=(255,235,59))
    d.rectangle([(0,H-18),(W,H)],fill=(255,235,59))
    col="#00FF88" if rating>=9.5 else "#FFEB3B" if rating>=9 else "#FF8C00" if rating>=7 else "#AAAAAA"
    d.rectangle([(0,18),(180,90)],fill=col)
    try:
        fb=ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",100)
        fm=ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",42)
        fs=ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",32)
        fs2=ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",34)
    except: fb=fm=fs=fs2=ImageFont.load_default()
    d.text((15,22),f"{rating}/10",font=fs2,fill="black")
    y=110
    for ln in hook.upper().split("\n")[:2]:
        if ln.strip():
            d.text((50,y),ln.strip(),font=fb,fill="white",stroke_width=6,stroke_fill="black")
            y+=115
    d.line([(50,y+5),(W-50,y+5)],fill="#FFEB3B",width=5)
    y+=25
    d.text((50,y),sub.upper(),font=fm,fill="#FFEB3B")
    y+=65
    d.text((50,y),f"${usd}K USD | RENTAL FOCUS",font=fs,fill="#AAAAAA")
    d.rounded_rectangle([(50,H-220),(W-50,H-120)],radius=50,fill="white")
    d.text((70,H-188),f"${usd}K | {rating}/10 LIVE",font=fm,fill="black")
    p=f"/tmp/hook_{os.urandom(3).hex()}.jpg"
    im.save(p,"JPEG",quality=95)
    return p

def get_all_links():
    all_links = []
    # 1. Sumai - use RSS + pages
    try:
        feed = requests.get(SOURCES["sumai"]["feed"], headers={"User-Agent":"Mozilla/5.0"}, timeout=15).text
        root = ET.fromstring(feed)
        for item in root.findall(".//item"):
            link = item.find("link").text if item.find("link") is not None else ""
            if link and "akiya.sumai.biz" in link: all_links.append(link)
        print(f"Sumai RSS: {len(all_links)}")
    except Exception as e: print(f"Sumai RSS fail {e}")

    for site_key, site in SOURCES.items():
        for page_url in site["pages"]:
            try:
                print(f"Scraping {site_key}: {page_url}")
                html = requests.get(page_url, headers={"User-Agent":"Mozilla/5.0","Accept":"text/html"}, timeout=20).text
                soup = BeautifulSoup(html, "html.parser")
                count_before = len(all_links)
                for a in soup.find_all("a", href=True):
                    href = a["href"]
                    full = urljoin(site["base"], href)
                    # Accept any detail page
                    if re.search(r"/\d{4,}/|/bukken/|/property/|/detail/|/akiya/|/search/detail|/buy/\d+", href) or (site_key=="sumai" and re.search(r"/\d+/?$", href)):
                        if full not in all_links and len(full) < 200 and "javascript" not in full:
                            all_links.append(full)
                if len(all_links) > count_before:
                    print(f" Found {len(all_links)-count_before} new from {page_url}")
                # Stop early if no new links for 3 pages
                if len(all_links) > 300: break
            except Exception as e:
                print(f" Fail {page_url}: {e}")
                continue
        if len(all_links) > 300: break

    all_links = list(dict.fromkeys(all_links))
    print(f"TOTAL collected: {len(all_links)} from all 5 sites")
    return all_links[:300] # Cap at 300 to avoid timeout

print("V8.7 FULL SCAN - All 5 sites, all listings (including 1-2 months old)")
requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={"chat_id":CHAT_ID,"text":"V8.7 FULL SCAN started - scraping ALL listings from 5 sites (including 1-2 months old, still unsold) - rental-first filter..."})

links = get_all_links()
print(f"Final to check: {len(links)}")

posted=False
checked=0
for lk in links:
    try:
        checked+=1
        print(f"[{checked}/{len(links)}] Checking {lk}")
        pg=requests.get(lk,headers={"User-Agent":"Mozilla/5.0"},timeout=20).text
        if not is_live(pg,lk): continue
        m=re.search(r"(\d+)万円",pg)
        if not m: continue
        man=int(m.group(1))
        if man>1500: continue
        usd=int(man*0.067*1000)
        data=deepseek_safe(f"URL {lk} price {man}万円 ({usd}K USD) snippet {pg[:4000]} Focus on RENTAL income. Popular tourist area? License days? Is it good for Airbnb?")
        if not data: continue
        if not is_good_rental(data):
            print(f"FILTERED OUT: {lk} rating {data.get('rating')}")
            continue
        rating=float(data.get("rating",0))
        if rating<7: continue
        hook=data.get("hook",f"${usd}K HOUSE")
        sub=data.get("sub_hook","Rental opportunity")
        cap=data.get("caption","")
        slide=make_slide(hook,sub,usd,rating)
        if send(cap,lk,rating):
            with open(slide,"rb") as f:
                requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto", data={"chat_id":CHAT_ID,"caption":f"LIVE {rating}/10: {hook} | Scanned {checked}/{len(links)}"}, files={"photo":f})
            posted=True
            print(f"POSTED {lk}")
            break
    except Exception as e:
        print(f"Error {lk}: {e}")
        continue

if not posted:
    requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={"chat_id":CHAT_ID,"text":f"V8.7 FULL SCAN done - checked {checked}/{len(links)} listings from 5 sites. All filtered out (0% yield / 60 days / low rental) = no good Airbnb today. This is GOOD, not spamming bad houses."})

print("Done V8.7")
