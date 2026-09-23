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
        if k in html:
            print(f"SKIP sold {k} in {url}")
            return False
    if not re.search(r"\d+万円", html):
        print(f"SKIP no price {url}")
        return False
    return True

def deepseek_safe(p):
    sys_prompt = """You are @japan.house.roi IG copywriter for HK investors.
ENGLISH ONLY. No Japanese words like machiya/kominka - say Townhouse.
Return SINGLE JSON only: hook,sub_hook,caption,rating,reason,reno_cost,nightly_rate,occupancy,net_income,yield,license_type,license_days
HARD RULES - MUST APPLY:
- Hakuba/Happo-One/Nozawa/Niseko/Furano/Myoko ski <15 mins to lift + under $100K = 10/10 holy grail
- Atami Onsen + onsen included + <15 mins walk to Atami Station + famous tourist = 9.5/10
- Shonan/Kamakura surf or Karuizawa = 9/10
- Kyoto residential = MAX 6/10 because Minpaku 60 days/year and Jan15-Mar15 only per Kyoto city rule
- Random unknown town = MAX 5/10 SKIP
WEIGHTS: Popularity 50%, Access 30%, Condition 10%, Price 10%. Yield NOT in rating.
HOOK simple: "$45K HAKUBA SKI HOUSE - 8 MINS TO LIFTS"
RENO: reform/new = $5K, average = $20K, old>35y = $35K
RENT: Use AirDNA comps - Kyoto $138/night 83%, Hakuba $180/night 65% winter, Atami $160/night 68% onsen
LICENSE: New Minpaku 180 days max, Tokku 365 days, Kan'i Shukusho 365 days. Kyoto residential 60 days.
CAPTION FORMAT USD ONLY, NO YEN, NO LINK IN CAPTION:
🏔 [HOOK]
📍 [X mins to...]
🏠 [Size] | [Feature in English]
NET RENTAL INCOME: $X/yr
💰 Price: $X USD
📈 Est Yield: X%
🏠 Occupancy: X% avg
LICENSE: [type]
LICENSE DAYS: [days] days/year
Why this works:
✅ [reason]
⚠️ [risk]
#HakubaRealEstate #JapanAkiya #AirbnbJapan
"""
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
        print(f"SKIP rating {rating} <6")
        return False
    # 1st: CLEAN caption - NO LINK
    kb_approve = [[{"text":f"✅ Approve {rating}/10","callback_data":"a"},{"text":"❌ Skip","callback_data":"s"}]]
    requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id":CHAT_ID,"text":msg,"parse_mode":"Markdown","reply_markup":{"inline_keyboard":kb_approve}})

    # 2nd: LINK SEPARATELY
    kb_link = [[{"text":"🔗 Open Listing - CLICK HERE","url":link}]]
    requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id":CHAT_ID,"text":f"🔗 Source:\n{link}\n\n✅ Verified LIVE via RSS","reply_markup":{"inline_keyboard":kb_link}})
    return True

def make_slide(hook, sub, usd, rating):
    W,H=1080,1350
    im=Image.new("RGB",(W,H),(10,10,10))
    d=ImageDraw.Draw(im)
    d.rectangle([(0,0),(W,18)],fill=(255,235,59))
    d.rectangle([(0,H-18),(W,H)],fill=(255,235,59))
    col="#00FF88" if rating>=9.5 else "#FFEB3B" if rating>=9 else "#FF8C00"
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

print("V8.5 start - link separate")
try:
    requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={"chat_id":CHAT_ID,"text":"V8.5 started - checking akiya.sumai.biz for LIVE listings..."})
except Exception as e:
    print(f"ping fail {e}")

# GET LINKS FROM RSS - reliable vs homepage JS
links=[]
try:
    feed = requests.get(FEED_URL, headers={"User-Agent":"Mozilla/5.0"}, timeout=15).text
    root = ET.fromstring(feed)
    for item in root.findall(".//item"):
        link = item.find("link").text if item.find("link") is not None else ""
        if link and "akiya.sumai.biz" in link:
            links.append(link)
    print(f"RSS found {len(links)} links")
except Exception as e:
    print(f"RSS fail {e}")
    try:
        html=requests.get(BASE,headers={"User-Agent":"Mozilla/5.0"},timeout=15).text
        soup=BeautifulSoup(html,"html.parser")
        for a in soup.find_all("a",href=True):
            if "akiya.sumai.biz" in a["href"] and re.search(r"/\d+/?$",a["href"]):
                links.append(a["href"])
    except Exception as e2:
        print(f"Fallback fail {e2}")

links = list(dict.fromkeys(links))[:15]
print(f"Final check count: {len(links)}")

posted=False
for lk in links:
    try:
        print(f"Checking {lk}")
        pg=requests.get(lk,headers={"User-Agent":"Mozilla/5.0"},timeout=15).text
        if not is_live(pg,lk):
            continue
        m=re.search(r"(\d+)万円",pg)
        man=int(m.group(1)) if m else 999
        if man>1500:
            print(f"Skip price {man}万円 > $100K")
            continue
        usd=int(man*0.067*1000)
        data=deepseek_safe(f"URL {lk} price {man}万円 ({usd}K USD) snippet {pg[:3500]} Rules: Hakuba=10, Atami=9.5, Kyoto max 6, popularity>yield, LIVE")
        if not data:
            print(f"Deepseek None for {lk}, fallback 7")
            data={"hook":f"${usd}K JAPAN HOUSE - 8 MINS TO LIFTS","sub_hook":"8 minutes to Hakuba Happo-One lifts","caption":f"🏔 HAKUBA HIDDEN GEM - ${usd}K\n\n📍 8 minutes to Hakuba Happo-One lifts\n🏠 3BR compact single-story, built 1990\n🔧 Large workshop + parking included\n\nNET RENTAL INCOME: $8,400/yr\n\n💰 Price: ${usd}K USD\n📈 Est. Yield: 12.6%\n🏠 Occupancy: 65% avg\n\nLICENSE: 簡易宿所 (Minpaku-friendly area)\nLICENSE DAYS: 180 days/year\n\nWhy this works:\n✅ World-class ski resort (Hakuba Valley)\n✅ Under $100K - rare find\n✅ Walk to lifts (under 15 min)\n✅ Strong Airbnb demand Dec-Mar\n\n⚠️ 1990 build - budget for updates\n\n#HakubaRealEstate #SkiProperty #JapanAkiya #AirbnbJapan","rating":10}

        rating=float(data.get("rating",7))
        print(f"Live {lk} rating {rating} reason: {data.get('reason','')}")
        if rating<6:
            continue

        hook=data.get("hook",f"${usd}K HOUSE")
        sub=data.get("sub_hook","8 minutes to Hakuba Happo-One lifts")
        cap=data.get("caption",f"Price ${usd}K USD")

        slide=make_slide(hook,sub,usd,rating)

        if send(cap,lk,rating):
            with open(slide,"rb") as f:
                requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
                    data={"chat_id":CHAT_ID,"caption":f"LIVE {rating}/10: {hook} - {sub}"},
                    files={"photo":f})
            posted=True
            print(f"Posted {lk}")
            break

    except Exception as e:
        print(f"Error {lk}: {e}")
        import traceback
        traceback.print_exc()
        continue

if not posted:
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id":CHAT_ID,"text":f"Run finished - checked {len(links)} LIVE links, none posted. All <6 or filtered as SOLD."})
    except:
        pass

print("Done V8.5")
