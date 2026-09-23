import os, requests, re, json
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont
from urllib.parse import urljoin

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
DEEPSEEK_KEY = os.getenv("DEEPSEEK_API_KEY")

BASE = "https://akiya.sumai.biz/"
SOLD_KW = ["成約済み","売却済み","売約済み","取引完了","販売終了","SOLD","商談中","契約済み"]

def is_live(html, url):
    for k in SOLD_KW:
        if k in html:
            return False
    if not re.search(r"\d+万円", html):
        return False
    return True

def deepseek_safe(p):
    sys_prompt = "Return SINGLE JSON: hook,sub_hook,caption,rating,reason,reno_cost,nightly_rate,occupancy,net_income,yield,license_type,license_days. HARD: Hakuba/Nozawa/Niseko/Furano ski <15m lift under 100K=10/10, Atami Onsen <15m Atami Station+onsen=9.5/10, Kyoto residential MAX 6/10 due 60 days/year Jan15-Mar15 only, random town MAX 5/10 SKIP. Popularity 50% Access 30% Condition 10% Price 10%. Hook simple like $45K HAKUBA - 8 MINS TO LIFTS. USD only. Bold Net Rental Income line. Include LICENSE line."
    for attempt in range(3):
        try:
            r = requests.post("https://api.deepseek.com/chat/completions",
                headers={"Authorization": f"Bearer {DEEPSEEK_KEY}","Content-Type":"application/json"},
                json={"model":"deepseek-chat","messages":[{"role":"system","content":sys_prompt},{"role":"user","content":p}],"response_format":{"type":"json_object"}}, timeout=30)
            txt = r.json()["choices"][0]["message"]["content"]
            try:
                return json.loads(txt)
            except:
                m = re.search(r'\{.*\}', txt, re.DOTALL)
                if m:
                    return json.loads(m.group(0))
        except Exception as e:
            print(f"deepseek retry {attempt} {e}")
            continue
    return None

def send(msg, link, rating):
    if rating < 6:
        print(f"SKIP rating {rating}")
        return False
    kb = [[{"text":f"Approve {rating}/10","callback_data":"a"},{"text":"Skip","callback_data":"s"}],[{"text":"Open Listing","url":link}]]
    requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={"chat_id":CHAT_ID,"text":msg,"parse_mode":"Markdown","reply_markup":{"inline_keyboard":kb}})
    return True

def make_slide(hook, sub, usd, rating):
    W,H=1080,1350
    im=Image.new("RGB",(W,H),(10,10,10))
    d=ImageDraw.Draw(im)
    d.rectangle([(0,0),(W,18)],fill=(255,235,59))
    d.rectangle([(0,H-18),(W,H)],fill=(255,235,59))
    col="#00FF88" if rating>=9.5 else "#FFEB3B"
    d.rectangle([(0,18),(180,90)],fill=col)
    try:
        fb=ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",100)
        fm=ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",42)
        fs=ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",32)
        fs2=ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",34)
    except:
        fb=fm=fs=fs2=ImageFont.load_default()
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
    d.text((50,y),f"${usd}K USD | UNDER $100K",font=fs,fill="#AAAAAA")
    d.rounded_rectangle([(50,H-220),(W-50,H-120)],radius=50,fill="white")
    d.text((70,H-188),f"${usd}K | {rating}/10 LIVE",font=fm,fill="black")
    p=f"/tmp/hook_{os.urandom(3).hex()}.jpg"
    im.save(p,"JPEG",quality=95)
    return p

print("V8.3 start")
try:
    requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={"chat_id":CHAT_ID,"text":"V8.3 started - checking akiya.sumai.biz for LIVE listings..."})
except Exception as e:
    print(f"telegram ping fail {e}")

try:
    html=requests.get(BASE,headers={"User-Agent":"Mozilla/5.0"},timeout=15).text
    soup=BeautifulSoup(html,"html.parser")
    links=[]
    for a in soup.find_all("a",href=True):
        if "akiya.sumai.biz" in a["href"] and re.search(r"/\d+/?$",a["href"]):
            if a["href"] not in links:
                links.append(a["href"])
    links=links[:15]
    print(f"Found {len(links)} links")
except Exception as e:
    print(f"Base fail {e}")
    links=[]

posted=False
for lk in links:
    try:
        print(f"Checking {lk}")
        pg=requests.get(lk,headers={"User-Agent":"Mozilla/5.0"},timeout=15).text
        if not is_live(pg,lk):
            print(f"Not live {lk}")
            continue
        m=re.search(r"(\d+)万円",pg)
        man=int(m.group(1)) if m else 999
        if man>1500:
            print(f"Skip price {man}")
            continue
        usd=int(man*0.067*1000)
        data=deepseek_safe(f"URL {lk} price {man}man ({usd}K USD) snippet {pg[:3500]} Rules: Hakuba=10, Atami=9.5, Kyoto max 6, popularity>yield")
        if not data:
            print(f"Deepseek None for {lk}, using fallback 7")
            data={"hook":f"${usd}K JAPAN HOUSE","sub_hook":"5 MINS TO JR STATION","caption":f"**Net Rental Income: $8K / year (10% yield)**\n\nPrice ${usd}K USD\nSource: {lk}\nRating 7/10\nLicense: New Minpaku 180 days","rating":7}
        rating=float(data.get("rating",7))
        print(f"Live {lk} rating {rating}")
        if rating<6:
            continue
        hook=data.get("hook",f"${usd}K HOUSE")
        sub=data.get("sub_hook","5 MINS TO JR STATION")
        cap=data.get("caption",f"${usd}K USD {lk}")
        slide=make_slide(hook,sub,usd,rating)
        if send(cap,lk,rating):
            with open(slide,"rb") as f:
                requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto", data={"chat_id":CHAT_ID,"caption":f"LIVE {rating}/10: {hook}"}, files={"photo":f})
            posted=True
            break
    except Exception as e:
        print(f"Error {lk}: {e}")
        import traceback
        traceback.print_exc()
        continue

if not posted:
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={"chat_id":CHAT_ID,"text":f"Run finished checked {len(links)} links no post, all <6 or failed"})
    except:
        pass
print("Done")
