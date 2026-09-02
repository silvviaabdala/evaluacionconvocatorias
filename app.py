import base64, io, json, os, re, zipfile
import xml.etree.ElementTree as ET
from flask import Flask, request, jsonify, Response
import requests as req_lib

app     = Flask(__name__)
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")

try:
    from pdfminer.high_level import extract_text as pdf_extract
    PDF_OK = True
except ImportError:
    PDF_OK = False

def extract_docx(data):
    W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        xml = z.read("word/document.xml")
    root = ET.fromstring(xml)
    parts = []
    for para in root.iter(f"{W}p"):
        line = "".join(n.text or "" for n in para.iter(f"{W}t")).strip()
        if line:
            parts.append(line)
    return "\n".join(parts)

def extract_file(name, data):
    n = name.lower()
    if n.endswith(".pdf"):
        if not PDF_OK:
            raise RuntimeError("PDF no soportado. Usa Word (.docx) o Markdown (.md).")
        return pdf_extract(io.BytesIO(data))
    if n.endswith(".docx") or n.endswith(".doc"):
        return extract_docx(data)
    return data.decode("utf-8", errors="replace")

SYSTEM_PROMPT = """Eres evaluador experto del RDDUNJA de la UNAM. Analiza los CVs contra la convocatoria. Responde SOLO con JSON válido, sin texto adicional ni markdown.

ELEGIBILIDAD: req1(TC en UNAM), req2(edad H<=40/M<=43), req3(antigüedad>=3a TC), req1b(sin cargo directivo), req4(publicaciones calidad), req6(sin sanciones).

RÚBRICA 100pts:
1.Producción(40): libro 8pts(cap24), art-intern 3pts(cap18), art-nac 1.5pts(cap6), cap-libro 1pt(cap4), en-dictamen 50%.
2.Reconocimiento(20): SNI1=10, SNI2=15, premio=5c/u, estancia-intern-3m=2(cap6).
3.Proyectos(20): PAPIIT-PI=5, ext-intern-PI=6, ext-nac-PI=4, colaborador=1(cap4), en-proceso=50%.
4.FormaciónRRHH(10): doctoral-dir=3, posdoc=2, maestría-dir=2, en-proceso=50%, lic-dir=0.5.
5.Difusión(10): ponencia-intern=0.3(cap5), nac=0.1(cap3), org-evento=0.5(cap3), comité=1(cap3), divulgación=hasta2.

REGLAS: Códigos proporcionados. Sin nombres reales. Observaciones académicas naturales. Aplica caps.

JSON: {"convocatoria_area":"...","candidates":[{"code":"X","category":"...","field":"...","sni":"...","tenure":"...","eligibility":{"req1":{"status":"ok","note":"..."},"req2":{"status":"ok","note":"..."},"req3":{"status":"ok","note":"..."},"req1b":{"status":"ok","note":"..."},"req4":{"status":"ok","note":"..."},"req6":{"status":"ok","note":"..."}},"scores":{"produccion":{"score":0,"max":40,"details":["..."]},"reconocimiento":{"score":0,"max":20,"details":["..."]},"proyectos":{"score":0,"max":20,"details":["..."]},"formacion":{"score":0,"max":10,"details":["..."]},"participacion":{"score":0,"max":10,"details":["..."]}},"observations":["...","...","..."]}]}"""

def call_claude(api_key, conv_text, candidates):
    # Limitar texto para reducir tiempo de respuesta
    user_msg = (
        f"CONVOCATORIA:\n{conv_text[:6000]}\n\n" +
        "\n\n---\n\n".join(
            f"CANDIDATO {c['code']}:\n{c['text'][:5000]}" for c in candidates
        )
    )
    payload = {
        "model": "claude-haiku-4-5-20251001",  # más rápido para evitar timeout
        "max_tokens": 4000,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_msg}],
    }
    response = req_lib.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        json=payload,
        timeout=280,  # justo bajo el límite de gunicorn (300s)
    )
    if not response.ok:
        err = response.json().get("error", {})
        raise RuntimeError(err.get("message", f"API error {response.status_code}"))
    raw = re.sub(r"```json?\n?", "", response.json()["content"][0]["text"]).replace("```","").strip()
    return json.loads(raw)

@app.route("/")
def index():
    with open(HTML_PATH, "r", encoding="utf-8") as f:
        return Response(f.read(), mimetype="text/html")

@app.route("/health")
def health():
    return jsonify({"ok": True, "html": os.path.exists(HTML_PATH)})

@app.route("/analyze", methods=["POST"])
def analyze():
    try:
        body = request.get_json(force=True)
        key  = API_KEY or body.get("api_key", "")
        if not key:
            return jsonify({"ok": False, "error": "API Key no configurada."}), 500
        conv_raw = base64.b64decode(body["convocatoria"]["data"])
        conv_txt = extract_file(body["convocatoria"]["name"], conv_raw)
        cands = []
        for c in body["candidates"]:
            text = extract_file(c["name"], base64.b64decode(c["data"]))
            cands.append({"code": c["code"], "text": text})
        result = call_claude(key, conv_txt, cands)
        return jsonify({"ok": True, "data": result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
