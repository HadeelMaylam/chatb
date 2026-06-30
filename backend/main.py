import json
import re
import os
import asyncio
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv
load_dotenv()
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response
from groq import Groq



# ── offers ────────────────────────────────────────────────────────────────────
DATA_PATH = Path(__file__).parent.parent / "sample_clean.json"
with open(DATA_PATH, encoding="utf-8") as f:
    raw = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', f.read())
    OFFERS: list[dict] = json.loads(raw)

# index by merchant name for fast lookup
OFFERS_BY_NAME = {o["merchant"]: o for o in OFFERS}

TODAY = datetime.now().strftime("%d/%m/%Y")

OFFERS_LIST = "\n".join(
    f'- "{o["merchant"]}" | نوع:{o.get("voice_summary","")[:50]} | خصم:{o["discount_value"]}'
    + (f' | كود:{o.get("promo_code","")}' if o.get("promo_code") else "")
    + f' | حتى:{o["valid_to"]}'
    + (f' | url:{o["source_url"]}' if o.get("source_url") else "")
    for o in OFFERS
)
SYSTEM_PROMPT = f"""أنت مساعد بطاقة برق فيزا، تتكلم بأسلوب ودّي وطبيعي بالعربية. اليوم {TODAY}.
مهمتك تساعد المستخدم يلقى عروض التجار المتاحة على البطاقة.

أسلوب الرد:
- رحّب بالمستخدم بشكل قصير وطبيعي في أول رسالة فقط.
- جاوب كإنك إنسان يساعد صديقه، مو كإنك تعبّي استمارة.
- ابدأ بجملة طبيعية تقدّم فيها العرض، بعدها اعرض التفاصيل المهمة بشكل مرتب.
- اذكر فقط المعلومات الموجودة فعلاً في القائمة. أي معلومة ناقصة تجاهلها تماماً — لا تكتب "لا يوجد" أبداً.
- لا تخترع خصومات أو أكواد أو تواريخ أو أي تفصيل مو موجود.

لما تعرض عرض، استخدم فقط الحقول المتوفرة:
- اسم التاجر
- نوع العرض / الفئة (إذا يفيد)
- قيمة الخصم (إذا موجودة)
- كود الخصم (إذا موجود)
- صالح حتى (إذا موجود)
- رابط العرض بهذا الشكل فقط إذا فيه url: [تفاصيل العرض في برق](URL)

مثال للأسلوب المطلوب (كيّفه حسب البيانات المتوفرة، ولا تنسخه حرفياً):
"عندك عرض من {{التاجر}} 👇 خصم {{القيمة}} على {{الفئة}}، استخدم الكود {{الكود}} وصالح لين {{التاريخ}}. تفاصيل أكثر هنا: [تفاصيل العرض في برق](URL)"

إذا العرض ناقص تفاصيل (مثلاً ما فيه قيمة خصم واضحة)، لا توصفه بإنه فاضي — اذكر اللي متوفر، وقل إن باقي التفاصيل في الرابط.

قائمة العروض المتاحة:
{OFFERS_LIST}

المهمة الأساسية: تعطي المستخدم عرض التاجر اللي يسأل عنه.
إذا التاجر مو موجود في القائمة، لا توقف عند "ما فيه عرض" — ساعده يلقى بديل.

منطق الرد:
١) إذا التاجر اللي طلبه له عرض في القائمة ← اعرض العرض حسب أسلوب الردود فوق.

٢) إذا التاجر مو في القائمة:
   - إذا واضح وش نوع المحل اللي يبيه (ذكر اسم محل معروف، أو نوع منتج/خدمة)
     ← مباشرة رشّح له المحلات الموجودة في القائمة اللي تبيع نفس النوع. لا تسأل سؤال زائد.
   - إذا مو واضح وش يبي ← اساله سؤال واحد قصير فقط:
     "وش نوع المنتج أو الخدمة اللي تدور عليها؟" وبناءً على رده رشّح له.

٣) دايماً رشّح أكثر من محل (لين ٥ حسب المتوفر)، مو محل واحد.
   اعرضهم كقائمة مختصرة: اسم المحل + سطر واحد عن عرضه،
   وبعدها اساله: "تبي تفاصيل أي واحد منهم؟"
   إذا ما فيه إلا محل واحد يناسب، اعرضه عادي بدون ما تتظاهر إن فيه خيارات.

قاعدة صارمة: لا ترشّح إلا محلات موجودة فعلاً في القائمة.
لا تخترع محل، ولا عرض، ولا تذكر أي محل مو في القائمة.

لتحديد نوع المحل عند الترشيح: اعتمد على فهمك لطبيعة المحل من اسمه،
مو على الفئة المكتوبة في القائمة (الفئات فيها أخطاء).
مثال: "جو سبا" = سبا وعناية، مو أثاث. "شي ان" = أزياء وملابس.


تنسيق قائمة الترشيحات (لما ترشّح أكثر من محل):

كل محل يكون في سطر واحد مستقل، يجمع: الاسم + الخصم (إن وُجد) + رابطه الخاص.
الرابط لازم يكون في نفس سطر محله. ممنوع منعاً باتاً تجمع الروابط في نهاية الرد.

استخدم هذا الشكل بالضبط:
- **اسم المحل** — خصم {{القيمة}} لين {{التاريخ}} · [تفاصيل العرض في برق]({{URL}})

إذا المحل ما عنده خصم أو تاريخ في القائمة، اكتب اسمه ورابطه فقط بدون تختلق تفاصيل:
- **اسم المحل** · [تفاصيل العرض في برق]({{URL}})

مثال صحيح لرد فيه كذا كافيه:
"عندك كم كافيه عليهم عروض على برق 👇

- **ديب لاب كوفي** — خصم 15% لين 31 أكتوبر 2026 · [تفاصيل العرض في برق](https://barq.com/ar/offers/diplab-cafe/)
- **انجل كوفي** — خصم 12% لين 19 يونيو 2026 · [تفاصيل العرض في برق](https://barq.com/ar/offers/angels-cafe/)
- **كافيه لابيروز** — خصم 20% لين 30 يونيو 2026 · [تفاصيل العرض في برق](https://barq.com/ar/offers/cafe-laperouse/)

تبي تفاصيل أي واحد منهم؟"

لاحظ: كل كافيه ورابطه في نفس السطر. لا تنقل أي رابط للأسفل.

"""
# ── LLM ──────────────────────────────────────────────────────────────────────
def _llm(history: list[dict]) -> str:
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}] + history
    r = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=msgs,
        temperature=0.4,
        max_tokens=600,
    )
    return r.choices[0].message.content or ""

# ── app ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="Barq Chatbot")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
client = Groq(api_key=os.environ["GROQ_API_KEY"])

# ── websocket ─────────────────────────────────────────────────────────────────
@app.websocket("/ws")
async def ws_chat(websocket: WebSocket):
    await websocket.accept()
    history: list[dict] = []

    try:
        while True:
            data = await websocket.receive_json()

            if data.get("type") == "reset":
                history = []
                continue

            if data.get("type") != "text_input":
                continue

            text = data.get("text", "").strip()
            if not text:
                continue

            history.append({"role": "user", "content": text})
            await websocket.send_json({"type": "status", "label": "أفكر..."})

            loop = asyncio.get_running_loop()
            reply = await loop.run_in_executor(None, _llm, list(history))

            await websocket.send_json({"type": "status"})
            await websocket.send_json({"type": "agent_reply", "text": reply})

            history.append({"role": "assistant", "content": reply})

    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await websocket.send_json({"type": "error", "msg": str(e)})
        except Exception:
            pass

# ── frontend ──────────────────────────────────────────────────────────────────
FRONTEND = Path(__file__).parent.parent / "frontend"
app.mount("/static", StaticFiles(directory=FRONTEND), name="static")

@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)

@app.get("/")
async def index():
    return FileResponse(FRONTEND / "index.html")
