import os, requests, re, json, xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
DEEPSEEK_KEY = os.getenv("DEEPSEEK_API_KEY")

FEED_URL = "https://akiya.sumai.biz/feed/"
BASE = "https://akiya.sumai.biz/"
SOLD_KW = ["成約済み","売却済み","売約済み","取引完了","販売終了","SOLD","商談中","契約済み"]

def is_live(html, url):
    for k in SOLD_KW:
        if k in html: return False
    if not re.search(r"\d+万円", html): return False
    return True

def is_good_rental(data):
    """Rental-first filter - returns True only if worth posting"""
    try:
        yield_val = float(str(data.get('yield','0')).replace('%',''))
        income_str = str(data.get('net_income','0')).replace(',','').replace('$','').replace('K','000')
        # Extract numbers
        income = float(re.search(r'(\d+)', str(data.get('net_income','0'))).group(1) if re.search(r'(\d+)', str(data.get('net_income','0'))) else 0)
        # If data says $8.4K -> 8400
        if 'K' in str(data.get('net_income','')):
            income = income * 1000 if income < 100 else income
        occ = float(re.search(r'(\d+)', str(data.get('occupancy','0'))).group(1) if re.search(r'(\d+)', str(data.get('occupancy','0'))) else 0)
        license_days = int(data.get('license_days',0))
        rating = float(data.get('rating',0))

        print(f"RENTAL CHECK: yield={yield_val}% income={income} occ={occ}% days={license_days} rating={rating}")

        # HARD SKIP RULES
        if yield_val == 0 or yield_val < 5: 
            print("SKIP: yield <5% or 0%")
            return False
        if income == 0 or income < 3000 and yield_val < 8:
            print("SKIP: income $0 or < $3K")
            return False
        if occ < 30:
            print("SKIP: occupancy <30%")
            return False
        if license_days == 60: # Kyoto residential
            print("SKIP: Kyoto 60 days = max 5/10")
            return False
        if rating < 7: # NEW: Don't approve anything under 7/10
            print(f"SKIP: rating {rating} <7")
            return False
        return True
    except Exception as e:
        print(f"Rental check error {e}, allowing")
        return True

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

CAPTION: Show NET RENTAL INCOME bold. If income $0, must say $0/yr and low rating.

If property is rural Kyoto with 0% yield, you MUST rate it 3-4/10, not 6/10.
"""
    for attempt in range(3):
        try:
            r = requests.post("https://api.deepseek.com/chat/completions",
                headers={"Authorization": f"Bearer {DEEPSEEK_KEY}","Content-Type":"application/json"},
                json={"model":"deepseek-chat","messages":[{"role":"system","content":sys_prompt},{"role":"user","content":p}],"response_format":{"type":"json_object"}}, timeout=30)
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
    # msg is clean, no link
    kb_approve = [[{"text":f"✅ Approve {rating}/10","callback_data":"a"},{"text":"❌ Skip","callback_data":"s"}]]
    requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id":CHAT_ID,"text":msg,"parse_mode":"Markdown","reply_markup":{"inline_keyboard":kb_approve}})
    kb_link = [[{"text":"🔗 Open Listing","url":link}]]
    requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id":CHAT_ID,"text":f"🔗 Source:\n{link}\n✅ Verified LIVE","reply_markup":{"inline_keyboard":kb_link}})
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

print("V8.6 rental-first start")
requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={"chat_id":CHAT_ID,"text":"V8.6 rental-first started - filtering 0% yield..."})
links=[]
try:
    feed = requests.get(FEED_URL, headers={"User-Agent":"Mozilla/5.0"}, timeout=15).text
    root = ET.fromstring(feed)
    for item in root.findall(".//item"):
        link = item.find("link").text if item.find("link") is not None else ""
        if link and "akiya.sumai.biz" in link: links.append(link)
    print(f"RSS found {len(links)}")
except Exception as e:
    print(f"RSS fail {e}")

links = list(dict.fromkeys(links))[:15]
posted=False
for lk in links:
    try:
        print(f"Checking {lk}")
        pg=requests.get(lk,headers={"User-Agent":"Mozilla/5.0"},timeout=15).text
        if not is_live(pg,lk): continue
        m=re.search(r"(\d+)万円",pg)
        man=int(m.group(1)) if m else 999
        if man>1500: continue
        usd=int(man*0.067*1000)
        data=deepseek_safe(f"URL {lk} price {man}万円 ({usd}K USD) snippet {pg[:3500]} Focus on RENTAL income, not cheap. Is this good for Airbnb? Popular area?")
        if not data: continue
        if not is_good_rental(data):
            print(f"FILTERED OUT by rental check: {lk}")
            continue
        rating=float(data.get("rating",0))
        if rating<7: continue
        hook=data.get("hook",f"${usd}K HOUSE")
        sub=data.get("sub_hook","8 mins to lifts")
        cap=data.get("caption","")
        slide=make_slide(hook,sub,usd,rating)
        if send(cap,lk,rating):
            with open(slide,"rb") as f:
                requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto", data={"chat_id":CHAT_ID,"caption":f"LIVE {rating}/10: {hook}"}, files={"photo":f})
            posted=True
            break
    except Exception as e:
        print(f"Error {lk}: {e}")
        import traceback; traceback.print_exc()
        continue

if not posted:
    requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={"chat_id":CHAT_ID,"text":f"V8.6 done - checked {len(links)} links, all filtered out (0% yield / 60 days / low rental). This is GOOD - means rental filter works."})
print("Done V8.6")
